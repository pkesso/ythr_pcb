"""Export Gerber and drill files from a KiCad board, check them against the board, and zip them for the manufacturer.

    python gerber.py board.kicad_pcb                # gerbers/<board>-*.g* and *.drl, and gerbers/<board>-gerbers.zip
    python gerber.py board.kicad_pcb -o out.zip     # the unpacked files go next to the zip (or --dir)
    python gerber.py board.kicad_pcb --no-drc       # skip DRC: for experiments only, never for an order

The board file is never modified: zones are refilled in memory (a generated board has unfilled zones, and exporting
it as it is gives copper without any pour). Files are written to a temporary directory and published only if every
check passes, so files that exist have been validated. The unpacked files are tracked in git (diffs show what changed
on the board): the comment lines with the export date are dropped and line endings are LF, so the same board gives
the same files. The zip is for the order and is git-ignored. Files of this board left from an earlier export that the
new one no longer has are removed; other files in the directory are not touched.

Checks: the project has JLCPCB's limits (jlcpcb_rules.py), DRC has no errors; every layer file exists and is complete; the board outline is closed; every copper layer
with a zone has a pour; copper pads, paste and drill hits match the board (per diameter); nothing lies outside the
outline.

Exit code: 0 when the zip was written, 1 when a check failed, 2 if KiCad could not run.
"""
import argparse
import collections
import glob
import math
import os
import re
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_checks  # noqa: E402
import kicad_cli  # noqa: E402
import jlcpcb_rules  # noqa: E402
from kicad_gen import sexpr, kids, kid  # noqa: E402

TECH_LAYERS = ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
FILE_LAYER = {"F.SilkS": "F_Silkscreen", "B.SilkS": "B_Silkscreen"}  # the others are the name with "." -> "_"
TOLERANCE = 0.01  # mm
GERBER_EXT = re.compile(r"\.(g[a-z0-9]{1,3}|gbr|drl)$", re.I)  # KiCad's Protel-style names (.gtl, .g1, .gm1), drills
_DRAW = re.compile(r"(?:G0?[123])?(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0?([123])")
_XY = re.compile(r"(?:X(-?[\d.]+))?(?:Y(-?[\d.]+))?(G85)?")


# ---------------------------------------------------------------- what the board says
def hole_size(drill):
    return round(min(float(v) for v in drill[1:] if isinstance(v, str) and v != "oval"), 3)


def expected(pcb):
    """Copper layers, zones, pads and holes of the board file."""
    with open(pcb, encoding="utf-8") as fh:
        t = sexpr(fh.read())
    copper = [x[1] for x in kid(t, "layers")[1:] if x[1].endswith(".Cu")]
    e = argparse.Namespace(copper=copper, zones=set(), flashes=collections.Counter(), paste=collections.Counter(),
                           pth=collections.Counter(), npth=collections.Counter())
    for z in kids(t, "zone"):
        if not kid(z, "keepout"):
            layers = (kid(z, "layers") or kid(z, "layer"))[1:]
            e.zones |= {c for c in copper for l in layers if l == c or l == "*.Cu" or (l == "F&B.Cu" and c in ("F.Cu", "B.Cu"))}
    for v in kids(t, "via"):
        e.pth[hole_size(kid(v, "drill"))] += 1
        for c in copper:
            e.flashes[c] += 1
    for fp in kids(t, "footprint"):
        for p in kids(fp, "pad"):
            layers = kid(p, "layers")[1:]
            if p[2] in ("thru_hole", "np_thru_hole"):
                (e.pth if p[2] == "thru_hole" else e.npth)[hole_size(kid(p, "drill"))] += 1
            if p[2] != "np_thru_hole":
                for c in copper:
                    if c in layers or "*.Cu" in layers or ("F&B.Cu" in layers and c in ("F.Cu", "B.Cu")):
                        e.flashes[c] += 1
            for side in ("F.Paste", "B.Paste"):
                if side in layers:
                    e.paste[side] += 1
    return e


# ---------------------------------------------------------------- what the files say
class Gerber:
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.complete = text.rstrip().endswith("M02*")
        fs = re.search(r"%FSLAX\d(\d)Y\d\d\*%", text)
        self.metric = "%MOMM*%" in text and fs is not None
        self.points, self.segments, self.flashes, self.regions = [], [], 0, 0
        if self.metric:
            self._parse(re.sub(r"%.*?%", "", text, flags=re.S), 10.0 ** int(fs.group(1)))

    def _parse(self, body, scale):
        x = y = 0.0
        region = False
        mode = 1  # G01 linear, G02 clockwise, G03 counter-clockwise; modal
        for st in body.split("*"):
            st = st.strip()
            g = re.match(r"G0?([123])(?!\d)", st)
            if g:
                mode = int(g[1])
            if st.startswith("G36"):
                region = True
                self.regions += 1
            elif st.startswith("G37"):
                region = False
            m = _DRAW.fullmatch(st)
            if not m:
                continue
            x0, y0 = x, y
            x = int(m[1]) / scale if m[1] else x
            y = int(m[2]) / scale if m[2] else y
            self.points.append((x, y))
            if m[5] == "3":
                self.flashes += 1
            elif m[5] == "1" and not region:
                self.segments.append(((x0, y0), (x, y)))
                if (m[3] or m[4]) and mode in (2, 3):  # arc: its ends are in already; add its own extremes
                    cx, cy = x0 + int(m[3] or 0) / scale, y0 + int(m[4] or 0) / scale
                    r = math.dist((cx, cy), (x, y))
                    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y - cy, x - cx)
                    turn = 1 if mode == 3 else -1  # G03 runs counter-clockwise
                    span = (turn * (a1 - a0)) % math.tau or math.tau  # same start and end: a full circle
                    for q in range(4):  # the points of the circle furthest right, up, left and down
                        a = q * math.pi / 2
                        if (turn * (a - a0)) % math.tau <= span:
                            self.points.append((cx + r * math.cos(a), cy + r * math.sin(a)))


def read_drill(path):
    """Excellon file -> (hits per diameter, hole positions)."""
    tools, hits, pts, current = {}, collections.Counter(), [], None
    x = y = 0.0
    with open(path, encoding="utf-8") as fh:
        for line in (l.strip() for l in fh):
            m = re.fullmatch(r"T(\d+)C([\d.]+)", line)
            if m:
                tools[m[1]] = round(float(m[2]), 3)
                continue
            m = re.fullmatch(r"T(\d+)", line)
            if m:
                current = tools.get(m[1])
                continue
            if line.startswith(("X", "Y")):
                m = _XY.match(line)
                x, y = float(m[1]) if m[1] else x, float(m[2]) if m[2] else y
                hits[current] += 1
                pts.append((x, y))
    return hits, pts


def open_ends(segments):
    """Endpoints of the outline that are not joined to another one."""
    ends = [p for s in segments for p in s]
    return [p for p in ends if sum(math.dist(p, q) < TOLERANCE for q in ends) % 2]


def validate(directory, stem, e):
    """Errors in the exported files; `e` is what expected() read from the board."""
    errors, layers = [], {}
    for name in e.copper + TECH_LAYERS:
        found = glob.glob(os.path.join(glob.escape(directory), f"{glob.escape(stem)}-{FILE_LAYER.get(name, name.replace('.', '_'))}.*"))
        if len(found) != 1:
            errors.append(f"{name}: expected one file, found {len(found)}")
            continue
        g = layers[name] = Gerber(found[0])
        if not g.complete:
            errors.append(f"{name}: file is truncated (no M02)")
        if not g.metric:
            errors.append(f"{name}: not in millimetres or no format statement")
    edge = layers.get("Edge.Cuts")
    if edge is None or not edge.segments:
        return errors + ["Edge.Cuts: no board outline"]
    errors += [f"Edge.Cuts: outline is open at ({x:g}, {y:g})" for x, y in sorted(set(open_ends(edge.segments)))]
    xs, ys = [p[0] for p in edge.points], [p[1] for p in edge.points]

    def outside(points):
        return [p for p in points if not (min(xs) - TOLERANCE <= p[0] <= max(xs) + TOLERANCE and
                                          min(ys) - TOLERANCE <= p[1] <= max(ys) + TOLERANCE)]

    for name, g in layers.items():
        out = outside(g.points)
        if out:
            errors.append(f"{name}: {len(out)} points outside the outline, e.g. ({out[0][0]:g}, {out[0][1]:g})")
    for c in e.copper:
        if c in layers:
            if c in e.zones and not layers[c].regions:
                errors.append(f"{c}: the board has a zone here but the file has no pour (zones not filled)")
            if layers[c].flashes != e.flashes[c]:
                errors.append(f"{c}: {layers[c].flashes} flashed pads and vias, the board has {e.flashes[c]}")
    for side in ("F.Paste", "B.Paste"):
        if side in layers and bool(layers[side].flashes or layers[side].regions) != bool(e.paste[side]):
            errors.append(f"{side}: file and board disagree on whether this side has paste")
    for kind, want in (("PTH", e.pth), ("NPTH", e.npth)):
        path = os.path.join(directory, f"{stem}-{kind}.drl")
        if not want and not os.path.isfile(path):
            continue
        if not os.path.isfile(path):
            errors.append(f"{kind}: drill file is missing")
            continue
        hits, pts = read_drill(path)
        if hits != want:
            errors.append(f"{kind}: drill hits {dict(sorted(hits.items()))}, the board has {dict(sorted(want.items()))}")
        if outside(pts):
            errors.append(f"{kind}: {len(outside(pts))} holes outside the outline")
    return errors


# ---------------------------------------------------------------- main
_DATED = re.compile(r"^(G04 #@! TF\.CreationDate,.*|G04 Created by KiCad .* date .*|; DRILL file .* date .*|"
                    r"; #@! TF\.CreationDate,.*)$")


def undate(directory):
    """Drop the comment lines holding the export date and write LF, so the same board gives the same files (the
    unpacked copy is tracked in git). Only comments: G04 in Gerber, ';' in Excellon."""
    for f in os.listdir(directory):
        path = os.path.join(directory, f)
        with open(path, "rb") as fh:
            lines = fh.read().decode("ascii", errors="surrogateescape").replace("\r\n", "\n").split("\n")
        with open(path, "wb") as fh:
            fh.write("\n".join(l for l in lines if not _DATED.match(l)).encode("ascii", errors="surrogateescape"))


def publish(tmp, files, directory, stem):
    """Copy the checked files into the unpacked directory, removing this board's files of an earlier export that the
    new one no longer has (a layer that went away); other files there are left alone."""
    for f in os.listdir(directory):
        if f.startswith(stem + "-") and GERBER_EXT.search(f) and f not in files:
            os.remove(os.path.join(directory, f))
    for f in files:
        shutil.copyfile(os.path.join(tmp, f), os.path.join(directory, f))


def export(pcb, directory, copper):
    layers = ",".join(copper + TECH_LAYERS)
    kicad_cli.run("pcb", "export", "gerbers", "-l", layers, "--check-zones", "--no-x2", "--no-netlist", "--subtract-soldermask",
                  "-o", directory + os.sep, pcb)
    kicad_cli.run("pcb", "export", "drill", "--excellon-separate-th", "--excellon-oval-format", "alternate",
                  "--excellon-units", "mm", "--excellon-zeros-format", "decimal", "--drill-origin", "absolute",
                  "-o", directory + os.sep, pcb)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help=".kicad_pcb file")
    ap.add_argument("-o", "--output", help="zip to write (default: gerbers/<board>-gerbers.zip next to the board)")
    ap.add_argument("--dir", help="directory for the unpacked files (default: the zip's directory)")
    ap.add_argument("--no-drc", action="store_true", help="skip DRC: for experiments only, never for an order")
    kicad_cli.add_priority_argument(ap)
    args = ap.parse_args()
    kicad_cli.lower_priority(args.priority)  # the DRC and the export inherit it
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected

    pcb = os.path.abspath(args.board)
    stem = os.path.splitext(os.path.basename(pcb))[0]
    out = os.path.abspath(args.output or os.path.join(os.path.dirname(pcb), "gerbers", stem + "-gerbers.zip"))
    unpacked = os.path.abspath(args.dir or os.path.dirname(out))
    try:
        kicad_cli.writable(out)  # before the DRC and the export, not after them
        kicad_cli.writable(unpacked, directory=True)
        kicad_cli.ensure_ready()
        print(f"Gerbers of {os.path.basename(pcb)}: " + ("export, check" if args.no_drc else "DRC, export, check"))
        if args.no_drc:
            print("DRC SKIPPED: do not order from this output")
        else:
            below = jlcpcb_rules.problems(os.path.splitext(pcb)[0] + ".kicad_pro")
            if below:
                print("\n".join(below))
                print("no Gerbers written: DRC would not check JLCPCB's limits; run jlcpcb_rules.py on the project first")
                return 1
            print("[1/3] DRC with zones refilled")
            bad = [v for v in kicad_checks.drc(pcb) if v["severity"] == "error"]
            if bad:
                for (_, typ), vs in kicad_checks.summarise(bad).items():
                    print(f"DRC error {typ} x{len(vs)}")
                print("no Gerbers written: fix the DRC errors first")
                return 1
        e = expected(pcb)
        with tempfile.TemporaryDirectory() as tmp:
            print(f"[2/3] export: {len(e.copper)} copper layers, drill files")
            export(pcb, tmp, e.copper)
            undate(tmp)
            print("[3/3] check the files: layers complete, outline closed, holes against the board")
            errors = validate(tmp, stem, e)  # the files as they are published
            if errors:
                print("\n".join(errors))
                print(f"{len(errors)} problems: nothing written")
                return 1
            files = sorted(f for f in os.listdir(tmp) if not f.endswith(".gbrjob"))
            publish(tmp, files, unpacked, stem)
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(os.path.join(tmp, f), f)
    except (kicad_cli.KicadError, OSError) as err:
        print(err)
        return 2
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:
            print(f"{out}: archive is damaged")
            return 1
    print(f"ok: {len(files)} files, {sum(e.pth.values())} plated and {sum(e.npth.values())} non-plated holes -> {out}, "
          f"unpacked in {unpacked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Render a KiCad board (top, bottom, isometric) and draw a height map of what stands on it.

    python views.py board.kicad_pcb                      # writes renders/ next to the board
    python views.py board.kicad_pcb -o some/dir
    python views.py board.kicad_pcb --heights parts.csv  # datasheet heights that replace the 3D models' ones
    python views.py board.kicad_pcb --panel 12 --through RV1,RV2,SW1   # only what stays inside the enclosure

Output: top.png, bottom.png, iso.png, side.png (along the board's long side), edge-<top|bottom|left|right>.png (one for
every edge a connector reaches or overhangs: J, X, XS, P or CN), heightmap.png, heightmap.csv. Needs
matplotlib in the project's .venv.

--panel is the height of the panel's inner face above the board (the board-to-panel height), on the side given by
--panel-side (front by default). The parts listed in --through pass through holes in the panel (pot and encoder shafts
and threads, footswitch threads and buttons): their height is cut at the panel, so the map shows only what is inside
the enclosure. Any other part taller than the panel is reported as a collision.

Each part's height comes from its 3D model (.step or .wrl) and is drawn over its courtyard's bounding box, so the map
is a block model for checking clearances, not a surface scan. Heights are in mm from the board surface on that side;
pins of a through-hole part count on the opposite side. Parts without a usable model are listed, and need a line in
the --heights file (columns: ref,front,back; heightmap.csv has this format). A model that is rotated about X or Y is
treated as unusable, because its height then needs more than the Z extent.

Exit code: 0 when every part has a height and nothing hits the panel, 1 otherwise, 2 if KiCad could not run.
"""
import argparse
import csv
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_cli  # noqa: E402
from kicad_gen import sexpr, kids, kid, courtyard_points  # noqa: E402

VIEWS = {"top": ["--side", "top", "--zoom", "0.7"], "bottom": ["--side", "bottom", "--zoom", "0.7"],
         "iso": ["--side", "top", "--rotate", "-45,0,45", "--zoom", "0.55"]}
# kicad-cli's side that looks at each edge of the board as drawn (y down): "front" looks at the bottom edge
EDGE_SIDE = {"top": "back", "bottom": "front", "left": "left", "right": "right"}
CONNECTOR = re.compile(r"^(J|X|XS|P|CN)\d")
EDGE_REACH = 0.0  # mm: a connector whose courtyard reaches the edge or overhangs it faces that edge (wire pads 0.2 mm
                  # inside it do not: they are not reached from outside)
_STEP_POINT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*[-\d.E+]+\s*,\s*[-\d.E+]+\s*,\s*([-\d.E+]+)\s*\)", re.I)
_WRL_POINTS = re.compile(r"point\s*\[([^\]]*)\]")
WRL_UNIT = 2.54  # KiCad's VRML models are in units of 0.1 inch
PIN_MIN = 0.2  # mm below the surface that counts as pins coming through the board
_cache = {}


# ---------------------------------------------------------------- heights
def resolve(path, board_dir):
    """A model path from the board file -> a file on this machine (the .step/.wrl twin if only that one exists)."""
    share = os.environ.get("KICAD_3DMODEL_DIR") or os.path.join(os.path.dirname(kicad_cli.kicad_bin()), "share", "kicad", "3dmodels")
    p = re.sub(r"\$\{KICAD\d+_3DMODEL_DIR\}", lambda _: share, path).replace("${KIPRJMOD}", board_dir)
    root = os.path.splitext(p)[0]
    return next((f for f in (p, root + ".step", root + ".stp", root + ".wrl") if os.path.isfile(f)), None)


def z_extent(path):
    """(lowest, highest) Z of a 3D model in mm, or None if the file has no points."""
    if path not in _cache:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if path.lower().endswith(".wrl"):
            nums = [float(v) for block in _WRL_POINTS.findall(text) for v in re.split(r"[\s,]+", block.strip()) if v]
            zs = [z * WRL_UNIT for z in nums[2::3]]
        else:
            zs = [float(z) for z in _STEP_POINT.findall(text)]
        _cache[path] = (min(zs), max(zs)) if zs else None
    return _cache[path]


def model_heights(fp, board_dir):
    """(front, back) of a footprint from its 3D models, or None when no model is usable."""
    low, high = [], []
    bottom = kid(fp, "layer")[1] == "B.Cu"
    for m in kids(fp, "model"):
        if kid(m, "hide") and kid(m, "hide")[1] == "yes":
            continue
        rx, ry = (float(v) for v in kid(kid(m, "rotate"), "xyz")[1:3])
        path = resolve(m[1], board_dir)
        ext = z_extent(path) if path and not (rx % 360 or ry % 360) else None
        if ext is None:
            return None
        scale, offset = float(kid(kid(m, "scale"), "xyz")[3]), float(kid(kid(m, "offset"), "xyz")[3])
        low.append(ext[0] * scale + offset)
        high.append(ext[1] * scale + offset)
    if not high:
        return None
    above, below = max(0.0, max(high)), -min(low) if min(low) < -PIN_MIN else 0.0  # SMD models dip slightly below zero
    return (below, above) if bottom else (above, below)


def box(fp):
    """Courtyard bounding box on the board, or None."""
    at = kid(fp, "at")
    x, y, a = float(at[1]), float(at[2]), math.radians(float(at[3]) if len(at) > 3 else 0.0)
    pts = [(x + px * math.cos(a) + py * math.sin(a), y - px * math.sin(a) + py * math.cos(a)) for px, py in courtyard_points(fp)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)) if pts else None


def parts(tree, board_dir, overrides, panel=None, side="front", through=()):
    """[{ref, box, front, back, source}], the references with no height or no courtyard, and the parts that are not
    in `through` but reach past the panel."""
    rows, missing, collisions = [], [], []
    for fp in kids(tree, "footprint"):
        ref = next(p[2] for p in kids(fp, "property") if p[1] == "Reference")
        h = overrides.get(ref)
        source = "given" if h else "model"
        h = h or model_heights(fp, board_dir)
        b = box(fp)
        if h is None or b is None:
            missing.append(ref)
            continue
        r = dict(ref=ref, box=b, front=h[0], back=h[1], source=source)
        if panel is not None and r[side] > panel:
            if ref in through:
                r[side], r["source"] = panel, source + ", cut at the panel"
            else:
                collisions.append(f"{ref} {r[side]:.1f} mm")
        rows.append(r)
    return sorted(rows, key=lambda r: r["ref"]), sorted(missing), collisions


def outline(tree):
    """Board outline as a list of polylines."""
    lines = []
    for g in tree:
        if not (isinstance(g, list) and g[0] in ("gr_line", "gr_arc", "gr_rect", "gr_circle") and kid(g, "layer") and kid(g, "layer")[1] == "Edge.Cuts"):
            continue
        p = {k: (float(kid(g, k)[1]), float(kid(g, k)[2])) for k in ("start", "mid", "end", "center") if kid(g, k)}
        if g[0] == "gr_rect":
            (x0, y0), (x1, y1) = p["start"], p["end"]
            lines.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif g[0] == "gr_circle":
            r = math.dist(p["center"], p["end"])
            lines.append([(p["center"][0] + r * math.cos(i * math.pi / 24), p["center"][1] + r * math.sin(i * math.pi / 24)) for i in range(49)])
        else:
            lines.append([p[k] for k in ("start", "mid", "end") if k in p])
    return lines


def edge_views(tree, edges):
    """{name: render args}: a side view along the board's long side, and one view of every edge a connector meets."""
    xs, ys = [p[0] for l in edges for p in l], [p[1] for l in edges for p in l]
    if not xs:
        return {}
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    side = "left" if y1 - y0 >= x1 - x0 else "bottom"  # look at a long edge, so the whole length shows
    views = {"side": ["--side", EDGE_SIDE[side]]}
    faced = set()
    for fp in kids(tree, "footprint"):
        ref = next((p[2] for p in kids(fp, "property") if p[1] == "Reference"), "")
        b = box(fp) if CONNECTOR.match(ref) else None
        if not b:
            continue
        for edge, gap in (("top", b[1] - y0), ("bottom", y1 - b[3]), ("left", b[0] - x0), ("right", x1 - b[2])):
            if gap <= EDGE_REACH:
                faced.add(edge)
    for edge in sorted(faced):
        views[f"edge-{edge}"] = ["--side", EDGE_SIDE[edge]]
    return views


# ---------------------------------------------------------------- output
def draw(rows, edges, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    ink, muted = "#1f2328", "#6b7280"
    top = max(max(r["front"], r["back"]) for r in rows)
    norm = matplotlib.colors.Normalize(0, top)
    blues = matplotlib.colormaps["Blues"]  # without its near-white start, so low parts stay visible on white
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("height", [blues(0.25 + 0.75 * i / 255) for i in range(256)])
    fig, axes = plt.subplots(1, 2, figsize=(11, 8), layout="constrained")
    for ax, side, title in zip(axes, ("front", "back"), ("Front (top view)", "Back (seen from below)")):
        ordered = sorted(rows, key=lambda r: r[side])
        for r in (r for r in ordered if r[side] > 0):
            x0, y0, x1, y1 = r["box"]
            ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor=cmap(norm(r[side])), edgecolor="white", linewidth=0.8))
        for r in ordered[-8:]:  # direct labels only on the tallest parts
            if r[side] > 0:
                x0, y0, x1, y1 = r["box"]
                dark = norm(r[side]) > 0.55
                ax.text((x0 + x1) / 2, (y0 + y1) / 2, f"{r['ref']}\n{r[side]:.1f}", ha="center", va="center", fontsize=7,
                        color="white" if dark else ink)
        for line in edges:
            ax.plot(*zip(*line), color=ink, linewidth=1.2)
        ax.set_title(f"{title}, tallest {max(r[side] for r in rows):.1f} mm", color=ink, fontsize=11, loc="left")
        ax.set_aspect("equal")
        ax.autoscale_view()
        ax.invert_yaxis()
        if side == "back":
            ax.invert_xaxis()
        ax.tick_params(colors=muted, labelsize=8)
        ax.set_xlabel("mm", color=muted, fontsize=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
    bar = fig.colorbar(matplotlib.cm.ScalarMappable(norm=norm, cmap=cmap), ax=axes, shrink=0.6)
    bar.set_label("Height above the board surface, mm", color=muted, fontsize=9)
    bar.ax.tick_params(colors=muted, labelsize=8)
    bar.outline.set_visible(False)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help=".kicad_pcb file")
    ap.add_argument("-o", "--output", help="directory for the images (default: renders/ next to the board)")
    ap.add_argument("--heights", help="CSV with columns ref,front,back: heights that replace the 3D models' ones")
    ap.add_argument("--panel", type=float, help="height of the panel's inner face above the board, mm")
    ap.add_argument("--panel-side", choices=("front", "back"), default="front", help="side the panel is on (default front)")
    ap.add_argument("--through", default="", help="comma-separated references of the parts that pass through the panel")
    ap.add_argument("--no-render", action="store_true", help="only the height map")
    kicad_cli.add_priority_argument(ap)
    args = ap.parse_args()
    kicad_cli.lower_priority(args.priority)  # kicad-cli's renders inherit it
    through = {r.strip() for r in args.through.split(",") if r.strip()}
    if through and args.panel is None:
        ap.error("--through needs --panel")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected

    pcb = os.path.abspath(args.board)
    out = os.path.abspath(args.output or os.path.join(os.path.dirname(pcb), "renders"))
    try:  # before the renders, not after them
        kicad_cli.writable(out, directory=True)
    except OSError as e:
        print(f"cannot write to {out}: {e}")
        return 2
    try:  # the height map needs it at the end; find out now, not after the renders
        import matplotlib  # noqa: F401
    except ImportError:
        print("matplotlib is missing: run this with the project's .venv (pip install matplotlib there)")
        return 2
    overrides = {}
    if args.heights:
        with open(args.heights, encoding="utf-8-sig", newline="") as fh:
            overrides = {r["ref"]: (float(r["front"]), float(r["back"])) for r in csv.DictReader(fh)}
    try:
        kicad_cli.ensure_ready()
        with open(pcb, encoding="utf-8") as fh:
            tree = sexpr(fh.read())
        if not args.no_render:
            sides = edge_views(tree, outline(tree))
            for name, view in {**VIEWS, **sides}.items():
                size = ["-w", "1600", "-h", "1600"] if name in VIEWS else ["-w", "1600", "-h", "700", "--zoom", "0.9"]
                kicad_cli.run("pcb", "render", *view, *size, "--background", "opaque", "--quality", "high",
                              "-o", os.path.join(out, name + ".png"), pcb)
            print("side views: " + ", ".join(sides) if sides else "no outline: no side views")
        rows, missing, collisions = parts(tree, os.path.dirname(pcb), overrides, args.panel, args.panel_side, through)
        unknown = sorted(through - {r["ref"] for r in rows} - set(missing))
        if unknown:
            print(f"--through names parts that are not on the board: {', '.join(unknown)}")
            return 1
    except kicad_cli.KicadError as err:
        print(err)
        return 2
    with open(os.path.join(out, "heightmap.csv"), "w", encoding="utf-8", newline="\n") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ref", "front", "back", "source"])
        w.writerows([r["ref"], f"{r['front']:.2f}", f"{r['back']:.2f}", r["source"]] for r in rows)
    if not rows:
        print("no part has a height")
        return 1
    draw(rows, outline(tree), os.path.join(out, "heightmap.png"))
    for side in ("front", "back"):
        tall = sorted(rows, key=lambda r: -r[side])[:3]
        print(f"{side}: " + ", ".join(f"{r['ref']} {r[side]:.1f} mm" for r in tall))
    if missing:
        print(f"no height for {len(missing)} parts (add them with --heights): {', '.join(missing)}")
    if collisions:
        print(f"taller than the panel at {args.panel:g} mm, not in --through: {', '.join(collisions)}")
    print(f"written to {out}")
    return 1 if missing or collisions else 0


if __name__ == "__main__":
    sys.exit(main())

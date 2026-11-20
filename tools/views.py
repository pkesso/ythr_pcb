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
the enclosure. Any other part taller than the panel is reported as a collision. The parts in --through are also
checked for their fit in the panel and written to panel_fit.csv: the gap between the mounting surface and the panel's
inner face (negative: the part pushes the board away, an error; over 2 mm: a warning), and how far the bushing comes
out past the wall's outer face (--wall, 2 mm; not at all: an error). Nuts and washers are not counted. Mounting
surface and bushing come from the shared library's part.json when the footprint is a library part, else from the 3D
model's steps.

Each part's height comes from its 3D model (.step or .wrl). Its body is drawn as the projection of the model onto the
board: the convex outline of each solid (STEP) or shape (VRML) above the surface, each at its own height. Pins that
come through the board are drawn as dots on their pads on the opposite side, each at the length that sticks out; a
model's part below the board that is not a pin (an edge connector hanging past the board) is drawn as a body there.
Heights are in mm from the board surface on that side. A part given in the --heights file (columns: ref,front,back;
heightmap.csv has this format) is drawn over its courtyard's bounding box with dots on its through-hole pads. Parts
without a usable model are listed and need a line there. A model that is rotated about X or Y is treated as
unusable, because its height then needs more than the Z extent; a STEP assembly (parts placed by transforms) is
drawn over the courtyard's box at the model's height.

Every part above 1 mm is labelled with its reference and height where it is visible (not covered by a taller part),
tallest first, without overlapping another label; a table next to the maps repeats them with both heights and marks a
part for which no free spot was found. A part with steps (a pot's body, bushing and shaft; a footswitch's base, thread
and button) has each lower step's height written on that step, or next to it with a leader line when the step is too
thin; the table lists them under Steps. The colours are only a few height bands (BANDS), with a key under the maps.

Exit code: 0 when every part has a height, nothing hits the panel and the panel parts fit, 1 otherwise, 2 if KiCad could
not run.
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
_STEP_ENTITY = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]*)\s*\((.*)\)\s*$", re.S)
_STEP_REF = re.compile(r"#(\d+)")
_SURFACE = re.compile(r"SURFACE$|^SURFACE_OF|SURFACE_WITH|^PLANE$")  # not SURFACE_CURVE, whose 3D curve is an edge
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?")
_WRL_POINTS = re.compile(r"point\s*\[([^\]]*)\]")
WRL_UNIT = 2.54  # KiCad's VRML models are in units of 0.1 inch
PIN_MIN = 0.2  # mm below the surface that counts as pins coming through the board
PIN_REACH = 1.5  # mm from a through-hole pad's centre within which a model's point below the board is that pad's pin
LABEL_MIN = 1.0  # mm: parts taller than this are labelled
LEVEL_GAP = 0.5  # mm: a part's pieces closer in height than this are one step of it
BANDS = (1, 2, 3, 5, 10, 20)  # mm: the map's colours are these height bands, not a continuous scale
GRID = 0.2  # mm: raster that finds where each part is visible
_cache = {}


# ---------------------------------------------------------------- heights
def resolve(path, board_dir):
    """A model path from the board file -> a file on this machine (the .step/.wrl twin if only that one exists)."""
    share = os.environ.get("KICAD_3DMODEL_DIR") or os.path.join(os.path.dirname(kicad_cli.kicad_bin()), "share", "kicad", "3dmodels")
    p = re.sub(r"\$\{KICAD\d+_3DMODEL_DIR\}", lambda _: share, path).replace("${KIPRJMOD}", board_dir)
    root = os.path.splitext(p)[0]
    return next((f for f in (p, root + ".step", root + ".stp", root + ".wrl") if os.path.isfile(f)), None)


def _step_groups(text):
    """Point groups of a STEP model in mm, one per solid: the vertices, circles and ellipses sampled, and B-spline
    control points of its edges (not the placements of its surfaces, which may lie anywhere). For an assembly, whose
    parts are placed by transforms this reader does not apply, only {"extent": (lowest, highest) Z of its points}."""
    if "NEXT_ASSEMBLY_USAGE_OCCURRENCE" in text:
        zs = [float(m.group(1)) for m in re.finditer(
            r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*[-\d.E+]+\s*,\s*[-\d.E+]+\s*,\s*([-\d.E+]+)\s*\)", text, re.I)]
        return {"extent": (min(zs), max(zs))} if zs else None
    ent = {}
    for stmt in text[text.find("DATA;"):].split(";"):
        m = _STEP_ENTITY.match(stmt.strip())
        if m:
            ent[int(m.group(1))] = (m.group(2), m.group(3))

    def point(i):
        nums = [float(v) for v in _NUM.findall(ent[i][1].split(",", 1)[1])] if i in ent else []
        return tuple(nums) if len(nums) == 3 else None

    def unit(v):
        n = math.sqrt(sum(c * c for c in v)) or 1.0
        return tuple(c / n for c in v)

    def conic(args, radii):
        """Points around a circle or an ellipse from its AXIS2_PLACEMENT_3D."""
        refs = [int(r) for r in _STEP_REF.findall(ent[int(_STEP_REF.search(args).group(1))][1])]
        c = point(refs[0])
        n = unit(point(refs[1]) or (0, 0, 1)) if len(refs) > 1 else (0, 0, 1)
        u = point(refs[2]) if len(refs) > 2 else None
        u = u or ((1, 0, 0) if abs(n[0]) < 0.9 else (0, 1, 0))
        d = sum(a * b for a, b in zip(u, n))
        u = unit(tuple(a - d * b for a, b in zip(u, n)))
        v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
        r1, r2 = radii[0], radii[-1]
        return [tuple(c[k] + r1 * math.cos(t) * u[k] + r2 * math.sin(t) * v[k] for k in range(3))
                for t in (i * math.pi / 16 for i in range(32))] if c else []

    def collect(root):
        pts, seen, stack = [], set(), [root]
        while stack:
            i = stack.pop()
            if i in seen or i not in ent:
                continue
            seen.add(i)
            kind, args = ent[i]
            if kind == "VERTEX_POINT":
                p = point(int(_STEP_REF.search(args).group(1)))
                pts += [p] if p else []
            elif kind in ("CIRCLE", "ELLIPSE"):
                pts += conic(args, [float(v) for v in _NUM.findall(args.split("#", 1)[1].split(",", 1)[1])])
            elif "B_SPLINE_CURVE" in kind or (not kind and "B_SPLINE_CURVE" in args):
                pts += [p for p in (point(int(r)) for r in _STEP_REF.findall(args)) if p]
            elif i != root and (kind == "PCURVE" or _SURFACE.search(kind)):
                continue  # 2D parameter-space curves and surfaces: their points are not the body's
            else:
                stack += [int(r) for r in _STEP_REF.findall(args)]
        return pts

    solids = [i for i, (k, _) in ent.items() if k in ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "FACETED_BREP",
                                                      "SHELL_BASED_SURFACE_MODEL")]
    groups = [g for g in (collect(i) for i in solids) if g]
    if not groups:  # no solid: every vertex as one group
        groups = [[p for p in (point(int(_STEP_REF.search(a).group(1))) for k, a in ent.values()
                               if k == "VERTEX_POINT") if p]]
    return [g for g in groups if g] or None


def model_groups(path):
    """Point groups of a 3D model file in model coordinates (mm, y up), one per solid or VRML shape; None if
    unusable."""
    if path not in _cache:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if path.lower().endswith(".wrl"):
            groups = []
            for block in _WRL_POINTS.findall(text):
                nums = [float(v) * WRL_UNIT for v in re.split(r"[\s,]+", block.strip()) if v]
                groups.append(list(zip(nums[0::3], nums[1::3], nums[2::3])))
            _cache[path] = [g for g in groups if g] or None
        else:
            _cache[path] = _step_groups(text)
    return _cache[path]


def z_extent(path):
    """(lowest, highest) z of a 3D model in mm from the footprint's board surface, or None if unusable. parts_lib.py
    checks a part's height with it."""
    groups = model_groups(path)
    if not groups:
        return None
    if isinstance(groups, dict):
        return groups["extent"]
    zs = [z for g in groups for _, _, z in g]
    return min(zs), max(zs)


def hull(pts):
    """Convex hull of 2D points (Andrew's monotone chain), counter-clockwise."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(pts) + half(reversed(pts))


def placement(fp):
    """(x, y, angle in radians) of a footprint on the board."""
    at = kid(fp, "at")
    return float(at[1]), float(at[2]), math.radians(float(at[3]) if len(at) > 3 else 0.0)


def to_board(fp, pts):
    """Points in the footprint's coordinates as stored on the board -> board coordinates."""
    x, y, a = placement(fp)
    c, s = math.cos(a), math.sin(a)
    return [(x + px * c + py * s, y - px * s + py * c) for px, py in pts]


def box(fp):
    """Courtyard bounding box on the board, or None."""
    pts = to_board(fp, courtyard_points(fp))
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)) if pts else None


def through_pads(fp):
    """[(x, y, diameter)] of the footprint's plated through-hole pads, in the footprint's stored coordinates."""
    out = []
    for p in kids(fp, "pad"):
        if p[2] != "thru_hole":
            continue
        at, drill, size = kid(p, "at"), kid(p, "drill"), kid(p, "size")
        d = float(drill[2] if drill[1] == "oval" else drill[1]) if drill else float(size[1]) * 0.6
        out.append((float(at[1]), float(at[2]), max(d, 0.8)))
    return out


def circle(x, y, d, n=16):
    return [(x + d / 2 * math.cos(i * 2 * math.pi / n), y + d / 2 * math.sin(i * 2 * math.pi / n)) for i in range(n)]


def model_pieces(fp, board_dir):
    """[(polygon on the board, height, 'front'|'back')] drawn for a footprint from its 3D models, or None when no model
    is usable. The body above the surface is one convex piece per solid or shape; points below it within PIN_REACH of
    a through-hole pad are that pad's pin, drawn as a dot; any other point below is a body hanging under the board."""
    bottom = kid(fp, "layer")[1] == "B.Cu"
    near, far = ("back", "front") if bottom else ("front", "back")
    pads = through_pads(fp)
    pin_len = [0.0] * len(pads)
    pieces, models = [], 0
    for m in kids(fp, "model"):
        if kid(m, "hide") and kid(m, "hide")[1] == "yes":
            continue
        rx, ry, rz = (float(v) for v in kid(kid(m, "rotate"), "xyz")[1:4])
        path = resolve(m[1], board_dir)
        groups = model_groups(path) if path and not (rx % 360 or ry % 360) else None
        if groups is None:
            return None
        models += 1
        sx, sy, sz = (float(v) for v in kid(kid(m, "scale"), "xyz")[1:4])
        ox, oy, oz = (float(v) for v in kid(kid(m, "offset"), "xyz")[1:4])
        c, s = math.cos(math.radians(-rz)), math.sin(math.radians(-rz))  # KiCad rotates a model by minus its angle
        if isinstance(groups, dict):  # a STEP assembly: its Z extent over the courtyard's box, its pins on every pad
            b = box(fp)
            if b is None:
                return None
            lo, hi = (v * sz + oz for v in groups["extent"])
            if hi > 0:
                pieces.append(([(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])], hi, near))
            pin_len = [max(p, -lo if lo < -PIN_MIN else 0.0) for p in pin_len]
            continue
        for g in groups:
            up, down = [], []
            for px, py, pz in g:
                px, py, pz = px * sx, py * sy, pz * sz
                mx, my, mz = px * c - py * s + ox, px * s + py * c + oy, pz + oz
                # model y points up; the board's y points down, and a bottom footprint is stored mirrored. z stays
                # the height on the footprint's own side
                local, z = ((mx, my) if bottom else (mx, -my)), mz
                if z > 0:
                    up.append((local, z))
                elif z < -PIN_MIN:
                    k = min(range(len(pads)), key=lambda i: math.dist(local, pads[i][:2]), default=None)
                    if k is not None and math.dist(local, pads[k][:2]) <= PIN_REACH:
                        pin_len[k] = max(pin_len[k], -z)
                    else:
                        down.append((local, -z))
            for pts, side in ((up, near), (down, far)):
                if len(pts) >= 3:
                    poly = hull(to_board(fp, [p for p, _ in pts]))
                    if len(poly) >= 3:
                        pieces.append((poly, max(z for _, z in pts), side))
    if not models:
        return None
    pieces += [(to_board(fp, circle(x, y, d)), h, far) for (x, y, d), h in zip(pads, pin_len) if h > 0]
    return pieces


def given_pieces(fp, front, back):
    """Pieces for a part with given heights: its courtyard's box on its own side, dots on its through-hole pads on
    the other."""
    b = box(fp)
    if b is None:
        return None
    bottom = kid(fp, "layer")[1] == "B.Cu"
    near, far = (back, front) if bottom else (front, back)
    names = ("back", "front") if bottom else ("front", "back")
    x0, y0, x1, y1 = b
    pieces = [([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], near, names[0])] if near > 0 else []
    pads = through_pads(fp)
    if far > 0 and pads:
        pieces += [(to_board(fp, circle(x, y, d)), far, names[1]) for x, y, d in pads]
    elif far > 0:  # nothing through-hole: the height on the other side covers the courtyard there too
        pieces.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], far, names[1]))
    return pieces


def parts(tree, board_dir, overrides, panel=None, side="front", through=()):
    """[{ref, box, front, back, source, pieces}], the references with no height or no courtyard, and the parts that
    are not in `through` but reach past the panel."""
    rows, missing, collisions = [], [], []
    for fp in kids(tree, "footprint"):
        ref = next(p[2] for p in kids(fp, "property") if p[1] == "Reference")
        b = box(fp)
        if ref in overrides:
            source, pieces = "given", given_pieces(fp, *overrides[ref])
        else:
            source, pieces = "model", model_pieces(fp, board_dir)
        if pieces is None or b is None:
            missing.append(ref)
            continue
        h = {s: max((z for _, z, ps in pieces if ps == s), default=0.0) for s in ("front", "back")}
        if source == "given":
            h["front"], h["back"] = overrides[ref]
        r = dict(ref=ref, box=b, front=h["front"], back=h["back"], source=source, pieces=pieces, footprint=fp[1])
        r["uncut_levels"] = levels(r, side)  # before the panel cut: the panel-fit check reads the bushing from them
        if panel is not None and r[side] > panel:
            if ref in through:
                r[side], r["source"] = panel, source + ", cut at the panel"
                r["pieces"] = [(p, min(z, panel) if ps == side else z, ps) for p, z, ps in pieces]
            else:
                collisions.append(f"{ref} {r[side]:.1f} mm")
        rows.append(r)
    return sorted(rows, key=lambda r: r["ref"]), sorted(missing), collisions


# ---------------------------------------------------------------- panel fit
WALL = 2.0  # mm, the enclosure wall unless the user says otherwise (pedals' enclosure.md)
GAP_WARN = 2.0  # mm between a mounting surface and the panel above which it is reported
# nuts and washers are left out on purpose: the check is the parts' geometry against the panel's


def panel_mechanics(rows, through, panel):
    """[{ref, mount, top, source}] for the parts that pass through the panel: the height of the mounting surface (the
    shoulder that seats under the panel) and of the bushing's end above the board. From the shared library's
    part.json (mechanics.mount_surface, bushing) when the footprint is a library part, else from the 3D model's
    levels: the highest step at or below the panel is the mounting surface, the next one up the bushing."""
    out = []
    for r in rows:
        if r["ref"] not in through:
            continue
        name = r.get("footprint", "").split(":")[-1]
        mech = None
        try:
            import parts_lib
            if name and os.path.isdir(parts_lib.part_dir(name)):
                mech = parts_lib.info(name).get("mechanics") or {}
        except (ImportError, OSError, ValueError):
            mech = None
        numbers = mech and all(isinstance(mech.get(k), (int, float)) for k in ("mount_surface", "bushing"))
        if numbers:
            out.append(dict(ref=r["ref"], mount=float(mech["mount_surface"]),
                            top=float(mech["mount_surface"]) + float(mech["bushing"]), source=f"part.json ({name})"))
            continue
        steps = sorted(r.get("uncut_levels") or [])
        below = [z for z in steps if z <= panel + 0.05]
        above = [z for z in steps if below and z > below[-1]]
        out.append(dict(ref=r["ref"], mount=below[-1] if below else None, top=above[0] if above else None,
                        source="3D model levels (check them against the datasheet)"))
    return out


def panel_fit(mech, panel, wall=WALL):
    """(rows for the table, errors, warnings) of the panel parts: the gap between the mounting surface and the
    panel's inner face, and how far the bushing comes out past the wall's outer face. Nuts and washers are not
    counted."""
    rows, errors, warnings = [], [], []
    mounts = [m["mount"] for m in mech if m["mount"] is not None]
    for m in mech:
        ref = m["ref"]
        if m["mount"] is None or m["top"] is None:
            errors.append(f"{ref}: mounting surface or bushing unknown ({m['source']}); add them to part.json")
            continue
        gap = panel - m["mount"]
        outside = m["top"] - panel - wall
        rows.append(dict(ref=ref, mount=m["mount"], top=m["top"], gap=gap, outside=outside, source=m["source"]))
        if gap < -0.05:
            errors.append(f"{ref}: its mounting surface is {-gap:.1f} mm above the panel's inner face: it pushes the "
                          "board away (lower the panel parts' common plane or choose another part)")
        elif gap > GAP_WARN:
            warnings.append(f"{ref}: {gap:.1f} mm between its mounting surface and the panel, more than {GAP_WARN:g} "
                            "mm: a part with a taller body or a spacer, or the board closer")
        if outside <= 0:
            errors.append(f"{ref}: the bushing ends {-outside:.1f} mm short of the {wall:g} mm wall's outer face: "
                          "it does not come through")
    if len(mounts) > 1 and max(mounts) - min(mounts) > GAP_WARN:
        warnings.append(f"mounting surfaces from {min(mounts):g} to {max(mounts):g} mm above the board, more than "
                        f"{GAP_WARN:g} mm apart (enclosure.md, \"The board hangs on its panel parts\")")
    return rows, errors, warnings


def write_panel_fit(rows, path, panel, wall):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ref", "mount_surface", "bushing_end", "gap_under_panel", "thread_outside", "source"])
        w.writerows([r["ref"], f"{r['mount']:.2f}", f"{r['top']:.2f}", f"{r['gap']:.2f}", f"{r['outside']:.2f}",
                     r["source"]] for r in rows)
    lines = [f"panel fit (panel {panel:g} mm above the board, wall {wall:g} mm; nuts and washers not counted):",
             f"  {'Ref':<6}{'mount':>7}{'end':>7}{'gap':>7}{'out':>7}"]
    lines += [f"  {r['ref']:<6}{r['mount']:7.1f}{r['top']:7.1f}{r['gap']:7.1f}{r['outside']:7.1f}" for r in rows]
    return lines


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
def height_bands(top):
    """The colour bands up to the tallest part: [(low, high, colour)], light to dark. The colour only sorts parts
    into a few classes; the heights themselves are written on the labels."""
    import matplotlib

    edges = [0.0] + [b for b in BANDS if b < top] + [top]
    blues = matplotlib.colormaps["Blues"]  # without its near-white start, so low parts stay visible on white
    n = len(edges) - 1
    return [(edges[k], edges[k + 1], blues(0.22 + 0.73 * k / max(n - 1, 1))) for k in range(n)]


def band_colour(bands, z):
    return next((c for lo, hi, c in bands if z <= hi), bands[-1][2])


def visible(rows, side, extent):
    """Raster of the map: for each GRID cell the index in `rows` of the tallest part there (-1 where nothing is) and
    that part's height there."""
    import numpy as np
    from matplotlib.path import Path

    x0, y0, x1, y1 = extent
    nx, ny = int(math.ceil((x1 - x0) / GRID)) + 1, int(math.ceil((y1 - y0) / GRID)) + 1
    owner, height = np.full((ny, nx), -1), np.zeros((ny, nx))
    pieces = sorted(((z, i, poly) for i, r in enumerate(rows) for poly, z, ps in r["pieces"] if ps == side and z > 0),
                    key=lambda p: p[0])
    for z, i, poly in pieces:  # lowest first, so a taller part covers it
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        i0, i1 = max(0, int((min(xs) - x0) / GRID)), min(nx, int((max(xs) - x0) / GRID) + 2)
        j0, j1 = max(0, int((min(ys) - y0) / GRID)), min(ny, int((max(ys) - y0) / GRID) + 2)
        if i1 <= i0 or j1 <= j0:
            continue
        gx, gy = np.meshgrid(x0 + GRID * np.arange(i0, i1), y0 + GRID * np.arange(j0, j1))
        inside = Path(poly).contains_points(np.column_stack([gx.ravel(), gy.ravel()])).reshape(gx.shape)
        inside &= z >= height[j0:j1, i0:i1]
        owner[j0:j1, i0:i1][inside], height[j0:j1, i0:i1][inside] = i, z
    return owner, height


def levels(row, side):
    """The heights of a part's steps on one side, tallest first: its pieces' heights above LABEL_MIN, those within
    LEVEL_GAP of a taller one merged into it (the top of a pot's shaft, its bushing, its body)."""
    out = []
    for z in sorted((z for _, z, s in row["pieces"] if s == side and z > LABEL_MIN), reverse=True):
        if not out or out[-1] - z >= LEVEL_GAP:
            out.append(z)
    return out


def _shift(a, dy, dx):
    """`a` moved by (dy, dx) cells, padded with False."""
    import numpy as np

    out = np.zeros_like(a)
    h, w = a.shape
    src = a[max(-dy, 0):h + min(-dy, 0), max(-dx, 0):w + min(-dx, 0)]
    out[max(dy, 0):h + min(dy, 0), max(dx, 0):w + min(dx, 0)] = src
    return out


def label_spots(mask, free, reach):
    """Candidate label centres for a part, as (row, column) cells: first its visible area, deepest inside it first
    (depth = how many erosions the cell survives); then the free cells (nothing drawn there) within `reach` cells
    of it, nearest first. At most 60 of each, spread out."""
    import numpy as np

    depth, cur = np.zeros(mask.shape, int), mask.copy()
    while cur.any():
        depth += cur
        cur = cur & _shift(cur, 1, 0) & _shift(cur, -1, 0) & _shift(cur, 0, 1) & _shift(cur, 0, -1)
    rr, cc = np.nonzero(mask)
    if not len(rr):
        return [], []
    cy, cx = rr.mean(), cc.mean()
    inside = sorted(zip(rr, cc), key=lambda p: (-depth[p], (p[0] - cy) ** 2 + (p[1] - cx) ** 2))
    j0, j1, i0, i1 = max(0, rr.min() - reach), rr.max() + reach + 1, max(0, cc.min() - reach), cc.max() + reach + 1
    fr, fc = np.nonzero(free[j0:j1, i0:i1])
    pts = np.column_stack([rr, cc])
    near = []
    for r, c in zip(fr + j0, fc + i0):
        d = np.min((pts[:, 0] - r) ** 2 + (pts[:, 1] - c) ** 2) if len(pts) < 4000 else (r - cy) ** 2 + (c - cx) ** 2
        if d <= reach * reach:
            near.append((d, r, c))
    near = [(r, c) for _, r, c in sorted(near)]
    return inside[::max(1, len(inside) // 60)][:60], near[::max(1, len(near) // 60)][:60]


def place_labels(ax, rows, side, raster, extent, renderer):
    """Label every part taller than LABEL_MIN on this side, tallest first, never over another label.

    The part's label (reference and height) goes, in this order of preference: inside the visible area of its top
    step, then of the whole part, if it fits there; next to it on free board, covering nothing (so small pins stay
    visible), then covering only the part itself; on its visible area even if it spills over a neighbour. Then each
    lower step of the part (a pot's bushing and body, a footswitch's thread and base) gets its height inside the
    visible area of that step where it fits, else next to it with a leader line. Returns the references with no
    spot left, the steps not labelled and the steps covered by a taller part or step (both as {(ref, height)})."""
    import matplotlib.patheffects as pe
    import numpy as np

    owner, height = raster
    x0, y0 = extent[0], extent[1]
    ny, nx = owner.shape
    taken, missed, unlabelled, hidden = [], [], set(), set()
    axbox = ax.get_window_extent(renderer)
    inv = ax.transData.inverted()

    def text(s, size):
        t = ax.text(x0, y0, s, ha="center", va="center", fontsize=size, linespacing=1.0, color="#1f2328", zorder=5,
                    path_effects=[pe.withStroke(linewidth=2, foreground="white")])
        bb = t.get_window_extent(renderer)
        (ax0, ay0), (ax1, ay1) = inv.transform([(bb.x0, bb.y0), (bb.x1, bb.y1)])
        return t, int(abs(ax1 - ax0) / 2 / GRID) + 1, int(abs(ay1 - ay0) / 2 / GRID) + 1  # half size in cells

    def covers(r, c, hw, hh, grid):
        """Set of the values of `grid` under the label centred on cell (r, c)."""
        return set(grid[max(0, r - hh):min(ny, r + hh + 1), max(0, c - hw):min(nx, c + hw + 1)].ravel().tolist())

    def put(t, spots):
        """Moves the label to the first spot where it fits; that spot, or None (and the label removed)."""
        for r, c in spots:
            t.set_position((x0 + c * GRID, y0 + r * GRID))
            bb = t.get_window_extent(renderer).expanded(1.08, 1.15)
            if axbox.contains(bb.x0, bb.y0) and axbox.contains(bb.x1, bb.y1) and not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                return r, c
        t.remove()
        return None

    def step_mask(i, z):
        return (owner == i) & (height > z - LEVEL_GAP) & (height <= z + 1e-6)

    order = sorted((i for i, r in enumerate(rows) if r[side] > LABEL_MIN), key=lambda i: -rows[i][side])
    for i in order:
        steps = levels(rows[i], side)
        t, hw, hh = text(f"{rows[i]['ref']}\n{rows[i][side]:.1f}", 6.5)
        on_top, _ = label_spots(step_mask(i, steps[0]), owner == -1, 0)
        inside, near = label_spots(owner == i, owner == -1, 2 * hw + 4)
        cover = {p: covers(*p, hw, hh, owner) for p in near}
        own = [p for p in on_top if covers(*p, hw, hh, owner) <= {i}]
        tiers = [own, [p for p in inside if covers(*p, hw, hh, owner) <= {i}],
                 [p for p in near if cover[p] <= {-1}], [p for p in near if cover[p] <= {i, -1}], inside]
        if put(t, [p for tier in tiers for p in tier]) is None:
            missed.append(rows[i]["ref"])
    for i in order:  # the lower steps after every part's own label, so those are never pushed out by a step
        for z in levels(rows[i], side)[1:]:
            mask = step_mask(i, z)
            if not mask.any():
                hidden.add((rows[i]["ref"], z))  # covered by a taller step: no surface to see
                continue
            t, hw, hh = text(f"{z:.1f}", 6)
            spots, near = label_spots(mask, (owner == i) | (owner == -1), 2 * hw + 6)
            inside = [p for p in spots if mask[max(0, p[0] - hh):p[0] + hh + 1, max(0, p[1] - hw):p[1] + hw + 1].all()]
            # too thin for its height (a pot's bushing): next to it, over the part itself or free board, with a leader
            beside = [p for p in near if covers(*p, hw, hh, owner) <= {i, -1}]
            spot = put(t, inside + beside)
            if spot is None:
                unlabelled.add((rows[i]["ref"], z))
            elif spot not in inside:
                rr, cc = np.nonzero(mask)
                k = np.argmin((rr - spot[0]) ** 2 + (cc - spot[1]) ** 2)
                ax.plot([x0 + spot[1] * GRID, x0 + cc[k] * GRID], [y0 + spot[0] * GRID, y0 + rr[k] * GRID],
                        color="white", linewidth=1.6, zorder=4, solid_capstyle="round")
                ax.plot([x0 + spot[1] * GRID, x0 + cc[k] * GRID], [y0 + spot[0] * GRID, y0 + rr[k] * GRID],
                        color="#1f2328", linewidth=0.6, zorder=4.5)
    return missed, unlabelled, hidden


def draw(rows, edges, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch, Polygon

    ink, muted = "#1f2328", "#6b7280"
    top = max(max(r["front"], r["back"]) for r in rows)
    bands = height_bands(top)
    pts = [p for line in edges for p in line] + [p for r in rows for poly, _, _ in r["pieces"] for p in poly]
    extent = (min(p[0] for p in pts) - 1, min(p[1] for p in pts) - 1, max(p[0] for p in pts) + 1,
              max(p[1] for p in pts) + 1)
    aspect = (extent[3] - extent[1]) / (extent[2] - extent[0])
    h_map = min(5.2 * aspect, 7.5)
    w_map = h_map / aspect
    labelled = [r for r in rows if max(r["front"], r["back"]) > LABEL_MIN]
    table_cols = 1 if len(labelled) <= 40 else 2
    fig_h = max(h_map + 1.9, 0.16 * len(labelled) / table_cols + 1.6)  # the table's rows need room too
    fig = plt.figure(figsize=(2 * w_map + 1.4 + 2.9 * table_cols, fig_h), layout="constrained")
    grid = fig.add_gridspec(1, 3, width_ratios=[w_map, w_map, 2.9 * table_cols])
    axes = [fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1])]
    for ax, side, title in zip(axes, ("front", "back"), ("Front (top view)", "Back (seen from below)")):
        pieces = [(z, poly) for r in rows for poly, z, ps in r["pieces"] if ps == side and z > 0]
        for z, poly in sorted(pieces, key=lambda p: p[0]):  # lowest first, so a taller part covers it
            ax.add_patch(Polygon(poly, closed=True, facecolor=band_colour(bands, z), edgecolor="white", linewidth=0.5))
        for line in edges:
            ax.plot(*zip(*line), color=ink, linewidth=1.2)
        ax.set_title(f"{title}, tallest {max(r[side] for r in rows):.1f} mm", color=ink, fontsize=11, loc="left")
        ax.set_xlim(extent[0], extent[2])
        ax.set_ylim(extent[3], extent[1])
        ax.set_aspect("equal")
        if side == "back":
            ax.invert_xaxis()
        ax.tick_params(colors=muted, labelsize=8)
        ax.set_xlabel("mm", color=muted, fontsize=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
    key = [Patch(facecolor=c, edgecolor="none", label=f"{lo:g}-{hi:.3g}") for lo, hi, c in bands]
    fig.legend(handles=key, loc="outside lower center", ncols=len(key), frameon=False, fontsize=8, labelcolor=muted,
               title="Height above the board surface, mm (labels: reference and height)", title_fontsize=8,
               handlelength=1.2, columnspacing=1.2)
    tab = fig.add_subplot(grid[0, 2])
    tab.axis("off")
    fig.canvas.draw()  # fixes the layout, so the labels below are placed where they will be drawn
    fig.set_layout_engine("none")
    renderer = fig.canvas.get_renderer()
    missed, unlabelled, hidden = {}, set(), set()
    for ax, side in zip(axes, ("front", "back")):
        refs, steps, covered = place_labels(ax, rows, side, visible(rows, side, extent), extent, renderer)
        missed[side] = set(refs)
        unlabelled |= {(side, ref, z) for ref, z in steps}
        hidden |= {(side, ref, z) for ref, z in covered}

    def cell(r, side):
        if r[side] <= 0:
            return "   -  "
        return f"{r[side]:5.1f}" + ("*" if r["ref"] in missed[side] else " ")

    def steps(r):
        out = []
        for side, mark in (("front", ""), ("back", "b")):
            out += [f"{mark}{z:.1f}" + ("*" if (side, r["ref"], z) in unlabelled else "")
                    for z in levels(r, side)[1:] if (side, r["ref"], z) not in hidden]
        return " ".join(out)

    lines = [f"{'Ref':<7}{'Front':>6} {'Back':>6}  Steps"] + [
        f"{r['ref']:<7}{cell(r, 'front')} {cell(r, 'back')}  {steps(r)}".rstrip()
        for r in sorted(labelled, key=lambda r: ref_key(r['ref']))]
    per = math.ceil((len(lines) - 1) / table_cols)
    for k in range(table_cols):
        chunk = [lines[0]] + lines[1 + k * per:1 + (k + 1) * per]
        tab.text(k / table_cols, 1.0, "\n".join(chunk), family="monospace", fontsize=7.5, color=ink, va="top",
                 transform=tab.transAxes)
    note = (f"Parts above {LABEL_MIN:g} mm, mm.\nSteps: the lower levels of a part\n(pot bushing and body,"
            "\nfootswitch thread and base); b = back.")
    if missed["front"] or missed["back"] or unlabelled:
        note += "\n* not labelled on the map:\n  no visible spot free"
    tab.text(0, 0, note, fontsize=7, color=muted, va="bottom", transform=tab.transAxes)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def ref_key(ref):
    """Natural order: C2 before C10."""
    return re.sub(r"\d+", "", ref), int(re.sub(r"\D", "", ref) or 0), ref


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help=".kicad_pcb file")
    ap.add_argument("-o", "--output", help="directory for the images (default: renders/ next to the board)")
    ap.add_argument("--heights", help="CSV with columns ref,front,back: heights that replace the 3D models' ones")
    ap.add_argument("--panel", type=float, help="height of the panel's inner face above the board, mm")
    ap.add_argument("--panel-side", choices=("front", "back"), default="front", help="side the panel is on (default front)")
    ap.add_argument("--through", default="", help="comma-separated references of the parts that pass through the panel")
    ap.add_argument("--wall", type=float, default=WALL, help=f"enclosure wall thickness, mm (default {WALL:g})")
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
            todo = {**VIEWS, **sides}
            print(f"Views of {os.path.basename(pcb)}: {len(todo)} renders ({', '.join(todo)}), then the height map")
            if not sides:
                print("no outline: no side views")
            for k, (name, view) in enumerate(todo.items(), 1):
                print(f"[{k}/{len(todo)}] render {name}")
                size = ["-w", "1600", "-h", "1600"] if name in VIEWS else ["-w", "1600", "-h", "700", "--zoom", "0.9"]
                kicad_cli.run("pcb", "render", *view, *size, "--background", "opaque", "--quality", "high",
                              "-o", os.path.join(out, name + ".png"), pcb)
        print("height map: reading the 3D models" + (f", panel at {args.panel:g} mm" if args.panel is not None else ""))
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
    fit_errors = []
    if through:
        fit, fit_errors, fit_warnings = panel_fit(panel_mechanics(rows, through, args.panel), args.panel, args.wall)
        print("\n".join(write_panel_fit(fit, os.path.join(out, "panel_fit.csv"), args.panel, args.wall)))
        for e in fit_errors:
            print(f"  ERROR {e}")
        for w_ in fit_warnings:
            print(f"  warning {w_}")
    print(f"written to {out}")
    return 1 if missing or collisions or fit_errors else 0


if __name__ == "__main__":
    sys.exit(main())

"""Exact-geometry copper check for a router's result: clearances, holes, board edge, connectivity, loose ends.

A router builds a Board from its own result and checks it before writing any file, in milliseconds, so bad
candidates are rejected at once and errors are reported in the router's own terms:

    from copper_check import Board, Rules, check
    b = Board(outline)                                   # outline: [[(x, y), ...], ...] closed polylines, mm
    b.pad("GND", ["F.Cu", "B.Cu"], "circle", x, y, 1.7, 1.7, drill=1.0, ref="J1.1")
    b.pad("/IN", ["B.Cu"], "roundrect", x, y, 1.0, 0.95, angle=90, rratio=0.25, ref="R1.1")
    b.track("/IN", "B.Cu", (x1, y1), (x2, y2), 0.25)
    b.via("GND", x, y, 0.6, 0.3)
    problems = check(b, Rules(clearance=0.2))            # [] when clean

It does not replace KiCad's DRC on the written board (silkscreen, courtyards, zones, footprints, parity), and it does
not model zones: nets in `Board.zone_nets` are not checked for connectivity, since a zone may join them.

As a script it reads a written board, which is how it is compared with KiCad's DRC:

    python copper_check.py board.kicad_pcb [--clearance 0.2] [--edge 0.3]

Rules default to JLCPCB's limits (the same figures as jlcpcb_rules.py). Arcs are approximated by straight pieces
within 0.001 mm. Exit code: 0 when clean, 1 otherwise.
"""
import argparse
import collections
import dataclasses
import math
import sys

TOL = 1e-6
ARC_ERROR = 0.001  # mm, largest gap between an arc and its straight pieces


@dataclasses.dataclass
class Rules:
    clearance: float = 0.15       # copper of different nets
    edge: float = 0.2             # copper to the board edge
    track_width: float = 0.10
    via_diameter: float = 0.45
    via_drill: float = 0.20
    via_annular: float = 0.075
    pad_annular: float = 0.18     # plated pad holes
    hole_to_hole: float = 0.20
    pad_hole_to_hole: float = 0.45
    hole_clearance: float = 0.20  # a hole to copper of another net


# ---------------------------------------------------------------- geometry
def _seg_point(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _cross(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def seg_seg(a, b, c, d):
    """Distance between segments ab and cd (0 when they cross)."""
    d1, d2, d3, d4 = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return 0.0
    return min(_seg_point(a, c, d), _seg_point(b, c, d), _seg_point(c, a, b), _seg_point(d, a, b))


def _inside(p, poly):
    """Point in polygon (even-odd)."""
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def arc_points(start, mid, end):
    """Points along a circular arc through start, mid, end."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))
    span = (a3 - a1) % math.tau
    if (a2 - a1) % math.tau > span:  # mid is not on the counter-clockwise way: go clockwise
        span -= math.tau
    step = 2 * math.acos(max(-1.0, 1 - ARC_ERROR / r)) if r > ARC_ERROR else math.pi / 4
    n = max(2, math.ceil(abs(span) / max(step, 1e-3)))
    return [(ux + r * math.cos(a1 + span * k / n), uy + r * math.sin(a1 + span * k / n)) for k in range(n + 1)]


class Shape:
    """A point, segment or polygon grown by radius r: covers every copper shape a board has."""
    __slots__ = ("pts", "r", "bbox")

    def __init__(self, pts, r=0.0):
        self.pts, self.r = [tuple(p) for p in pts], r
        xs, ys = [p[0] for p in self.pts], [p[1] for p in self.pts]
        self.bbox = (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)

    def edges(self):
        p = self.pts
        if len(p) <= 2:
            return [(p[0], p[-1])]
        return list(zip(p, p[1:] + p[:1]))


def distance(s, t):
    """Gap between two shapes; negative when they overlap."""
    if len(t.pts) >= 3 and _inside(s.pts[0], t.pts) or len(s.pts) >= 3 and _inside(t.pts[0], s.pts):
        base = 0.0
    else:
        base = min(seg_seg(a, b, c, d) for a, b in s.edges() for c, d in t.edges())
    return base - s.r - t.r


def _rotate(pts, angle, x, y):
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    return [(x + px * c + py * s, y - px * s + py * c) for px, py in pts]  # KiCad: y down, angles counter-clockwise


def pad_shapes(shape, x, y, w, h, angle=0.0, rratio=0.0, primitives=(), anchor="rect"):
    """Board-space Shapes of a pad. A custom pad is its anchor (rect or circle, the pad's size) plus its primitives:
    [(points, radius)] relative to the pad centre, unrotated (a polygon, a line with half its width, a circle...)."""
    if shape == "custom":
        return [pad_shape(anchor, x, y, w, h, angle)] + [Shape(_rotate(p, angle, x, y), r) for p, r in primitives]
    return [pad_shape(shape, x, y, w, h, angle, rratio)]


def pad_shape(shape, x, y, w, h, angle=0.0, rratio=0.0):
    """Board-space Shape of a plain pad."""
    if shape == "circle":
        return Shape([(x, y)], w / 2)
    if shape == "oval":
        r = min(w, h) / 2
        half = abs(w - h) / 2
        ends = [(-half, 0), (half, 0)] if w >= h else [(0, -half), (0, half)]
        return Shape(_rotate(ends, angle, x, y), r)
    r = min(w, h) * rratio if shape == "roundrect" else 0.0
    hw, hh = w / 2 - r, h / 2 - r
    return Shape(_rotate([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)], angle, x, y), r)


# ---------------------------------------------------------------- the board
@dataclasses.dataclass
class Item:
    kind: str            # "pad", "track", "via"
    net: object          # net name; None = connected to nothing (clears everything)
    layers: frozenset
    shapes: list         # Shapes of the copper; empty for a non-plated hole
    ref: str
    hole: float = 0.0    # drill diameter (the smaller side of an oval), 0 when none
    hole_at: tuple = None
    size: float = 0.0    # pad: smaller side; via: diameter; track: width


class Board:
    def __init__(self, outline, zone_nets=()):
        self.outline = [list(p) for p in outline]
        self.items = []
        self.zone_nets = set(zone_nets)

    def pad(self, net, layers, shape, x, y, w, h, angle=0.0, drill=0.0, rratio=0.0, primitives=(), anchor="rect",
            ref="", plated=True):
        copper = [l for l in layers if l.endswith(".Cu")]
        has_copper = copper and (plated or min(w, h) > drill)
        s = pad_shapes(shape, x, y, w, h, angle, rratio, primitives, anchor) if has_copper else []
        self.items.append(Item("pad" if plated else "npth", net if plated else None, frozenset(copper), s, ref,
                               drill, (x, y) if drill else None, min(w, h)))

    def track(self, net, layer, a, b, width, ref=""):
        self.items.append(Item("track", net, frozenset([layer]), [Shape([a, b], width / 2)], ref or f"track {net}", 0.0,
                               None, width))

    def via(self, net, x, y, diameter, drill, layers=("F.Cu", "B.Cu"), ref=""):
        self.items.append(Item("via", net, frozenset(layers), [Shape([(x, y)], diameter / 2)], ref or f"via {net}",
                               drill, (x, y), diameter))


def gap(shapes, others):
    """Smallest gap between two sets of Shapes."""
    return min(distance(s, t) for s in shapes for t in others)


def _bbox(shapes):
    b = [s.bbox for s in shapes]
    return min(x[0] for x in b), min(x[1] for x in b), max(x[2] for x in b), max(x[3] for x in b)


def _where(item):
    p = item.hole_at or (item.shapes[0].pts[0] if item.shapes else (0, 0))
    return round(p[0], 3), round(p[1], 3)


def _pairs(boxes, margin, cell=2.0):
    """Index pairs whose boxes (grown by margin) may touch, through a grid of cells."""
    grid = collections.defaultdict(list)
    for i, box in enumerate(boxes):
        if box is None:
            continue
        x0, y0, x1, y1 = box
        for gx in range(int((x0 - margin) // cell), int((x1 + margin) // cell) + 1):
            for gy in range(int((y0 - margin) // cell), int((y1 + margin) // cell) + 1):
                grid[gx, gy].append(i)
    seen = set()
    for members in grid.values():
        for k, i in enumerate(members):
            for j in members[k + 1:]:
                if (i, j) not in seen:
                    seen.add((i, j))
                    yield i, j


def check(board, rules=Rules()):
    """[{kind, items, actual, required, at}]: every problem found, in a fixed order."""
    items, out = board.items, []

    def problem(kind, refs, actual, required, at):
        out.append(dict(kind=kind, items=refs, actual=round(actual, 4), required=required, at=at))

    for it in items:
        if it.kind == "track" and it.size < rules.track_width - TOL:
            problem("track_width", [it.ref], it.size, rules.track_width, _where(it))
        if it.kind == "via":
            if it.size < rules.via_diameter - TOL:
                problem("via_diameter", [it.ref], it.size, rules.via_diameter, _where(it))
            if it.hole < rules.via_drill - TOL:
                problem("via_drill", [it.ref], it.hole, rules.via_drill, _where(it))
            if (it.size - it.hole) / 2 < rules.via_annular - TOL:
                problem("annular", [it.ref], (it.size - it.hole) / 2, rules.via_annular, _where(it))
        if it.kind == "pad" and it.hole and (it.size - it.hole) / 2 < rules.pad_annular - TOL:
            problem("annular", [it.ref], (it.size - it.hole) / 2, rules.pad_annular, _where(it))

    # copper to copper, holes, connectivity
    copper = [i for i, it in enumerate(items) if it.shapes]
    parent = {i: i for i in range(len(items))}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def hole_shape(it):
        return Shape([it.hole_at], it.hole / 2)

    boxes = [_bbox(it.shapes) if it.shapes else hole_shape(it).bbox if it.hole else None for it in items]
    margin = max(rules.clearance, rules.pad_hole_to_hole, rules.hole_clearance)
    for i, j in _pairs(boxes, margin):
        a, b = items[i], items[j]
        if a.shapes and b.shapes and a.layers & b.layers:
            d = gap(a.shapes, b.shapes)
            if a.net is not None and a.net == b.net:
                if d <= TOL:
                    parent[find(i)] = find(j)
            elif d < rules.clearance - TOL:
                problem("clearance", sorted([a.ref, b.ref]), max(d, 0.0), rules.clearance, _where(a))
        if a.hole and b.hole:
            need = rules.pad_hole_to_hole if a.kind in ("pad", "npth") and b.kind in ("pad", "npth") else rules.hole_to_hole
            d = distance(hole_shape(a), hole_shape(b))
            if d < need - TOL:
                problem("hole_to_hole", sorted([a.ref, b.ref]), max(d, 0.0), need, _where(a))
        for h, c in ((a, b), (b, a)):  # a hole and the copper of another net
            if h.hole and c.shapes and (c.net is None or c.net != h.net):
                d = gap([hole_shape(h)], c.shapes)
                if d < rules.hole_clearance - TOL:
                    problem("hole_clearance", sorted([h.ref, c.ref]), max(d, 0.0), rules.hole_clearance, _where(h))

    # the board edge
    edges = [Shape([p, q]) for loop in board.outline for p, q in zip(loop, loop[1:] + loop[:1]) if p != q]
    reach = rules.edge + 1.0  # edges farther than this from an item's box cannot matter
    for i in copper:
        it = items[i]
        inside = sum(_inside(it.shapes[0].pts[0], loop) for loop in board.outline) % 2 == 1
        x0, y0, x1, y1 = boxes[i]
        near = [e for e in edges if e.bbox[0] < x1 + reach and e.bbox[2] > x0 - reach and
                e.bbox[1] < y1 + reach and e.bbox[3] > y0 - reach]
        d = min((gap(it.shapes, [e]) for e in near), default=float("inf"))
        if not inside or d < rules.edge - TOL:
            problem("edge_clearance", [it.ref], max(d, 0.0) if inside else -1.0, rules.edge, _where(it))

    # connectivity and loose track ends
    groups = collections.defaultdict(set)
    for i in copper:
        net = items[i].net
        if net is not None and net not in board.zone_nets:
            groups[net].add(find(i))
    for net, g in sorted(groups.items()):
        if len(g) > 1:
            problem("unconnected", [net], len(g), 1, None)
    by_net = collections.defaultdict(list)
    for i in copper:
        by_net[items[i].net].append(i)
    for i in copper:
        it = items[i]
        if it.kind != "track" or it.net is None or it.net in board.zone_nets:  # a zone may hold that end
            continue
        seg = it.shapes[0]
        for end in (seg.pts[0], seg.pts[-1]):
            tip = [Shape([end], seg.r)]  # the round end of the track: KiCad joins copper that overlaps
            if not any(j != i and items[j].layers & it.layers and gap(tip, items[j].shapes) <= TOL
                       for j in by_net[it.net]):
                problem("track_dangling", [it.ref], 0.0, 0.0, (round(end[0], 3), round(end[1], 3)))
    return out


# ---------------------------------------------------------------- reading a written board
def from_kicad_pcb(path):
    """Board of a .kicad_pcb file: pads, tracks (lines and arcs), vias, the Edge.Cuts outline, nets with zones."""
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from kicad_gen import sexpr, kids, kid

    with open(path, encoding="utf-8") as fh:
        t = sexpr(fh.read())
    names = {n[1]: n[2] for n in kids(t, "net") if len(n) > 2}

    def net_of(node):
        n = kid(node, "net")
        if not n:
            return None
        name = n[2] if len(n) > 2 else names.get(n[1], n[1])
        return None if name in ("", "0") else name  # net 0 is "no net" in the numbered format

    def xy(node, key):
        k = kid(node, key)
        return (float(k[1]), float(k[2])) if k else None

    def copper_layers(node):
        ls = kid(node, "layers") or kid(node, "layer")
        out = set()
        for l in ls[1:]:
            if l in ("*.Cu", "F&B.Cu"):
                out |= {"F.Cu", "B.Cu"}
            elif l.endswith(".Cu"):
                out.add(l)
        return sorted(out)

    lines = []
    for g in t:
        if not (isinstance(g, list) and g[0].startswith("gr_") and kid(g, "layer") and kid(g, "layer")[1] == "Edge.Cuts"):
            continue
        if g[0] == "gr_line":
            lines.append([xy(g, "start"), xy(g, "end")])
        elif g[0] == "gr_arc":
            lines.append(arc_points(xy(g, "start"), xy(g, "mid"), xy(g, "end")))
        elif g[0] == "gr_rect":
            (x0, y0), (x1, y1) = xy(g, "start"), xy(g, "end")
            lines.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif g[0] == "gr_circle":
            c, e = xy(g, "center"), xy(g, "end")
            r = math.dist(c, e)
            n = max(16, math.ceil(math.pi / math.acos(max(-1.0, 1 - ARC_ERROR / r))))
            lines.append([(c[0] + r * math.cos(k * math.tau / n), c[1] + r * math.sin(k * math.tau / n)) for k in range(n + 1)])
        elif g[0] == "gr_poly":
            pts = [(float(p[1]), float(p[2])) for p in kids(kid(g, "pts"), "xy")]
            lines.append(pts + pts[:1])
    zone_nets = {net_of(z) for z in kids(t, "zone") if not kid(z, "keepout")} - {None}
    b = Board(_loops(lines), zone_nets)

    for fp in kids(t, "footprint"):
        at = kid(fp, "at")
        fx, fy, fa = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
        ref = next((p[2] for p in kids(fp, "property") if p[1] == "Reference"), "?")
        for p in kids(fp, "pad"):
            pat = kid(p, "at")
            px, py = _rotate([(float(pat[1]), float(pat[2]))], fa, fx, fy)[0]
            angle = float(pat[3]) if len(pat) > 3 else 0.0
            size = kid(p, "size")
            w, h = float(size[1]), float(size[2])
            drill = kid(p, "drill")
            dsize = [float(v) for v in (drill or [])[1:] if not isinstance(v, list) and v != "oval"]
            rr = kid(p, "roundrect_rratio")
            prims, anchor = [], "rect"
            if p[3] == "custom":
                anchor = (kid(kid(p, "options") or [], "anchor") or ["", "rect"])[1]
                prims = _primitives(kid(p, "primitives") or [], xy, kid)
            b.pad(net_of(p), copper_layers(p), p[3], px, py, w, h, angle, min(dsize) if dsize else 0.0,
                  float(rr[1]) if rr else 0.0, prims, anchor, f"{ref}.{p[1]}", plated=p[2] != "np_thru_hole")
    for s in kids(t, "segment"):
        b.track(net_of(s), kid(s, "layer")[1], xy(s, "start"), xy(s, "end"), float(kid(s, "width")[1]))
    for a in kids(t, "arc"):
        pts = arc_points(xy(a, "start"), xy(a, "mid"), xy(a, "end"))
        for p, q in zip(pts, pts[1:]):
            b.track(net_of(a), kid(a, "layer")[1], p, q, float(kid(a, "width")[1]))
    for v in kids(t, "via"):
        x, y = xy(v, "at")
        b.via(net_of(v), x, y, float(kid(v, "size")[1]), float(kid(v, "drill")[1]), copper_layers(v) or ("F.Cu", "B.Cu"))
    return b


def _primitives(node, xy, kid):
    """[(points, radius)] of a custom pad's primitives, relative to the pad."""
    out = []
    for g in node[1:]:
        if not isinstance(g, list):
            continue
        w = kid(g, "width")
        r = float(w[1]) / 2 if w else 0.0
        if g[0] == "gr_poly":
            out.append(([(float(q[1]), float(q[2])) for q in g_kids(kid(g, "pts"), "xy")], r))
        elif g[0] == "gr_line":
            out.append(([xy(g, "start"), xy(g, "end")], r))
        elif g[0] == "gr_circle":
            c = xy(g, "center")
            out.append(([c], math.dist(c, xy(g, "end")) + r))
        elif g[0] == "gr_rect":
            (x0, y0), (x1, y1) = xy(g, "start"), xy(g, "end")
            out.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], r))
        elif g[0] == "gr_arc":
            pts = arc_points(xy(g, "start"), xy(g, "mid"), xy(g, "end"))
            out += [([p, q], r) for p, q in zip(pts, pts[1:])]
    return out


def g_kids(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def _loops(lines, tol=0.01):
    """Join outline pieces into closed loops."""
    pieces, loops = [list(p) for p in lines], []
    while pieces:
        loop = pieces.pop(0)
        while math.dist(loop[0], loop[-1]) > tol:
            for k, p in enumerate(pieces):
                if math.dist(p[0], loop[-1]) <= tol:
                    loop += p[1:]
                elif math.dist(p[-1], loop[-1]) <= tol:
                    loop += p[::-1][1:]
                else:
                    continue
                pieces.pop(k)
                break
            else:
                break  # an open outline: KiCad's DRC and gerber.py report it
        loops.append(loop[:-1] if math.dist(loop[0], loop[-1]) <= tol else loop)
    return loops


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help=".kicad_pcb file")
    for f in dataclasses.fields(Rules):
        ap.add_argument("--" + f.name.replace("_", "-"), type=float, default=f.default)
    ap.add_argument("--max", type=int, default=10, help="examples printed per kind")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected
    import time
    t = time.time()
    b = from_kicad_pcb(args.board)
    found = check(b, Rules(**{f.name: getattr(args, f.name) for f in dataclasses.fields(Rules)}))
    by = collections.defaultdict(list)
    for p in found:
        by[p["kind"]].append(p)
    print(f"{len(b.items)} items, {len(b.outline)} outline loops, {len(found)} problems ({time.time() - t:.2f} s)")
    for kind, ps in by.items():
        print(f"  {kind} x{len(ps)}")
        for p in ps[:args.max]:
            print(f"      {', '.join(map(str, p['items']))}: {p['actual']} (needs {p['required']}) at {p['at']}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

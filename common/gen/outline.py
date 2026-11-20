"""Board outlines from enclosure templates (pcb/common/templates/<name>.kicad_pcb, copied from the templates the
enclosures are drawn in; the outline is kept as it is).

The template's Edge.Cuts items (lines, arcs, circles) are moved so that the outline's top-left corner is the board
origin. Circles are holes (the enclosure's mounting points). The outer contour gives, per row, the x range inside the
board: the router and the silkscreen labels keep out of everything else (corner cut-outs, the narrow tabs)."""
import math
import os
import sys

from .libs import COMMON, PCB
sys.path.insert(0, os.path.join(PCB, "tools"))
from kicad_gen import sexpr, kid  # noqa: E402

TEMPLATES = os.path.join(COMMON, "templates")


def _xy(node, key):
    v = kid(node, key)
    return float(v[1]), float(v[2])


def _arc_points(a, m, b, n=24):
    """Points along the circular arc a -> m -> b."""
    (x1, y1), (x2, y2), (x3, y3) = a, m, b
    dd = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / dd
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / dd
    r = math.hypot(x1 - ux, y1 - uy)
    t1, t2, t3 = (math.atan2(y - uy, x - ux) for x, y in (a, m, b))
    span = (t3 - t1) % (2 * math.pi)
    if (t2 - t1) % (2 * math.pi) > span:          # the mid point is on the other way round
        span -= 2 * math.pi
    return [(ux + r * math.cos(t1 + span * k / n), uy + r * math.sin(t1 + span * k / n)) for k in range(n + 1)]


def _span(segments, y):
    """(x_left, x_right) of a closed contour (straight pieces) on row y, or None outside it."""
    for yy in (y, y + 1e-7, y - 1e-7):        # a row through a vertex of the contour: look just beside it
        xs = []
        for (x1, y1), (x2, y2) in segments:
            if (y1 <= yy < y2) or (y2 <= yy < y1):
                xs.append(x1 + (yy - y1) * (x2 - x1) / (y2 - y1))
        if len(xs) >= 2:
            return min(xs), max(xs)
    return None


class Outline:
    def __init__(self, name):
        self.name = name
        with open(os.path.join(TEMPLATES, name + ".kicad_pcb"), encoding="utf-8") as fh:
            t = sexpr(fh.read())
        raw = []                                    # (kind, points) in template coordinates
        drawings = []                               # template silkscreen (F side), kept as a helper drawing
        for it in t:
            if not isinstance(it, list) or it[0] not in ("gr_line", "gr_arc", "gr_circle"):
                continue
            layer = kid(it, "layer")[1].strip('"')
            if it[0] == "gr_line":
                pts = ("line", [_xy(it, "start"), _xy(it, "end")])
            elif it[0] == "gr_arc":
                pts = ("arc", [_xy(it, "start"), _xy(it, "mid"), _xy(it, "end")])
            else:
                pts = ("circle", [_xy(it, "center"), _xy(it, "end")])
            if layer == "Edge.Cuts":
                raw.append(pts)
            elif layer == "F.SilkS":
                drawings.append(pts)
        xs = [p[0] for k, ps in raw if k != "circle" for p in ps]
        ys = [p[1] for k, ps in raw if k != "circle" for p in ps]
        self.ox, self.oy = min(xs), min(ys)
        self.width, self.height = round(max(xs) - self.ox, 4), round(max(ys) - self.oy, 4)
        mv = lambda ps: [(round(x - self.ox, 6), round(y - self.oy, 6)) for x, y in ps]   # noqa: E731
        self.items = [(k, mv(ps)) for k, ps in raw]
        self.drawings = [(k, mv(ps)) for k, ps in drawings]
        self.holes = [(ps[0][0], ps[0][1], math.dist(ps[0], ps[1])) for k, ps in self.items if k == "circle"]
        self.segments = []                          # the outer contour as straight pieces
        for k, ps in self.items:
            if k == "line":
                self.segments.append((ps[0], ps[1]))
            elif k == "arc":
                pts = _arc_points(*ps)
                self.segments += list(zip(pts, pts[1:]))

    @property
    def frame(self):
        """The template's silkscreen rim along the edge: the largest chain of its drawings joined end to end (with
        the detours around the holes). Lines that only cross it - the centre lines - and loose marks (the arrow)
        are not part of it."""
        items = [(k, ps) for k, ps in self.drawings if k != "circle"]
        ends = [(ps[0], ps[-1]) for _, ps in items]
        key = lambda p: (round(p[0], 2), round(p[1], 2))   # noqa: E731
        parent = list(range(len(items)))

        def root(a):
            while parent[a] != a:
                a = parent[a]
            return a
        by_point = {}
        for n, (a, b) in enumerate(ends):
            for p in (a, b):
                m = by_point.setdefault(key(p), n)
                parent[root(n)] = root(m)
        groups = {}
        for n in range(len(items)):
            groups.setdefault(root(n), []).append(n)
        best = max(groups.values(), key=lambda g: (len(g), -min(g)))
        return [items[n] for n in sorted(best)]

    def frame_segments(self):
        """The rim as straight pieces (arcs as polylines), for keeping labels and copper off it."""
        if getattr(self, "_frame_segs", None) is not None:
            return list(self._frame_segs)
        out = []
        for kind, ps in self.frame:
            pts = ps if kind == "line" else _arc_points(*ps)
            out += list(zip(pts, pts[1:]))
        self._frame_segs = out
        return list(out)

    def rim_loop(self):
        """The rim as one closed polyline (arcs as polylines), its points in order round the board, without the
        closing repeat."""
        segs = self.frame_segments()
        key = lambda p: (round(p[0], 4), round(p[1], 4))   # noqa: E731
        nxt = {}
        for a, b in segs:
            nxt.setdefault(key(a), []).append(b)
            nxt.setdefault(key(b), []).append(a)
        start = min(segs[0][0], segs[0][1])
        loop, prev, cur = [start], None, start
        while True:
            cands = [p for p in nxt[key(cur)] if prev is None or key(p) != key(prev)]
            if not cands:
                raise ValueError(f"{self.name}: the rim is not closed at {cur}")
            prev, cur = cur, min(cands)
            if key(cur) == key(start):
                return loop
            if len(loop) > len(segs):
                raise ValueError(f"{self.name}: the rim is not one closed loop")
            loop.append(cur)

    def beyond_rim(self, pad=0.5):
        """The ring between the rim and the board edge as two simple polygons (the left and the right half, cut on
        the centre line), reaching `pad` mm past the outline's bounding box: for keep-out rule areas."""
        loop = self.rim_loop()
        cx = self.width / 2
        pts = []                                    # the loop with its crossings of the centre line put in
        for a, b in zip(loop, loop[1:] + loop[:1]):
            pts.append(a)
            if (a[0] - cx) * (b[0] - cx) < 0:
                t = (cx - a[0]) / (b[0] - a[0])
                pts.append((cx, round(a[1] + t * (b[1] - a[1]), 6)))
        cut = sorted(k for k, p in enumerate(pts) if abs(p[0] - cx) < 1e-9)
        if len(cut) != 2:
            raise ValueError(f"{self.name}: the rim crosses the centre line {len(cut)} times, not twice")
        i, j = cut
        halves = [pts[i:j + 1], pts[j:] + pts[:i + 1]]
        x0, y0, x1, y1 = -pad, -pad, self.width + pad, self.height + pad
        out = []
        for h in halves:
            top, bot = (h[0], h[-1]) if h[0][1] < h[-1][1] else (h[-1], h[0])
            if h[0] is not top:
                h = h[::-1]                             # from the top crossing down to the bottom one
            ox = x0 if sum(p[0] for p in h) / len(h) < cx else x1
            out.append(h + [(cx, y1), (ox, y1), (ox, y0), (cx, y0)])
        return out

    def span(self, y, rim=False):
        """(x_left, x_right) inside the board (rim: inside the rim) on row y, or None outside it."""
        return _span(self.frame_segments() if rim else self.segments, y)

    def keepouts(self, margin, step=0.125, rim=False):
        """Rectangles (x0, y0, x1, y1) covering everything outside the board (rim: outside the rim), grown by
        `margin` (rows are taken together over +-margin, so corners are grown too). Inside the bounding box only."""
        segs = self.frame_segments() if rim else self.segments
        rows = [k * step for k in range(int(round(self.height / step)) + 1)]
        spans = [_span(segs, min(max(y, 1e-6), self.height - 1e-6)) for y in rows]
        out = []
        n = int(math.ceil(margin / step))
        for i, y in enumerate(rows):
            near = [spans[k] for k in range(max(0, i - n), min(len(rows), i + n + 1))]
            if any(s is None for s in near):
                out.append((0.0, y - step / 2, self.width, y + step / 2))
                continue
            xl = max(s[0] for s in near) + margin
            xr = min(s[1] for s in near) - margin
            if xl > 0:
                out.append((0.0, y - step / 2, xl, y + step / 2))
            if xr < self.width:
                out.append((xr, y - step / 2, self.width, y + step / 2))
        return out

    def inside(self, box, margin, rim=False):
        """Whether the rectangle (x0, y0, x1, y1) lies inside the board (rim: inside the rim), `margin` from the edge
        and the holes."""
        segs = self.frame_segments() if rim else self.segments
        x0, y0, x1, y1 = box
        k = max(2, int(math.ceil((y1 - y0 + 2 * margin) / 0.25)))
        for i in range(k + 1):
            y = y0 - margin + (y1 - y0 + 2 * margin) * i / k
            s = _span(segs, y)
            if s is None or x0 < s[0] + margin or x1 > s[1] - margin:
                return False
        for hx, hy, r in self.holes:
            cx, cy = min(max(hx, x0), x1), min(max(hy, y0), y1)
            if math.hypot(cx - hx, cy - hy) < r + margin:
                return False
        return True

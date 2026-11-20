"""Grid router for small two-layer boards.

A* over (layer, i, j, direction) on a fine grid with turn costs (straight tracks, few bends), vias as layer changes,
and a per-layer preference. Copper of every net is rasterised as a halo of (copper extent + clearance + half track
width) around it: a grid point is a legal track centre line for net n when no other net's halo covers it.
Deterministic: fixed net order, no randomness.
"""
import heapq
import math

FREE, BLOCK = -1, -2
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


# ---------------------------------------------------------------- shapes
class Shape:
    """Copper shape on the board: 'rect' (axis aligned, w x h), 'circle' (d = w), 'oval' (stadium w x h)."""

    def __init__(self, kind, cx, cy, w, h):
        self.kind, self.cx, self.cy, self.w, self.h = kind, cx, cy, w, h

    def dist(self, x, y):
        dx, dy = abs(x - self.cx), abs(y - self.cy)
        if self.kind == "circle":
            return math.hypot(dx, dy) - self.w / 2
        if self.kind == "oval":
            if self.w >= self.h:
                r = self.h / 2
                dx = max(dx - (self.w / 2 - r), 0)
            else:
                r = self.w / 2
                dy = max(dy - (self.h / 2 - r), 0)
            return math.hypot(dx, dy) - r
        ex, ey = max(dx - self.w / 2, 0), max(dy - self.h / 2, 0)
        if ex == 0 and ey == 0:
            return -min(self.w / 2 - dx, self.h / 2 - dy)
        return math.hypot(ex, ey)

    def bbox(self):
        return self.cx - self.w / 2, self.cy - self.h / 2, self.cx + self.w / 2, self.cy + self.h / 2


def seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - x1 - t * dx, py - y1 - t * dy)


# ---------------------------------------------------------------- router
class Router:
    def __init__(self, width, height, grid=0.125, track=0.25, clearance=0.2, via=0.6, via_drill=0.3,
                 edge=0.35, layer_cost=(1.0, 1.8), turn_cost=(0, 3, 10, 40), via_cost=28):
        self.G = grid
        self.nx, self.ny = int(round(width / grid)) + 1, int(round(height / grid)) + 1
        self.W, self.H = width, height
        self.hw, self.clr, self.rv, self.via_drill = track / 2, clearance, via / 2, via_drill
        self.track = track
        self.layer_cost = list(layer_cost)
        self.turn_cost = turn_cost
        self.via_cost = via_cost
        self.h_weight = 1.3                    # weighted A*: much faster, paths stay near-shortest
        n = self.nx * self.ny
        self.map = [[FREE] * n, [FREE] * n]
        self.novia = bytearray(n)              # 1 = no via here (SMD pads)
        self.tree = {}                         # net -> set of (layer, cell) on its copper
        self.segments = []                     # (net, layer, x1, y1, x2, y2)
        self.vias = []                         # (net, x, y)
        self.pads = []                         # (net, layers, Shape)
        R = self.hw + edge
        for j in range(self.ny):
            for i in range(self.nx):
                x, y = i * grid, j * grid
                if x < R or y < R or x > width - R or y > height - R:
                    c = j * self.nx + i
                    self.map[0][c] = self.map[1][c] = BLOCK

    # -- marking
    def _mark(self, layer, c, net):
        m = self.map[layer]
        v = m[c]
        if v == FREE:
            m[c] = net
        elif v != net:
            m[c] = BLOCK

    def _cells_near(self, x0, y0, x1, y1, R):
        G = self.G
        i0, i1 = max(0, int(math.floor((x0 - R) / G))), min(self.nx - 1, int(math.ceil((x1 + R) / G)))
        j0, j1 = max(0, int(math.floor((y0 - R) / G))), min(self.ny - 1, int(math.ceil((y1 + R) / G)))
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                yield i, j, i * G, j * G

    def add_pad(self, net, layers, shape, smd):
        """net: int id, or None for an unconnected pad (blocks everybody)."""
        self.pads.append((net, layers, shape))
        nid = BLOCK if net is None else net
        R = self.hw + self.clr
        x0, y0, x1, y1 = shape.bbox()
        for i, j, x, y in self._cells_near(x0, y0, x1, y1, R + 0.3):
            d = shape.dist(x, y)
            c = j * self.nx + i
            if d <= R:
                for L in layers:
                    self._mark(L, c, nid)
            if d <= self.rv + 0.1:          # no vias in or right next to any pad
                self.novia[c] = 1

    def add_hole(self, x, y, d):
        R = d / 2 + 0.25 + self.hw
        for i, j, px, py in self._cells_near(x, y, x, y, R):
            if math.hypot(px - x, py - y) <= R:
                c = j * self.nx + i
                self.map[0][c] = self.map[1][c] = BLOCK
        R2 = d / 2 + 0.25 + self.rv
        for i, j, px, py in self._cells_near(x, y, x, y, R2):
            if math.hypot(px - x, py - y) <= R2:
                self.novia[j * self.nx + i] = 1

    def keepout_circle(self, x, y, r):
        """No tracks or vias inside radius r (both layers)."""
        R = r + self.hw
        for i, j, px, py in self._cells_near(x, y, x, y, R):
            if math.hypot(px - x, py - y) <= R:
                c = j * self.nx + i
                self.map[0][c] = self.map[1][c] = BLOCK
                self.novia[c] = 1

    def keepout(self, x0, y0, x1, y1, layers):
        for i, j, x, y in self._cells_near(x0, y0, x1, y1, 0):
            if x0 <= x <= x1 and y0 <= y <= y1:
                for L in layers:
                    self.map[L][j * self.nx + i] = BLOCK

    def _add_seg(self, net, layer, x1, y1, x2, y2):
        self.segments.append((net, layer, x1, y1, x2, y2))
        R = self.hw + self.clr + self.hw
        for i, j, x, y in self._cells_near(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), R):
            d = seg_dist(x, y, x1, y1, x2, y2)
            c = j * self.nx + i
            if d <= R:
                self._mark(layer, c, net)
            if d <= 0.01:
                self.tree.setdefault(net, set()).add((layer, c))

    def _add_via(self, net, x, y):
        self.vias.append((net, x, y))
        R = self.rv + self.clr + self.hw
        for i, j, px, py in self._cells_near(x, y, x, y, R):
            d = math.hypot(px - x, py - y)
            c = j * self.nx + i
            if d <= R:
                self._mark(0, c, net)
                self._mark(1, c, net)
            if d <= self.rv + self.clr + self.rv:
                self.novia[c] = 1
        c = int(round(y / self.G)) * self.nx + int(round(x / self.G))
        self.tree.setdefault(net, set()).update({(0, c), (1, c)})

    # -- search
    def _via_ok(self, net, i, j):
        if self.novia[j * self.nx + i]:
            return False
        r = int(math.ceil((self.rv - self.hw) / self.G + 0.71))
        m0, m1, nx = self.map[0], self.map[1], self.nx
        for dj in range(-r, r + 1):
            jj = j + dj
            if jj < 0 or jj >= self.ny:
                return False
            for di in range(-r, r + 1):
                if di * di + dj * dj > r * r:
                    continue
                ii = i + di
                if ii < 0 or ii >= nx:
                    return False
                c = jj * nx + ii
                if (m0[c] != FREE and m0[c] != net) or (m1[c] != FREE and m1[c] != net):
                    return False
        return True

    def route(self, net, src, dst_xy, window=8.0, max_nodes=600000, layer_cost=None):
        """Connect the cells `src` [(layer, cell)] to the existing copper of `net`. dst_xy: target point for the
        heuristic. Returns True when routed."""
        nx, ny, G = self.nx, self.ny, self.G
        goal = self.tree.get(net, set())
        if not goal:
            return False
        if any(s in goal for s in src):
            return True
        lc = layer_cost or self.layer_cost
        tx, ty = dst_xy[0] / G, dst_xy[1] / G
        sx = [c % nx for _, c in src]
        sy = [c // nx for _, c in src]
        w = window / G
        i0, i1 = max(0, int(min(min(sx), tx) - w)), min(nx - 1, int(max(max(sx), tx) + w))
        j0, j1 = max(0, int(min(min(sy), ty) - w)), min(ny - 1, int(max(max(sy), ty) + w))
        maps = self.map
        tc = self.turn_cost
        hw_ = self.h_weight * min(lc)
        NC = nx * ny
        goal_i = {L * NC + c for L, c in goal}
        # state = (L*NC + cell)*9 + dir ; dir 8 = none (at a pad / after a via)
        best = {}
        prev = {}
        heap = []
        push, pop = heapq.heappush, heapq.heappop
        for L, c in src:
            i, j = c % nx, c // nx
            s = (L * NC + c) * 9 + 8
            best[s] = 0.0
            prev[s] = -1
            dx, dy = abs(i - tx), abs(j - ty)
            push(heap, ((dx + dy - 0.586 * min(dx, dy)) * hw_, 0.0, s))
        found = -1
        n = 0
        steps = [(nd, di, dj, di * dj != 0, dj * nx + di) for nd, (di, dj) in enumerate(DIRS)]
        tcost = [[0.0] * 8 for _ in range(9)]
        for d in range(8):
            for nd in range(8):
                turn = (nd - d) % 8
                turn = min(turn, 8 - turn)
                tcost[d][nd] = -1.0 if turn == 4 else tc[turn]
        via_cost = self.via_cost
        while heap:
            fcost, g, s = pop(heap)
            if g > best.get(s, 1e18):
                continue
            d = s % 9
            lc_ = s // 9
            if lc_ in goal_i:
                found = s
                break
            n += 1
            if n > max_nodes:
                break
            L = lc_ // NC
            c = lc_ - L * NC
            j, i = divmod(c, nx)
            m = maps[L]
            tcd = tcost[d]
            lcl = lc[L]
            base = L * NC
            for nd, di, dj, diag, off in steps:
                t = tcd[nd]
                if t < 0:
                    continue
                ii, jj = i + di, j + dj
                if ii < i0 or ii > i1 or jj < j0 or jj > j1:
                    continue
                cc = c + off
                v = m[cc]
                if v != FREE and v != net:
                    continue
                if diag:
                    a, b = m[c + di], m[c + dj * nx]
                    if (a != FREE and a != net) or (b != FREE and b != net):
                        continue
                    ng = g + 1.4142 * lcl + t
                else:
                    ng = g + lcl + t
                ns = (base + cc) * 9 + nd
                if ng < best.get(ns, 1e18):
                    best[ns] = ng
                    prev[ns] = s
                    dx, dy = abs(ii - tx), abs(jj - ty)
                    push(heap, (ng + (dx + dy - 0.586 * (dx if dx < dy else dy)) * hw_, ng, ns))
            # via
            ns = ((1 - L) * NC + c) * 9 + 8
            ng = g + via_cost
            if ng < best.get(ns, 1e18) and self._via_ok(net, i, j):
                best[ns] = ng
                prev[ns] = s
                dx, dy = abs(i - tx), abs(j - ty)
                push(heap, (ng + (dx + dy - 0.586 * min(dx, dy)) * hw_, ng, ns))
        self.last_nodes = n
        if found < 0:
            return False
        path = []
        s = found
        while s >= 0:
            lc_ = s // 9
            L, c = divmod(lc_, NC)
            path.append((L, c % nx, c // nx, s % 9))
            s = prev[s]
        path.reverse()
        self._commit(net, path)
        return True

    def _commit(self, net, path):
        G = self.G
        # split into runs per layer; vias where the layer changes
        runs = []
        cur = [path[0]]
        for s in path[1:]:
            if s[0] != cur[-1][0]:
                runs.append(cur)
                self._add_via(net, s[1] * G, s[2] * G)
                cur = [s]
            else:
                cur.append(s)
        runs.append(cur)
        for run in runs:
            pts = [(s[1], s[2]) for s in run]
            # merge collinear steps
            verts = [pts[0]]
            for k in range(1, len(pts) - 1):
                ax, ay = pts[k][0] - verts[-1][0], pts[k][1] - verts[-1][1]
                bx, by = pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]
                if ax * by - ay * bx != 0:
                    verts.append(pts[k])
            if len(pts) > 1:
                verts.append(pts[-1])
            for (a, b), (c, d) in zip(verts, verts[1:]):
                self._add_seg(net, run[0][0], a * G, b * G, c * G, d * G)
        for s in path:
            self.tree.setdefault(net, set()).add((s[0], s[2] * self.nx + s[1]))

    def pad_cells(self, shape, layers, net):
        """Grid cells inside a pad that are legal for its net (route start/goal cells)."""
        out = []
        x0, y0, x1, y1 = shape.bbox()
        for i, j, x, y in self._cells_near(x0, y0, x1, y1, 0):
            if shape.dist(x, y) <= -0.05:
                c = j * self.nx + i
                for L in layers:
                    v = self.map[L][c]
                    if v == FREE or v == net:
                        out.append((L, c))
        return out

    def seed(self, net, cells):
        self.tree.setdefault(net, set()).update(cells)

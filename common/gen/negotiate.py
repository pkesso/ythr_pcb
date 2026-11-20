"""Negotiated-congestion router (PathFinder) on the same grid as gen.router.

Pads, holes and keep-outs are hard obstacles (static map, as in gen.router.Router). Tracks are soft: every net is
routed against the others' copper with a cost for sharing space (present congestion) plus a growing history cost
where conflicts persist; all nets are ripped up and rerouted each iteration until no net's track centre line lies in
another net's clearance halo. Deterministic: fixed order, no randomness.
"""
import heapq
import math

from .router import Router, FREE, DIRS, seg_dist


class NegotiatedRouter(Router):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        n = self.nx * self.ny
        self.occ = [bytearray(n), bytearray(n)]          # number of other nets' halos covering a cell
        self.hist = [[0.0] * n, [0.0] * n]
        self.routes = {}                                  # net -> dict(segs, vias, halo[2] set, line[2] set)
        self.pres = 0.5

    # ---------------------------------------------------------------- geometry of one net's routing
    def _paths_to_geometry(self, paths):
        G = self.G
        segs, vias = [], []
        for path in paths:
            runs, cur = [], [path[0]]
            for s in path[1:]:
                if s[0] != cur[-1][0]:
                    runs.append(cur)
                    vias.append((s[1] * G, s[2] * G))
                    cur = [s]
                else:
                    cur.append(s)
            runs.append(cur)
            for run in runs:
                pts = [(s[1], s[2]) for s in run]
                verts = [pts[0]]
                for k in range(1, len(pts) - 1):
                    ax, ay = pts[k][0] - verts[-1][0], pts[k][1] - verts[-1][1]
                    bx, by = pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]
                    if ax * by - ay * bx != 0:
                        verts.append(pts[k])
                if len(pts) > 1:
                    verts.append(pts[-1])
                for (a, b), (c, d) in zip(verts, verts[1:]):
                    segs.append((run[0][0], a * G, b * G, c * G, d * G))
        vias = sorted(set(vias))
        return segs, vias

    def _halo(self, segs, vias):
        halo = [set(), set()]
        line = [set(), set()]
        R = 2 * self.hw + self.clr
        nx = self.nx
        for L, x1, y1, x2, y2 in segs:
            for i, j, x, y in self._cells_near(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), R):
                d = seg_dist(x, y, x1, y1, x2, y2)
                if d <= R:
                    halo[L].add(j * nx + i)
                if d <= 0.01:
                    line[L].add(j * nx + i)
        Rv = self.rv + self.clr + self.hw
        for x, y in vias:
            for i, j, px, py in self._cells_near(x, y, x, y, Rv):
                if math.hypot(px - x, py - y) <= Rv:
                    halo[0].add(j * nx + i)
                    halo[1].add(j * nx + i)
            c = int(round(y / self.G)) * nx + int(round(x / self.G))
            line[0].add(c)
            line[1].add(c)
        return halo, line

    def _occupy(self, net, sign):
        r = self.routes.get(net)
        if not r:
            return
        for L in (0, 1):
            o = self.occ[L]
            for c in r["halo"][L]:
                o[c] += sign

    # ---------------------------------------------------------------- one connection
    def _search(self, net, src, goal_i, dst_xy, window, max_nodes, lc):
        nx, ny, G = self.nx, self.ny, self.G
        NC = nx * ny
        tx, ty = dst_xy[0] / G, dst_xy[1] / G
        sx = [c % nx for _, c in src]
        sy = [c // nx for _, c in src]
        w = window / G
        i0, i1 = max(0, int(min(min(sx), tx) - w)), min(nx - 1, int(max(max(sx), tx) + w))
        j0, j1 = max(0, int(min(min(sy), ty) - w)), min(ny - 1, int(max(max(sy), ty) + w))
        maps, occ, hist = self.map, self.occ, self.hist
        pres = self.pres
        hw_ = self.h_weight * min(lc)
        tc = self.turn_cost
        tcost = [[0.0] * 8 for _ in range(9)]
        for d in range(8):
            for nd in range(8):
                turn = (nd - d) % 8
                turn = min(turn, 8 - turn)
                tcost[d][nd] = -1.0 if turn == 4 else tc[turn]
        steps = [(nd, di, dj, di * dj != 0, dj * nx + di) for nd, (di, dj) in enumerate(DIRS)]
        best, prev, heap = {}, {}, []
        push, pop = heapq.heappush, heapq.heappop
        for L, c in src:
            s = (L * NC + c) * 9 + 8
            best[s] = 0.0
            prev[s] = -1
            i, j = c % nx, c // nx
            dx, dy = abs(i - tx), abs(j - ty)
            push(heap, ((dx + dy - 0.586 * min(dx, dy)) * hw_, 0.0, s))
        found, n = -1, 0
        via_cost = self.via_cost
        rv_cells = int(math.ceil((self.rv - self.hw) / G + 0.71))
        while heap:
            fcost, g, s = pop(heap)
            if g > best.get(s, 1e18):
                continue
            lc_ = s // 9
            if lc_ in goal_i:
                found = s
                break
            n += 1
            if n > max_nodes:
                break
            d = s % 9
            L = lc_ // NC
            c = lc_ - L * NC
            j, i = divmod(c, nx)
            m, oc, hi = maps[L], occ[L], hist[L]
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
                    step = 1.4142 * lcl
                else:
                    step = lcl
                k = oc[cc]
                ng = g + step * (1.0 + hi[cc]) * (1.0 + pres * k) + t
                ns = (base + cc) * 9 + nd
                if ng < best.get(ns, 1e18):
                    best[ns] = ng
                    prev[ns] = s
                    dx, dy = abs(ii - tx), abs(jj - ty)
                    push(heap, (ng + (dx + dy - 0.586 * (dx if dx < dy else dy)) * hw_, ng, ns))
            ns = ((1 - L) * NC + c) * 9 + 8
            ng = g + via_cost
            if ng < best.get(ns, 1e18) and self._via_ok(net, i, j):
                # congestion around the via
                k = 0
                for dj2 in (-rv_cells, 0, rv_cells):
                    for di2 in (-rv_cells, 0, rv_cells):
                        cc = c + dj2 * nx + di2
                        if 0 <= cc < NC:
                            k += occ[0][cc] + occ[1][cc]
                ng += via_cost * pres * k
                if ng < best.get(ns, 1e18):
                    best[ns] = ng
                    prev[ns] = s
                    dx, dy = abs(i - tx), abs(j - ty)
                    push(heap, (ng + (dx + dy - 0.586 * min(dx, dy)) * hw_, ng, ns))
        if found < 0:
            return None
        path = []
        s = found
        while s >= 0:
            L, c = divmod(s // 9, NC)
            path.append((L, c % nx, c // nx, s % 9))
            s = prev[s]
        path.reverse()
        return path

    # ---------------------------------------------------------------- one net
    def route_net(self, net, terms, lc=None):
        """terms: [(cells, (x, y))] per pad, in connection order. Returns True if all pads are connected."""
        lc = lc or self.layer_cost
        NC = self.nx * self.ny
        tree = {L * NC + c for L, c in terms[0][0]}
        paths = []
        ok = True
        for k in range(1, len(terms)):
            cells, xy = terms[k]
            if not cells:
                ok = False
                continue
            if any(L * NC + c in tree for L, c in cells):
                tree.update(L * NC + c for L, c in cells)
                continue
            # aim at the nearest connected pad
            tx, ty = min((terms[q][1] for q in range(k) if any(L * NC + c in tree for L, c in terms[q][0])),
                         key=lambda p: math.dist(p, xy), default=terms[0][1])
            path = None
            for window, nodes in ((5.0, 120000), (30.0, 600000)):
                path = self._search(net, cells, tree, (tx, ty), window, nodes, lc)
                if path:
                    break
            if not path:
                ok = False
                continue
            paths.append(path)
            tree.update(s[0] * NC + s[2] * self.nx + s[1] for s in path)
            tree.update(L * NC + c for L, c in cells)
        old = self.routes.get(net)
        if not ok and old and old["ok"]:
            return True          # keep the previous (complete) routing of this net for now
        segs, vias = self._paths_to_geometry(paths)
        halo, line = self._halo(segs, vias)
        self.routes[net] = dict(segs=segs, vias=vias, halo=halo, line=line, ok=ok)
        return ok

    def conflicts(self, net):
        """Centre-line cells of `net` inside another net's halo, plus vias too close to other vias."""
        r = self.routes[net]
        bad = []
        for L in (0, 1):
            o = self.occ[L]
            own = r["halo"][L]
            m = self.map[L]
            for c in r["line"][L]:
                # next to its own pads the static map already keeps other nets' centre lines away
                if m[c] == net:
                    continue
                if o[c] - (1 if c in own else 0) > 0:
                    bad.append((L, c))
        return bad

    def via_conflicts(self):
        allv = [(x, y, n) for n, r in self.routes.items() for x, y in r["vias"]]
        lim = 2 * self.rv + self.clr
        bad = set()
        for a in range(len(allv)):
            for b in range(a + 1, len(allv)):
                if allv[a][2] != allv[b][2] and math.dist(allv[a][:2], allv[b][:2]) < lim - 1e-6:
                    bad.add(allv[a][2])
                    bad.add(allv[b][2])
        return bad

    def negotiate(self, nets, iterations=30, log=print, lc_for=lambda net: None):
        """nets: [(net_id, terms)] in routing order. Uses gen.fastroute (Numba + worker processes) when Numba is
        installed and PEDALGEN_SLOW is not set; the pure-Python loop below is the reference implementation."""
        import os
        if not os.environ.get("PEDALGEN_SLOW"):
            try:
                from . import fastroute
            except ImportError:
                fastroute = None
            if fastroute:
                return fastroute.negotiate(self, nets, iterations=iterations, log=log, lc_for=lc_for)
        import time
        todo = {net for net, _ in nets}
        w0 = self.h_weight
        for it in range(iterations):
            t0 = time.time()
            self.h_weight = min(2.5, w0 + 0.15 * it)   # greedier as the penalties grow: keeps the searches short
            for net, terms in nets:
                if net not in todo:
                    continue
                self._occupy(net, -1)
                self.route_net(net, terms, lc_for(net))
                self._occupy(net, +1)
            bad_nets = {}
            for net, _ in nets:
                b = self.conflicts(net)
                if b:
                    bad_nets[net] = b
                    for L, c in b:
                        self.hist[L][c] += 1.0
            vbad = self.via_conflicts()
            failed = [net for net, _ in nets if not self.routes[net]["ok"]]
            log(f"  iteration {it + 1}: rerouted {len(todo)}, {len(bad_nets)} nets in conflict, {len(vbad)} via clashes, "
                f"{len(failed)} unrouted, pres={self.pres:.2f} ({time.time() - t0:.0f} s)")
            if not bad_nets and not vbad and not failed:
                break
            todo = set(bad_nets) | vbad | set(failed)
            self.pres *= 1.8
        self.collect()
        problems = sorted(set(bad_nets) | vbad | set(failed))
        return problems

    def collect(self):
        fixed_s = getattr(self, "fixed_segments", [])
        fixed_v = getattr(self, "fixed_vias", [])
        self.segments = fixed_s + [(net, L, x1, y1, x2, y2) for net, r in sorted(self.routes.items())
                                   for L, x1, y1, x2, y2 in r["segs"]]
        self.vias = fixed_v + [(net, x, y) for net, r in sorted(self.routes.items()) for x, y in r["vias"]]

    def reserve_ground(self, net, pads, log=print):
        """Before any routing: a via (and a short track) next to every SMD GND pad, fixed like a pad, so the signal
        routing has to leave it room. pads: [(Shape, (part_x, part_y))]; the via goes outward from the part."""
        G = self.G
        self.fixed_segments, self.fixed_vias = [], []
        missing = []
        for sh, (px, py) in pads:
            out = math.degrees(math.atan2(sh.cy - py, sh.cx - px)) if (sh.cx, sh.cy) != (px, py) else 90.0
            angles = sorted(range(0, 360, 15), key=lambda a: (abs((a - out + 180) % 360 - 180), a))
            base = max(sh.w, sh.h) / 2
            done = False
            for rad in [base + 0.45 + 0.125 * k for k in range(16)]:
                for a in angles:
                    x = sh.cx + rad * math.cos(math.radians(a))
                    y = sh.cy + rad * math.sin(math.radians(a))
                    i, j = int(round(x / G)), int(round(y / G))
                    if not (2 <= i < self.nx - 2 and 2 <= j < self.ny - 2):
                        continue
                    if not self._via_ok(net, i, j) or not self._line_free(net, 0, sh.cx, sh.cy, i * G, j * G):
                        continue
                    if any(math.dist((i * G, j * G), (vx, vy)) < 2 * self.rv + self.clr for _, vx, vy in self.fixed_vias):
                        continue
                    self._add_seg(net, 0, sh.cx, sh.cy, i * G, j * G)
                    self._add_via(net, i * G, j * G)
                    done = True
                    break
                if done:
                    break
            if not done:
                missing.append((round(sh.cx, 2), round(sh.cy, 2)))
        self.fixed_segments, self.fixed_vias = list(self.segments), list(self.vias)
        log(f"  ground: {len(pads) - len(missing)} pad vias reserved, no room at {missing}")
        return missing

    # ---------------------------------------------------------------- ground
    def _via_free(self, net, i, j):
        """A via of `net` at (i, j): static obstacles (pads, holes) and every routed track kept clear."""
        if not self._via_ok(net, i, j):
            return False
        nx = self.nx
        r = int(math.ceil((self.rv - self.hw) / self.G + 0.71))
        for dj in range(-r, r + 1):
            for di in range(-r, r + 1):
                if di * di + dj * dj <= r * r:
                    c = (j + dj) * nx + i + di
                    if self.occ[0][c] or self.occ[1][c]:
                        return False
        x, y = i * self.G, j * self.G
        lim = 2 * self.rv + self.clr
        return all(math.dist((x, y), (vx, vy)) >= lim for r_ in self.routes.values() for vx, vy in r_["vias"])

    def _open_area(self, i, j, radius):
        """Nothing but empty board within `radius` on both layers: the GND zones will fill around a via here."""
        r = int(radius / self.G)
        nx = self.nx
        for dj in range(-r, r + 1):
            for di in range(-r, r + 1):
                if di * di + dj * dj <= r * r:
                    c = (j + dj) * nx + i + di
                    if self.map[0][c] != FREE or self.map[1][c] != FREE or self.occ[0][c] or self.occ[1][c]:
                        return False
        return True

    def _line_free(self, net, L, x1, y1, x2, y2):
        n = max(2, int(math.dist((x1, y1), (x2, y2)) / (self.G / 2)))
        for k in range(n + 1):
            x, y = x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n
            c = int(round(y / self.G)) * self.nx + int(round(x / self.G))
            v = self.map[L][c]
            if (v != FREE and v != net) or self.occ[L][c]:
                return False
        return True

    def _ground_path(self, net, sh, r, reach=4.0):
        """Breadth-first search on the top layer from the pad to the nearest spot with room for a via, through
        cells that keep clear of every other net. Adds the track and the via; True when found."""
        from collections import deque
        G, nx = self.G, self.nx
        m, o = self.map[0], self.occ[0]
        start = [c for L, c in self.pad_cells(sh, [0], net)]
        if not start:
            return False
        prev = {c: -1 for c in start}
        dq = deque(start)
        lim = (reach / G) ** 2
        cx, cy = sh.cx / G, sh.cy / G
        while dq:
            c = dq.popleft()
            j, i = divmod(c, nx)
            if c not in start and sh.dist(i * G, j * G) > self.rv + 0.2 and self._via_free(net, i, j):
                path = []
                while c != -1:
                    path.append(c)
                    c = prev[c]
                path.reverse()
                pts = [((c % nx) * G, (c // nx) * G) for c in path]
                verts = [pts[0]]
                for k in range(1, len(pts) - 1):
                    ax, ay = pts[k][0] - verts[-1][0], pts[k][1] - verts[-1][1]
                    bx, by = pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]
                    if abs(ax * by - ay * bx) > 1e-9:
                        verts.append(pts[k])
                verts.append(pts[-1])
                for (a, b), (c2, d2) in zip(verts, verts[1:]):
                    r["segs"].append((0, a, b, c2, d2))
                r["vias"].append(pts[-1])
                return True
            for di, dj in DIRS:
                ii, jj = i + di, j + dj
                if (ii - cx) ** 2 + (jj - cy) ** 2 > lim or not (0 <= ii < nx and 0 <= jj < self.ny):
                    continue
                cc = jj * nx + ii
                if cc in prev:
                    continue
                v = m[cc]
                if (v != FREE and v != net) or o[cc]:
                    continue
                if di and dj:
                    a, b = c + di, c + dj * nx
                    if (m[a] != FREE and m[a] != net) or o[a] or (m[b] != FREE and m[b] != net) or o[b]:
                        continue
                prev[cc] = c
                dq.append(cc)
        return False

    def ground(self, net, smd_pads, stitch=6.0, log=print):
        """GND without tracks: a via next to every SMD GND pad (short track to it) into the bottom GND zone, and
        stitching vias on a grid where there is room. The zones on both layers carry the ground."""
        G = self.G
        r = self.routes.setdefault(net, dict(segs=[], vias=[], halo=[set(), set()], line=[set(), set()], ok=True))
        missing = []
        for sh in smd_pads:
            done = False
            base = max(sh.w, sh.h) / 2
            for rad in [base + 0.45 + 0.125 * k for k in range(20)]:
                for a in range(0, 360, 15):
                    x = sh.cx + rad * math.cos(math.radians(a))
                    y = sh.cy + rad * math.sin(math.radians(a))
                    i, j = int(round(x / G)), int(round(y / G))
                    if not (2 <= i < self.nx - 2 and 2 <= j < self.ny - 2):
                        continue
                    if not self._via_free(net, i, j):
                        continue
                    if not self._line_free(net, 0, sh.cx, sh.cy, i * G, j * G):
                        continue
                    r["segs"].append((0, sh.cx, sh.cy, i * G, j * G))
                    r["vias"].append((i * G, j * G))
                    done = True
                    break
                if done:
                    break
            if not done:
                done = self._ground_path(net, sh, r)
            if not done:
                missing.append((round(sh.cx, 2), round(sh.cy, 2)))
        n_st = 0
        if stitch:
            k = int(round(stitch / G))
            for j in range(k // 2, self.ny, k):
                for i in range(k // 2, self.nx, k):
                    if self._via_free(net, i, j) and self._open_area(i, j, 1.0):
                        r["vias"].append((i * G, j * G))
                        n_st += 1
        log(f"  ground: {len(smd_pads) - len(missing)} pad vias, {n_st} stitching vias, no room at {missing}")
        self.collect()
        return missing

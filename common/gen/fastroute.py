"""Fast path for gen.negotiate: the A* search compiled with Numba, and the reroute of the nets in conflict spread
over worker processes.

Same algorithm and costs as NegotiatedRouter._search / route_net. The first iteration routes the nets one after
another (each sees the copper of the ones before it); later iterations reroute the conflicting nets in parallel,
all against the same snapshot of the congestion map (Jacobi-style PathFinder), and apply the results in net order,
so the outcome does not depend on scheduling. Static map, via map, congestion and history live in shared memory.
"""
import math
import os
import time
from multiprocessing import shared_memory, get_context

import numpy as np
from numba import njit

FREE = -1


# ---------------------------------------------------------------- the search kernel
@njit(cache=True)
def _via_ok(m0, m1, novia, nx, ny, net, i, j, r):
    if novia[j * nx + i]:
        return False
    for dj in range(-r, r + 1):
        jj = j + dj
        if jj < 0 or jj >= ny:
            return False
        for di in range(-r, r + 1):
            if di * di + dj * dj > r * r:
                continue
            ii = i + di
            if ii < 0 or ii >= nx:
                return False
            c = jj * nx + ii
            a = m0[c]
            b = m1[c]
            if (a != -1 and a != net) or (b != -1 and b != net):
                return False
    return True


@njit(cache=True)
def _push(hk, hg, hv, n, key, g, val):
    # binary heap on parallel arrays, keyed by (key, insertion order) for determinism
    i = n
    hk[i] = key
    hg[i] = g
    hv[i] = val
    while i > 0:
        p = (i - 1) >> 1
        if hk[p] <= hk[i]:
            break
        hk[p], hk[i] = hk[i], hk[p]
        hg[p], hg[i] = hg[i], hg[p]
        hv[p], hv[i] = hv[i], hv[p]
        i = p
    return n + 1


@njit(cache=True)
def _pop(hk, hg, hv, n):
    key, g, val = hk[0], hg[0], hv[0]
    n -= 1
    hk[0], hg[0], hv[0] = hk[n], hg[n], hv[n]
    i = 0
    while True:
        l = 2 * i + 1
        if l >= n:
            break
        r = l + 1
        c = l if (r >= n or hk[l] <= hk[r]) else r
        if hk[i] <= hk[c]:
            break
        hk[c], hk[i] = hk[i], hk[c]
        hg[c], hg[i] = hg[i], hg[c]
        hv[c], hv[i] = hv[i], hv[c]
        i = c
    return key, g, val, n


@njit(cache=True)
def astar(m0, m1, occ0, occ1, hist0, hist1, novia, nx, ny, net, src, goal, gep,
          i0, i1, j0, j1, tx, ty, lc0, lc1, hwt, pres, tcost, via_cost, rvc, max_nodes,
          best, bstamp, prev, epoch, hard):
    """One connection: from the src cells [(L*NC + c)] to any cell with goal[...] == gep. Returns the path as an
    array of states (L*NC + c)*9 + dir, empty if not found."""
    NC = nx * ny
    cap = 1 << 16
    hk = np.empty(cap, np.float64)
    hg = np.empty(cap, np.float64)
    hv = np.empty(cap, np.int64)
    n = 0
    di_ = np.array([1, 1, 0, -1, -1, -1, 0, 1])
    dj_ = np.array([0, 1, 1, 1, 0, -1, -1, -1])
    for k in range(src.shape[0]):
        lc_ = src[k]
        s = lc_ * 9 + 8
        best[s] = 0.0
        bstamp[s] = epoch
        prev[s] = -1
        c = lc_ % NC
        i = c % nx
        j = c // nx
        dx = abs(i - tx)
        dy = abs(j - ty)
        h = (dx + dy - 0.586 * min(dx, dy)) * hwt
        if n >= cap:
            cap *= 2
            hk = np.concatenate((hk, np.empty(cap // 2, np.float64)))
            hg = np.concatenate((hg, np.empty(cap // 2, np.float64)))
            hv = np.concatenate((hv, np.empty(cap // 2, np.int64)))
        n = _push(hk, hg, hv, n, h, 0.0, s)
    found = -1
    nodes = 0
    while n > 0:
        f_, g, s, n = _pop(hk, hg, hv, n)
        if bstamp[s] == epoch and g > best[s]:
            continue
        lc_ = s // 9
        if goal[lc_] == gep:
            found = s
            break
        nodes += 1
        if nodes > max_nodes:
            break
        d = s % 9
        L = lc_ // NC
        c = lc_ - L * NC
        j = c // nx
        i = c - j * nx
        if L == 0:
            m = m0
            oc = occ0
            hi = hist0
            lcl = lc0
        else:
            m = m1
            oc = occ1
            hi = hist1
            lcl = lc1
        base = L * NC
        for nd in range(8):
            t = tcost[d, nd]
            if t < 0:
                continue
            di = di_[nd]
            dj = dj_[nd]
            ii = i + di
            jj = j + dj
            if ii < i0 or ii > i1 or jj < j0 or jj > j1:
                continue
            cc = jj * nx + ii
            v = m[cc]
            if v != -1 and v != net:
                continue
            if di != 0 and dj != 0:
                a = m[c + di]
                b = m[c + dj * nx]
                if (a != -1 and a != net) or (b != -1 and b != net):
                    continue
                step = 1.4142 * lcl
            else:
                step = lcl
            if hard and oc[cc] > 0:          # polish pass: other nets' copper is a hard obstacle
                continue
            ng = g + step * (1.0 + hi[cc]) * (1.0 + pres * oc[cc]) + t
            ns = (base + cc) * 9 + nd
            if bstamp[ns] != epoch or ng < best[ns]:
                best[ns] = ng
                bstamp[ns] = epoch
                prev[ns] = s
                dx = abs(ii - tx)
                dy = abs(jj - ty)
                if n >= cap:
                    cap *= 2
                    hk = np.concatenate((hk, np.empty(cap // 2, np.float64)))
                    hg = np.concatenate((hg, np.empty(cap // 2, np.float64)))
                    hv = np.concatenate((hv, np.empty(cap // 2, np.int64)))
                n = _push(hk, hg, hv, n, ng + (dx + dy - 0.586 * min(dx, dy)) * hwt, ng, ns)
        # via
        ns = ((1 - L) * NC + c) * 9 + 8
        ng = g + via_cost
        if (bstamp[ns] != epoch or ng < best[ns]) and _via_ok(m0, m1, novia, nx, ny, net, i, j, rvc):
            k = 0
            for dj2 in (-rvc, 0, rvc):
                for di2 in (-rvc, 0, rvc):
                    cc = c + dj2 * nx + di2
                    if 0 <= cc < NC:
                        k += occ0[cc] + occ1[cc]
            ng += via_cost * pres * k
            if (not hard or k == 0) and (bstamp[ns] != epoch or ng < best[ns]):
                best[ns] = ng
                bstamp[ns] = epoch
                prev[ns] = s
                dx = abs(i - tx)
                dy = abs(j - ty)
                if n >= cap:
                    cap *= 2
                    hk = np.concatenate((hk, np.empty(cap // 2, np.float64)))
                    hg = np.concatenate((hg, np.empty(cap // 2, np.float64)))
                    hv = np.concatenate((hv, np.empty(cap // 2, np.int64)))
                n = _push(hk, hg, hv, n, ng + (dx + dy - 0.586 * min(dx, dy)) * hwt, ng, ns)
    if found < 0:
        return np.empty(0, np.int64)
    cnt = 0
    s = found
    while s >= 0:
        cnt += 1
        s = prev[s]
    out = np.empty(cnt, np.int64)
    s = found
    k = cnt - 1
    while s >= 0:
        out[k] = s
        k -= 1
        s = prev[s]
    return out


# ---------------------------------------------------------------- one net (used in-process and in the workers)
MEM_BUDGET = 4 * 2 ** 30          # bytes of routing buffers in all worker processes together


class Engine:
    """Arrays + scratch buffers for routing nets. `static` (2, N) int32, `novia` (N) uint8, `occ` (2, N) uint8,
    `hist` (2, N) float32 may be views of shared memory."""

    def __init__(self, static, novia, occ, hist, nx, ny, consts):
        self.static, self.novia, self.occ, self.hist = static, novia, occ, hist
        self.nx, self.ny = nx, ny
        self.c = consts
        N = nx * ny
        self.best = np.zeros(2 * N * 9, np.float64)
        self.bstamp = np.zeros(2 * N * 9, np.int32)
        self.prev = np.zeros(2 * N * 9, np.int64)
        self.goal = np.zeros(2 * N, np.int32)
        self.epoch = 0
        self.gep = 0

    def route_net(self, net, terms, lc, pres, hweight, occ, hard=0, tcost=None, hist=None):
        """terms: [(cells int64 array of L*NC + c, (x, y))]. Returns (paths [state arrays], ok).
        hard=1 (polish pass): other nets' copper may not be crossed at all; tcost / hist override the turn costs and
        the history map."""
        c = self.c
        tcost = c["tcost"] if tcost is None else tcost
        hist = self.hist if hist is None else hist
        nx, ny, G = self.nx, self.ny, c["G"]
        NC = nx * ny
        self.gep += 1
        gep = self.gep
        goal = self.goal
        goal[terms[0][0]] = gep
        connected = [True] + [False] * (len(terms) - 1)
        paths, ok = [], True
        hwt = hweight * min(lc)
        for k in range(1, len(terms)):
            cells, xy = terms[k]
            if len(cells) == 0:
                ok = False
                continue
            if np.any(goal[cells] == gep):
                goal[cells] = gep
                connected[k] = True
                continue
            tgt = min((terms[q][1] for q in range(k) if connected[q]), key=lambda p: math.dist(p, xy))
            tx, ty = tgt[0] / G, tgt[1] / G
            ci = cells % NC
            si, sj = ci % nx, ci // nx
            path = None
            for window, nodes in ((5.0, 120000), (30.0, 600000)):
                w = window / G
                i0, i1 = max(0, int(min(si.min(), tx) - w)), min(nx - 1, int(max(si.max(), tx) + w))
                j0, j1 = max(0, int(min(sj.min(), ty) - w)), min(ny - 1, int(max(sj.max(), ty) + w))
                self.epoch += 1
                p = astar(self.static[0], self.static[1], occ[0], occ[1], hist[0], hist[1], self.novia,
                          nx, ny, net, cells, goal, gep, i0, i1, j0, j1, tx, ty, float(lc[0]), float(lc[1]), hwt,
                          pres, tcost, c["via_cost"], c["rvc"], nodes, self.best, self.bstamp, self.prev,
                          self.epoch, hard)
                if len(p):
                    path = p
                    break
            if path is None:
                ok = False
                continue
            paths.append(path)
            goal[path // 9] = gep
            goal[cells] = gep
            connected[k] = True
        return paths, ok


def decode(path, nx, ny):
    """State array -> [(L, i, j, dir)] as gen.negotiate expects."""
    NC = nx * ny
    lc = path // 9
    L, c = lc // NC, lc % NC
    return list(zip(L.tolist(), (c % nx).tolist(), (c // nx).tolist(), (path % 9).tolist()))


# ---------------------------------------------------------------- worker processes
_W = {}


def _attach(names, shapes, nx, ny, consts):
    shms = [shared_memory.SharedMemory(name=n) for n in names]
    dt = [np.int32, np.uint8, np.uint8, np.float32]
    arrs = [np.ndarray(s, dtype=d, buffer=m.buf) for m, s, d in zip(shms, shapes, dt)]
    _W["shm"] = shms
    _W["eng"] = Engine(arrs[0], arrs[1], arrs[2], arrs[3], nx, ny, consts)


def _work(task):
    net, terms, lc, pres, hweight, own0, own1 = task
    eng = _W["eng"]
    occ = eng.occ.copy()
    occ[0][own0] -= 1
    occ[1][own1] -= 1
    paths, ok = eng.route_net(net, terms, lc, pres, hweight, occ)
    return net, paths, ok


# ---------------------------------------------------------------- negotiation driver
def negotiate(router, nets, iterations=60, log=print, lc_for=lambda net: None, workers=None):
    """Drop-in for NegotiatedRouter.negotiate (same result format, router.routes / segments / vias)."""
    r = router
    nx, ny, G = r.nx, r.ny, r.G
    N = nx * ny
    NC = N
    tcost = np.zeros((9, 8), np.float64)
    for d in range(8):
        for nd in range(8):
            turn = (nd - d) % 8
            turn = min(turn, 8 - turn)
            tcost[d, nd] = -1.0 if turn == 4 else r.turn_cost[turn]
    consts = dict(G=G, tcost=tcost, via_cost=float(r.via_cost), rvc=int(math.ceil((r.rv - r.hw) / G + 0.71)))
    # shared arrays
    specs = [((2, N), np.int32), ((N,), np.uint8), ((2, N), np.uint8), ((2, N), np.float32)]
    shms = [shared_memory.SharedMemory(create=True, size=int(np.prod(s)) * np.dtype(d).itemsize) for s, d in specs]
    static, novia, occ, hist = [np.ndarray(s, dtype=d, buffer=m.buf) for m, (s, d) in zip(shms, specs)]
    static[0] = np.asarray(r.map[0], np.int32)
    static[1] = np.asarray(r.map[1], np.int32)
    novia[:] = np.frombuffer(bytes(r.novia), np.uint8)
    occ[:] = 0
    hist[:] = 0
    eng = Engine(static, novia, occ, hist, nx, ny, consts)
    jobs = {net: [(np.array(sorted(L * NC + c for L, c in cells), np.int64), xy) for cells, xy in terms]
            for net, terms in nets}
    order = [net for net, _ in nets]
    lcs = {net: tuple(lc_for(net) or r.layer_cost) for net in order}
    halo_idx = {}

    def occupy(net, sign):
        h = halo_idx.get(net)
        if h is not None:
            occ[0][h[0]] += np.uint8(1) if sign > 0 else np.uint8(255)
            occ[1][h[1]] += np.uint8(1) if sign > 0 else np.uint8(255)

    def apply(net, paths, ok):
        old = r.routes.get(net)
        occupy(net, -1)
        if not ok and old and old["ok"]:
            occupy(net, +1)              # keep the previous complete routing of this net
            return
        segs, vias = r._paths_to_geometry([decode(p, nx, ny) for p in paths])
        halo, line = r._halo(segs, vias)
        r.routes[net] = dict(segs=segs, vias=vias, halo=halo, line=line, ok=ok)
        halo_idx[net] = (np.array(sorted(halo[0]), np.int64), np.array(sorted(halo[1]), np.int64))
        occupy(net, +1)

    def conflicts(net, mark=True):
        rt = r.routes.get(net)
        if not rt:
            return False
        bad = False
        for L in (0, 1):
            line = np.fromiter(rt["line"][L], np.int64) if rt["line"][L] else np.empty(0, np.int64)
            if not len(line):
                continue
            line = line[static[L][line] != net]
            own = np.isin(line, halo_idx[net][L])
            hit = line[occ[L][line].astype(np.int32) - own > 0]
            if len(hit):
                bad = True
                if mark:
                    hist[L][hit] += 1.0
        return bad

    def polish(passes=2):
        """Once there are no conflicts: reroute every net again with the other nets' copper as hard obstacles, an
        exact heuristic, stronger turn costs and no history - straighter tracks, fewer jogs. A net keeps its old
        routing when the clean search does not connect all its pads."""
        t1 = time.time()
        qcost = tcost.copy()
        for d in range(8):
            for nd in range(8):
                turn = min((nd - d) % 8, 8 - (nd - d) % 8)
                qcost[d, nd] = -1.0 if turn == 4 else (0.0, 6.0, 20.0, 80.0)[turn]
        zero = np.zeros((2, N), np.float32)
        kept = 0
        for _ in range(passes):
            for net in order:
                occupy(net, -1)
                paths, ok = eng.route_net(net, jobs[net], lcs[net], 0.0, 1.0, occ, hard=1, tcost=qcost, hist=zero)
                occupy(net, +1)
                if ok:
                    apply(net, paths, True)
                else:
                    kept += 1
        log(f"  polish: {passes} passes, {kept} net routings kept as they were ({time.time() - t1:.1f} s)")

    # one Engine per worker: best / prev / bstamp over 2 layers x 9 states (~20 bytes a state); a big board
    # (Main, 180 x 110 mm) needs ~0.45 GB per worker, so the count is capped by MEM_BUDGET too
    per_worker = 2 * N * 9 * 20
    workers = workers or max(1, min((os.cpu_count() or 2) - 2, MEM_BUDGET // per_worker))
    pool = None
    w0 = r.h_weight
    todo = set(order)
    try:
        for it in range(iterations):
            t0 = time.time()
            hweight = min(2.5, w0 + 0.15 * it)
            pres = r.pres
            batch = [n for n in order if n in todo]
            if it == 0 or len(batch) < 12:          # small batches: sequential converges better
                for net in batch:
                    h = halo_idx.get(net)
                    if h is not None:
                        occupy(net, -1)
                    paths, ok = eng.route_net(net, jobs[net], lcs[net], pres, hweight, occ)
                    if h is not None:
                        occupy(net, +1)
                    apply(net, paths, ok)
            else:
                if pool is None:
                    ctx = get_context("spawn")
                    pool = ctx.Pool(workers, initializer=_attach,
                                    initargs=([m.name for m in shms], [s for s, _ in specs], nx, ny, consts))
                empty = np.empty(0, np.int64)
                tasks = [(net, jobs[net], lcs[net], pres, hweight,
                          halo_idx.get(net, (empty, empty))[0], halo_idx.get(net, (empty, empty))[1]) for net in batch]
                for net, paths, ok in pool.map(_work, tasks, chunksize=1):
                    apply(net, paths, ok)
            # conflicts (vectorised)
            bad_nets = []
            for net in order:
                rt = r.routes.get(net)
                if not rt:
                    continue
                bad = False
                for L in (0, 1):
                    line = np.fromiter(rt["line"][L], np.int64) if rt["line"][L] else np.empty(0, np.int64)
                    if not len(line):
                        continue
                    line = line[static[L][line] != net]
                    own = np.isin(line, halo_idx[net][L])
                    other = occ[L][line].astype(np.int32) - own
                    hit = line[other > 0]
                    if len(hit):
                        bad = True
                        hist[L][hit] += 1.0
                if bad:
                    bad_nets.append(net)
            vbad = r.via_conflicts()
            failed = [net for net in order if not r.routes[net]["ok"]]
            log(f"  iteration {it + 1}: rerouted {len(batch)}, {len(bad_nets)} nets in conflict, {len(vbad)} via clashes, "
                f"{len(failed)} unrouted, pres={r.pres:.2f} ({time.time() - t0:.1f} s)")
            if not bad_nets and not vbad and not failed:
                polish()
                bad_nets = [net for net in order if conflicts(net, mark=False)]
                vbad = r.via_conflicts()
                failed = [net for net in order if not r.routes[net]["ok"]]
                break
            todo = set(bad_nets) | vbad | set(failed)
            r.pres = min(r.pres * 1.8, 1e6)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
        # hand the congestion map back to the router (ground() and the checks use it)
        r.occ = [occ[0].copy(), occ[1].copy()]
        r.hist = [hist[0].copy(), hist[1].copy()]
        for m in shms:
            m.close()
            m.unlink()
    r.collect()
    return sorted(set(bad_nets) | vbad | set(failed))

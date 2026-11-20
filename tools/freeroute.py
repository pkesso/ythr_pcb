"""Route a placed KiCad board with Freerouting, then stitch its ground zones and key every new UUID by geometry.

    python freeroute.py board.kicad_pcb                 # routes in place
    python freeroute.py board.kicad_pcb -o routed.kicad_pcb --stitch 10 --stitch-edge 5 --silk-keepouts

Steps (the board file is only written at the end, and only if every step worked):
  1. KiCad's Python: remove the old tracks and vias, except locked ones (laid by the generator, kicad_gen.locked_track:
     a bypass path), export a Specctra DSN. KiCad writes the locked ones as fixed wires; Freerouting routes around
     them and they come back unchanged.
  2. The edge: first every track `--prefer-edge` (3 mm) from it, by Freerouting's own edge clearance, within
     `--prefer-timeout`; if that leaves anything unrouted, keep-out strips along every Edge.Cuts edge, `--edge` wide
     (the project's clearance), written into the DSN: KiCad does not export the edge clearance, and Freerouting ignores
     keep-outs with holes. With --silk-keepouts, also along the board-level silkscreen outlines (the template's border
     and hole rings mark where the enclosure, standoffs and screws come close).
  3. Freerouting (kicad_cli.freerouting()), on every core but one, with telemetry and the window off. Routing is
     deterministic: the same input gives the same result on any number of threads.
  4. KiCad's Python: import the session, add stitching vias to every net with zones on both copper layers (on a
     `--stitch` grid, plus every `--stitch-edge` mm along the outline), unfill the zones, save.
  5. Tracks and vias get UUIDs keyed by their geometry, and KiCad resaves the file, so the result is byte-identical
     run after run. Before that, tracks Freerouting narrowed are widened back to their net's usual width (at least the
     project's minimum track width) wherever the clearance allows. Then copper_check.py checks the result with the
     project's clearance, edge clearance and minimum track width.

Zone nets (usually GND) are routed with tracks too, by default: left to the zones (--zones-as-planes, Freerouting's own
default), pieces of a zone cut off by tracks on both layers stayed unconnected where no via could reach them. The final
connectivity check is KiCad's DRC with the zones refilled. Freerouting may narrow a track at a pin (seen down to
0.125 mm): step 5 widens it where it fits, copper_check reports what is still under the minimum. Stitching vias keep
out of rule areas that forbid vias.

Net classes come through the DSN with their width, clearance and via; a class can be kept on one layer
(--class-layer), and the project's HiZ class is, on F.Cu (--hiz-layer). Every routed via is set to its net class's
size after the import.

Exit code: 0 when routed and clean, 1 when routed with problems or unrouted nets, 2 when a tool failed.
"""
import argparse
import collections
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
NAMESPACE = uuid.UUID("6c1f0b0e-3a51-4f7a-9d2e-5f0b4c2a9e11")


# ---------------------------------------------------------------- the part that runs in KiCad's Python
def kicad_export(src, bare, dsn):
    import pcbnew
    b = pcbnew.LoadBoard(src)
    for t in list(b.GetTracks()):
        if not t.IsLocked():  # locked tracks and vias are laid by the generator (a bypass path): they stay as they are
            b.Remove(t)
    pcbnew.SaveBoard(bare, b)
    if not pcbnew.ExportSpecctraDSN(b, dsn):
        sys.exit("DSN export failed")


def kicad_import(bare, ses, out, cfg):
    import pcbnew
    cfg = json.loads(cfg)
    b = pcbnew.LoadBoard(bare)
    if not pcbnew.ImportSpecctraSES(b, ses):
        sys.exit("session import failed")
    mm = pcbnew.FromMM
    # every routed via as its net class says: the import can take the diameter from Freerouting's via and the drill
    # from the class, which once gave a 0.6 mm via with a 0.5 mm hole (0.05 mm annular ring) on a net with 1.0/0.5 vias.
    # The class comes by name and its sizes from the project file: KiCad 10's GetEffectiveNetClass() returns an
    # unwrapped SwigPyObject, so asking it for the sizes failed and every via was skipped (0.6/0.4 vias on a 0.8/0.4
    # Power net, 0.1 mm annular ring). A net in several classes ("Audio,HiZ") takes the largest via among them.
    try:
        with open(os.path.splitext(bare)[0] + ".kicad_pro", encoding="utf-8") as fh:
            classes = {c["name"]: (c.get("via_diameter") or 0, c.get("via_drill") or 0)
                       for c in json.load(fh)["net_settings"]["classes"]}
    except (OSError, ValueError, KeyError, TypeError):
        classes = {}
    # A via is never grown here: Freerouting's fanout places vias of the default size whatever the class, and growing
    # one after routing can reach its neighbours. A via smaller than its class keeps its diameter and gets the drill
    # that leaves the class's annular ring (the Default via when it has that diameter).
    default = tuple(mm(v) for v in classes.get("Default", (0, 0)))
    smaller = 0
    for t in b.GetTracks():
        if t.GetClass() != "PCB_VIA":
            continue
        names = [n.strip() for n in t.GetNet().GetNetClassName().split(",") if n.strip() in classes] or ["Default"]
        if not any(n in classes for n in names):
            continue
        d, dr = (mm(v) for v in max(classes[n] for n in names if n in classes))
        if not (d and dr):
            continue
        try:
            w = t.GetWidth(pcbnew.F_Cu)
        except TypeError:
            w = t.GetWidth()
        if w < d:
            smaller += 1
            d, dr = default if default[0] == w and default[1] else (w, max(mm(0.2), w - (d - dr)))
        if t.GetDrill() != dr or w != d:
            t.SetDrill(dr)
            try:
                t.SetWidth(d)
            except TypeError:
                t.SetWidth(pcbnew.F_Cu, d)
    if smaller:
        print(f"note: {smaller} vias smaller than their net class (Freerouting's fanout uses the default via): kept "
              "their diameter, drill set for the class's annular ring")
    zones = list(b.Zones())
    nets = sorted({z.GetNetname() for z in zones if not z.GetIsRuleArea() and z.GetNetname()})
    pcbnew.ZONE_FILLER(b).Fill(zones)
    outline = pcbnew.SHAPE_POLY_SET()
    b.GetBoardPolygonOutlines(outline, False)
    r = mm(cfg["via"] / 2)
    keep = mm(cfg["via"] / 2 + 0.5)  # from pads and other vias: no via in a pad, hole-to-hole room
    obstacles = [p.GetBoundingBox() for fp in b.GetFootprints() for p in fp.Pads()]
    obstacles += [t.GetBoundingBox() for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
    no_vias = []  # rule areas that forbid vias (keep-outs from the template's border, around holes...), grown by r
    for z in zones:
        if z.GetIsRuleArea() and z.GetDoNotAllowVias() and (z.IsOnLayer(pcbnew.F_Cu) or z.IsOnLayer(pcbnew.B_Cu)):
            area = pcbnew.SHAPE_POLY_SET(z.Outline())
            area.Inflate(r + mm(0.05), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, mm(0.005))
            no_vias.append(area)

    def free(x, y):
        v = pcbnew.VECTOR2I(x, y)
        return (all(not (bb.GetLeft() - keep < x < bb.GetRight() + keep and bb.GetTop() - keep < y < bb.GetBottom() + keep)
                    for bb in obstacles) and not any(a.Contains(v) for a in no_vias))

    # candidate points: a grid, and a row along the outline
    bb = outline.BBox()
    step = mm(cfg["stitch"])
    pts = [(x, y) for x in range(bb.GetLeft() + step // 2, bb.GetRight(), step)
           for y in range(bb.GetTop() + step // 2, bb.GetBottom(), step)] if cfg["stitch"] > 0 else []
    def canonical(chain):
        """The chain's points from its lowest-leftmost one, counter-clockwise: KiCad's order varies between runs."""
        c = [(chain.CPoint(k).x, chain.CPoint(k).y) for k in range(chain.PointCount())]
        if sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(c, c[1:] + c[:1])) < 0:
            c.reverse()
        k = c.index(min(c))
        return [pcbnew.VECTOR2I(x, y) for x, y in c[k:] + c[:k]]

    if cfg["stitch_edge"] > 0:
        inset = mm(cfg["edge"]) + r + mm(0.5)
        chains = [canonical(outline.Outline(i)) for i in range(outline.OutlineCount())]
        chains += [canonical(outline.Hole(i, h)) for i in range(outline.OutlineCount()) for h in range(outline.HoleCount(i))]
        for c in sorted(chains, key=lambda c: (c[0].x, c[0].y)):
            if True:
                carry = 0.0
                for p, q in zip(c, c[1:] + c[:1]):
                    L = math.hypot(q.x - p.x, q.y - p.y)
                    if L < 1:
                        continue
                    nx, ny = -(q.y - p.y) / L, (q.x - p.x) / L
                    d = carry
                    while d < L:
                        x, y = p.x + (q.x - p.x) * d / L, p.y + (q.y - p.y) * d / L
                        for s in (1, -1):
                            cx, cy = int(x + s * nx * inset), int(y + s * ny * inset)
                            if outline.Contains(pcbnew.VECTOR2I(cx, cy)):
                                pts.append((cx, cy))
                                break
                        d += mm(cfg["stitch_edge"])
                    carry = d - L
    added, missed = 0, []
    for net in nets:
        layers = {}
        for z in zones:
            if z.GetNetname() != net:
                continue
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                if z.IsOnLayer(layer):
                    fill = pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(layer))
                    fill.Inflate(-r, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, mm(0.005))
                    layers.setdefault(layer, []).append(fill)
        if len(layers) < 2:
            continue
        info = b.FindNet(net)
        placed = []

        def ok(x, y):
            v = pcbnew.VECTOR2I(x, y)
            return (all(any(f.Contains(v) for f in fills) for fills in layers.values()) and free(x, y)
                    and not any(math.hypot(x - px, y - py) < 2 * keep for px, py in placed))

        def add(x, y):
            via = pcbnew.PCB_VIA(b)
            via.SetPosition(pcbnew.VECTOR2I(x, y))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetDrill(mm(cfg["drill"]))
            try:
                via.SetWidth(mm(cfg["via"]))
            except TypeError:
                via.SetWidth(pcbnew.F_Cu, mm(cfg["via"]))
            via.SetNet(info)
            b.Add(via)
            placed.append((x, y))

        for x, y in sorted(set(pts)):
            if ok(x, y):
                add(x, y)
        # every piece of the fill that the grid missed (a small island) gets a via of its own, where both layers
        # have fill: otherwise it may hold a pad cut off from the rest of the net
        net_vias = [(t.GetPosition().x, t.GetPosition().y) for t in b.GetTracks()
                    if t.GetClass() == "PCB_VIA" and t.GetNetname() == net]
        fine = mm(0.5)
        pieces = []
        for layer in sorted(layers):
            for fill in layers[layer]:
                for i in range(fill.OutlineCount()):
                    piece = pcbnew.SHAPE_POLY_SET()
                    piece.AddOutline(fill.Outline(i))
                    pb = piece.BBox()
                    pieces.append(((layer, pb.GetLeft(), pb.GetTop(), pb.GetRight(), pb.GetBottom()), piece))
        net_pads = [p.GetPosition() for fp in b.GetFootprints() for p in fp.Pads() if p.GetNetname() == net]
        for (layer, x0, y0, x1, y1), piece in sorted(pieces, key=lambda p: p[0]):  # fill order varies between runs
            if any(piece.Contains(pcbnew.VECTOR2I(x, y)) for x, y in net_vias + placed):
                continue
            hit = None
            for x in range(x0, x1 + 1, fine):
                hit = next((y for y in range(y0, y1 + 1, fine)
                            if piece.Contains(pcbnew.VECTOR2I(x, y)) and ok(x, y)), None)
                if hit is not None:
                    add(x, hit)
                    break
            if hit is None and any(piece.Contains(p) for p in net_pads):
                missed.append(f"{net} on {b.GetLayerName(layer)} near ({pcbnew.ToMM(x0):.1f}, {pcbnew.ToMM(y0):.1f})")
        added += len(placed)
    for z in zones:
        z.UnFill()
    pcbnew.SaveBoard(out, b)
    print(f"stitching vias: {added} ({', '.join(nets) or 'no zone nets'})")
    for m in missed:
        print(f"note: zone piece with pads and no stitching via (joined by tracks, or not: see DRC): {m}")


# ---------------------------------------------------------------- the part that runs in the project's Python
def kicad_python():
    sys.path.insert(0, HERE)
    import kicad_cli
    exe = os.path.join(kicad_cli.kicad_bin(), "python.exe")
    return exe if os.path.isfile(exe) else sys.executable  # elsewhere KiCad's pcbnew module is on the system Python


KICAD_SIDE_TIMEOUT = 900  # s: DSN export, or session import with zone fill and stitching


def run_kicad(*args):
    import kicad_cli
    try:
        p = kicad_cli.run_process([kicad_python(), os.path.abspath(__file__), "--kicad-side", *args], KICAD_SIDE_TIMEOUT,
                                  encoding="utf-8", errors="replace")
    except TimeoutError as e:
        raise RuntimeError(f"KiCad's Python hung: {e}")
    noise = re.compile(r"swig/python detected a memory leak|Adding duplicate image handler")
    out = "\n".join(l for l in (p.stdout + p.stderr).splitlines() if l.strip() and not noise.search(l))
    if p.returncode:
        raise RuntimeError(f"KiCad's Python failed ({p.returncode}):\n{out[-2000:]}")
    return out


def strips(loops, width, closed=True):
    """Keep-out rectangles 2*width wide centred on every edge of the polylines (DSN um, y up)."""
    out = []
    for loop in loops:
        for (x1, y1), (x2, y2) in zip(loop, loop[1:] + (loop[:1] if closed else [])):
            L = math.hypot(x2 - x1, y2 - y1)
            if L < 1e-6:
                continue
            nx, ny = -(y2 - y1) / L * width, (x2 - x1) / L * width
            pts = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
            out.append('    (keepout "edge" (polygon signal 0  ' +
                       "  ".join(f"{x * 1000:.1f} {-y * 1000:.1f}" for x, y in pts) + "))")
    return out


def silk_lines(board):
    """Board-level silkscreen lines, arcs, circles and rectangles, as [(points, closed)]."""
    sys.path.insert(0, HERE)
    from kicad_gen import sexpr, kids, kid
    import copper_check
    with open(board, encoding="utf-8") as fh:
        t = sexpr(fh.read())
    out = []
    for g in t:
        if not (isinstance(g, list) and g[0] in ("gr_line", "gr_arc", "gr_circle", "gr_rect") and kid(g, "layer")
                and kid(g, "layer")[1].endswith("SilkS")):
            continue
        p = {k: (float(kid(g, k)[1]), float(kid(g, k)[2])) for k in ("start", "mid", "end", "center") if kid(g, k)}
        if g[0] == "gr_line":
            out.append(([p["start"], p["end"]], False))
        elif g[0] == "gr_arc":
            out.append((copper_check.arc_points(p["start"], p["mid"], p["end"]), False))
        elif g[0] == "gr_rect":
            (x0, y0), (x1, y1) = p["start"], p["end"]
            out.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True))
        else:
            c, r = p["center"], math.dist(p["center"], p["end"])
            out.append(([(c[0] + r * math.cos(k * math.tau / 48), c[1] + r * math.sin(k * math.tau / 48))
                         for k in range(48)], True))
    return out


def without_blocks(text, head):
    """text without every S-expression that starts with `head`."""
    out, i = [], 0
    while (j := text.find(head, i)) >= 0:
        depth, k = 0, j
        while True:
            depth += {"(": 1, ")": -1}.get(text[k], 0)
            k += 1
            if depth == 0:
                break
        out.append(text[i:j])
        i = k
    return "".join(out) + text[i:]


def project_rules(board):
    """(clearance, edge clearance, minimum track width, via diameter, via drill) from the project file, with
    JLCPCB-safe defaults."""
    pro = os.path.splitext(board)[0] + ".kicad_pro"
    clearance, edge, width, via, drill = 0.2, 0.3, 0.15, 0.6, 0.3
    if os.path.isfile(pro):
        with open(pro, encoding="utf-8") as fh:
            p = json.load(fh)
        rules = p.get("board", {}).get("design_settings", {}).get("rules", {})
        edge = max(rules.get("min_copper_edge_clearance", edge), 0.2)
        width = rules.get("min_track_width", width)
        default = next((c for c in p.get("net_settings", {}).get("classes", []) if c.get("name") == "Default"), {})
        clearance = default.get("clearance", clearance)
        via, drill = default.get("via_diameter", via), default.get("via_drill", drill)
    return clearance, edge, width, via, drill


_TRACK = re.compile(r"\((?:segment|arc)\s(?:[^()]|\([^()]*\))*\)")


def widen(path, clearance, edge, min_width):
    """Widen the tracks Freerouting narrowed (it necks a track down between pins, seen at 0.125 mm), each back to its
    net's usual width, or at least to min_width, wherever that breaks no clearance. Changes the file in place.
    Returns (widened, still under min_width)."""
    sys.path.insert(0, HERE)
    import copper_check
    from kicad_gen import f
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    tracks = []  # (start, end, width, net)
    for m in _TRACK.finditer(text):
        w, net = re.search(r"\(width ([\d.]+)\)", m.group(0)), re.search(r"\(net ([^)]*)\)", m.group(0))
        if w:
            tracks.append((m.start(), m.end(), float(w[1]), net[1] if net else ""))
    usual = {}
    for net in {t[3] for t in tracks}:
        c = collections.Counter(t[2] for t in tracks if t[3] == net)
        top = max(c.values())
        usual[net] = max(w for w, n in c.items() if n == top)  # the most common width, the wider on a tie
    narrow = [i for i, t in enumerate(tracks) if t[2] < max(usual[t[3]], min_width) - 1e-6]
    if not narrow:
        return 0, 0
    rules = copper_check.Rules(clearance=clearance, edge=edge, track_width=0)
    probe = path + ".widen.kicad_pcb"

    def build(chosen):
        out, i = [], 0
        for k in sorted(chosen):
            s, e, _, _ = tracks[k]
            out += [text[i:s], re.sub(r"\(width [\d.]+\)", f"(width {f(chosen[k])})", text[s:e], count=1)]
            i = e
        return "".join(out) + text[i:]

    def problems(t):
        with open(probe, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(t)
        return len(copper_check.check(copper_check.from_kicad_pcb(probe), rules))

    try:
        base = problems(text)
        chosen = {}
        for target in (lambda k: max(usual[tracks[k][3]], min_width), lambda k: min_width):
            todo = {k: target(k) for k in narrow if k not in chosen and target(k) > tracks[k][2] + 1e-6}
            if todo and problems(build({**chosen, **todo})) <= base:
                chosen.update(todo)
                continue
            for k, w in todo.items():  # one at a time: keep each that fits
                if problems(build({**chosen, k: w})) <= base:
                    chosen[k] = w
    finally:
        if os.path.exists(probe):
            os.remove(probe)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build(chosen))
    left = sum(1 for k in narrow if chosen.get(k, tracks[k][2]) < min_width - 1e-6)
    return len(chosen), left


def rekey(path, name):
    """Give tracks, arcs and vias UUIDs keyed by their geometry; KiCad's resave then sorts them deterministically."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    seen = {}

    def item(m):
        body = m.group(0)
        kind = m.group(1)
        nums = re.findall(r"\((?:start|mid|end|at|width|size|drill) ([-\d.]+)(?: ([-\d.]+))?\)", body)
        layer = re.search(r'\(layers? "([^"]+)"', body)
        key = "/".join([name, kind, layer.group(1) if layer else ""] + [" ".join(n) for n in nums])
        seen[key] = seen.get(key, 0) + 1
        new = uuid.uuid5(NAMESPACE, f"{key}/{seen[key]}")
        return re.sub(r'\(uuid "[0-9a-fA-F-]+"\)', f'(uuid "{new}")', body, count=1)

    text = re.sub(r"\((segment|arc|via)\s(?:[^()]|\([^()]*(?:\([^()]*\))*[^()]*\))*?\(uuid \"[0-9a-fA-F-]+\"\)\s*\)", item, text)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return sum(seen.values())


def class_blocks(dsn, cls):
    """Starts of the DSN class blocks of net class `cls`, last first. KiCad writes a net in several classes under
    their composite name, `(class Audio,HiZ /IN ...)`: a search for `(class HiZ` missed it, so a HiZ input that was
    also in Audio got routed with a via."""
    return [m.start() for m in re.finditer(r"\(class (\S+)\s", dsn) if cls in m.group(1).split(",")][::-1]


_DSN_NET = re.compile(r'"[^"]*"|[^\s()]+')


def restrict_layers(dsn, layers, fixed=None, skipped=None):
    """The DSN with the net classes in `layers` ({class: layer}) kept on that one layer: `(use_layer ...)` in the
    class's circuit, which KiCad's export does not write. A class on one layer gets no vias.

    `fixed` ({net: {layers}}): the nets with locked tracks. One locked on another layer (an input laid on B.Cu by the
    generator) is left out of the restriction, in a copy of its class block without it; otherwise Freerouting would
    see its own fixed track as breaking the class's rule. Their names go into the list `skipped`, if given."""
    fixed = fixed or {}
    for cls, layer in layers.items():
        starts = class_blocks(dsn, cls)
        if not starts:
            raise ValueError(f"--class-layer: no net class {cls} in the DSN (is it in the project, with nets?)")
        for start in starts:
            c = dsn.index("(circuit", start)
            end = dsn.index("\n    )", start) + len("\n    )")
            head = _DSN_NET.findall(dsn[start + len("(class "):c])
            name, nets = head[0], head[1:]
            free = [n for n in nets if not (fixed.get(n.strip('"'), set()) - {layer})]
            keep = [n for n in nets if n not in free]
            if skipped is not None:
                skipped += [n.strip('"') for n in keep]
            if not free:
                continue  # every net of the block is fixed off this layer: nothing to restrict
            block = dsn[start:end]
            body = block[block.index("(circuit"):]
            restricted = f"(class {name} {' '.join(free)}\n      " + body.replace(
                "(circuit", f"(circuit\n        (use_layer {layer})", 1)
            loose = f"\n    (class {name}_fixed {' '.join(keep)}\n      {body}" if keep else ""
            dsn = dsn[:start] + restricted + loose + dsn[end:]
    return dsn


def locked_layers(board):
    """{net: {copper layers}} of the locked tracks of a board file (the bare board before routing)."""
    import kicad_gen
    tree = kicad_gen.sexpr(open(board, encoding="utf-8").read())
    out = {}
    for s in kicad_gen.kids(tree, "segment") + kicad_gen.kids(tree, "arc"):
        lock, layer, net = kicad_gen.kid(s, "locked"), kicad_gen.kid(s, "layer"), kicad_gen.kid(s, "net")
        if lock and lock[1:2] == ["yes"] and layer and net:
            out.setdefault(net[-1], set()).add(layer[1])
    return out


AUDIO_NOISE_GAP = 1.0  # mm: jlcpcb_rules.NOISE_GAP, DRC's audio_from_noise rule (a test keeps the two equal)


def class_clearance(dsn, cls, mm):
    """The DSN with net class `cls` kept `mm` from everything, for the router only (the project's own clearance, which
    DRC checks, is unchanged). Freerouting has no clearance between two classes: it reads `(class_class ...)` and
    ignores it (tried 2026-11-20), so the Noisy class's own clearance stands in for Audio-to-Noisy. Returns the DSN
    and whether the class was there."""
    starts = class_blocks(dsn, cls)
    for start in starts:
        end = dsn.index("\n    )", start)  # the class block's end
        block = re.sub(r"\(clearance \d+(?:\.\d+)?\)", f"(clearance {round(mm * 1000)})", dsn[start:end], count=1)
        dsn = dsn[:start] + block + dsn[end:]
    return dsn, bool(starts)


PREFER_EDGE = 3.0  # mm: pcb-design.md, "Sensitive tracks" (audio about 3 mm from the edge), for every track at once
_FR_PREFIX = re.compile(r"^\S+ \S+ (INFO|WARN|ERROR)\s+(?:\[[^\]]*\]\s*)?")
_FR_NOISE = ("logging.file.location", "GUI is disabled")  # warnings Freerouting gives on every headless start


def progress_line(line):
    """Freerouting's log line as shown while it runs: its passes, stage ends, the result and real warnings, without
    the timestamp, job id and board hashes; None for the rest (the whole log is still kept)."""
    m = _FR_PREFIX.match(line)
    if not m:
        return None
    level, text = m[1], line[m.end():]
    text = re.sub(r" on board '[0-9a-f]+'", "", text)
    if level == "ERROR" or (level == "WARN" and not any(n in text for n in _FR_NOISE)):
        return f"{level.lower()}: {text}"
    if re.search(r"pass #\d+.*completed|stage completed|finished with state", text):
        text = re.split(r", using | \(\d+ SMD pins? still|, finished at", text)[0]
        return text.replace("completed:.", "completed:").rstrip(" ,")[:140]
    return None


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--kicad-side":
        {"export": kicad_export, "import": kicad_import}[sys.argv[2]](*sys.argv[3:])
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="placed .kicad_pcb (its tracks and vias are replaced)")
    ap.add_argument("-o", "--output", help="routed board to write (default: the input)")
    ap.add_argument("--edge", type=float, help="copper to board edge, mm (default: the project's, at least 0.2)")
    ap.add_argument("--silk-keepouts", action="store_true", help="no tracks across board-level silkscreen outlines")
    ap.add_argument("--prefer-edge", type=float, default=PREFER_EDGE,
                    help=f"try first with every track this far from the edge, mm (default {PREFER_EDGE:g}: keeps audio "
                         "off the edge on a roomy board); if that leaves anything unrouted, the project's edge "
                         "clearance is used. 0: only the project's")
    ap.add_argument("--prefer-timeout", type=float, default=120,
                    help="seconds the --prefer-edge try may take before the project's clearance is tried (default 120)")
    ap.add_argument("--no-edge-strips", action="store_true",
                    help="no keep-out strips along the edges (experiments: the edge clearance is then Freerouting's "
                         "own, --setting router.copper_to_edge_clearance_um=...)")
    ap.add_argument("--class-layer", action="append", default=[], metavar="CLASS=LAYER",
                    help="keep a net class on one copper layer, with no vias (e.g. HiZ=F.Cu); repeatable")
    ap.add_argument("--hiz-layer", default="F.Cu",
                    help="layer of the project's HiZ net class, if it has one (default F.Cu; none: no restriction)")
    ap.add_argument("--audio-noise-gap", type=float, default=0.0,
                    help=f"keep the Noisy net class this far from everything while routing, mm, when the project has "
                         f"Audio and Noisy (off by default: Freerouting has no clearance between two classes, and a "
                         f"Noisy net on fine-pitch pins, like a VSON eFuse's input, then stays unrouted; "
                         f"{AUDIO_NOISE_GAP:g} matches DRC's audio_from_noise)")
    ap.add_argument("--zones-as-planes", dest="route_zone_nets", action="store_false",
                    help="leave zone nets (GND) to the zones instead of also routing them with tracks; islands the "
                         "stitching cannot reach then stay unconnected")
    ap.add_argument("--stitch", type=float, default=10.0, help="stitching via grid, mm (0: none; default 10)")
    ap.add_argument("--stitch-edge", type=float, default=5.0, help="stitching vias along the outline every N mm (0: none)")
    ap.add_argument("--passes", type=int, default=100, help="Freerouting's maximum autorouter passes")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--fanout-ms", type=int, default=10000,
                    help="Freerouting's time budget per SMD pin in its fanout pass, ms (its default, 10000; raising it "
                         "did not make results under parallel load repeatable)")
    ap.add_argument("--keep", action="store_true", help="keep the DSN, session and log for inspection")
    ap.add_argument("--setting", action="append", default=[], metavar="KEY=VALUE",
                    help="one of Freerouting's own settings, passed as --KEY=VALUE (e.g. "
                         "router.automatic_neckdown=false); repeatable")
    ap.add_argument("--timeout", type=float, default=900, help="seconds Freerouting may run (default 900)")
    ap.add_argument("--quiet", action="store_true",
                    help="don't show Freerouting's own output as it runs (it is still in the log); for callers that "
                         "run many boards at once, like place_search.py")
    sys.path.insert(0, HERE)
    import kicad_cli
    import copper_check
    kicad_cli.add_priority_argument(ap)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected
    kicad_cli.lower_priority(args.priority)

    board = os.path.abspath(args.board)
    out = os.path.abspath(args.output or board)
    name = os.path.splitext(os.path.basename(out))[0]
    clearance, edge, min_width, via, drill = project_rules(board)
    edge = args.edge or edge
    try:  # before the routing, not after it
        kicad_cli.writable(out)
    except OSError as e:
        print(f"cannot write {out}: {e}")
        return 2
    work = tempfile.mkdtemp(prefix="fr")
    try:
        kicad_cli.ensure_ready()
        fr = kicad_cli.freerouting()
        bare, dsn, ses = (os.path.join(work, n) for n in ("bare.kicad_pcb", "board.dsn", "board.ses"))
        run_kicad("export", board, bare, dsn)
        loops = copper_check.from_kicad_pcb(bare).outline
        silk = []
        if args.silk_keepouts:
            for line, closed in silk_lines(bare):
                silk += strips([line], 0.05, closed)
        with open(dsn, encoding="utf-8") as fh:
            text = fh.read()
        # KiCad writes the file's path into the first line, and Freerouting's result depends on it: a fixed name
        text = re.sub(r"\A\(pcb [^\n]*", "(pcb board.dsn", text, count=1)
        if args.route_zone_nets:
            text = without_blocks(text, "(plane ")
        layers = dict(c.split("=", 1) for c in args.class_layer)
        if "HiZ" not in layers and args.hiz_layer != "none" and class_blocks(text, "HiZ"):
            layers["HiZ"] = args.hiz_layer  # jlcpcb_rules.py's class of high-impedance nodes: one layer, no vias
        skipped = []
        text = restrict_layers(text, layers, locked_layers(bare) if layers else {}, skipped)
        if layers:
            print("one layer: " + ", ".join(f"{c} on {l}" for c, l in layers.items()))
            if skipped:
                print(f"  not restricted, locked on another layer: {', '.join(sorted(set(skipped)))}")
        if args.audio_noise_gap > 0 and re.search(r"\(class Audio[,\s]", text):
            text, found = class_clearance(text, "Noisy", args.audio_noise_gap)
            if found:
                print(f"class clearance: Noisy {args.audio_noise_gap:g} mm from everything (router only), for Audio")
        k = text.rfind(")", 0, text.index("  (placement"))
        # (label, keep-outs, extra Freerouting settings, edge clearance): every track `--prefer-edge` mm from the edge
        # first, by Freerouting's own edge clearance (it holds its vias off too, unlike strips); if that leaves
        # anything unrouted, the project's edge clearance by keep-out strips (routing.md)
        attempts = []
        if args.prefer_edge > edge and not args.no_edge_strips:
            native = f"router.copper_to_edge_clearance_um={round(args.prefer_edge * 1000)}"
            attempts.append((f"{args.prefer_edge:g} mm from the edge", silk, [native], args.prefer_edge))
        attempts.append((f"{edge:g} mm from the edge", silk + ([] if args.no_edge_strips else strips(loops, edge)), [],
                         edge))
        for i, (label, extra, settings, used_edge) in enumerate(attempts):
            last = i == len(attempts) - 1
            with open(dsn, "w", encoding="utf-8") as fh:
                fh.write(text[:k] + "\n" + "\n".join(extra) + "\n  " + text[k:])
            if os.path.exists(ses):
                os.remove(ses)
            cmd = [fr, "-de", dsn, "-do", ses, "-mt", str(args.threads),
                   f"--router.autorouter.max_passes={args.passes}",
                   f"--router.fanout.max_milliseconds_per_pin={args.fanout_ms}",
                   "-l", "en", "--gui.enabled=false", "--feature_flags.multi_threading=true",
                   "--profile.allow_telemetry=false", "--usage_and_diagnostic_data.disable_analytics=true"]
            cmd += [f"--{s}" for s in settings + args.setting]
            # the preferred try gets a share of the time: on a board where it cannot work, Freerouting would spend all
            # its passes on it before the try that can (a normal board routes in under a minute)
            limit = args.timeout if last else min(args.timeout, args.prefer_timeout)
            try:
                print(f"Freerouting ({label}): routing on {args.threads} threads, at most {limit:g} s")
                # cwd: Freerouting writes its own log to en/freerouting.log under the current directory
                p = kicad_cli.run_process(cmd, limit, encoding="utf-8", errors="replace", cwd=work,
                                          echo=None if args.quiet else "  freerouting: ", echo_filter=progress_line)
            except TimeoutError as e:
                if not last:
                    print(f"Freerouting ({label}): not done in {limit:g} s; routing again {attempts[-1][0]}")
                    continue
                print(f"Freerouting stopped: {e}")
                args.keep = True
                return 2
            with open(os.path.join(work, "freerouting.log"), "w", encoding="utf-8") as fh:
                fh.write(p.stdout + p.stderr)
            # "1 violation" in the singular: a pattern for the plural alone missed it, and the run looked unsummarised
            score = re.findall(r"final score: ([\d.]+) \((\d+) unrouted and (\d+) violations?\)", p.stdout + p.stderr)
            failed = p.returncode or not os.path.isfile(ses)
            clean = not failed and score and score[0][1] == "0" and score[0][2] == "0"
            if not last and not clean:
                why = "failed" if failed else (f"{score[0][1]} unrouted, {score[0][2]} violations" if score else
                                               "no summary in its log")
                print(f"Freerouting ({label}): {why}; routing again {attempts[-1][0]}")
                continue
            if failed:
                print(f"Freerouting failed ({p.returncode}); its log: {os.path.join(work, 'freerouting.log')}")
                args.keep = True
                return 2
            break
        print(f"Freerouting: tracks {used_edge:g} mm from the edge ({len(extra)} keep-out strips); "
              + (f"{score[0][1]} unrouted, {score[0][2]} violations" if score else "no summary in its log"))
        tmp_out = os.path.join(work, name + ".kicad_pcb")
        cfg = json.dumps(dict(stitch=args.stitch, stitch_edge=args.stitch_edge, edge=edge, via=via, drill=drill))
        print(run_kicad("import", bare, ses, tmp_out, cfg))
        widened, left = widen(tmp_out, clearance, edge, min_width)
        if widened or left:
            print(f"narrowed tracks: {widened} widened back" + (f", {left} still under {min_width:g} mm" if left else ""))
        n = rekey(tmp_out, name)
        kicad_cli.upgrade("pcb", tmp_out)
        shutil.copyfile(tmp_out, out)
        found = copper_check.check(copper_check.from_kicad_pcb(out),
                                   copper_check.Rules(clearance=clearance, edge=edge, track_width=min_width))
        print(f"{n} tracks and vias keyed; copper check (clearance {clearance:g}, edge {edge:g}, "
              f"track width {min_width:g}): "
              f"{len(found)} problems")
        for f in found[:10]:
            print(f"    {f['kind']} {', '.join(map(str, f['items']))}: {f['actual']} at {f['at']}")
        import kicad_checks  # connectivity through the refilled zones is KiCad's to judge
        for ext in (".kicad_pro", ".kicad_dru"):  # DRC takes its rules from the files next to the board: the input's
            src = os.path.splitext(board)[0] + ext   # (an output under another name has none, and got KiCad's defaults)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(work, name + ext))
        drc = [v for v in kicad_checks.drc(tmp_out, parity=False)
               if v["severity"] == "error" or v["type"] in ("unconnected_items", "track_dangling", "via_dangling")]
        print(f"KiCad DRC (zones refilled): {len(drc)} errors and loose items")
        for (_, typ), vs in kicad_checks.summarise(drc).items():
            print(f"    {typ} x{len(vs)}: {kicad_checks.describe(vs[0])[:150]}")
        unrouted = int(score[0][1]) if score else 0
        return 1 if found or unrouted or drc else 0
    except (kicad_cli.KicadError, RuntimeError, OSError) as e:
        print(e)
        args.keep = True
        return 2
    finally:
        if args.keep:
            print(f"work files: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

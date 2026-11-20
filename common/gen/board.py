"""Board writer: library footprints copied whole, routing by gen.router, GND zones on both layers, silkscreen
labels placed automatically, BOM/CPL for JLCPCB. Coordinates: board-local mm with the origin at the top-left
corner, Y down; written to the file with an offset (OX, OY)."""
import csv
import json
import math
import os
import re
import sys

from .libs import PRETTY, LIB, PCB
from .router import Shape
from .negotiate import NegotiatedRouter
sys.path.insert(0, os.path.join(PCB, "tools"))
from kicad_gen import Uuids, load_footprint, board_footprint_items, to_board, kids, kid, q, f  # noqa: E402

OX, OY = 100.0, 100.0
TRACK, VIA, VIA_DRILL, CLEAR = 0.25, 0.6, 0.3, 0.2
EDGE = 0.35                  # copper to board edge (the router's default for the bounding rectangle)
RIM_GAP = 0.1                # copper to the template's rim line (Design.rim): nothing goes beyond it


def fx(v):
    return f(v + OX)


def fy(v):
    return f(v + OY)


def stackup(mask, silk=None):
    """2-layer 1.6 mm stackup with the solder mask colour; silkscreen white, black on a white board (JLCPCB), or
    `silk` when the design sets it (the colour is an ordering option: Gerbers carry no colour)."""
    silk = silk or ("Black" if mask == "White" else "White")
    return (f'(stackup (layer "F.SilkS" (type "Top Silk Screen") (color {q(silk)})) '
            '(layer "F.Paste" (type "Top Solder Paste")) '
            f'(layer "F.Mask" (type "Top Solder Mask") (color {q(mask)}) (thickness 0.01)) '
            '(layer "F.Cu" (type "copper") (thickness 0.035)) '
            '(layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)) '
            '(layer "B.Cu" (type "copper") (thickness 0.035)) '
            f'(layer "B.Mask" (type "Bottom Solder Mask") (color {q(mask)}) (thickness 0.01)) '
            '(layer "B.Paste" (type "Bottom Solder Paste")) '
            f'(layer "B.SilkS" (type "Bottom Silk Screen") (color {q(silk)})) '
            '(copper_finish "HAL lead-free") (dielectric_constraints no))')


# ---------------------------------------------------------------- footprint geometry
def _pad_geom(item):
    """(number, kind, shape, x, y, angle, w, h, layers, drill, anchor) of a library pad: centre in footprint
    coordinates, size in the pad's own frame. A custom pad is described by the bounding box of its primitives
    (copper extent, for clearances) and its anchor rect `anchor` = (x, y, w, h), which is surely copper (where
    tracks may end)."""
    num = item[1].strip('"')
    kind, shape = item[2], item[3]
    at = kid(item, "at")
    x, y = float(at[1]), float(at[2])
    a = float(at[3]) if len(at) > 3 else 0.0
    size = kid(item, "size")
    w, h = float(size[1]), float(size[2])
    anchor = (x, y, w, h)
    if shape == "custom":
        prim = kid(item, "primitives")
        xs, ys = [-w / 2, w / 2], [-h / 2, h / 2]
        for g in prim[1:] if prim else []:
            if g[0] == "gr_poly":
                for xy in kids(kid(g, "pts"), "xy"):
                    xs.append(float(xy[1]))
                    ys.append(float(xy[2]))
            elif g[0] == "gr_circle":
                c, e = kid(g, "center"), kid(g, "end")
                r = math.dist((float(c[1]), float(c[2])), (float(e[1]), float(e[2])))
                xs += [float(c[1]) - r, float(c[1]) + r]
                ys += [float(c[2]) - r, float(c[2]) + r]
        # anchor-relative bbox -> treat as a rect around its centre
        ox_, oy_ = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        # the primitives are in the pad's own frame: turn the offset with the pad's orientation
        ra = math.radians(a)
        c_, s_ = round(math.cos(ra), 9), round(math.sin(ra), 9)
        x, y = x + ox_ * c_ + oy_ * s_, y - ox_ * s_ + oy_ * c_
    layers = [t.strip('"') for t in kid(item, "layers")[1:]]
    drill = kid(item, "drill")
    return num, kind, shape, x, y, a, w, h, layers, drill, anchor


class BoardPad:
    def __init__(self, ref, num, net, kind, shape, layers, sh, target=None):
        self.ref, self.num, self.net, self.kind, self.shape, self.layers, self.sh = ref, num, net, kind, shape, layers, sh
        self.target = target or sh     # where a track may end (the anchor rect for custom pads)


def _shape_kind(shape, w, h):
    if shape == "circle" or (shape == "oval" and abs(w - h) < 1e-6):
        return "circle"
    return "oval" if shape == "oval" else "rect"


# ---------------------------------------------------------------- text extents
def text_box(s, size, thick=0.15):
    w = len(s) * size * (0.95 if size >= 2 else 0.84) + thick
    h = size * 1.15 + thick
    return w, h


class Board:
    def __init__(self, design, out_dir, sch_uuid, netlist=None):
        """netlist: {(ref, pin): KiCad net name} from the schematic (kicad-cli netlist export); pins that are not
        connected get KiCad's 'unconnected-(...)' nets, so the board matches the schematic exactly."""
        self.d = design
        self.netlist = netlist or {}
        self.out = out_dir
        self.sch_uuid = sch_uuid
        self.fps = {}
        for p in design.parts.values():
            if p.cat["footprint"] not in self.fps:
                self.fps[p.cat["footprint"]] = load_footprint(os.path.join(PRETTY, p.cat["footprint"] + ".kicad_mod"))
        nets = sorted(design.nets())
        self.net_id = {n: i + 1 for i, n in enumerate(nets)}
        # nets of unconnected pins (one pad each)
        self.nc_net = {}
        for (ref, pin), name in sorted(self.netlist.items()):
            if name.startswith("unconnected-"):
                self.nc_net[(ref, pin)] = name
        for name in sorted(set(self.nc_net.values())):
            self.net_id[name] = len(self.net_id) + 1
        self.pads = []
        self.holes = []
        self.silk_segs = []          # footprint silkscreen strokes in board coords, per side
        self.courtyards = {}
        for p in design.parts.values():
            self._collect(p)
        if design.outline and design.rim:
            for a, b in design.outline.frame_segments():
                self.silk_segs += [("F", a, b), ("B", a, b)]

    def kname(self, net):
        return net if net.startswith("unconnected-") else "/" + net

    def _collect(self, p):
        fp = self.fps[p.cat["footprint"]]
        bottom = p.side == "B"
        for item in fp.raw[2:]:
            if not isinstance(item, list):
                continue
            if item[0] == "pad":
                num, kind, shape, x, y, a, w, h, layers, drill, anchor = _pad_geom(item)
                bx, by = to_board(x, y, p.x, p.y, p.rot, p.side)
                ang = ((-a if bottom else a) + p.rot) % 180
                ax, ay, aw, ah = anchor
                if abs(ang - 90) < 1e-6:
                    w, h = h, w
                    aw, ah = ah, aw
                tx, ty = to_board(ax, ay, p.x, p.y, p.rot, p.side)
                if kind == "np_thru_hole":
                    self.holes.append((bx, by, float(drill[1])))
                    continue
                if any(L in ("*.Cu",) for L in layers):
                    lay = [0, 1]
                else:
                    lay = [0 if (L == "F.Cu") != bottom else 1 for L in layers if L in ("F.Cu", "B.Cu")]
                net = p.pins.get(num)
                target = Shape("rect", tx, ty, aw, ah) if shape == "custom" else None
                self.pads.append(BoardPad(p.ref, num, net, kind, shape, lay,
                                          Shape(_shape_kind(shape, w, h), bx, by, w, h), target))
            elif item[0] in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
                layer = kid(item, "layer")[1].strip('"')
                pts = self._item_points(item)
                if layer.endswith("SilkS"):
                    side = "F" if (layer.startswith("F")) != bottom else "B"
                    bpts = [to_board(x, y, p.x, p.y, p.rot, p.side) for x, y in pts]
                    for a_, b_ in zip(bpts, bpts[1:]):
                        self.silk_segs.append((side, a_, b_))
                if layer.endswith("CrtYd"):
                    bpts = [to_board(x, y, p.x, p.y, p.rot, p.side) for x, y in pts]
                    self.courtyards.setdefault(p.ref, []).extend(bpts)

    @staticmethod
    def _item_points(item):
        if item[0] == "fp_line":
            s, e = kid(item, "start"), kid(item, "end")
            return [(float(s[1]), float(s[2])), (float(e[1]), float(e[2]))]
        if item[0] == "fp_rect":
            s, e = kid(item, "start"), kid(item, "end")
            x0, y0, x1, y1 = float(s[1]), float(s[2]), float(e[1]), float(e[2])
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        if item[0] == "fp_poly":
            pts = [(float(xy[1]), float(xy[2])) for xy in kids(kid(item, "pts"), "xy")]
            return pts + pts[:1]
        if item[0] == "fp_circle":
            c, e = kid(item, "center"), kid(item, "end")
            cx, cy = float(c[1]), float(c[2])
            r = math.dist((cx, cy), (float(e[1]), float(e[2])))
            return [(cx + r * math.cos(t * math.pi / 8), cy + r * math.sin(t * math.pi / 8)) for t in range(17)]
        s, m, e = kid(item, "start"), kid(item, "mid"), kid(item, "end")
        return [(float(n[1]), float(n[2])) for n in (s, m, e)]

    def courtyard_bbox(self, ref):
        pts = self.courtyards.get(ref)
        if not pts:
            p = self.d.parts[ref]
            return p.x - 1, p.y - 1, p.x + 1, p.y + 1
        return min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts)

    # ---------------------------------------------------------------- routing
    def route(self, iterations=60, log=print):
        """Negotiated-congestion routing of every net (GND too, preferring the bottom layer; the GND zones fill
        around it). When conflicts remain, routing is repeated with other via costs (no single value suits every
        board); the first clean result wins, else the one with the fewest problem nets. Returns the nets that could
        not be routed without conflicts. A big board may ask for more negotiation iterations
        (Design.route_iterations)."""
        iterations = getattr(self.d, "route_iterations", iterations)
        best = None
        for via_cost in (28, 20, 36):
            r, problems = self._route_once(via_cost, iterations, log)
            if best is None or len(problems) < len(best[1]):
                best = (r, problems)
            if not problems:
                break
            log(f"  conflicts left with via cost {via_cost}: retrying")
        r, problems = best
        inv = {i: n for n, i in self.net_id.items()}
        self.router, self.failed = r, [inv[i] for i in problems]
        return self.failed

    def _route_once(self, via_cost, iterations, log):
        d = self.d
        r = NegotiatedRouter(d.width, d.height, grid=0.125, track=TRACK, clearance=CLEAR, via=VIA, via_drill=VIA_DRILL,
                             via_cost=via_cost)
        for bp in self.pads:
            r.add_pad(self.net_id.get(bp.net) if bp.net else None, bp.layers, bp.sh, smd=bp.kind == "smd")
        for x, y, dd in self.holes:
            r.add_hole(x, y, dd)
        for x0, y0, x1, y1, layers in d.keepouts:
            r.keepout(x0, y0, x1, y1, layers)
        for x, y, rr in d.keep_circles:
            r.keepout_circle(x, y, rr)
        if d.outline:
            # outside the template's outline (cut-outs, narrow tabs) and its holes: the router's edge clearance
            for x0, y0, x1, y1 in d.outline.keepouts(EDGE + TRACK / 2):
                r.keepout(x0, y0, x1, y1, (0, 1))
            for hx, hy, hr in d.outline.holes:
                r.keepout_circle(hx, hy, hr + EDGE)
            if d.rim:
                # nothing beyond the rim (it marks where the enclosure, standoffs and screws come close): track
                # and via copper RIM_GAP inside its line; the rule areas in write() make DRC check it
                for x0, y0, x1, y1 in d.outline.keepouts(RIM_GAP + TRACK / 2, rim=True):
                    r.keepout(x0, y0, x1, y1, (0, 1))
        nets = d.nets()
        gnd = self.net_id.get("GND")
        if gnd:
            r.reserve_ground(gnd, [(bp.sh, (d.parts[bp.ref].x, d.parts[bp.ref].y)) for bp in self.pads
                                   if bp.net == "GND" and bp.kind == "smd"], log=log)

        def span(n):
            pts = [(bp.sh.cx, bp.sh.cy) for bp in self.pads if bp.net == n]
            return (max(x for x, _ in pts) - min(x for x, _ in pts)) + (max(y for _, y in pts) - min(y for _, y in pts))
        order = sorted((n for n in nets if n not in ("GND", "+3V3")), key=lambda n: (span(n), n)) + ["+3V3"]
        jobs = []
        for n in order:
            if n not in nets:
                continue
            nid = self.net_id[n]
            pads = [bp for bp in self.pads if bp.net == n]
            if len(pads) < 2:
                continue
            seq = [pads[0]]
            rest = pads[1:]
            while rest:
                k = min(range(len(rest)), key=lambda i: (min(math.dist((rest[i].sh.cx, rest[i].sh.cy), (o.sh.cx, o.sh.cy))
                                                             for o in seq), i))
                seq.append(rest.pop(k))
            jobs.append((nid, [(r.pad_cells(bp.target, bp.layers, nid), (bp.sh.cx, bp.sh.cy)) for bp in seq]))
        if gnd:
            # GND tree: the reserved pad vias and all through-hole GND pads joined by tracks (bottom layer
            # preferred), so ground does not depend on the zones staying in one piece
            G = r.G
            terms = []
            for _, x, y in r.fixed_vias:
                c = int(round(y / G)) * r.nx + int(round(x / G))
                terms.append(([(0, c), (1, c)], (x, y)))
            for bp in self.pads:
                if bp.net == "GND" and bp.kind != "smd":
                    terms.append((r.pad_cells(bp.target, bp.layers, gnd), (bp.sh.cx, bp.sh.cy)))
            if len(terms) > 1:
                seq = [terms[0]]
                rest = terms[1:]
                while rest:
                    k = min(range(len(rest)), key=lambda i: (min(math.dist(rest[i][1], o[1]) for o in seq), i))
                    seq.append(rest.pop(k))
                jobs.append((gnd, seq))
        problems = r.negotiate(jobs, iterations=iterations, log=log,
                               lc_for=lambda nid: (3.0, 1.0) if nid == gnd else None)
        if gnd:
            r.ground(gnd, [], log=log)          # stitching vias only: the pad vias are reserved above
        return r, problems

    # ---------------------------------------------------------------- silkscreen
    def place_labels(self):
        """Reference designators (and functional labels) placed around each part's courtyard, clear of pads,
        other silkscreen, holes and the board edge. Deterministic order."""
        obstacles = {"F": [], "B": []}
        for bp in self.pads:
            x0, y0, x1, y1 = bp.sh.bbox()
            for L in bp.layers:
                obstacles["F" if L == 0 else "B"].append((x0 - 0.25, y0 - 0.25, x1 + 0.25, y1 + 0.25))
        for x, y, dd in self.holes:
            for s in "FB":
                obstacles[s].append((x - dd / 2 - 0.3, y - dd / 2 - 0.3, x + dd / 2 + 0.3, y + dd / 2 + 0.3))
        for side, (ax, ay), (bx, by) in self.silk_segs:
            obstacles[side].append((min(ax, bx) - 0.2, min(ay, by) - 0.2, max(ax, bx) + 0.2, max(ay, by) + 0.2))
        for t in self.d.texts:
            s, x, y, size = t[0], t[1], t[2], t[3]
            side = "B" if t[4].startswith("B") else "F"
            w, h = text_box(s, size, max(0.15, size * 0.15))
            obstacles[side].append((x - w / 2 - 0.2, y - h / 2 - 0.2, x + w / 2 + 0.2, y + h / 2 + 0.2))
        self.labels = []         # (ref, text, x, y, side, size, kind)
        W, H = self.d.width, self.d.height

        def free(box, side):
            x0, y0, x1, y1 = box
            if x0 < 0.5 or y0 < 0.5 or x1 > W - 0.5 or y1 > H - 0.5:
                return False
            if self.d.outline and not self.d.outline.inside(box, 0.5):
                return False
            if self.d.outline and self.d.rim and not self.d.outline.inside(box, 0.3, rim=True):
                return False
            for o in obstacles[side]:
                if x0 < o[2] and x1 > o[0] and y0 < o[3] and y1 > o[1]:
                    return False
            return True

        def put(ref, text, side, size, prefer, kind):
            w, h = text_box(text, size)
            cx0, cy0, cx1, cy1 = self.courtyard_bbox(ref)
            mx, my = (cx0 + cx1) / 2, (cy0 + cy1) / 2
            cands = []
            for name in prefer:
                if name == "above":
                    cands += [(mx + dx, cy0 - h / 2 - 0.1) for dx in (0, -1, 1, -2, 2)]
                elif name == "below":
                    cands += [(mx + dx, cy1 + h / 2 + 0.1) for dx in (0, -1, 1, -2, 2)]
                elif name == "left":
                    cands += [(cx0 - w / 2 - 0.1, my + dy) for dy in (0, -0.8, 0.8, -1.6, 1.6)]
                elif name == "right":
                    cands += [(cx1 + w / 2 + 0.1, my + dy) for dy in (0, -0.8, 0.8, -1.6, 1.6)]
                elif name == "inside":
                    cands += [(mx, my)]
                elif isinstance(name, tuple):
                    cands += [(mx + name[0], my + name[1])]
            # wider search rings
            for k in range(1, 8):
                for ang in range(0, 360, 30):
                    cands.append((mx + math.cos(math.radians(ang)) * (max(cx1 - cx0, w) / 2 + k * 0.8),
                                  my + math.sin(math.radians(ang)) * (max(cy1 - cy0, h) / 2 + k * 0.8)))
            for x, y in cands:
                box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
                if free(box, side):
                    obstacles[side].append((box[0] - 0.3, box[1] - 0.3, box[2] + 0.3, box[3] + 0.3))
                    self.labels.append((ref, text, x, y, side, size, kind))
                    return True
            self.labels.append((ref, text, None, None, side, size, kind))
            return False

        def refkey(r):
            m = re.match(r"([A-Z]+)(\d+)", r)
            return (m.group(1), int(m.group(2)))
        # functional labels first (they matter more), then references
        parts = sorted(self.d.parts.values(), key=lambda p: refkey(p.ref))
        for p in parts:
            if p.silk:
                prefer = [p.silk_at] if p.silk_at else ["above", "below", "left", "right"]
                put(p.ref, p.silk, p.side, 1.0, prefer, "func")
        for p in parts:
            if p.ref_on_fab:
                continue                 # written to the Fab layer by write()
            put(p.ref, p.ref, p.side, 1.0, ["above", "below", "left", "right", "inside"], "ref")
        return [l for l in self.labels if l[2] is None]

    # ---------------------------------------------------------------- writing
    def write(self, path):
        d = self.d
        ids = Uuids(Uuids(self.sch_uuid)("board"))
        sch_ids = Uuids(self.sch_uuid)
        out = ['(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")',
               '(general (thickness 1.6) (legacy_teardrops no))', '(paper "A4")',
               f'(title_block (title {q(d.title)}) (rev {q(d.rev)}) (company "YTHR") '
               f'(comment 1 "Generated by pcb/{d.name}/gen_{d.name}.py - do not edit"))',
               '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (9 "F.Adhes" user "F.Adhesive") (11 "B.Adhes" user "B.Adhesive") '
               '(13 "F.Paste" user) (15 "B.Paste" user) (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen") '
               '(1 "F.Mask" user) (3 "B.Mask" user) (17 "Dwgs.User" user "User.Drawings") (19 "Cmts.User" user "User.Comments") '
               '(25 "Edge.Cuts" user) (27 "Margin" user) (31 "F.CrtYd" user "F.Courtyard") (29 "B.CrtYd" user "B.Courtyard") '
               '(35 "F.Fab" user) (33 "B.Fab" user))',
               f'(setup {stackup(getattr(d, "mask", "Green"), getattr(d, "silk", None))} '
               '(pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no) '
               '(pcbplotparams (layerselection 0x00010fc_ffffffff) (outputformat 1) (outputdirectory "gerbers/")))',
               '(net 0 "")']
        for n, i in sorted(self.net_id.items(), key=lambda kv: kv[1]):
            out.append(f'(net {i} {q(self.kname(n))})')
        # labels by ref
        lab = {}
        for ref, text, x, y, side, size, kind in self.labels:
            lab.setdefault(ref, []).append((text, x, y, side, size, kind))
        for ref in sorted(d.parts, key=lambda r: (re.match(r"[A-Z]+", r).group(0), int(re.search(r"\d+", r).group(0)))):
            p = d.parts[ref]
            fp = self.fps[p.cat["footprint"]]
            layer = "B.Cu" if p.side == "B" else "F.Cu"
            sl = "B.SilkS" if p.side == "B" else "F.SilkS"
            fab = "B.Fab" if p.side == "B" else "F.Fab"
            mir = " (justify mirror)" if p.side == "B" else ""
            o = [f'(footprint {q(LIB + ":" + p.cat["footprint"])} (layer {q(layer)}) (uuid {ids.q(ref)}) '
                 f'(at {fx(p.x)} {fy(p.y)}{" " + f(p.rot) if p.rot else ""})']
            refl = [l for l in lab.get(ref, []) if l[5] == "ref"]

            def local(x, y):
                # board position -> footprint-local (unrotated) text position
                dx, dy = x - p.x, y - p.y
                a = math.radians(p.rot)
                return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
            if refl and refl[0][1] is not None:
                lx, ly = local(refl[0][1], refl[0][2])
                o.append(f'(property "Reference" {q(ref)} (at {f(lx)} {f(ly)} 0) (layer {q(sl)}) (uuid {ids.q(ref, "Reference")}) '
                         f'(effects (font (size 1 1) (thickness 0.15)){mir}))')
            else:
                o.append(f'(property "Reference" {q(ref)} (at 0 0 0) (layer {q(fab)}) (uuid {ids.q(ref, "Reference")}) '
                         f'(effects (font (size 1 1) (thickness 0.15)){mir}))')
            o.append(f'(property "Value" {q(p.val)} (at 0 0 0) (layer {q(fab)}) (hide yes) (uuid {ids.q(ref, "Value")}) '
                     f'(effects (font (size 1 1) (thickness 0.15)){mir}))')
            fields = [("Footprint", LIB + ":" + p.cat["footprint"]), ("Datasheet", p.cat["datasheet"]), ("Description", "")]
            if p.cat.get("lcsc"):
                fields.append(("LCSC", p.cat["lcsc"]))
            for k, v in fields:
                o.append(f'(property {q(k)} {q(v)} (at 0 0 0) (layer {q(fab)}) (hide yes) (uuid {ids.q(ref, k)}) '
                         f'(effects (font (size 1.27 1.27)){mir}))')
            o.append(f'(path {q("/" + sch_ids("symbol", ref, 1))}) (sheetname "Root") (sheetfile {q(d.name + ".kicad_sch")})')

            smd_pads = {bp.num for bp in self.pads if bp.ref == ref and bp.kind == "smd"}

            def pad_extra(num, p=p, smd_pads=smd_pads):
                net = p.pins.get(num) or self.nc_net.get((p.ref, num))
                if not net:
                    return ()
                ex = [["net", str(self.net_id[net]), q(self.kname(net))]]
                if net == "GND":
                    ex.append(["zone_connect", "2"])     # solid: fine-pitch / crowded GND pads get no second spoke
                return ex
            items = board_footprint_items(fp, ref, p.side, p.rot, ids, pad_extra)
            if not p.cat.get("bom", True):
                # not fitted by the assembler (e.g. hand-soldered on Main): as the symbol says, out of the BOM
                items = [re.sub(r"^\(attr ((?:(?!exclude_from_bom)[^)])*)\)$", r"(attr \1 exclude_from_bom)", s)
                         for s in items]
            o += items
            o.append(")")
            out.append(" ".join(o))
        # functional labels and board texts
        for ref, text, x, y, side, size, kind in self.labels:
            if kind != "func" or x is None:
                continue
            layer = "B.SilkS" if side == "B" else "F.SilkS"
            mir = " (justify mirror)" if side == "B" else ""
            out.append(f'(gr_text {q(text)} (at {fx(x)} {fy(y)} 0) (layer {q(layer)}) (uuid {ids.q("label", ref, text)}) '
                       f'(effects (font (size {f(size)} {f(size)}) (thickness 0.15)){mir}))')
        seen = set()
        for t in d.texts:
            s, x, y, size, layer = t[:5]
            th = max(0.15, round(size * 0.15, 2))
            mir = " (justify mirror)" if layer.startswith("B") else ""
            key = ("text", layer, s) if ("text", layer, s) not in seen else ("text", layer, s, f(x), f(y))
            if len(t) > 5:
                key = ("text", t[5])                    # keyed by role: a new revision only changes the string
            seen.add(key)
            out.append(f'(gr_text {q(s)} (at {fx(x)} {fy(y)} 0) (layer {q(layer)}) (uuid {ids.q(*key)}) '
                       f'(effects (font (size {f(size)} {f(size)}) (thickness {f(th)}) (bold {"yes" if size >= 2 else "no"})){mir}))')
        # outline
        W, H = d.width, d.height
        if d.outline:
            # the template's outline as it is; its silkscreen as a helper drawing (Dwgs.User): it would run
            # through pads and labels on the copper side
            def ex(v):              # the template's figures exactly (up to 6 decimals; f() keeps 6 digits only)
                return f"{v + OX:.6f}".rstrip("0").rstrip(".")

            def ey(v):
                return f"{v + OY:.6f}".rstrip("0").rstrip(".")
            layers = [("Edge.Cuts", d.outline.items, "edge"), ("Dwgs.User", d.outline.drawings, "tpl")]
            if d.rim:               # the template's rim along the edge, on the silkscreen of both sides
                layers += [("F.SilkS", d.outline.frame, "rimF"), ("B.SilkS", d.outline.frame, "rimB")]
            width = {"edge": "0.05", "tpl": "0.1", "rimF": "0.15", "rimB": "0.15"}     # 0.15: JLCPCB's silk line
            for layer, items, tag in layers:
                for k, (kind, ps) in enumerate(items):
                    st = f'(stroke (width {width[tag]}) (type default))'
                    uid = ids.q(tag, d.outline.name, k)
                    if kind == "line":
                        out.append(f'(gr_line (start {ex(ps[0][0])} {ey(ps[0][1])}) (end {ex(ps[1][0])} {ey(ps[1][1])}) '
                                   f'{st} (layer {q(layer)}) (uuid {uid}))')
                    elif kind == "arc":
                        out.append(f'(gr_arc (start {ex(ps[0][0])} {ey(ps[0][1])}) (mid {ex(ps[1][0])} {ey(ps[1][1])}) '
                                   f'(end {ex(ps[2][0])} {ey(ps[2][1])}) {st} (layer {q(layer)}) (uuid {uid}))')
                    else:
                        out.append(f'(gr_circle (center {ex(ps[0][0])} {ey(ps[0][1])}) (end {ex(ps[1][0])} {ey(ps[1][1])}) '
                                   f'{st} (fill no) (layer {q(layer)}) (uuid {uid}))')
        else:
            out.append(f'(gr_rect (start {fx(0)} {fy(0)}) (end {fx(W)} {fy(H)}) (stroke (width 0.05) (type default)) (fill no) '
                       f'(layer "Edge.Cuts") (uuid {ids.q("edge")}))')
        # tracks and vias
        r = self.router
        self._prune(r)
        lname = ["F.Cu", "B.Cu"]
        for net, L, x1, y1, x2, y2 in r.segments:
            out.append(f'(segment (start {fx(x1)} {fy(y1)}) (end {fx(x2)} {fy(y2)}) (width {f(TRACK)}) (layer {q(lname[L])}) '
                       f'(net {net}) (uuid {ids.q("segment", lname[L], f(x1), f(y1), f(x2), f(y2))}))')
        for net, x, y in r.vias:
            out.append(f'(via (at {fx(x)} {fy(y)}) (size {f(VIA)}) (drill {f(VIA_DRILL)}) (layers "F.Cu" "B.Cu") (net {net}) '
                       f'(uuid {ids.q("via", f(x), f(y))}))')
        # GND zones
        g = self.net_id.get("GND")
        if g:
            for layer in ("F.Cu", "B.Cu"):
                pts = " ".join(f"(xy {fx(x)} {fy(y)})" for x, y in ((0, 0), (W, 0), (W, H), (0, H)))
                out.append(f'(zone (net {g}) (net_name {q(self.kname("GND"))}) (layer {q(layer)}) (uuid {ids.q("zone", layer)}) '
                           f'(hatch edge 0.5) (connect_pads (clearance 0.25)) (min_thickness 0.25) (filled_areas_thickness no) '
                           f'(fill (thermal_gap 0.3) (thermal_bridge_width 0.4) (island_removal_mode 0)) (polygon (pts {pts})))')
        if d.outline and d.rim:
            # beyond the rim: keep-out rule areas on both copper layers, so DRC checks it and the GND zones stop at
            # the rim; pads and footprints stay allowed (a connector reaches through it to the edge)
            for k, poly in enumerate(d.outline.beyond_rim()):
                pts = " ".join(f"(xy {fx(x)} {fy(y)})" for x, y in poly)
                out.append(f'(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid {ids.q("rim_keepout", k)}) '
                           f'(name "beyond rim") (hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25) '
                           f'(filled_areas_thickness no) (keepout (tracks not_allowed) (vias not_allowed) '
                           f'(pads allowed) (copperpour not_allowed) (footprints allowed)) '
                           f'(fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts {pts})))')
        out.append(")")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")

    def _prune(self, r):
        """Drop track stubs whose end touches nothing (no pad, via or other track of the net); repeat until none."""
        from .router import seg_dist
        pads = {}
        for bp in self.pads:
            if bp.net:
                pads.setdefault(self.net_id[bp.net], []).append(bp)
        # exact duplicates (e.g. a reserved GND stub retraced by the GND tree), either direction, zero length
        seen, segs = set(), []
        for net, L, x1, y1, x2, y2 in r.segments:
            k = (net, L) + tuple(sorted(((x1, y1), (x2, y2))))
            if (x1, y1) == (x2, y2) or k in seen:
                continue
            seen.add(k)
            segs.append((net, L, x1, y1, x2, y2))
        # T-junctions: KiCad connects tracks only at their ends, so split a track where another one ends on it
        changed = True
        while changed:
            changed = False
            ends = {(n, L, x, y) for n, L, a, b, c, d in segs for x, y in ((a, b), (c, d))}
            for k, (net, L, x1, y1, x2, y2) in enumerate(segs):
                for n, LL, x, y in sorted(ends):
                    if n == net and LL == L and (x, y) not in ((x1, y1), (x2, y2)) and                             seg_dist(x, y, x1, y1, x2, y2) < 1e-6:
                        segs[k:k + 1] = [(net, L, x1, y1, x, y), (net, L, x, y, x2, y2)]
                        changed = True
                        break
                if changed:
                    break
        while True:
            drop = None
            for k, (net, L, x1, y1, x2, y2) in enumerate(segs):
                for (x, y) in ((x1, y1), (x2, y2)):
                    if any(bp.sh.dist(x, y) <= 0 and L in bp.layers for bp in pads.get(net, ())):
                        continue
                    if any(n == net and math.dist((x, y), (vx, vy)) < 1e-6 for n, vx, vy in r.vias):
                        continue
                    if any(j != k and n == net and LL == L and seg_dist(x, y, a, b, c, d) < 1e-6
                           for j, (n, LL, a, b, c, d) in enumerate(segs)):
                        continue
                    drop = k
                    break
                if drop is not None:
                    break
            if drop is None:
                break
            segs.pop(drop)
        # the T-junction split can cut two overlapping tracks into identical pieces: keep one of each
        seen, out = set(), []
        for net, L, x1, y1, x2, y2 in segs:
            k = (net, L) + tuple(sorted(((f(x1), f(y1)), (f(x2), f(y2)))))
            if k not in seen:
                seen.add(k)
                out.append((net, L, x1, y1, x2, y2))
        r.segments = out

    # ---------------------------------------------------------------- assembly files
    def assembly(self, bom_path, cpl_path):
        groups = {}
        for p in self.d.parts.values():
            if not p.cat.get("lcsc"):
                continue
            groups.setdefault((p.val, p.cat["footprint"], p.cat["lcsc"]), []).append(p.ref)

        def refkey(r):
            m = re.match(r"([A-Z]+)(\d+)", r)
            return (m.group(1), int(m.group(2)))
        with open(bom_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
            for (val, fp, lcsc), refs in sorted(groups.items(), key=lambda kv: refkey(min(kv[1], key=refkey))):
                w.writerow([val, ",".join(sorted(refs, key=refkey)), fp, lcsc])
        with open(cpl_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
            for p in sorted(self.d.parts.values(), key=lambda p: refkey(p.ref)):
                if not p.cat.get("lcsc"):
                    continue
                w.writerow([p.ref, f"{p.x + OX:.4f}mm", f"{-(p.y + OY):.4f}mm", "Bottom" if p.side == "B" else "Top",
                            f"{p.rot % 360:g}"])


def project_files(out_dir, name, root_uuid, jlc_rules=False):
    """.kicad_pro (only when missing), fp-lib-table and sym-lib-table. jlc_rules: JLCPCB's manufacturing limits put
    into the project on every build (pcb/tools/jlcpcb_rules.py; only raises minimums, so it is idempotent)."""
    pro = os.path.join(out_dir, name + ".kicad_pro")
    if not os.path.exists(pro):
        data = {
            "board": {"design_settings": {
                "defaults": {"board_outline_line_width": 0.05, "copper_line_width": 0.2, "silk_line_width": 0.12,
                             "silk_text_size_h": 1.0, "silk_text_size_v": 1.0, "silk_text_thickness": 0.15},
                "rules": {"min_clearance": 0.15, "min_track_width": 0.15, "min_via_diameter": 0.5,
                          "min_via_annular_width": 0.1, "min_through_hole_diameter": 0.3, "min_hole_to_hole": 0.25,
                          "min_hole_clearance": 0.25, "min_copper_edge_clearance": 0.3, "min_silk_clearance": 0.0,
                          "min_text_height": 0.8, "min_text_thickness": 0.08, "solder_mask_to_copper_clearance": 0.0},
                "track_widths": [0.0, 0.25, 0.4], "via_dimensions": [{"diameter": 0.0, "drill": 0.0},
                                                                     {"diameter": 0.6, "drill": 0.3}]}},
            "meta": {"filename": name + ".kicad_pro", "version": 1},
            "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6,
                                          "via_drill": 0.3, "microvia_diameter": 0.3, "microvia_drill": 0.1,
                                          "diff_pair_gap": 0.25, "diff_pair_width": 0.2, "diff_pair_via_gap": 0.25,
                                          "wire_width": 6, "bus_width": 12, "line_style": 0, "schematic_color": "rgba(0, 0, 0, 0.000)",
                                          "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647}],
                             "meta": {"version": 3}},
            "sheets": [[root_uuid, "Root"]],
        }
        with open(pro, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    with open(os.path.join(out_dir, "fp-lib-table"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'(fp_lib_table\n  (version 7)\n  (lib (name "{LIB}")(type "KiCad")(uri "${{KIPRJMOD}}/../common/lib/{LIB}.pretty")'
                 f'(options "")(descr "THR pedalboard project footprints"))\n)\n')
    with open(os.path.join(out_dir, "sym-lib-table"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'(sym_lib_table\n  (version 7)\n  (lib (name "{LIB}")(type "KiCad")(uri "${{KIPRJMOD}}/../common/lib/{LIB}.kicad_sym")'
                 f'(options "")(descr "THR pedalboard project symbols"))\n)\n')
    if jlc_rules:
        import jlcpcb_rules
        jlcpcb_rules.install(pro)

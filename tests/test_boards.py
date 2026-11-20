"""Board and fabrication tests for the generated pedal boards (no simulation).

    .venv/Scripts/python -m unittest discover -s tests -t tests -v            # everything (~40 s: KiCad ERC/DRC)
    .venv/Scripts/python -m unittest discover -s tests -t tests -k Fab -v     # one group

Groups: Checks (KiCad ERC / DRC with schematic parity), Regenerate (the generators reproduce the committed files),
Fab (JLCPCB manufacturing limits), Mechanics (sizes, knob grid, holes, connector edge, what sits on top),
Assembly (BOM / CPL against the board, extended-part count).
"""
import hashlib
import math
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardfile import BOARDS, PCB, Board, design, path, read_csv, OX, OY  # noqa: E402

import kicad_checks  # noqa: E402  (tools/, on sys.path via boardfile)
from kicad_gen import kid  # noqa: E402
from gen.build import build  # noqa: E402
from gen.parts import CATALOG  # noqa: E402
from gen.outline import Outline  # noqa: E402
import jlcpcb_rules  # noqa: E402

# JLCPCB standard 2-layer limits (jlcpcb.com/capabilities), with the margins we design to
JLC = dict(track=0.127, via_drill=0.3, via_diameter=0.45, via_annular=0.13, pth_drill=0.3, pth_annular=0.15,
           npth_drill=0.5, silk_height=1.0, silk_thickness=0.15)
PEDAL_SIZE = (50.0, 65.0)
SIZES = {"presets": (200.0, 50.0), "main": (180.0, 110.0), "gate_comp": (60.0, 116.0)}
# boards cut to an enclosure template (common/templates): parts and copper stay inside its outline
OUTLINES = {"gate_comp": "Type_Z-77_1590N1"}
# pedal encoder squares other than the default 28 mm one at (11..39, 20..48)
ENC_GRID = {"gate_comp": [(9.5, 18.0), (9.5, 59.0), (50.5, 18.0), (50.5, 59.0)]}       # 41 mm square
# extended part numbers per design (each costs a feeder fee); others 4. Main: + the display, blue and orange LEDs
MAX_EXTENDED = {"presets": 3, "main": 6, "gate_comp": 5}       # Gate + Comp: + the PCA9511A bus buffer
TOP_THT_ALLOWED = ("ENC", "J", "H", "DS")    # on the top side (6 mm under the panel) only encoders, wire pads, holes
#                                              and the 7-segment digits (8 mm: their faces reach the panel window)


def gnd(d):
    """The board's ground net as KiCad names it: GND on a global label (Design.global_nets), else /GND."""
    return "GND" if "GND" in design(d).global_nets else "/GND"


def _md5(file):
    with open(file, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def setUpModule():
    warnings.simplefilter("ignore", ResourceWarning)     # the generator opens files without `with`


def boards():
    return [(d, Board(path(d, ".kicad_pcb"))) for d in BOARDS]


class Checks(unittest.TestCase):
    def test_erc(self):
        for d in BOARDS:
            with self.subTest(board=d[2]):
                items = kicad_checks.erc(path(d, ".kicad_sch"))
                self.assertEqual([], [f'{v["type"]}: {v["description"]}' for v in items])

    def test_drc(self):
        """Clearances, connectivity (zones refilled), silkscreen, schematic parity - errors and warnings."""
        for d in BOARDS:
            with self.subTest(board=d[2]):
                items = kicad_checks.drc(path(d, ".kicad_pcb"))
                self.assertEqual([], [kicad_checks.describe(v)[:200] for v in items])


class Regenerate(unittest.TestCase):
    def test_generators_reproduce_files(self):
        """Running each generator again (routing from its cache) gives byte-identical schematic, board, BOM and CPL:
        nothing is random, and the committed files are up to date with the generator."""
        for d in BOARDS:
            with self.subTest(board=d[2]):
                files = [path(d, s) for s in (".kicad_pcb", ".kicad_sch", "_bom.csv", "_cpl.csv")]
                before = [_md5(f) for f in files]
                devnull = open(os.devnull, "w")
                old, sys.stdout = sys.stdout, devnull
                try:
                    build(design(d), os.path.join(PCB, d[0]), checks=False, render=False)
                finally:
                    sys.stdout = old
                    devnull.close()
                after = [_md5(f) for f in files]
                changed = [os.path.basename(f) for f, a, b in zip(files, before, after) if a != b]
                self.assertEqual([], changed, "regenerated files differ: stale outputs or non-determinism")


class Fab(unittest.TestCase):
    def test_tracks(self):
        for d, b in boards():
            with self.subTest(board=d[2]):
                thin = [s for s in b.segments if s["w"] < JLC["track"]]
                self.assertEqual([], thin)

    def test_vias(self):
        for d, b in boards():
            with self.subTest(board=d[2]):
                bad = [v for v in b.vias if v["drill"] < JLC["via_drill"] or v["size"] < JLC["via_diameter"]
                       or (v["size"] - v["drill"]) / 2 < JLC["via_annular"] - 1e-9]
                self.assertEqual([], bad)

    def test_holes(self):
        for d, b in boards():
            with self.subTest(board=d[2]):
                bad = []
                for fp in b.footprints:
                    for p in fp.pads:
                        if p.kind == "thru_hole":
                            if p.drill < JLC["pth_drill"] or (min(p.w, p.h) - p.drill) / 2 < JLC["pth_annular"] - 1e-9:
                                bad.append((fp.ref, p.num, p.w, p.h, p.drill))
                        elif p.kind == "np_thru_hole" and p.drill < JLC["npth_drill"]:
                            bad.append((fp.ref, "NPTH", p.drill))
                self.assertEqual([], bad)

    def test_silkscreen_text(self):
        """Every text printed on silkscreen is at least 1.0 mm high with a 0.15 mm stroke (JLCPCB legibility)."""
        for d, b in boards():
            with self.subTest(board=d[2]):
                texts = [(t, l, f) for t, l, f in b.texts if "SilkS" in l]
                for fp in b.footprints:
                    texts += fp.texts
                bad = [(t, h, th) for t, l, (h, th) in texts
                       if h < JLC["silk_height"] - 1e-9 or (th is not None and th < JLC["silk_thickness"] - 1e-9)]
                self.assertEqual([], bad)

    def test_gnd_zones_both_layers(self):
        for d, b in boards():
            with self.subTest(board=d[2]):
                layers = sorted(kid_layer(z) for z in b.zones if not kid(z, "keepout"))     # rule areas aside
                self.assertEqual(["B.Cu", "F.Cu"], layers)


def _seg_dist(px, py, a, b):
    """Distance from a point to the segment a-b."""
    (x1, y1), (x2, y2) = a, b
    dx, dy = x2 - x1, y2 - y1
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy or 1.0)))
    return math.hypot(px - x1 - t * dx, py - y1 - t * dy)


def kid_layer(zone):
    for n in zone:
        if isinstance(n, list) and n[0] == "layer":
            return n[1]


class Mechanics(unittest.TestCase):
    def test_board_size(self):
        for d, b in boards():
            with self.subTest(board=d[2]):
                x0, y0, x1, y1 = b.outline
                self.assertEqual((0.0, 0.0), (round(x0, 3), round(y0, 3)))
                self.assertEqual(SIZES.get(d[2], PEDAL_SIZE), (round(x1 - x0, 3), round(y1 - y0, 3)))

    def test_parts_inside_board(self):
        for d, b in boards():
            with self.subTest(board=d[2]):
                x0, y0, x1, y1 = b.outline
                tpl = Outline(OUTLINES[d[2]]) if d[2] in OUTLINES else None
                out = [(p.ref, p.num, round(p.x, 2), round(p.y, 2)) for fp in b.footprints for p in fp.pads
                       if not (x0 + 0.3 <= p.x - p.w / 2 and p.x + p.w / 2 <= x1 - 0.3
                               and y0 + 0.3 <= p.y - p.h / 2 and p.y + p.h / 2 <= y1 - 0.3
                               and (tpl is None or tpl.inside((p.x - p.w / 2, p.y - p.h / 2, p.x + p.w / 2,
                                                               p.y + p.h / 2), 0.3)))
                       and p.kind != "np_thru_hole"]
                self.assertEqual([], out)

    def test_encoder_grid(self):
        """Pedal encoders: a 28 mm square (the 5th in its centre), knobs at least 19 mm apart, lugs on GND."""
        for d, b in boards():
            encs = [fp for fp in b.footprints if fp.ref.startswith("ENC")]
            if not encs or d[2] == "main":
                continue
            with self.subTest(board=d[2]):
                pts = sorted((round(f.x, 3), round(f.y, 3)) for f in encs)
                corners = [p for p in pts if p != (25.0, 34.0)]
                self.assertEqual(ENC_GRID.get(d[2], [(11.0, 20.0), (11.0, 48.0), (39.0, 20.0), (39.0, 48.0)]),
                                 corners)
                if len(pts) == 5:
                    self.assertIn((25.0, 34.0), pts)
                dmin = min(math.dist(a, b_) for i, a in enumerate(pts) for b_ in pts[i + 1:])
                self.assertGreaterEqual(dmin, 19.0)
                for f in encs:
                    self.assertEqual({gnd(d)}, {p.net for p in f.pads if p.num == "MP"}, f.ref)
                    self.assertEqual("F.Cu", f.layer, f.ref)

    def test_rj45_at_top_edge(self):
        """Pedals: the jack is on the bottom side with its plug opening flush with the top edge (y = 0)."""
        for d, b in boards():
            if d[2] == "main":
                continue
            with self.subTest(board=d[2]):
                j = [f for f in b.footprints if "RJ45" in f.lib_id]
                self.assertEqual(1, len(j))
                self.assertEqual("B.Cu", j[0].layer)
                self.assertEqual(0.0, j[0].rot)
                self.assertAlmostEqual(0.0, j[0].y - 8.6, places=3)       # opening at local +8.6, mirrored

    def test_top_side_low_parts(self):
        """Under the panel (6 mm gap) the top side carries SMD parts, encoders, wire pads and holes only."""
        for d, b in boards():
            with self.subTest(board=d[2]):
                bad = [f.ref for f in b.footprints if f.layer == "F.Cu" and "through_hole" in f.attr
                       and not f.ref.startswith(TOP_THT_ALLOWED)]
                self.assertEqual([], bad)

    def test_presets_holes(self):
        """Presets: M12 footswitch holes (12.5 mm) at y = 36 on a 45 mm pitch with the 3rd one in the centre, and an
        18 mm hole above each, its edge 1 mm from the 20 mm nut area (centre at y = 16)."""
        d = next(x for x in BOARDS if x[2] == "presets")
        b = Board(path(d, ".kicad_pcb"))
        holes = sorted((round(p.x, 3), round(p.y, 3), p.drill) for f in b.footprints for p in f.pads
                       if p.kind == "np_thru_hole" and f.ref.startswith("H"))
        xs = [10.0, 55.0, 100.0, 145.0, 190.0]
        self.assertEqual(sorted([(x, 36.0, 12.5) for x in xs] + [(x, 16.0, 18.0) for x in xs]), holes)
        x0, _, x1, _ = b.outline
        self.assertAlmostEqual((x0 + x1) / 2, xs[2])


class GateComp(unittest.TestCase):
    """Gate + Comp: the 1590N1 template outline, footswitch holes, markings, test pads, JLCPCB rules."""

    @classmethod
    def setUpClass(cls):
        cls.d = next(x for x in BOARDS if x[2] == "gate_comp")
        cls.b = Board(path(cls.d, ".kicad_pcb"))
        cls.des = design(cls.d)

    def test_outline_is_the_template(self):
        """Edge.Cuts = the template's outline, moved to the origin: same items, same coordinates."""
        tpl = Outline(OUTLINES["gate_comp"])
        got = []
        for g in self.b.edges:
            pts = [(round(float(g_[1]) - OX, 6), round(float(g_[2]) - OY, 6))
                   for k in ("center", "start", "mid", "end") for g_ in [x for x in g if isinstance(x, list)
                                                                         and x[0] == k]]
            got.append(sorted(pts))
        want = [sorted(ps) for k, ps in tpl.items]
        self.assertEqual(sorted(want), sorted(got))
        self.assertEqual(4, len(tpl.holes))

    def test_outline_rows(self):
        """Every grid row of the board has an inside span (a row through a vertex of the contour once found one
        crossing only, and the router's keep-out then closed the whole row, cutting the top tab off)."""
        tpl = Outline(OUTLINES["gate_comp"])
        step = 0.125
        rows = [k * step for k in range(int(round(tpl.height / step)) + 1)]
        self.assertEqual([], [y for y in rows if tpl.span(min(max(y, 1e-6), tpl.height - 1e-6)) is None])

    def test_footswitch_holes(self):
        """12.5 mm footswitch holes at (16, 96) and (44, 96) with the 20 mm nut area free of parts (courtyard)."""
        holes = sorted((round(p.x, 3), round(p.y, 3), p.drill) for f in self.b.footprints for p in f.pads
                       if p.kind == "np_thru_hole" and f.ref.startswith("H"))
        self.assertEqual([(16.0, 96.0, 12.5), (44.0, 96.0, 12.5)], holes)

    def test_markings(self):
        """Name and revision from the generator's one place, and 'Made on the Island', on silkscreen."""
        import gen_gate_comp
        texts = {t for t, layer, _ in self.b.texts if "SilkS" in layer}
        self.assertIn(gen_gate_comp.NAME, texts)
        self.assertIn(f"Rev{gen_gate_comp.REV}", texts)
        self.assertIn("Made on the Island", texts)
        self.assertEqual(gen_gate_comp.REV, self.des.rev)

    def test_test_points(self):
        """Test pads on the bottom side for GND (the global net), 3V3, the card-side I2C, INT and RESET."""
        tps = {p.net for f in self.b.footprints if f.ref.startswith("TP") for p in f.pads}
        self.assertEqual({"GND", "/+3V3", "/SCL_L", "/SDA_L", "/INT", "/RESET"}, tps)
        self.assertEqual({"B.Cu"}, {f.layer for f in self.b.footprints if f.ref.startswith("TP")})

    def test_jlcpcb_rules(self):
        """The project carries JLCPCB's limits, so the clean DRC means the board can be made."""
        self.assertEqual([], jlcpcb_rules.problems(path(self.d, ".kicad_pro")))

    # nothing beyond the template's rim (it marks where the enclosure, standoffs and screws come close), except the
    # bus jack, which reaches through it to the edge
    THROUGH_RIM = ("J1",)

    def rim_clearance(self, x, y, r=0.0):
        """Distance from a disc's edge to the rim line: negative when it crosses the rim or lies beyond it."""
        tpl = self.des.outline
        segs = tpl.frame_segments()
        s = tpl.span(y, rim=True)
        dist = min(_seg_dist(x, y, a, b) for a, b in segs)
        return dist - r if s and s[0] < x < s[1] else -(dist + r)

    def test_copper_inside_rim(self):
        """Every track and via lies inside the rim (the router's rim keep-outs)."""
        bad = []
        for s in self.b.segments:
            n = max(1, int(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) / 0.1))
            c = min(self.rim_clearance(s["x1"] + (s["x2"] - s["x1"]) * k / n, s["y1"] + (s["y2"] - s["y1"]) * k / n,
                                       s["w"] / 2) for k in range(n + 1))
            if c < 0:
                bad.append(("track", s["layer"], round(s["x1"], 2), round(s["y1"], 2), round(c, 3)))
        for v in self.b.vias:
            c = self.rim_clearance(v["x"], v["y"], v["size"] / 2)
            if c < 0:
                bad.append(("via", round(v["x"], 2), round(v["y"], 2), round(c, 3)))
        self.assertEqual([], bad)

    def test_rim_rule_areas(self):
        """Beyond the rim: keep-out rule areas on both copper layers (no tracks, vias or pour), so DRC checks it and
        the GND zones stop at the rim."""
        areas = [z for z in self.b.zones if kid(z, "keepout") and kid(z, "name") and kid(z, "name")[1] == "beyond rim"]
        self.assertEqual(2, len(areas))
        for z in areas:
            ko = {k[0]: k[1] for k in kid(z, "keepout")[1:] if isinstance(k, list)}
            self.assertEqual(("not_allowed",) * 3, (ko["tracks"], ko["vias"], ko["copperpour"]))
            self.assertEqual({"F.Cu", "B.Cu"}, set(kid(z, "layers")[1:]))

    def test_parts_and_labels_inside_rim(self):
        """Courtyards (but the bus jack's) and silkscreen labels inside the rim."""
        from gen.board import Board as GenBoard, text_box
        gb = GenBoard(self.des, PCB, None)
        bad = [(ref, round(min(self.rim_clearance(x, y) for x, y in pts), 3)) for ref, pts in sorted(gb.courtyards.items())
               if ref not in self.THROUGH_RIM and min(self.rim_clearance(x, y) for x, y in pts) < 0]
        gb.place_labels()
        for ref, text, x, y, side, size, kind in gb.labels:
            if x is None:
                continue
            w, h = text_box(text, size)
            corners = [(x + sx * w / 2, y + sy * h / 2) for sx in (-1, 1) for sy in (-1, 1)]
            if min(self.rim_clearance(cx, cy) for cx, cy in corners) < 0:
                bad.append((ref, text))
        self.assertEqual([], bad)


class MainLayout(unittest.TestCase):
    """Main: the panel layout from the sketch (180 x 110 mm, centre line x = 90)."""

    @classmethod
    def setUpClass(cls):
        d = next(x for x in BOARDS if x[2] == "main")
        cls.b = Board(path(d, ".kicad_pcb"))
        cls.des = design(d)

    def part_xy(self, ref):
        p = self.des.parts[ref]
        return p.x, p.y

    def test_encoders(self):
        """Lower row: 5 on a 30 mm pitch, 25 mm above the bottom edge, the middle one on the centre line; upper
        row: 4, 25 mm higher, offset by 15 mm. Top side, lugs on GND."""
        encs = [f for f in self.b.footprints if f.ref.startswith("ENC")]
        pts = sorted((round(f.y, 3), round(f.x, 3)) for f in encs)
        upper = [(60.0, 45.0 + 30 * k) for k in range(4)]
        lower = [(85.0, 30.0 + 30 * k) for k in range(5)]
        self.assertEqual(upper + lower, pts)
        for f in encs:
            self.assertEqual("F.Cu", f.layer, f.ref)
            self.assertEqual({"/GND"}, {p.net for p in f.pads if p.num == "MP"}, f.ref)

    def test_display(self):
        """Two digits side by side, symmetric about the centre line, upright (pin 1 bottom left)."""
        ds = sorted((f for f in self.b.footprints if f.ref.startswith("DS")), key=lambda f: f.x)
        self.assertEqual(2, len(ds))
        centres = []
        for f in ds:
            pads = {p.num: p for p in f.pads}
            self.assertLess(pads["1"].x, pads["5"].x)
            self.assertGreater(pads["1"].y, pads["10"].y)          # pin 1 row at the bottom
            centres.append(((pads["1"].x + pads["6"].x) / 2, (pads["1"].y + pads["6"].y) / 2))
        self.assertAlmostEqual(90.0, (centres[0][0] + centres[1][0]) / 2, places=3)
        self.assertAlmostEqual(centres[0][1], centres[1][1], places=3)

    def test_flavor_leds_on_arc(self):
        """Blue straight above the toggle, green and orange 30 degrees to either side, all 25 mm from it."""
        hole = next(f for f in self.b.footprints if f.ref.startswith("H"))
        leds = {f.props["Value"]: f for f in self.b.footprints if f.ref.startswith("D") and not f.ref.startswith("DS")}
        for colour, ang in (("blue", 90), ("green", 120), ("orange", 60)):
            f = leds[colour]
            self.assertAlmostEqual(25.0, math.dist((f.x, f.y), (hole.x, hole.y)), places=2, msg=colour)
            self.assertAlmostEqual(ang, math.degrees(math.atan2(hole.y - f.y, f.x - hole.x)), places=1, msg=colour)

    def test_feather_sockets(self):
        """Feather sockets on the bottom side, rows 20.32 mm apart, the 16-pin row's pin 1 (USB-A end) at the top."""
        s16 = next(f for f in self.b.footprints if "PinSocket_1x16" in f.lib_id)
        s12 = next(f for f in self.b.footprints if "PinSocket_1x12" in f.lib_id)
        self.assertEqual(("B.Cu", "B.Cu"), (s16.layer, s12.layer))
        p16 = {p.num: p for p in s16.pads}
        p12 = {p.num: p for p in s12.pads}
        self.assertAlmostEqual(20.32, p12["1"].x - p16["1"].x, places=3)
        self.assertLess(p16["1"].y, p16["16"].y)
        self.assertAlmostEqual(p16["1"].y, p12["12"].y, places=3)      # D4 and SDA: the USB-A end
        self.assertAlmostEqual(p16["1"].y + 2.54 * 11, p12["1"].y, places=3)

    def test_rj45_at_bottom_edge(self):
        """Six jacks on the bottom side, plug openings flush with the bottom edge, symmetric about x = 90."""
        js = sorted((f for f in self.b.footprints if "RJ45" in f.lib_id), key=lambda f: f.x)
        self.assertEqual(6, len(js))
        for f in js:
            self.assertEqual("B.Cu", f.layer, f.ref)
            self.assertEqual(180.0, f.rot, f.ref)
            self.assertAlmostEqual(110.0, f.y + 8.6, places=3)
        xs = [f.x for f in js]
        self.assertEqual([round(180.0 - x, 3) for x in reversed(xs)], [round(x, 3) for x in xs])


class Assembly(unittest.TestCase):
    def test_bom_and_cpl_match_board(self):
        """Every footprint with an LCSC number is in the BOM (same value and number) and the CPL (same position,
        side, rotation); footprints without one are in neither."""
        for d, b in boards():
            with self.subTest(board=d[2]):
                bom = read_csv(path(d, "_bom.csv"))
                cpl = {r["Designator"]: r for r in read_csv(path(d, "_cpl.csv"))}
                bom_refs = {}
                for row in bom:
                    for ref in row["Designator"].split(","):
                        bom_refs[ref] = row
                placed = {f.ref: f for f in b.footprints if f.props.get("LCSC")}
                self.assertEqual(sorted(placed), sorted(bom_refs))
                self.assertEqual(sorted(placed), sorted(cpl))
                for ref, f in placed.items():
                    self.assertEqual(f.props["LCSC"], bom_refs[ref]["LCSC Part #"], ref)
                    self.assertEqual(f.props["Value"], bom_refs[ref]["Comment"], ref)
                    c = cpl[ref]
                    self.assertAlmostEqual(f.x + OX, float(c["Mid X"].rstrip("m")), places=3, msg=ref)
                    self.assertAlmostEqual(-(f.y + OY), float(c["Mid Y"].rstrip("m")), places=3, msg=ref)
                    self.assertEqual("Bottom" if f.layer == "B.Cu" else "Top", c["Layer"], ref)
                    self.assertAlmostEqual(f.rot % 360, float(c["Rotation"]), places=3, msg=ref)

    def test_extended_parts(self):
        """Few extended part numbers per design: each one costs a feeder fee at JLCPCB."""
        by_lcsc = {c["lcsc"]: c for c in CATALOG.values() if c.get("lcsc")}
        for d, b in boards():
            with self.subTest(board=d[2]):
                ext = sorted({f.props["LCSC"] for f in b.footprints if f.props.get("LCSC")
                              and "extended" in by_lcsc[f.props["LCSC"]].get("jlc", "")})
                self.assertLessEqual(len(ext), MAX_EXTENDED.get(d[2], 4), ext)

    def test_lcsc_numbers_known(self):
        """Every LCSC number on the boards comes from the checked part catalogue (gen/parts.py)."""
        known = {c["lcsc"] for c in CATALOG.values() if c.get("lcsc")}
        for d, b in boards():
            with self.subTest(board=d[2]):
                self.assertEqual([], sorted({f.props["LCSC"] for f in b.footprints if f.props.get("LCSC")} - known))


if __name__ == "__main__":
    unittest.main()

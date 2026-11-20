"""Gate + Compressor pedal board (MCP23017 at 0x22): port A = Gate, port B = Compressor.

Pin assignment as in feather/doc/wiring.md (Gate + Compressor - 0x22). Outline: the Hammond 1590N1 template
(common/templates/Type_Z-77_1590N1.kicad_pcb, 60 x 116 mm, narrow tabs 47 mm wide at both ends); top edge =
RJ45 side. Encoders in a 41 mm square, as large as the board allows, the top row 18 mm below the edge: THRESHOLD /
LEVEL on top, DECAY / SUSTAIN below (names under the knobs); the MCP23017 sits in the free centre column. Indicators
GATE (left) and COMP (right) below them, then the two Q27 push buttons soldered into the board: the board hangs on
them (their nuts on the lid, the lid on their housings 10.43 mm above the board), the encoders pass through 7.5 mm
holes in the lid. The bus enters through the shielded RJ45 with two LEDs (power, INT), a PCA9511A hot-swap buffer
and a ferrite bead; the buttons are debounced in hardware; test pads on the bottom side.

    ../../.venv/Scripts/python gen_gate_comp.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, GND, V33, bus, mcp, encoder, indicator, switch, testpoint  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_bus, place_indicators, place_filter, place_addr_jumpers  # noqa: E402
from gen.outline import Outline  # noqa: E402

NAME, REV = "YTHR GATE-COMP", "0"
ENC_X = 9.5      # encoder centres 9.5 mm from the side edges
ENC_Y = 18.0     # top encoder row (rev A: 34; moved up 16 mm)        # the one place for the board's name and revision (silkscreen, title block)


def design():
    d = Design("gate_comp", NAME, addr=0x22, rev=REV)
    d.use_outline(Outline("Type_Z-77_1590N1"))
    d.mask = "Yellow"
    d.rim = True                         # the template's rim along the edge, both sides
    d.rim_width = 0.3
    d.jlc_rules = True
    d.global_nets = {GND}                # ground is the global net GND (a global label), not /GND
    d.router = "freerouting"             # routed by tools/freeroute.py, cached in routing.pkl
    used = {}
    b = bus(d, buffer=True, ferrite=True, leds=True)
    encs = []
    # wiring.md: gate decay GPA2/GPA3, gate threshold GPA4/GPA5, comp sustain GPB2/GPB3, comp level GPB4/GPB5
    for label, a, bb in (("THRESHOLD", "GPA4", "GPA5"), ("LEVEL", "GPB4", "GPB5"), ("DECAY", "GPA2", "GPA3"),
                         ("SUSTAIN", "GPB2", "GPB3")):
        encs.append(encoder(d, label, a, bb))
        encs[-1]["enc"].silk_at = "below"    # the knob's name under it, the reference designator above
        used[a], used[bb] = a, bb
    leds = [indicator(d, "GATE", "GPA1"), indicator(d, "COMP", "GPB1")]
    used["GPA1"], used["GPB1"] = "GPA1", "GPB1"
    sw_gate = switch(d, "SW", "GPA0", debounce=True)
    sw_comp = switch(d, "SW", "GPB0", debounce=True)
    used["GPA0"], used["GPB0"] = "GPA0", "GPB0"
    m = mcp(d, used, scl=b["scl"], sda=b["sda"])
    # Q27 push buttons in parallel with the footswitch wire pads, before the debounce: 1 COMM = GND, 2 NO, 3 NC open
    buttons = [d.add("SW", "Q27", {"1": GND, "2": sw.pins["1"], "3": None}, "sw") for sw in (sw_gate, sw_comp)]
    tps = [testpoint(d, label, net) for label, net in (("GND", GND), ("3V3", V33), ("SCL", b["scl"]),
                                                         ("SDA", b["sda"]), ("INT", "INT"), ("RST", "RESET"))]

    P = d.place
    cx = d.width / 2                     # 30: centre column
    place_bus(d, b, x=cx)
    # the shielded jack's LED and shield pins take the TVS's and the bulk cap's places: TVS, ferrite and the LED
    # resistors in one row over the jack, between its contact rows and its LED pins; bulk cap right of the shield
    P(b["tvs"].ref, cx, 12.6, 0)
    P(b["fb"].ref, cx + 3.8, 12.2, 0)
    P(b["rled_int"].ref, cx - 7.6, 12.2, 0)
    P(b["rled_pwr"].ref, cx + 7.6, 12.2, 0)
    P(b["cbulk"].ref, cx + 11.4, 12.0, 90)
    # hot-swap buffer between the jack and the encoders; card-side pull-ups left of it, decoupling right
    P(b["buf"].ref, cx, 22.0, 0)
    P(b["cbuf"].ref, cx + 5.2, 20.6, 90)
    P(b["rscl_l"].ref, cx - 5.6, 20.9, 0)
    P(b["rsda_l"].ref, cx - 5.6, 23.1, 0)
    ex0, ex1, ey0, ey1 = ENC_X, d.width - ENC_X, ENC_Y, ENC_Y + d.width - 2 * ENC_X     # the square
    for e, (x, y, r) in zip(encs, [(ex0, ey0, 90), (ex1, ey0, 270), (ex0, ey1, 90), (ex1, ey1, 270)]):
        P(e["enc"].ref, x, y, r)
    # filters next to the encoder pins (pins point to the centre column)
    for e, (x, y, r) in zip(encs, [(ex0 + 10.4, ey0, 0), (ex1 - 10.4, ey0, 180), (ex0 + 10.4, ey1, 0),
                                   (ex1 - 10.4, ey1, 180)]):
        place_filter(d, e, x, y, r)
    # MCP23017 in the centre between the rows: port A (gate, left encoders) on the left, port B (comp) on the right
    my = (ey0 + ey1) / 2
    P(m["u"].ref, cx, my, 180)
    P(m["c"].ref, cx + 6.4, my - 1.6, 90)
    place_addr_jumpers(d, m, 14.5, my - 2.8)
    P(m["rr"].ref, 19.5, my, 0)
    place_indicators(d, leds, [16.0, 44.0], y=76.0, names=["GATE", "COMPRESSOR"])
    # Q27 push buttons, their names under them as large as the board's name on the back
    for btn, x, name in zip(buttons, (16.0, 44.0), ("GATE", "COMPRESSOR")):
        P(btn.ref, x, 96.0, 0)
        d.texts.append((name, x, 104.4, 1.5, "F.SilkS"))
    # footswitch wire pads upright at the side edges, beside the rim's detour round the mounting hole; the debounce
    # RC below them
    P(sw_gate.ref, 2.9, 90.6, 0)
    P(sw_comp.ref, d.width - 2.9, 90.6, 0)
    for sw, x0, s in ((sw_gate, 0.0, 1), (sw_comp, d.width, -1)):
        P(sw.db["rs"].ref, x0 + s * 2.4, 97.4, 90)
        P(sw.db["pu"].ref, x0 + s * 4.3, 97.4, 90)
        P(sw.db["c"].ref, x0 + s * 3.4, 101.0, 0)
    # test pads on the bottom side, in the centre column between the buttons
    for k, tp in enumerate(tps):
        P(tp.ref, cx, 85.5 + 3.4 * k, 0, "B")
    d.texts.append(("ADDR 0x22", 14.5, my + 5.6, 1.0, "F.SilkS"))
    d.texts.append((NAME, cx, 109.6, 1.5, "B.SilkS", "name"))
    d.texts.append((f"Rev{REV}", cx, 111.7, 1.0, "B.SilkS", "revision"))
    d.texts.append(("Made on the Island", cx, 113.5, 1.0, "B.SilkS", "origin"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

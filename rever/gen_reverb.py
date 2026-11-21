"""Reverb pedal board (MCP23017 at 0x25): 4 encoders, SPRING / ROOM / PLATE / HALL indicators, MODE and ON buttons.

Pin assignment as in feather/doc/wiring.md (Reverb - 0x25). Outline: the enclosure template Type_Z-76_Z-77
(common/templates/Type_Z-76_Z-77.kicad_pcb, 50 x 104 mm, narrow tabs 37 mm wide at both ends); top edge = RJ45
side. Encoders without the push switch (not needed) in a 32 mm square on the side lines x = 9 / 41 (DECAY / PR_DL on
top, TONE / MIX below, names under the knobs); the MCP23017 in the square's centre, the address jumpers right of it.
The four indicators in a row below the square, then the two Q27 push buttons soldered into the board (MODE left, ON
right): the board hangs on them (their nuts on the lid, the lid on their housings 10.43 mm above the board), the
encoders pass through 7.5 mm holes in the lid. The bus enters through the shielded RJ45 with two LEDs (power, INT),
a PCA9511A hot-swap buffer and a ferrite bead; the buttons are debounced in hardware; test pads on the bottom side.
Blue solder mask. Layout and blocks as on Effect (effect/gen_effect.py).

    ../../.venv/Scripts/python gen_reverb.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, GND, V33, bus, mcp, encoder, indicator, switch, testpoint  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_bus, place_indicators, place_filter, place_addr_jumpers  # noqa: E402
from gen.outline import Outline  # noqa: E402

NAME, REV = "YTHR REVERB", "0"   # the one place for the board's name and revision (silkscreen, title block)
SIDE = 16.0                      # side lines 16 mm either side of the centre line (32 mm apart, even for drilling)
ENC_Y = (21.0, 53.0)             # the square's rows
LED_Y = 65.0                     # indicator row (second quarter), on x = 10 ... 40
BTN_Y = 87.0                     # Q27 row (first quarter; lower, they reach the rim round the corner notches)


def design():
    d = Design("reverb", NAME, addr=0x25, rev=REV)
    d.use_outline(Outline("Type_Z-76_Z-77"))
    d.mask = "Blue"
    d.rim = True                         # the template's rim along the edge, both sides
    d.rim_width = 0.3
    d.jlc_rules = True
    d.global_nets = {GND}                # ground is the global net GND (a global label), not /GND
    d.router = "freerouting"             # routed by tools/freeroute.py, cached in routing.pkl
    used = {}
    b = bus(d, buffer=True, ferrite=True, leds=True)
    encs = []
    for label, a, bb in (("DECAY", "GPA1", "GPB1"), ("PR_DL", "GPA2", "GPB2"), ("TONE", "GPA3", "GPB3"),
                         ("MIX", "GPA4", "GPB4")):
        encs.append(encoder(d, label, a, bb, switch=False))
        encs[-1]["enc"].silk_at = "below"    # the knob's name under it, the reference designator above
        used[a], used[bb] = a, bb
    leds = []
    for label, gp in (("SP", "GPB5"), ("RM", "GPA5"), ("PT", "GPB6"), ("HL", "GPA6")):
        leds.append(indicator(d, label, gp))
        used[gp] = gp
    sw_mode = switch(d, "MODE", "GPB0", debounce=True)
    sw_on = switch(d, "ON", "GPA0", debounce=True)
    used["GPB0"], used["GPA0"] = "GPB0", "GPA0"
    m = mcp(d, used, scl=b["scl"], sda=b["sda"])
    # Q27 push buttons in parallel with the footswitch wire pads, before the debounce: 1 COMM = GND, 2 NO, 3 NC open
    buttons = [d.add("SW", "Q27", {"1": GND, "2": sw.pins["1"], "3": None}, "sw") for sw in (sw_mode, sw_on)]
    tps = [testpoint(d, label, net) for label, net in (("GND", GND), ("3V3", V33), ("SCL", b["scl"]),
                                                         ("SDA", b["sda"]), ("INT", "INT"), ("RST", "RESET"))]

    P = d.place
    cx = d.width / 2                     # 25: centre line
    xl, xr = cx - SIDE, cx + SIDE        # 9 / 41: side lines
    place_bus(d, b, x=cx)
    # the shielded jack's LED and shield pins take the TVS's and the bulk cap's places: TVS, ferrite and the LED
    # resistors in one row over the jack, between its contact rows and its LED pins; bulk cap right of the shield
    P(b["tvs"].ref, cx, 12.6, 0)
    P(b["fb"].ref, cx + 3.8, 12.2, 0)
    P(b["rled_int"].ref, cx - 7.6, 12.2, 0)
    P(b["rled_pwr"].ref, cx + 7.6, 12.2, 0)
    P(b["cbulk"].ref, cx + 11.4, 12.0, 90)
    # hot-swap buffer upright in the centre column between the top encoders' filters; card-side pull-ups above it,
    # decoupling below
    P(b["buf"].ref, cx, 22.2, 90)
    P(b["rscl_l"].ref, cx - 1.5, 16.8, 0)
    P(b["rsda_l"].ref, cx + 1.5, 16.8, 0)
    P(b["cbuf"].ref, cx, 27.2, 0)
    # encoders on the side lines, their pins towards the centre line
    y0, y1 = ENC_Y
    for e, (x, y, r) in zip(encs, [(xl, y0, 90), (xr, y0, 270), (xl, y1, 90), (xr, y1, 270)]):
        P(e["enc"].ref, x, y, r)
    # filters next to the encoder pins
    for e, (x, y, r) in zip(encs, [(xl + 10.4, y0, 0), (xr - 10.4, y0, 180), (xl + 10.4, y1, 0),
                                   (xr - 10.4, y1, 180)]):
        place_filter(d, e, x, y, r)
    # MCP23017 in the square's centre, its decoupling above it; address jumpers and the RESET pull-up right of it
    my = (y0 + y1) / 2
    P(m["u"].ref, cx, my + 1.0, 0)
    P(m["c"].ref, cx, my - 7.0, 0)
    place_addr_jumpers(d, m, 40.0, my - 2.8)
    P(m["rr"].ref, 44.4, my + 2.8, 90)
    d.texts.append(("ADDR 0x25", 40.0, my + 6.4, 1.0, "F.SilkS"))
    # indicators in a row, 10 mm apart (on the side lines the outer jumpers would reach the holes' rim):
    # [JP][R][LED], the lamp's wire pads below
    place_indicators(d, leds, [cx - 15.0 + 10.0 * k for k in range(4)], y=LED_Y,
                     names=["SPRING", "ROOM", "PLATE", "HALL"])
    # Q27 push buttons on the side lines, their names under them as large as the board's name on the back
    for btn, x, name, dx in zip(buttons, (xl, xr), ("MODE", "ON"), (1.5, 0.0)):
        P(btn.ref, x, BTN_Y, 0)
        d.texts.append((name, x + dx, BTN_Y + 7.6, 1.5, "F.SilkS"))     # MODE clear of the corner notch's rim
    # footswitch wire pads (for external switches in parallel) between the buttons, the debounce RC below them
    for sw, s in ((sw_mode, -1), (sw_on, 1)):
        x = cx + s * 5.0
        P(sw.ref, x, 74.5, 90)
        P(sw.db["rs"].ref, x - 1.0, 78.2, 90)
        P(sw.db["pu"].ref, x + 1.0, 78.2, 90)
        P(sw.db["c"].ref, x, 81.2, 0)
    # test pads on the bottom side, two columns between the buttons
    for k, tp in enumerate(tps):
        P(tp.ref, cx + (-3.0 if k % 2 == 0 else 3.0), 84.0 + 3.4 * (k // 2), 0, "B")
    d.texts.append((NAME, cx, 97.6, 1.5, "B.SilkS", "name"))
    d.texts.append((f"Rev{REV}", cx, 99.6, 1.0, "B.SilkS", "revision"))
    d.texts.append(("Made on the Island", cx, 101.3, 1.0, "B.SilkS", "origin"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

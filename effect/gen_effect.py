"""Effect pedal board (MCP23017 at 0x23): 5 encoders, 4 mode indicators, MODE and ON push buttons.

Pin assignment as in feather/doc/wiring.md (Effect - 0x23). Outline: the enclosure template Type_Z-76_Z-77
(common/templates/Type_Z-76_Z-77.kicad_pcb, 50 x 104 mm, narrow tabs 37 mm wide at both ends); top edge = RJ45
side. Encoders in a 30 mm square on the side lines x = 10 / 40 (FDBK / DEPTH on top, PR_DL / SPEED below, names under
the knobs), MIX in its centre; the MCP23017 left of MIX, the address jumpers right of it. The four indicators in a row
below the square, then the two Q27 push buttons soldered into the board (MODE left, ON right): the board hangs on
them (their nuts on the lid, the lid on their housings 10.43 mm above the board), the encoders pass through 7.5 mm
holes in the lid. The bus enters through the shielded RJ45 with two LEDs (power, INT), a PCA9511A hot-swap buffer
and a ferrite bead; the buttons are debounced in hardware; test pads on the bottom side. Purple solder mask.

    ../../.venv/Scripts/python gen_effect.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, GND, V33, bus, mcp, encoder, indicator, switch, testpoint  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_bus, place_indicators, place_filter, place_addr_jumpers  # noqa: E402
from gen.outline import Outline  # noqa: E402

NAME, REV = "YTHR EFFECT", "0"   # the one place for the board's name and revision (silkscreen, title block)
SIDE = 15.0                      # side lines 15 mm either side of the centre line (30 mm apart, even for drilling)
ENC_Y = (22.0, 52.0)             # the square's rows; MIX in its centre
LED_Y = 65.0                     # indicator row (second quarter)
BTN_Y = 87.0                     # Q27 row (first quarter; lower, they reach the rim round the corner notches)


def design():
    d = Design("effect", NAME, addr=0x23, rev=REV)
    d.use_outline(Outline("Type_Z-76_Z-77"))
    d.mask = "Purple"
    d.rim = True                         # the template's rim along the edge, both sides
    d.rim_width = 0.3
    d.jlc_rules = True
    d.global_nets = {GND}                # ground is the global net GND (a global label), not /GND
    d.router = "freerouting"             # routed by tools/freeroute.py, cached in routing.pkl
    used = {}
    b = bus(d, buffer=True, ferrite=True, leds=True)
    encs = []
    for label, a, bb in (("FDBK", "GPA1", "GPB1"), ("DEPTH", "GPA2", "GPB2"), ("MIX", "GPA3", "GPB3"),
                         ("PR_DL", "GPA4", "GPB4"), ("SPEED", "GPA5", "GPB5")):
        encs.append(encoder(d, label, a, bb))
        encs[-1]["enc"].silk_at = "below"    # the knob's name under it, the reference designator above
        used[a], used[bb] = a, bb
    leds = []
    for label, gp in (("CH", "GPB6"), ("FL", "GPA6"), ("PH", "GPB7"), ("TR", "GPA7")):
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
    xl, xr = cx - SIDE, cx + SIDE        # 10 / 40: side lines
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
    # encoders: the side ones with their pins towards the centre line, MIX with its pins down
    y0, y1 = ENC_Y
    pos = [(xl, y0, 90), (xr, y0, 270), (cx, (y0 + y1) / 2, 0), (xl, y1, 90), (xr, y1, 270)]
    for e, (x, y, r) in zip(encs, pos):
        P(e["enc"].ref, x, y, r)
    # filters next to the encoder pins
    for e, (x, y, r) in zip([encs[0], encs[1], encs[3], encs[4]],
                            [(xl + 10.4, y0, 0), (xr - 10.4, y0, 180), (xl + 10.4, y1, 0), (xr - 10.4, y1, 180)]):
        place_filter(d, e, x, y, r)
    my = (y0 + y1) / 2
    mix = encs[2]
    P(mix["rn"].ref, cx, my + 10.2, 270)
    P(mix["ca"].ref, cx - 0.8, my + 14.0, 90)
    P(mix["cb"].ref, cx + 0.8, my + 14.0, 90)
    # MCP23017 left of MIX, between the left encoders; address jumpers and the RESET pull-up right of MIX
    P(m["u"].ref, xl + 1.8, my, 0)
    P(m["c"].ref, xl + 4.5, my - 6.3, 0)
    place_addr_jumpers(d, m, xr, my - 2.8)
    P(m["rr"].ref, xr + 4.4, my + 2.8, 90)
    d.texts.append(("ADDR 0x23", xr, my + 6.4, 1.0, "F.SilkS"))
    # indicators in a row on the side lines and between them: [JP][R][LED], the lamp's wire pads below
    place_indicators(d, leds, [xl + 10.0 * k for k in range(4)], y=LED_Y,
                     names=["CHORUS", "FLANGER", "PHASER", "TREMOLO"])
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

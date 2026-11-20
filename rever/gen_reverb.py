"""Reverb pedal board (MCP23017 at 0x25): 4 encoders, SP / RM / PT / HL indicators, MODE and ON footswitches.

Pin assignment as in feather/doc/wiring.md (Reverb - 0x25). Board 50 x 65 mm, top edge = RJ45 side. Encoders in a
28 mm square as on the prototype (DECAY, PR_DL on top; TONE, MIX below); the MCP23017 sits in the free centre.

    ../../.venv/Scripts/python gen_reverb.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, bus, mcp, encoder, indicator, switch  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_bus, place_indicators, place_filter, place_corner_switches, place_addr_jumpers  # noqa: E402


def design():
    d = Design("reverb", "YTHR - Reverb (0x25)", addr=0x25)
    d.width, d.height = 50.0, 65.0
    d.mask = "Blue"
    used = {}
    b = bus(d)
    encs = []
    for label, a, bb in (("DECAY", "GPA1", "GPB1"), ("PR_DL", "GPA2", "GPB2"), ("TONE", "GPA3", "GPB3"),
                         ("MIX", "GPA4", "GPB4")):
        encs.append(encoder(d, label, a, bb))
        used[a], used[bb] = a, bb
    leds = []
    for label, gp in (("SP", "GPB5"), ("RM", "GPA5"), ("PT", "GPB6"), ("HL", "GPA6")):
        leds.append(indicator(d, label, gp))
        used[gp] = gp
    sw_mode = switch(d, "MODE", "GPB0")
    sw_on = switch(d, "ON", "GPA0")
    used["GPB0"], used["GPA0"] = "GPB0", "GPA0"
    m = mcp(d, used)

    P = d.place
    place_bus(d, b)
    for e, (x, y, r) in zip(encs, [(11, 20, 90), (39, 20, 270), (11, 48, 90), (39, 48, 270)]):
        P(e["enc"].ref, x, y, r)
    for e, (x, y, r) in zip(encs, [(21.4, 20, 0), (28.6, 20, 180), (21.4, 48, 0), (28.6, 48, 180)]):
        place_filter(d, e, x, y, r)
    P(m["u"].ref, 25.0, 34.0, 270)
    P(m["c"].ref, 24.0, 28.3, 0)
    place_addr_jumpers(d, m, 9.5, 31.2)
    P(m["rr"].ref, 14.5, 34.0, 0)
    place_indicators(d, leds, [10.0 + 10.0 * k for k in range(4)],
                     names=["SPRING", "ROOM", "PLATE", "HALL"])
    place_corner_switches(d, sw_mode, sw_on)
    d.texts.append(("REVERB", 25.0, 55.3, 2.5, "F.SilkS"))
    d.texts.append(("ADDR 0x25", 9.5, 38.9, 1.0, "F.SilkS"))
    d.texts.append(("YTHR Reverb rev A", 25.0, 30.0, 1.0, "B.SilkS"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

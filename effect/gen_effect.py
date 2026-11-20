"""Effect pedal board (MCP23017 at 0x23): 5 encoders, 4 mode indicators, MODE and ON footswitches.

Pin assignment as in feather/doc/wiring.md (Effect - 0x23). Board 50 x 65 mm, top edge = RJ45 side (far from
the foot). Encoders in a 28 mm square with MIX in the centre; on-board LEDs in a row along the bottom edge, the
bezel lamps sit in the panel just below the board.

    ../../.venv/Scripts/python gen_effect.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, bus, mcp, encoder, indicator, switch  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_indicators, place_corner_switches, place_addr_jumpers  # noqa: E402


def design():
    d = Design("effect", "YTHR - Effect (0x23)", addr=0x23)
    d.width, d.height = 50.0, 65.0
    d.mask = "Purple"
    used = {}
    b = bus(d)
    encs = []
    for label, a, bb in (("FDBK", "GPA1", "GPB1"), ("DEPTH", "GPA2", "GPB2"), ("MIX", "GPA3", "GPB3"),
                         ("PR_DL", "GPA4", "GPB4"), ("SPEED", "GPA5", "GPB5")):
        encs.append(encoder(d, label, a, bb))
        used[a], used[bb] = a, bb
    leds = []
    for label, gp in (("CH", "GPB6"), ("FL", "GPA6"), ("PH", "GPB7"), ("TR", "GPA7")):
        leds.append(indicator(d, label, gp))
        used[gp] = gp
    sw_mode = switch(d, "MODE", "GPB0")
    sw_on = switch(d, "ON", "GPA0")
    used["GPB0"], used["GPA0"] = "GPB0", "GPA0"
    m = mcp(d, used)

    # ---------------------------------------------------------------- placement (board mm, Y down)
    P = d.place
    P(b["j"].ref, 25.0, 8.6, 0, "B")             # plug opening flush with the top edge
    P(b["tvs"].ref, 15.0, 9.0, 0)
    P(b["cbulk"].ref, 34.6, 11.0, 0)
    P(b["jp"].ref, 37.5, 2.6, 0)
    P(b["rscl"].ref, 37.5, 5.4, 0)
    P(b["rsda"].ref, 37.5, 7.6, 0)
    # encoders: pins towards the centre column, MIX pins down
    pos = [(11, 20, 90), (39, 20, 270), (25, 34, 0), (11, 48, 90), (39, 48, 270)]
    for e, (x, y, r) in zip(encs, pos):
        P(e["enc"].ref, x, y, r)
    # encoder filters
    P(encs[0]["rn"].ref, 21.4, 20.0, 0)
    P(encs[0]["ca"].ref, 23.8, 22.4, 90)
    P(encs[0]["cb"].ref, 23.8, 17.6, 90)
    P(encs[1]["rn"].ref, 28.6, 20.0, 180)
    P(encs[1]["ca"].ref, 26.2, 17.6, 90)
    P(encs[1]["cb"].ref, 26.2, 22.4, 90)
    P(encs[2]["rn"].ref, 25.0, 44.6, 270)
    P(encs[2]["ca"].ref, 22.0, 44.6, 90)
    P(encs[2]["cb"].ref, 28.0, 44.6, 90)
    P(encs[3]["rn"].ref, 21.4, 48.6, 0)
    P(encs[3]["ca"].ref, 21.4, 52.0, 0)
    P(encs[3]["cb"].ref, 23.9, 48.0, 90)
    P(encs[4]["rn"].ref, 28.6, 48.6, 180)
    P(encs[4]["ca"].ref, 26.1, 48.0, 90)
    P(encs[4]["cb"].ref, 28.6, 52.0, 0)
    # MCP23017 in the left gap, address jumpers + RESET pull-up in the right gap
    P(m["u"].ref, 8.5, 34.0, 90)
    P(m["c"].ref, 12.6, 39.6, 0)
    place_addr_jumpers(d, m, 40.5, 31.8)
    P(m["rr"].ref, 45.0, 34.6, 0)
    # indicators along the bottom edge: [JP][R][LED], lamp wire pads below; footswitch pads in the corners
    place_indicators(d, leds, [10.0 + 10.0 * k for k in range(4)],
                     names=["CHORUS", "FLANGER", "PHASER", "TREMOLO"])
    place_corner_switches(d, sw_mode, sw_on)
    d.texts.append(("EFFECT", 25.0, 55.3, 2.5, "F.SilkS"))
    d.texts.append(("ADDR 0x23", 40.5, 27.7, 1.0, "F.SilkS"))
    d.texts.append(("YTHR Effect rev A", 25.0, 30.0, 1.0, "B.SilkS"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

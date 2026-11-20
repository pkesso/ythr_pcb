"""Echo pedal board (MCP23017 at 0x24): 5 encoders, TAPE / DIGI indicators, TAP button, MODE and ON footswitches.

Pin assignment as in feather/doc/wiring.md (Echo - 0x24). Board 50 x 65 mm, top edge = RJ45 side. Encoders in a
28 mm square with MIX in the centre (FDBK, TIME on top; BASS, TREBLE below), as on the prototype; the small TAP
button sits between the two indicators. Pushing the TIME knob is a second TAP: its push switch is wired in parallel
with the TAP button (GPB7 to GND).

    ../../.venv/Scripts/python gen_echo.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, GND, bus, mcp, encoder, indicator, switch  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_bus, place_indicators, place_corner_switches, place_addr_jumpers  # noqa: E402


def design():
    d = Design("echo", "YTHR - Echo (0x24)", addr=0x24)
    d.width, d.height = 50.0, 65.0
    d.mask = "White"
    used = {}
    b = bus(d)
    encs = []
    for label, a, bb in (("FDBK", "GPA1", "GPB1"), ("TIME", "GPA2", "GPB2"), ("MIX", "GPA3", "GPB3"),
                         ("BASS", "GPA4", "GPB4"), ("TREBLE", "GPA5", "GPB5")):
        encs.append(encoder(d, label, a, bb))
        used[a], used[bb] = a, bb
    leds = [indicator(d, "TAPE", "GPB6"), indicator(d, "DIGI", "GPA6")]
    used["GPB6"], used["GPA6"] = "GPB6", "GPA6"
    sw_mode = switch(d, "MODE", "GPB0")
    sw_on = switch(d, "ON", "GPA0")
    sw_tap = switch(d, "TAP", "GPB7")
    time = encs[1]["enc"]                      # TIME's push switch duplicates TAP
    time.pins["S1"], time.pins["S2"] = "GPB7", GND
    used["GPB0"], used["GPA0"], used["GPB7"] = "GPB0", "GPA0", "GPB7"
    m = mcp(d, used)

    P = d.place
    place_bus(d, b)
    pos = [(11, 20, 90), (39, 20, 270), (25, 34, 0), (11, 48, 90), (39, 48, 270)]
    for e, (x, y, r) in zip(encs, pos):
        P(e["enc"].ref, x, y, r)
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
    P(m["u"].ref, 8.5, 34.0, 90)
    P(m["c"].ref, 12.6, 39.6, 0)
    place_addr_jumpers(d, m, 40.5, 31.8)
    P(m["rr"].ref, 45.0, 34.6, 0)
    place_indicators(d, leds, [15.0, 40.0], names=["TAPE", "DIGITAL"])
    P(sw_tap.ref, 23.73, 62.9, 90)                 # TAP button wires, between the indicators
    place_corner_switches(d, sw_mode, sw_on)
    d.texts.append(("ECHO", 25.0, 55.3, 2.5, "F.SilkS"))
    d.texts.append(("ADDR 0x24", 40.5, 27.7, 1.0, "F.SilkS"))
    d.texts.append(("YTHR Echo rev A", 25.0, 30.0, 1.0, "B.SilkS"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

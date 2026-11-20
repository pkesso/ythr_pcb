"""Presets board (MCP23017 at 0x26): 5 preset footswitches (GPA0-4). No indicators on this board (the firmware's
preset LEDs on GPB0-4 are left unconnected).

Pin assignment as in feather/doc/wiring.md (Presets - 0x26). Board 200 x 50 mm, top edge = RJ45 side (RJ45
25 mm right of centre). The board hangs on the five footswitches: 12.5 mm holes (M12) at y = 36 mm, 45 mm
pitch, the third one in the centre, with a 20 mm nut area kept free of parts and tracks; above each one a plain
18 mm hole as close as possible (its edge 1 mm from the nut area: centre 20 mm higher, y = 16 mm). The
footswitch wire pads sit next to the M12 hole.

    ../../.venv/Scripts/python gen_presets.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, bus, mcp, switch  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_bus, place_addr_jumpers  # noqa: E402


def design():
    d = Design("presets", "YTHR - Presets (0x26)", addr=0x26)
    d.width, d.height = 200.0, 50.0
    d.mask = "Black"
    used = {}
    b = bus(d)
    sws = []
    for k in range(5):
        sws.append(switch(d, f"SW{k + 1}", f"GPA{k}"))
        used[f"GPA{k}"] = f"GPA{k}"
    m = mcp(d, used)
    holes = [d.add("H", "HOLE12", {}, "mech") for _ in range(5)]
    holes18 = [d.add("H", "HOLE18", {}, "mech") for _ in range(5)]

    P = d.place
    place_bus(d, b, x=125.0)
    xs = [100.0 + 45.0 * (k - 2) for k in range(5)]      # 10, 55, 100, 145, 190: footswitch holes
    for h, x in zip(holes, xs):
        P(h.ref, x, 36.0, 0)
        d.keep_circles.append((x, 36.0, 10.0))           # nut / washer area: no tracks
    for h, x in zip(holes18, xs):
        P(h.ref, x, 36.0 - (9.0 + 10.0 + 1.0), 0)        # 18 mm hole: 1 mm from the M12 nut area (y = 16)
    P(m["u"].ref, 77.5, 25.0, 90)
    P(m["c"].ref, 78.5, 30.6, 0)
    place_addr_jumpers(d, m, 67.0, 19.0)
    P(m["rr"].ref, 67.0, 27.6, 0)
    for k, (sw, x) in enumerate(zip(sws, xs)):
        P(sw.ref, x + 12.0 if k < 4 else x - 12.0, 34.73, 0)  # wire pads next to the button, outside the nut area
    d.texts.append(("PRESETS", 77.5, 42.0, 3.5, "F.SilkS"))
    d.texts.append(("ADDR 0x26", 70.0, 13.9, 1.0, "F.SilkS"))
    d.texts.append(("YTHR Presets rev A", 77.5, 40.0, 1.0, "B.SilkS"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

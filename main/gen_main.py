"""Main board (two MCP23017: Main1 at 0x20, Main2 at 0x21): 9 encoders, flavor toggle with 3 LEDs, 4 battery LEDs,
2-digit 7-segment display on two 74HC595, Feather RP2040 USB Host on sockets, 6 bus jacks.

Pin assignment as in feather/src/bindings.cpp (Main1 / Main2, SEG7). Board 180 x 110 mm, held by the encoders.
Lower row: 5 encoders on a 30 mm pitch, 25 mm above the bottom edge, the middle one on the centre line; upper row:
4 encoders 25 mm higher, offset by 15 mm. Top: the display on the centre line, the flavor toggle (in an 18 mm hole:
its body does not fit in the 6 mm under the panel) with its LEDs on a 25 mm radius (blue straight above, green and
orange 30 degrees to either side), the battery LEDs in a row at the top right. Bottom side, soldered by hand: the
Feather sockets (USB-A towards the top edge; the Feather's own USB-C powers the system) and 6 RJ45 along the
bottom edge. Indicators are SMD LEDs only (no lamp pads).

    ../../.venv/Scripts/python gen_main.py [--reroute] [--no-checks]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from gen.design import Design, GND, V33, mcp, encoder  # noqa: E402
from gen.build import build  # noqa: E402
from gen.layout import place_addr_jumpers  # noqa: E402

W, H = 180.0, 110.0
LOWER = [("GAIN", 1, 1), ("MASTER", 1, 2), ("BASS", 1, 3), ("MID", 2, 4), ("TREBLE", 2, 5)]   # (label, chip, n)
UPPER = [("AMP", 2, 0), ("CAB", 2, 1), ("GUITAR", 2, 2), ("AUDIO", 2, 3)]                      # pinA = GPAn, B = GPBn
FLAVOR = [("BOUTIQUE", "GPA6", "LED_BLU", "R100", 90), ("MODERN", "GPB6", "LED_GRN", "R100", 120),
          ("CLASSIC", "GPB7", "LED_ORG", "R470", 60)]          # (name, Main1 pin, LED, resistor, angle on the arc)
BATTERY = ["GPB4", "GPA4", "GPB5", "GPA5"]                     # Main1, bar 1 (lowest) ... 4
SEG = ["A", "B", "C", "D", "E", "F", "G", "DP"]                # 74HC595 QA..QH -> segment (bit0 = a ... bit7 = dp)
Q_PIN = ["15", "1", "2", "3", "4", "5", "6", "7"]              # 74HC595 QA..QH
SEG_PIN = {"A": "7", "B": "6", "C": "4", "D": "2", "E": "1", "F": "9", "G": "10", "DP": "5"}   # FJ5161BH
FEATHER_X, FEATHER_TOP = 116.0, 2.0                            # Feather centre line, its USB-A end
RJ45_X = [25.0, 50.0, 75.0, 105.0, 130.0, 155.0]


def net(chip, pin):
    return f"M{chip}_{pin}"


def main_mcp(d, chip, addr, used):
    """mcp() for one of the two chips: the address, RESET and the chip's GPIO nets get the M1_ / M2_ prefix."""
    d.addr = addr
    m = mcp(d, {k: net(chip, k) for k in used})
    local = {"A0", "A1", "A2", "RESET"}
    for p in [m["u"], m["rr"]] + m["jp"]:
        p.pins = {k: (net(chip, v) if v in local else v) for k, v in p.pins.items()}
    return m


def led(d, label, gpio, kind="LED", res="R470"):
    """SMD indicator straight from the MCP pin: resistor -> LED -> GND."""
    n = d.counters.get("D", 0) + 1
    r = d.add("R", res, {"1": gpio, "2": f"LED{n}_A"}, f"led{n}")
    dd = d.add("D", kind, {"2": f"LED{n}_A", "1": GND}, f"led{n}")
    for p in (r, dd):
        p.ref_on_fab = True
    return dict(r=r, led=dd, name=label)


def place_led(d, l, x, y, name_dy=-2.0):
    d.place(l["led"].ref, x, y, 180)                    # anode (pad 2) towards the resistor
    d.place(l["r"].ref, x - (3.3 if "0805" in l["led"].cat["footprint"] else 3.0), y, 0)
    d.texts.append((l["name"], x, y + name_dy, 1.0, "F.SilkS"))


def design():
    d = Design("main", "YTHR - Main (0x20, 0x21)", addr=0x20)
    d.width, d.height = W, H
    d.mask = "Green"
    d.route_iterations = 150             # ~100 nets over 180 x 110 mm: the negotiation needs longer than a pedal's
    d.addrs = {1: 0x20, 2: 0x21}
    used = {1: set(), 2: set()}

    # ---------------------------------------------------------------- circuit
    encs = []
    for label, chip, k in LOWER + UPPER:
        encs.append(encoder(d, label, net(chip, f"GPA{k}"), net(chip, f"GPB{k}")))
        used[chip] |= {f"GPA{k}", f"GPB{k}"}
    flav = [led(d, name, net(1, pin), kind, res) for name, pin, kind, res, _ in FLAVOR]
    batt = [led(d, str(k + 1), net(1, pin)) for k, pin in enumerate(BATTERY)]
    used[1] |= {pin for _, pin, *_ in FLAVOR} | set(BATTERY)
    toggle = d.add("J", "WIRE3", {"1": net(1, "GPA0"), "2": GND, "3": net(1, "GPB0")}, "sw", silk="FLAVOR")
    used[1] |= {"GPA0", "GPB0"}
    hole = d.add("H", "HOLE18", {}, "mech")
    # 7-segment: Main2 GPB6 = serial data, GPA6 = shift clock, GPB7 = latch; the first 74HC595 (low byte) drives
    # the left digit; common anode, a segment is lit by a 0
    used[2] |= {"GPB6", "GPA6", "GPB7"}
    srs, digits, segr = [], [], []
    for k in range(2):
        ser = net(2, "GPB6") if k == 0 else "SR_CHAIN"
        pins = {"14": ser, "11": net(2, "GPA6"), "12": net(2, "GPB7"), "13": GND, "10": V33, "16": V33, "8": GND,
                "9": "SR_CHAIN" if k == 0 else None}
        for q_, s in zip(Q_PIN, SEG):
            pins[q_] = f"DIG{k + 1}_{s}_Q"
        srs.append((d.add("U", "HC595", pins, f"seg{k + 1}"), d.add("C", "C100n", {"1": V33, "2": GND}, f"seg{k + 1}")))
        rs = [d.add("R", "R470", {"1": f"DIG{k + 1}_{s}_Q", "2": f"DIG{k + 1}_{s}"}, f"seg{k + 1}") for s in SEG]
        for r in rs:
            r.ref_on_fab = True
        segr.append(rs)
        dpins = {SEG_PIN[s]: f"DIG{k + 1}_{s}" for s in SEG}
        dpins.update({"3": V33, "8": V33})
        digits.append(d.add("DS", "SEG7", dpins, f"seg{k + 1}"))
    m1 = main_mcp(d, 1, 0x20, used[1])
    m2 = main_mcp(d, 2, 0x21, used[2])
    d.addr = 0x20
    # Feather RP2040 USB Host: JP1 (16 pins: 13 GND, 14-15 3V3), JP3 (12 pins: 10 D5 = INT, 11 SCL, 12 SDA)
    jp1 = d.add("J", "SOCK16", {str(k): None for k in range(1, 17)} | {"13": GND, "14": V33, "15": V33}, "feather",
                silk="FEATHER")
    jp3 = d.add("J", "SOCK12", {str(k): None for k in range(1, 13)} | {"10": "INT", "11": "SCL", "12": "SDA"},
                "feather")
    pu = [d.add("R", "R2k2", {"1": V33, "2": "SCL"}, "bus"), d.add("R", "R2k2", {"1": V33, "2": "SDA"}, "bus"),
          d.add("R", "R10k", {"1": V33, "2": "INT"}, "bus")]
    cbulk = d.add("C", "C10u", {"1": V33, "2": GND}, "bus")
    jacks = []
    for _ in RJ45_X:
        j = d.add("J", "RJ45_HAND", {"1": GND, "2": "SCL", "3": GND, "4": "SDA", "5": GND, "6": V33, "7": GND,
                                     "8": "INT"}, "bus")
        tvs = d.add("U", "SRV05-4", {"1": "SCL", "2": GND, "3": "SDA", "4": "INT", "5": V33, "6": None}, "bus")
        jacks.append((j, tvs))

    # ---------------------------------------------------------------- placement (board mm, Y down)
    P = d.place
    lower_x = [30.0 + 30.0 * k for k in range(5)]
    upper_x = [45.0 + 30.0 * k for k in range(4)]
    for e, x in zip(encs[:5], lower_x):              # pins up, filter between the rows
        P(e["enc"].ref, x, 85.0, 180)
        P(e["rn"].ref, x, 74.8, 90)
        P(e["cb"].ref, x - 1.8, 72.3, 180)
        P(e["ca"].ref, x + 1.8, 72.3, 0)
    for e, x in zip(encs[5:], upper_x):              # pins down
        P(e["enc"].ref, x, 60.0, 0)
        P(e["rn"].ref, x, 70.2, 270)
        P(e["cb"].ref, x + 1.8, 72.7, 0)
        P(e["ca"].ref, x - 1.8, 72.7, 180)
    # flavor toggle in its hole, LEDs on the 25 mm arc; the toggle's wires on pads below the hole
    tx, ty = 40.0, 35.0
    P(hole.ref, tx, ty, 0)
    import math
    for l, (_, _, _, _, ang) in zip(flav, FLAVOR):
        a = math.radians(ang)
        place_led(d, l, round(tx + 25.0 * math.cos(a), 3), round(ty - 25.0 * math.sin(a), 3))
    P(toggle.ref, tx - 2.54, 47.5, 90)
    d.texts.append(("PREV", tx - 6.6, 47.5, 1.0, "F.SilkS"))
    d.texts.append(("NEXT", tx + 6.6, 47.5, 1.0, "F.SilkS"))
    for l, x in zip(batt, [140.0, 150.0, 160.0, 170.0]):
        place_led(d, l, x, 22.0)
    d.texts.append(("BATTERY", 155.0, 16.5, 1.5, "F.SilkS"))
    # display: two digits on the centre line, 74HC595 + segment resistors below each
    for k, (cx, (u, c), ds, rs) in enumerate(zip((83.0, 97.0), srs, digits, segr)):
        P(ds.ref, cx - 5.08, 25.0 + 7.62, 90)        # pin 1 bottom left, digit upright
        for i, r in enumerate(rs):
            P(r.ref, round(cx - 5.6 + 1.6 * i, 3), 37.6, 90)
        P(u.ref, cx, 45.0, 0)
        P(c.ref, cx + 5.6 if k == 0 else cx + 4.6, 42.0, 90)
    # each MCP next to its parts, away from the display in the middle: Main1 left of AMP (GAIN / MASTER / BASS,
    # the toggle, the flavor LEDs), Main2 on the top side between the Feather's socket rows (the Feather is under
    # the board): next to the 74HC595s, its encoder lines run down the corridor between GUITAR and AUDIO
    P(m1["u"].ref, 22.0, 60.0, 0)
    P(m1["c"].ref, 22.0, 67.6, 0)
    place_addr_jumpers(d, m1, 12.0, 55.0)
    P(m1["rr"].ref, 12.0, 64.4, 0)
    d.texts.append(("ADDR 0x20", 12.0, 50.6, 1.0, "F.SilkS"))
    P(m2["u"].ref, 115.5, 30.0, 0)               # its sides and bottom stay free for the lines going down
    P(m2["c"].ref, 112.5, 22.6, 0)
    place_addr_jumpers(d, m2, 122.8, 12.6)
    P(m2["rr"].ref, 122.8, 21.0, 0)
    d.texts.append(("ADDR 0x21", 115.0, 9.0, 1.0, "F.SilkS"))
    # Feather on the bottom side, seen through the board: JP1 on the left, pin 1 near the USB-A end
    x0 = FEATHER_X - 11.43
    P(jp1.ref, x0 + 1.27, FEATHER_TOP + 6.35, 180, "B")
    P(jp3.ref, x0 + 21.59, FEATHER_TOP + 34.29, 0, "B")
    # bus pull-ups under the Feather's far end, at the head of the free corridor between the upper encoders 105
    # and 135 / the lower 120 and 150 down to the jacks: the bus from the Feather is two short straight runs
    for k, r in enumerate(pu):                   # SCL, SDA, INT; signal pad (2) at the bottom
        P(r.ref, round(FEATHER_X + 4.0 + 1.6 * k, 3), 46.5, 270)
    P(cbulk.ref, x0 + 4.9, FEATHER_TOP + 39.4, 90)
    # bus jacks on the bottom side, plug openings flush with the bottom edge; TVS above each on the top side
    for (j, tvs), x in zip(jacks, RJ45_X):
        P(j.ref, x, H - 8.6, 180, "B")
        P(tvs.ref, x, 96.6, 0)
    d.texts.append(("MAIN", 155.0, 32.0, 2.5, "F.SilkS"))
    d.texts.append(("YTHR Main rev A", 155.0, 40.0, 1.0, "B.SilkS"))
    return d


if __name__ == "__main__":
    build(design(), HERE, checks="--no-checks" not in sys.argv, reroute="--reroute" in sys.argv)

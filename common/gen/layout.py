"""Placement helpers shared by the 50 x 65 mm pedal boards (top edge = RJ45 side)."""


def place_bus(d, b, x=25.0):
    """RJ45 on the bottom side, plug opening flush with the top edge; TVS left of it, bulk cap and the optional
    I2C pull-ups (jumper open) right of it."""
    P = d.place
    P(b["j"].ref, x, 8.6, 0, "B")
    P(b["tvs"].ref, x - 10.0, 9.0, 0)
    P(b["cbulk"].ref, x + 9.6, 11.0, 0)
    P(b["jp"].ref, x + 12.5, 2.6, 0)
    P(b["rscl"].ref, x + 12.5, 5.4, 0)
    P(b["rsda"].ref, x + 12.5, 7.6, 0)


def place_indicators(d, leds, xs, y=59.6, names=None, name_dx=0.0):
    """Indicator groups along the bottom edge: [JP][R][LED] in a row, the lamp's wire pads (+ -) below.
    names: full names printed above each on-board LED (centred on it, + name_dx)."""
    P = d.place
    for k, (led, x) in enumerate(zip(leds, xs)):
        P(led["jp"].ref, x - 6.2, y, 0)
        P(led["r"].ref, x - 3.0, y, 0)
        P(led["led"].ref, x, y, 180)
        P(led["j"].ref, x - 1.27, y + 3.3, 90)
        if names:
            d.texts.append((names[k], x + name_dx, y - 2.0, 1.0, "F.SilkS"))


def place_filter(d, e, x, y, rot):
    """An encoder's 4x10k array at (x, y) and its two 10n caps next to the series outputs: rot 0 (encoder on the
    left) has output B at the top right and A at the bottom right; rot 180 (encoder on the right) mirrors that."""
    P = d.place
    P(e["rn"].ref, x, y, rot)
    if rot == 0:
        P(e["cb"].ref, x + 0.9, y - 3.0, 0)
        P(e["ca"].ref, x + 0.9, y + 3.0, 0)
    else:
        P(e["cb"].ref, x - 0.9, y + 3.0, 0)
        P(e["ca"].ref, x - 0.9, y - 3.0, 0)


def place_corner_switches(d, left, right, width=50.0, y=62.9):
    """Footswitch wire pads (GPIO, GND) in the bottom corners, in the row of the lamp pads."""
    if left is not None:
        d.place(left.ref, 2.0, y, 90)
    if right is not None:
        d.place(right.ref, width - 4.54, y, 90)


def place_addr_jumpers(d, m, x, y0, pitch=2.8):
    """The three address jumpers in a column; left pad = GND (0), right pad = VCC (1) on every board,
    the factory bridge on the side of this board's bit. '0 1' printed above the column."""
    for k, (jp, bit) in enumerate(zip(m["jp"], m["bits"])):
        d.place(jp.ref, x, y0 + pitch * k, 180 if bit else 0)
        jp.silk_at = (-3.4, 0)              # 'A0'... just left of its jumper
    d.texts.append(("0", x - 1.3, y0 - 2.4, 1.0, "F.SilkS"))
    d.texts.append(("1", x + 1.3, y0 - 2.4, 1.0, "F.SilkS"))

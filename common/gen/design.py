"""Circuit description shared by the pedal boards: parts, nets, placement, and the standard blocks
(bus connector, MCP23017, encoder, indicator, footswitch). See pcb/README.md for the decisions behind them."""
from dataclasses import dataclass, field

from .parts import CATALOG

GND, V33 = "GND", "+3V3"

# MCP23017 SSOP-28 pin numbers
MCP_PIN = {**{f"GPB{i}": str(1 + i) for i in range(8)}, **{f"GPA{i}": str(21 + i) for i in range(8)},
           "VDD": "9", "VSS": "10", "NC11": "11", "SCL": "12", "SDA": "13", "NC14": "14",
           "A0": "15", "A1": "16", "A2": "17", "RESET": "18", "INTB": "19", "INTA": "20"}


@dataclass
class Part:
    ref: str
    kind: str
    pins: dict                      # pin number -> net name, or None for "not connected"
    group: str = ""                 # schematic group
    value: str = None
    x: float = None                 # board placement (mm, board coordinates, Y down)
    y: float = None
    rot: float = 0
    side: str = "F"
    silk: str = None                # functional label printed next to the part
    silk_at: tuple = None           # (dx, dy) of the functional label, relative to the part, or the side to put it
    #                                 on first ("above", "below", "left", "right")
    ref_on_fab: bool = False        # reference designator on the Fab layer only (crowded silkscreen)
    extra_fields: dict = field(default_factory=dict)
    db: dict = None                 # footswitch wire pads: their debounce parts (pu, rs, c), see switch()

    @property
    def cat(self):
        return CATALOG[self.kind]

    @property
    def val(self):
        return self.value or self.cat["value"]


class Design:
    def __init__(self, name, title, addr, rev="A"):
        self.name, self.title, self.addr, self.rev = name, title, addr, rev
        self.parts = {}
        self.counters = {}
        self.texts = []             # board texts: (text, x, y, size, layer)
        self.width = self.height = None
        self.keepouts = []          # extra router keepouts: (x0, y0, x1, y1, layers)
        self.keep_circles = []      # router keep-outs: (x, y, r), e.g. the nut area of a footswitch
        self.outline = None         # gen.outline.Outline of an enclosure template; None = plain width x height
        self.jlc_rules = False      # put JLCPCB's limits into the project (pcb/tools/jlcpcb_rules.py)
        self.rim = False            # print the template's rim (Outline.frame) on the silkscreen of both sides
        self.silk = None            # silkscreen colour (stackup); None = white, black on a white board

    def use_outline(self, outline):
        """Take the board outline (and its size) from an enclosure template; its holes become router keep-outs."""
        self.outline = outline
        self.width, self.height = outline.width, outline.height

    def ref(self, prefix):
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}{n}"

    def add(self, prefix, kind, pins, group="", **kw):
        r = self.ref(prefix)
        self.parts[r] = Part(r, kind, pins, group, **kw)
        return self.parts[r]

    def nets(self):
        out = {}
        for p in self.parts.values():
            for pin, n in p.pins.items():
                if n:
                    out.setdefault(n, []).append((p.ref, pin))
        return out

    def place(self, ref, x, y, rot=0, side="F"):
        p = self.parts[ref]
        p.x, p.y, p.rot, p.side = x, y, rot, side
        return p


# ---------------------------------------------------------------- blocks
def bus(d, buffer=False, ferrite=False):
    """8P8C bus jack (pinout: pcb/README.md, T568B wire order), TVS, bulk capacitor, optional I2C pull-ups.

    ferrite: the jack's VCC (net VBUS, clamped by the TVS) reaches the board's +3V3 through a ferrite bead; the bulk
    capacitor sits behind it. buffer: a PCA9511A hot-swap buffer between the bus (SCL / SDA, the jack side) and the
    board (SCL_L / SDA_L, 10k pull-ups to +3V3 on this side, as in the datasheet's application); ENABLE tied to VCC,
    READY unused. The MCP23017 then hangs on SCL_L / SDA_L (see b["scl"], b["sda"])."""
    vbus = "VBUS" if ferrite else V33
    j = d.add("J", "RJ45", {"1": GND, "2": "SCL", "3": GND, "4": "SDA", "5": GND, "6": vbus, "7": GND, "8": "INT"},
              "bus")
    tvs = d.add("U", "SRV05-4", {"1": "SCL", "2": GND, "3": "SDA", "4": "INT", "5": vbus, "6": None}, "bus")
    cb = d.add("C", "C10u", {"1": V33, "2": GND}, "bus")
    jp = d.add("JP", "SJ", {"1": V33, "2": "I2C_PU"}, "bus", silk="PU")
    rs = d.add("R", "R10k", {"1": "I2C_PU", "2": "SCL"}, "bus")
    rd = d.add("R", "R10k", {"1": "I2C_PU", "2": "SDA"}, "bus")
    b = dict(j=j, tvs=tvs, cbulk=cb, jp=jp, rscl=rs, rsda=rd, scl="SCL", sda="SDA")
    if ferrite:
        b["fb"] = d.add("FB", "FB600", {"1": vbus, "2": V33}, "bus")
    if buffer:
        b["buf"] = d.add("U", "PCA9511A", {"1": V33, "2": "SCL_L", "3": "SCL", "4": GND, "5": None, "6": "SDA",
                                           "7": "SDA_L", "8": V33}, "buffer")
        b["cbuf"] = d.add("C", "C100n", {"1": V33, "2": GND}, "buffer")
        b["rscl_l"] = d.add("R", "R10k", {"1": V33, "2": "SCL_L"}, "buffer")
        b["rsda_l"] = d.add("R", "R10k", {"1": V33, "2": "SDA_L"}, "buffer")
        b["scl"], b["sda"] = "SCL_L", "SDA_L"
    return b


def mcp(d, used, scl="SCL", sda="SDA"):
    """MCP23017 with decoupling, a 10k RESET pull-up and the address on three 3-pad solder jumpers (one per bit,
    pad 2 = Ax). Pad 1 is bridged at the factory and goes to this board's level of the bit (d.addr), pad 3 to the
    other rail: to change a bit, cut the 1-2 bridge and solder 2-3. `used` maps MCP pin names (GPA0...) to nets;
    unused GPIOs are left unconnected. scl / sda: the board's I2C nets (SCL_L / SDA_L behind a bus buffer)."""
    pins = {MCP_PIN["VDD"]: V33, MCP_PIN["VSS"]: GND, MCP_PIN["SCL"]: scl, MCP_PIN["SDA"]: sda,
            MCP_PIN["NC11"]: None, MCP_PIN["NC14"]: None, MCP_PIN["A0"]: "A0", MCP_PIN["A1"]: "A1",
            MCP_PIN["A2"]: "A2", MCP_PIN["RESET"]: "RESET", MCP_PIN["INTA"]: "INT", MCP_PIN["INTB"]: "INT"}
    for i in range(8):
        for port in "AB":
            name = f"GP{port}{i}"
            pins[MCP_PIN[name]] = used.get(name)
    u = d.add("U", "MCP23017", pins, "mcp")
    c = d.add("C", "C100n", {"1": V33, "2": GND}, "mcp")
    jp = []
    for k in range(3):
        bit = (d.addr - 0x20) >> k & 1
        # pad 1 carries the factory bridge; placed with rot 0 for a 0 (pad 1 left = GND) and rot 180 for a 1
        # (pad 1 right = VCC), so on every board left = GND = 0, right = VCC = 1 and the bridge shows the value
        jp.append(d.add("JP", "SJ3", {"1": V33 if bit else GND, "2": f"A{k}", "3": GND if bit else V33}, "mcp",
                        silk=f"A{k}"))
    rr = d.add("R", "R10k", {"1": V33, "2": "RESET"}, "mcp")
    bits = [(d.addr - 0x20) >> k & 1 for k in range(3)]
    return dict(u=u, c=c, jp=jp, rr=rr, bits=bits)


def encoder(d, label, gpa, gpb, first="B"):
    """EC11-type encoder (Bourns PEC11R, push switch unused) with the prototype's filter on each channel:
    10k pull-up -> 10k series -> 10n to GND at the MCP side. C and the mounting lugs go to GND.
    A = the MCP 'pinA' channel (gpa), B = 'pinB' (gpb), as in feather/src/bindings.cpp.

    4x10k array (R1 = 1-8, R2 = 2-7, R3 = 3-6, R4 = 4-5), laid out so that nothing crosses: the side facing the
    encoder carries the two channels in pairs (pins 1, 2 = `first` channel, pins 3, 4 = the other), the far side
    the series outputs on its ends (8 = first, 5 = other) and both pull-ups on +3V3 in the middle (7, 6)."""
    n = d.counters.get("ENC", 0) + 1
    a, b = f"ENC{n}_A", f"ENC{n}_B"
    e = d.add("ENC", "ENC", {"A": a, "B": b, "C": GND, "S1": None, "S2": None, "MP": GND}, f"enc{n}", silk=label)
    (r1, o1), (r2, o2) = ((b, gpb), (a, gpa)) if first == "B" else ((a, gpa), (b, gpb))
    rn = d.add("RN", "RN10k", {"1": r1, "8": o1, "2": r1, "7": V33, "3": r2, "6": V33, "4": r2, "5": o2}, f"enc{n}")
    ca = d.add("C", "C10n", {"1": gpa, "2": GND}, f"enc{n}")
    cb = d.add("C", "C10n", {"1": gpb, "2": GND}, f"enc{n}")
    return dict(enc=e, rn=rn, ca=ca, cb=cb)


def indicator(d, label, gpio):
    """Bezel lamp on wires (+ = GPIO, - = GND, resistor inside the lamp) and, in parallel, an on-board LED +
    470R behind a solder jumper that is open by default."""
    n = d.counters.get("D", 0) + 1
    j = d.add("J", "WIRE2", {"1": gpio, "2": GND}, f"led{n}", silk=label)
    jp = d.add("JP", "SJ", {"1": gpio, "2": f"LED{n}_J"}, f"led{n}")
    r = d.add("R", "R470", {"1": f"LED{n}_J", "2": f"LED{n}_A"}, f"led{n}")
    led = d.add("D", "LED", {"2": f"LED{n}_A", "1": GND}, f"led{n}")
    for p in (j, jp, r, led):
        p.ref_on_fab = True          # silkscreen keeps only the indicator's names
    return dict(j=j, jp=jp, r=r, led=led)


def switch(d, label, gpio, debounce=False):
    """Panel-mounted footswitch / push button on wires: GPIO and GND.

    debounce: hardware debounce in front of the MCP pin (firmware debounces too): 10k pull-up to +3V3 on the wire
    pad, 1k in series to the GPIO, 100n from the GPIO to GND. Release: tau = 11k x 100n = 1.1 ms; press: the 1k
    limits the capacitor's discharge through the contact (tau = 0.1 ms). Returns the wire pads' part; the RC parts
    are in d.parts (group "sw")."""
    if not debounce:
        return d.add("J", "WIRE2", {"1": gpio, "2": GND}, "sw", silk=label)
    n = d.counters.get("SWDB", 0) + 1
    d.counters["SWDB"] = n
    pad = f"SW{n}"
    j = d.add("J", "WIRE2", {"1": pad, "2": GND}, "sw", silk=label)
    j.db = dict(pu=d.add("R", "R10k", {"1": V33, "2": pad}, "sw"),
                rs=d.add("R", "R1k", {"1": pad, "2": gpio}, "sw"),
                c=d.add("C", "C100n", {"1": gpio, "2": GND}, "sw"))
    return j


def testpoint(d, label, net):
    """Test pad (1.5 mm, bottom side by default: reachable with the board in the enclosure), labelled."""
    return d.add("TP", "TP", {"1": net}, "tp", silk=label)

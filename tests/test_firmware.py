"""The boards against the firmware: the circuit on each generated board (nets read from the .kicad_pcb pads) must
be what feather/src/bindings.cpp and config.h expect for that pedal.

    .venv/Scripts/python -m unittest discover -s pcb/tests -t pcb/tests -k Firmware -v

- the address set by the factory bridges of the jumpers = 0x20 + the pedal's chip index in config.h;
- every encoder binding {chip, pinA, pinB}: the MCP pin pinA reaches the encoder's pad A through a series resistor,
  pinB reaches pad B; each channel has a pull-up to +3V3 and a capacitor to GND at the MCP side;
- every button binding: the MCP pin goes to a wire pad of a footprint that also has a GND pad (and, as a
  duplicate, possibly an encoder's push switch to GND: Echo TIME = TAP);
- every LED binding: the MCP pin goes to the lamp's `+` wire pad and, through a solder jumper and a resistor, to
  an LED's anode; the LED cathode and the lamp's `-` are GND (Main: SMD LEDs only, resistor straight from the pin);
- the 7-segment binding (Main2): SDI to the first 74HC595's SER, SCLK / LOAD to both, the first one's QA..QH through
  resistors to the left digit's a..dp, the second one's to the right digit, common anodes on +3V3;
- no MCP GPIO is connected without a binding; the fixed pins (VDD, VSS, SCL, SDA, INTA/INTB, RESET) are wired as
  the bus expects (RJ45 pins 2 / 4 / 8, RESET pulled up); on Main the Feather's SCL / SDA / D5 too.

A board can carry several chips (Main: Main1 and Main2); each MCP is matched to its chip by its address jumpers.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardfile import BOARDS, PCB, Board, design, path  # noqa: E402

FW = os.path.join(os.path.dirname(PCB), "feather", "src")
# firmware bindings deliberately not fitted on a board: {board: {"leds" / "buttons" / "encoders": reason}}
NOT_FITTED = {"presets": {"leds": "Presets has no indicators (removed for the 18 mm holes); GPB0-4 stay free"}}
CHIP_OF = {"effect": ["CHIP_EFFECT"], "gate_comp": ["CHIP_GATECOMP"], "echo": ["CHIP_ECHO"],
           "reverb": ["CHIP_REVERB"], "presets": ["CHIP_PRESETS"], "main": ["CHIP_MAIN1", "CHIP_MAIN2"]}
SMD_LEDS_ONLY = {"main"}                 # indicators without lamp pads and jumpers
LED_RESISTORS = {"470", "100"}           # 100 for the blue and green LEDs (higher forward voltage)
SEG = ["A", "B", "C", "D", "E", "F", "G", "DP"]
SEG_PIN = {"A": "7", "B": "6", "C": "4", "D": "2", "E": "1", "F": "9", "G": "10", "DP": "5"}   # FJ5161BH
Q_PIN = ["15", "1", "2", "3", "4", "5", "6", "7"]                                               # 74HC595 QA..QH


def mcp_pin(gpio):
    """bindings.cpp pin index (GPA0..7 = 0..7, GPB0..7 = 8..15) -> MCP23017 SSOP-28 pin number."""
    return str(21 + gpio) if gpio < 8 else str(1 + gpio - 8)


def firmware():
    """{chip name: index} from config.h, the ENCODERS / BUTTONS / LEDS tables and SEG7 from bindings.cpp."""
    with open(os.path.join(FW, "config.h"), encoding="utf-8") as fh:
        cfg = fh.read()
    chips = {m.group(1): int(m.group(2)) for m in re.finditer(r"\b(CHIP_[A-Z0-9]+)\s*=\s*(\d+)", cfg)}
    with open(os.path.join(FW, "bindings.cpp"), encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src)

    def table(name):
        body = re.search(name + r"\[\]\s*=\s*\{(.*?)\n\};", src, re.S).group(1)
        return [[x.strip() for x in m.group(1).split(",")] for m in re.finditer(r"\{([^{}]+)\}", body)]
    enc = [(c, int(a), int(b), t) for c, a, b, t in table("ENCODERS")]
    btn = [(c, int(p), a) for c, p, a in table("BUTTONS")]
    led = [(c, int(p), cnd) for c, p, cnd in table("LEDS")]
    c, sdi, sclk, load = [x.strip() for x in re.search(r"SEG7\s*=\s*\{([^{}]+)\}", src).group(1).split(",")]
    return chips, enc, btn, led, (c, int(sdi), int(sclk), int(load))


class Net:
    """Pads per net and nets per pad of a board, from the .kicad_pcb."""

    def __init__(self, board):
        self.b = board
        self.pads = {}
        self.at = {}
        for fp in board.footprints:
            for p in fp.pads:
                if p.net:
                    self.pads.setdefault(p.net, []).append((fp.ref, p.num))
                    self.at[(fp.ref, p.num)] = p.net
        self.kind = {fp.ref: fp.lib_id.split(":", 1)[1] for fp in board.footprints}
        self.value = {fp.ref: fp.props.get("Value") for fp in board.footprints}

    def net(self, ref, num):
        return self.at.get((ref, num))

    def on(self, net, prefix=""):
        return [(r, n) for r, n in self.pads.get(net, []) if r.startswith(prefix)]

    def other(self, ref, num):
        """The net on the other pad of a two-pad part."""
        return self.net(ref, "2" if num == "1" else "1")

    def mcps(self):
        return sorted(f.ref for f in self.b.footprints if "SSOP-28" in f.lib_id)

    def fps(self, lib):
        return sorted((f for f in self.b.footprints if lib in f.lib_id), key=lambda f: f.x)

    def jumper(self, u, k):
        """The address jumper of bit k: its pad 2 is on the MCP's A<k> pin."""
        jp = [r for r, num in self.on(self.net(u, str(15 + k)), "JP") if num == "2"]
        assert len(jp) == 1, f"{u} A{k}: one jumper"
        return jp[0]

    def address(self, u):
        """The MCP's address as the factory bridges set it: pad 1 of each jumper is the bit's level."""
        return 0x20 | sum((self.net(self.jumper(u, k), "1") == "/+3V3") << k for k in range(3))


RN_PAIR = {str(k): str(9 - k) for k in range(1, 9)}    # 4x resistor array: pin k <-> pin 9-k


class Firmware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chips, cls.enc, cls.btn, cls.led, cls.seg7 = firmware()

    def boards(self):
        for d in BOARDS:
            yield d, design(d), Net(Board(path(d, ".kicad_pcb")))

    def chips_on(self, d, n):
        """[(chip name, MCP ref)] of a board, each MCP matched to its chip by its address."""
        by_addr = {0x20 + self.chips[c]: c for c in CHIP_OF[d[2]]}
        return [(by_addr.get(n.address(u)), u) for u in n.mcps()]

    def test_address(self):
        for d, des, n in self.boards():
            with self.subTest(board=d[2]):
                want = sorted(0x20 + self.chips[c] for c in CHIP_OF[d[2]])
                self.assertEqual(want, sorted(getattr(des, "addrs", {0: des.addr}).values()))
                self.assertEqual(want, sorted(n.address(u) for u in n.mcps()))
                for u in n.mcps():
                    for k in range(3):
                        jp = n.jumper(u, k)
                        self.assertEqual({"/+3V3", "/GND"}, {n.net(jp, "1"), n.net(jp, "3")}, f"{u} A{k}")

    def test_encoders(self):
        for d, des, n in self.boards():
            with self.subTest(board=d[2]):
                chips = self.chips_on(d, n)
                encs = {r for r, _ in n.at if r.startswith("ENC")}
                self.assertEqual(len(encs), len([1 for c, *_ in self.enc if c in dict(chips)]), "encoder count")
                seen = set()
                for chip, u in chips:
                    seen |= self.check_encoders(n, chip, u)
                self.assertEqual(encs, seen)

    def check_encoders(self, n, chip, u):
        seen = set()
        for c, a, b, target in self.enc:
            if c != chip:
                continue
            found = {}
            for ch, gpio in (("A", a), ("B", b)):
                out = n.net(u, mcp_pin(gpio))
                self.assertIsNotNone(out, f"{target}: MCP pin for GPIO {gpio} unconnected")
                # capacitor to GND on the MCP side
                caps = [r for r, num in n.on(out, "C") if n.other(r, num) == "/GND"]
                self.assertTrue(caps, f"{target} {ch}: no capacitor to GND on {out}")
                # series resistor (array pin pair) to the encoder pad
                raw = None
                for r, num in n.on(out, "RN"):
                    other = n.net(r, RN_PAIR[num])
                    enc_pads = n.on(other, "ENC")
                    if enc_pads:
                        raw = other
                        found[ch] = enc_pads
                self.assertIsNotNone(raw, f"{target} {ch}: no series resistor from {out} to an encoder")
                # pull-up: another array resistor from the raw net to +3V3
                pu = [r for r, num in n.on(raw, "RN") if n.net(r, RN_PAIR[num]) == "/+3V3"]
                self.assertTrue(pu, f"{target} {ch}: no pull-up on {raw}")
            (ea, pa), = found["A"]
            (eb, pb), = found["B"]
            self.assertEqual(ea, eb, f"{target}: A and B go to different encoders")
            self.assertEqual(("A", "B"), (pa, pb), f"{target}: pinA must be pad A, pinB pad B ({ea})")
            self.assertEqual("/GND", n.net(ea, "C"), f"{ea}: common pin")
            seen.add(ea)
        return seen

    def test_buttons(self):
        for d, des, n in self.boards():
            with self.subTest(board=d[2]):
                for chip, u in self.chips_on(d, n):
                    for c, gpio, action in self.btn:
                        if c != chip:
                            continue
                        net = n.net(u, mcp_pin(gpio))
                        if self.debounced(n, u, gpio, net, action):
                            continue
                        wires = [(r, num) for r, num in n.on(net, "J") if "WirePads" in n.kind[r]
                                 and "/GND" in {n.net(r, k) for k in ("1", "2", "3")}]
                        self.assertEqual(1, len(wires), f"{action}: wire pads on {net}")
                        # only the MCP, the wire pad and encoder push switches closing to GND on a button net
                        pushes = [(r, num) for r, num in n.on(net, "ENC") if num == "S1" and n.net(r, "S2") == "/GND"]
                        self.assertEqual(sorted([(u, mcp_pin(gpio)), wires[0]] + pushes), sorted(n.pads[net]),
                                         action)

    def debounced(self, n, u, gpio, net, action):
        """A hardware-debounced button (Gate + Comp): MCP pin - 1k - wire pad, 100n from the pin to GND, 10k from
        the pad to +3V3, the pad's other pin on GND; nothing else on either net. False when the pin has no 1k."""
        series = [(r, num) for r, num in n.on(net, "R") if n.value[r] == "1k"]
        if not series:
            return False
        self.assertEqual(1, len(series), f"{action}: one series resistor")
        caps = [(r, num) for r, num in n.on(net, "C") if n.value[r] == "100n" and n.other(r, num) == "/GND"]
        self.assertEqual(1, len(caps), f"{action}: 100n to GND at the pin")
        self.assertEqual(sorted([(u, mcp_pin(gpio)), series[0], caps[0]]), sorted(n.pads[net]), action)
        pad = n.other(*series[0])
        wires = [(r, num) for r, num in n.on(pad, "J") if "WirePads" in n.kind[r]
                 and n.other(r, num) == "/GND"]
        self.assertEqual(1, len(wires), f"{action}: wire pads behind the 1k")
        pull = [(r, num) for r, num in n.on(pad, "R") if n.value[r] == "10k" and n.other(r, num) == "/+3V3"]
        self.assertEqual(1, len(pull), f"{action}: 10k pull-up on the pad")
        far = (series[0][0], "2" if series[0][1] == "1" else "1")
        self.assertEqual(sorted([far, wires[0], pull[0]]), sorted(n.pads[pad]), f"{action}: pad net")
        return True

    def test_echo_time_push_is_tap(self):
        """Echo: pushing the TIME knob = the TAP button (its switch on the EchoTap GPIO, other side GND)."""
        d = next(x for x in BOARDS if x[2] == "echo")
        n = Net(Board(path(d, ".kicad_pcb")))
        (chip, u), = self.chips_on(d, n)
        tap = next(p for c, p, a in self.btn if c == chip and a == "BtnAction::EchoTap")
        pin_a = next(a for c, a, b, t in self.enc if c == chip and t == "EncTarget::EchoTime")
        r, num = n.on(n.net(u, mcp_pin(pin_a)), "RN")[0]                    # TIME's series resistor
        (enc, _), = n.on(n.net(r, RN_PAIR[num]), "ENC")
        self.assertEqual(n.net(u, mcp_pin(tap)), n.net(enc, "S1"), f"{enc} S1 = TAP")
        self.assertEqual("/GND", n.net(enc, "S2"), f"{enc} S2 = GND")

    def test_leds(self):
        for d, des, n in self.boards():
            if "leds" in NOT_FITTED.get(d[2], {}):
                continue
            with self.subTest(board=d[2]):
                for chip, u in self.chips_on(d, n):
                    for c, gpio, cond in self.led:
                        if c == chip:
                            check = self.check_smd_led if d[2] in SMD_LEDS_ONLY else self.check_lamp_led
                            check(n, u, gpio, cond)

    def check_smd_led(self, n, u, gpio, cond):
        """MCP pin -> resistor -> LED anode; nothing else on the pin's net."""
        net = n.net(u, mcp_pin(gpio))
        res = [(r, num) for r, num in n.on(net, "R")]
        self.assertEqual(sorted([(u, mcp_pin(gpio))] + res), sorted(n.pads[net]), cond)
        self.assertEqual(1, len(res), f"{cond}: one resistor on {net}")
        self.assertIn(n.value[res[0][0]], LED_RESISTORS, f"{cond}: LED resistor")
        anode = n.other(*res[0])
        leds = [r for r, num in n.on(anode, "D") if num == "2" and not r.startswith("DS")]
        self.assertEqual(1, len(leds), f"{cond}: LED anode on {anode}")
        self.assertEqual("/GND", n.net(leds[0], "1"), f"{cond}: LED cathode")

    def check_lamp_led(self, n, u, gpio, cond):
        """MCP pin -> lamp wire pad, and -> open jumper -> resistor -> LED anode."""
        net = n.net(u, mcp_pin(gpio))
        lamp = [r for r, num in n.on(net, "J") if num == "1" and n.net(r, "2") == "/GND"]
        self.assertEqual(1, len(lamp), f"{cond}: lamp wire pads on {net}")
        jps = [(r, num) for r, num in n.on(net, "JP")]
        self.assertEqual(1, len(jps), f"{cond}: one LED jumper on {net}")
        mid = n.other(*jps[0])
        res = [r for r, _ in n.on(mid, "R")]
        self.assertEqual(1, len(res), f"{cond}: resistor after the jumper")
        self.assertEqual("470", n.value[res[0]], f"{cond}: LED resistor")
        anode_net = next(x for x in (n.net(res[0], "1"), n.net(res[0], "2")) if x != mid)
        leds = [r for r, num in n.on(anode_net, "D") if num == "2"]
        self.assertEqual(1, len(leds), f"{cond}: LED anode on {anode_net}")
        self.assertEqual("/GND", n.net(leds[0], "1"), f"{cond}: LED cathode")

    def test_seg7(self):
        chip, sdi, sclk, load = self.seg7
        for d, des, n in self.boards():
            us = {c: u for c, u in self.chips_on(d, n)}
            if chip not in us:
                continue
            with self.subTest(board=d[2]):
                u = us[chip]
                srs = [f.ref for f in n.fps("SOIC-16")]
                first = [r for r in srs if n.net(r, "14") == n.net(u, mcp_pin(sdi))]
                self.assertEqual(1, len(first), "SDI -> one 74HC595 SER")
                second = [r for r in srs if n.net(r, "14") == n.net(first[0], "9")]
                self.assertEqual(1, len(second), "QH' -> the second 74HC595 SER")
                digits = [f.ref for f in n.fps("7Segment")]           # left to right
                self.assertEqual(2, len(digits))
                for sr, ds in zip(first + second, digits):              # low byte = left digit
                    self.assertEqual(n.net(u, mcp_pin(sclk)), n.net(sr, "11"), f"{sr} SRCLK")
                    self.assertEqual(n.net(u, mcp_pin(load)), n.net(sr, "12"), f"{sr} RCLK")
                    self.assertEqual(["/GND", "/+3V3", "/+3V3", "/GND"], [n.net(sr, k) for k in ("13", "10", "16", "8")],
                                     f"{sr}: OE, SRCLR, VCC, GND")
                    self.assertEqual(["/+3V3", "/+3V3"], [n.net(ds, "3"), n.net(ds, "8")], f"{ds}: common anode")
                    for q_, seg in zip(Q_PIN, SEG):
                        res = n.on(n.net(sr, q_), "R")
                        self.assertEqual(1, len(res), f"{sr} Q -> {seg}: one resistor")
                        self.assertEqual(n.net(ds, SEG_PIN[seg]), n.other(*res[0]), f"{sr} -> {ds} {seg}")

    def test_no_unbound_gpio(self):
        for d, des, n in self.boards():
            with self.subTest(board=d[2]):
                for chip, u in self.chips_on(d, n):
                    bound = {a for c, a, b, t in self.enc if c == chip} | {b for c, a, b, t in self.enc if c == chip}
                    bound |= {p for c, p, _ in self.btn if c == chip}
                    if "leds" not in NOT_FITTED.get(d[2], {}):
                        bound |= {p for c, p, _ in self.led if c == chip}
                    if self.seg7[0] == chip:
                        bound |= set(self.seg7[1:])
                    connected = {g for g in range(16)
                                 if n.net(u, mcp_pin(g)) and not n.net(u, mcp_pin(g)).startswith("unconnected-")}
                    self.assertEqual(sorted(bound), sorted(connected), u)

    def test_mcp_fixed_pins(self):
        for d, des, n in self.boards():
            j = n.fps("RJ45")[0].ref
            for u in n.mcps():
                with self.subTest(board=d[2], mcp=u):
                    self.assertEqual("/+3V3", n.net(u, "9"))
                    self.assertEqual("/GND", n.net(u, "10"))
                    self.assertEqual(n.net(j, "2"), self.through_buffer(n, n.net(u, "12"), "2", "3"), "SCL = RJ45 pin 2")
                    self.assertEqual(n.net(j, "4"), self.through_buffer(n, n.net(u, "13"), "7", "6"), "SDA = RJ45 pin 4")
                    self.assertEqual(n.net(j, "8"), n.net(u, "19"), "INTB = RJ45 pin 8")
                    self.assertEqual(n.net(j, "8"), n.net(u, "20"), "INTA = RJ45 pin 8")
                    reset = n.net(u, "18")
                    self.assertTrue([r for r, num in n.on(reset, "R") if n.other(r, num) == "/+3V3"], "RESET pull-up")
            for f in n.fps("RJ45"):
                with self.subTest(board=d[2], jack=f.ref):
                    self.assertEqual([n.net(j, k) for k in ("2", "4", "8")], [n.net(f.ref, k) for k in ("2", "4", "8")])
                    vcc = n.net(f.ref, "6")
                    if vcc != "/+3V3":          # through a ferrite bead (Gate + Comp)
                        fb = [(r, num) for r, num in n.on(vcc, "FB") if n.other(r, num) == "/+3V3"]
                        self.assertEqual(1, len(fb), "RJ45 pin 6 = VCC through one ferrite bead")
                    for k in ("1", "3", "5", "7"):
                        self.assertEqual("/GND", n.net(f.ref, k), f"RJ45 pin {k} = GND")

    def through_buffer(self, n, net, out_pin, in_pin):
        """The bus net an MCP line reaches: the net itself, or - behind a PCA9511A hot-swap buffer (Gate + Comp) -
        the buffer's IN pin for the OUT pin the line is on. The buffer: ENABLE and VCC on +3V3, GND, and a 10k
        pull-up to +3V3 on the card side."""
        bufs = [r for r, num in self.on_kind(n, net, "TSSOP-8") if num == out_pin]
        if not bufs:
            return net
        u = bufs[0]
        self.assertEqual(("/+3V3", "/+3V3", "/GND"), (n.net(u, "1"), n.net(u, "8"), n.net(u, "4")), u)
        self.assertTrue([r for r, num in n.on(net, "R") if n.value[r] == "10k" and n.other(r, num) == "/+3V3"],
                        f"{net}: card-side pull-up")
        return n.net(u, in_pin)

    @staticmethod
    def on_kind(n, net, kind):
        return [(r, num) for r, num in n.pads.get(net, []) if kind in n.kind[r]]

    def test_feather(self):
        """Main: the Feather's SCL (JP3.11) / SDA (JP3.12) / D5 = INT (JP3.10) on the bus, GND (JP1.13), 3V3
        (JP1.14-15); bus pull-ups 2.2k on SCL / SDA, 10k on INT."""
        for d, des, n in self.boards():
            s16, s12 = n.fps("PinSocket_1x16"), n.fps("PinSocket_1x12")
            if not s16:
                continue
            with self.subTest(board=d[2]):
                j, a, b = n.fps("RJ45")[0].ref, s16[0].ref, s12[0].ref
                self.assertEqual([n.net(j, k) for k in ("2", "4", "8")], [n.net(b, k) for k in ("11", "12", "10")])
                self.assertEqual(["/GND", "/+3V3", "/+3V3"], [n.net(a, k) for k in ("13", "14", "15")])
                for k, val in (("2", "2.2k"), ("4", "2.2k"), ("8", "10k")):
                    pu = [r for r, num in n.on(n.net(j, k), "R") if n.other(r, num) == "/+3V3"]
                    self.assertEqual([val], [n.value[r] for r in pu], f"pull-up on RJ45 pin {k}")


if __name__ == "__main__":
    unittest.main()

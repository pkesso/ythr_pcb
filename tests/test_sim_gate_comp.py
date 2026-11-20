"""Circuit simulation (ngspice from KiCad) of the Gate + Comp board's input filters, built from the board's design
(gen_gate_comp.design(): part values and nets), with the expected values calculated from those parts.

    .venv/Scripts/python -m unittest discover -s tests -t tests -p "test_sim*" -v

MCP23017 inputs (datasheet DS20001952C, D031 / D041 / D070): GPIO are Schmitt-trigger inputs, VIL <= 0.2 VDD,
VIH >= 0.8 VDD; the weak pull-up (GPPU, which the firmware enables on inputs) sources 40-115 uA at 5 V, modelled
as a resistor of 43k .. 125k.
"""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardfile import BOARDS, design  # noqa: E402
from ngspice import Ngspice  # noqa: E402  (tools/, on sys.path via boardfile)

VDD = 3.3
VIL, VIH = 0.2 * VDD, 0.8 * VDD
R_WEAK = (43e3, 125e3)                     # MCP23017 weak pull-up, extremes (5 V / 115 uA, 5 V / 40 uA)
UNITS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6}

_ng = None


def ng():
    global _ng
    if _ng is None:
        _ng = Ngspice()
    return _ng


def value(part):
    """'10k' / '100n' / '470' -> float."""
    v = part.val
    return float(v[:-1]) * UNITS[v[-1]] if v[-1] in UNITS else float(v)


def gate_comp():
    return design(next(d for d in BOARDS if d[2] == "gate_comp"))


def run(lines, t_end, step=2e-6):
    r = ng().run(lines + [f".tran {step} {t_end} 0 {step}", ".end"], ["time", "gpio"])
    return r["time"], r["gpio"]


def crossing(t, v, level, after, rising):
    """First time after `after` the signal crosses `level` upwards (rising) or downwards."""
    i = np.searchsorted(t, after)
    hit = np.nonzero(v[i:] >= level)[0] if rising else np.nonzero(v[i:] <= level)[0]
    return t[i + hit[0]] if len(hit) else None


def switch_deck(r_pu, r_s, c, r_weak, ctl, v0=VDD):
    """Footswitch: wire pad pulled up by r_pu, r_s to the GPIO, c at the GPIO, the MCP weak pull-up there too.
    The contact is a switch to GND driven by `ctl` (PWL, 1 = closed)."""
    return ["* footswitch debounce", f"VDD vdd 0 {VDD}", f"RPU vdd pad {r_pu}", f"RS pad gpio {r_s}", f"C1 gpio 0 {c}",
            f"RW vdd gpio {r_weak}", f"VCTL ctl 0 PWL({ctl})", "S1 pad 0 ctl 0 SWC",
            ".model SWC SW(Ron=0.05 Roff=1e9 Vt=0.5 Vh=0.1)", f".ic v(gpio)={v0} v(pad)={v0}"]


class FootswitchDebounce(unittest.TestCase):
    """GPA0 / GPB0: 10k pull-up, 1k series, 100n - each footswitch, from the board's parts."""

    @classmethod
    def setUpClass(cls):
        d = gate_comp()
        cls.sws = [p for p in d.parts.values() if p.db]
        assert len(cls.sws) == 2

    def parts(self, sw):
        return value(sw.db["pu"]), value(sw.db["rs"]), value(sw.db["c"])

    def test_press_and_release(self):
        """Press at 1 ms: the pin falls below VIL within ~1.7 tau_press (tau = 1k x 100n, weak pull-up beside);
        release at 6 ms: it rises above VIH after tau_rel x ln(5) (tau_rel = (pull-up + series) || weak x 100n)."""
        for sw in self.sws:
            r_pu, r_s, c = self.parts(sw)
            for r_weak in R_WEAK:
                with self.subTest(sw=sw.ref, r_weak=r_weak):
                    t, v = run(switch_deck(r_pu, r_s, c, r_weak, "0 0 1m 0 1.001m 1 6m 1 6.001m 0"), 12e-3)
                    # press: the cap discharges through r_s into the closed contact; final level r_s / (r_s + weak)
                    v_low = VDD * r_s / (r_s + r_weak)
                    tau_p = c * r_s * r_weak / (r_s + r_weak)
                    exp_fall = tau_p * math.log((VDD - v_low) / (VIL - v_low))
                    fall = crossing(t, v, VIL, 1e-3, rising=False) - 1e-3
                    self.assertAlmostEqual(exp_fall, fall, delta=0.1 * exp_fall + 5e-6)
                    self.assertLess(v[np.searchsorted(t, 5.9e-3)], VIL)
                    # release: rises through (r_pu + r_s) in parallel with the weak pull-up
                    r_eq = (r_pu + r_s) * r_weak / (r_pu + r_s + r_weak)
                    exp_rise = c * r_eq * math.log((VDD - v_low) / (VDD - VIH))
                    rise = crossing(t, v, VIH, 6e-3, rising=True) - 6e-3
                    self.assertAlmostEqual(exp_rise, rise, delta=0.1 * exp_rise + 5e-6)
                    self.assertLess(rise, 3e-3, "release must register well within the firmware's debounce")

    def test_release_bounce_filtered(self):
        """Contact bounce on release - 0.3 ms open gaps between re-closures - never reaches VIH."""
        for sw in self.sws:
            r_pu, r_s, c = self.parts(sw)
            with self.subTest(sw=sw.ref):
                pwl = "0 1 2m 1 2.001m 0 2.3m 0 2.301m 1 2.6m 1 2.601m 0 2.9m 0 2.901m 1 3.2m 1 3.201m 0"
                t, v = run(switch_deck(r_pu, r_s, c, R_WEAK[0], pwl, v0=0), 3.2e-3)
                seg = v[(t > 2.0e-3) & (t < 3.2e-3)]
                self.assertLess(seg.max(), VIH)


class EncoderFilter(unittest.TestCase):
    """Encoder channels: 10k pull-up (array) -> 10k series (array) -> 10n at the pin, contact C to GND."""

    def test_edges(self):
        d = gate_comp()
        rn = next(p for p in d.parts.values() if p.kind == "RN10k")
        cap = next(p for p in d.parts.values() if p.ref.startswith("C") and p.val == "10n")
        r, c = 10e3, value(cap)
        self.assertEqual("4x10k", rn.val)
        lines = ["* encoder channel", f"VDD vdd 0 {VDD}", f"RPU vdd a {r}", f"RS a gpio {r}", f"C1 gpio 0 {c}",
                 f"RW vdd gpio {R_WEAK[1]}", "VCTL ctl 0 PWL(0 0 0.5m 0 0.501m 1 2m 1 2.001m 0)", "S1 a 0 ctl 0 SWC",
                 ".model SWC SW(Ron=0.05 Roff=1e9 Vt=0.5 Vh=0.1)", ".ic v(gpio)=3.3 v(a)=3.3"]
        t, v = run(lines, 4e-3)
        fall = crossing(t, v, VIL, 0.5e-3, rising=False) - 0.5e-3
        rise = crossing(t, v, VIH, 2e-3, rising=True) - 2e-3
        # fall through the series 10k against the weak pull-up, rise through pull-up + series (20k) || weak
        rw = R_WEAK[1]
        v_low = VDD * r / (r + rw)
        exp_fall = c * r * rw / (r + rw) * math.log((VDD - v_low) / (VIL - v_low))
        exp_rise = c * 2 * r * rw / (2 * r + rw) * math.log((VDD - v_low) / (VDD - VIH))
        self.assertAlmostEqual(exp_fall, fall, delta=0.1 * exp_fall)
        self.assertAlmostEqual(exp_rise, rise, delta=0.1 * exp_rise)
        # a fast spin (24 detents per turn, 5 turns/s: 4 quadrature edges per detent) leaves >= 2 ms per phase
        self.assertLess(max(fall, rise), 1e-3)


if __name__ == "__main__":
    unittest.main()

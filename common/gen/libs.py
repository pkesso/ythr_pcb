"""Project libraries: footprint snapshot + own footprints (PedalLib.pretty) and symbols (PedalLib.kicad_sym).

The libraries live in pcb/common/lib and are shared by every board. Library footprints are copied from the installed
KiCad (a snapshot, so the boards do not depend on the installed library version); footprints that KiCad does not
have are generated here from the manufacturers' land patterns.

    python -m gen.libs          (from pcb/common) regenerates the libraries
"""
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = os.path.dirname(HERE)
PCB = os.path.dirname(COMMON)
sys.path.insert(0, os.path.join(PCB, "tools"))
import kicad_cli  # noqa: E402
from kicad_gen import Uuids, sexpr, dump, kids, kid, q, f  # noqa: E402

LIB = "PedalLib"
LIB_DIR = os.path.join(COMMON, "lib")
PRETTY = os.path.join(LIB_DIR, LIB + ".pretty")
SYMLIB = os.path.join(LIB_DIR, LIB + ".kicad_sym")

# KiCad library footprints used by the boards: (library, name)
SNAPSHOT_FOOTPRINTS = [
    ("Package_SO", "SSOP-28_5.3x10.2mm_P0.65mm"),
    ("Package_TO_SOT_SMD", "SOT-23-6"),
    ("Resistor_SMD", "R_0603_1608Metric"),
    ("Resistor_SMD", "R_Array_Convex_4x0603"),
    ("Capacitor_SMD", "C_0603_1608Metric"),
    ("Capacitor_SMD", "C_0805_2012Metric"),
    ("LED_SMD", "LED_0603_1608Metric"),
    ("Jumper", "SolderJumper-2_P1.3mm_Open_RoundedPad1.0x1.5mm"),
    ("Jumper", "SolderJumper-3_P1.3mm_Bridged12_RoundedPad1.0x1.5mm"),
    ("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical"),
    ("MountingHole", "MountingHole_3.2mm_M3"),
    # Main board
    ("Package_SO", "SOIC-16_3.9x9.9mm_P1.27mm"),
    ("Display_7Segment", "7SegmentLED_LTS6760_LTS6780"),
    ("Connector_PinSocket_2.54mm", "PinSocket_1x16_P2.54mm_Vertical"),
    ("Connector_PinSocket_2.54mm", "PinSocket_1x12_P2.54mm_Vertical"),
    ("LED_SMD", "LED_0805_2012Metric"),
    # Gate + Comp: hot-swap bus buffer, supply ferrite, test points
    ("Package_SO", "TSSOP-8_4.4x3mm_P0.65mm"),
    ("Inductor_SMD", "L_0603_1608Metric"),
    ("TestPoint", "TestPoint_Pad_D1.5mm"),
]

# KiCad library symbols used by the schematics: (library, name)
SNAPSHOT_SYMBOLS = [
    ("Interface_Expansion", "MCP23017x-x-SS"),
    ("Power_Protection", "SRV05-4"),
    ("Device", "RotaryEncoder_Switch_MP"),
    ("Device", "R"),
    ("Device", "C"),
    ("Device", "LED"),
    ("Device", "R_Pack04"),
    ("Connector", "8P8C"),
    ("Jumper", "SolderJumper_2_Open"),
    ("Jumper", "SolderJumper_3_Bridged12"),
    ("Connector_Generic", "Conn_01x02"),
    ("Connector_Generic", "Conn_01x03"),
    ("power", "PWR_FLAG"),
    ("Mechanical", "MountingHole"),
    ("74xx", "74HC595"),
    ("Connector_Generic", "Conn_01x12"),
    ("Connector_Generic", "Conn_01x16"),
    ("Device", "FerriteBead_Small"),
    ("Connector", "TestPoint"),
]


def kicad_share():
    return os.path.join(os.path.dirname(kicad_cli.kicad_bin()), "share", "kicad")


# ---------------------------------------------------------------- own footprints
def _fp_header(name, descr, tags, attr):
    return [f'(footprint {q(name)} (version 20240108) (generator "pedalgen") (layer "F.Cu")',
            f'(descr {q(descr)}) (tags {q(tags)})',
            '(property "Reference" "REF**" (at 0 0 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
            f'(property "Value" {q(name)} (at 0 0 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))',
            '(property "Datasheet" "" (at 0 0 0) (layer "F.Fab") (hide yes) (effects (font (size 1.27 1.27))))',
            '(property "Description" "" (at 0 0 0) (layer "F.Fab") (hide yes) (effects (font (size 1.27 1.27))))',
            f'(attr {attr})']


def _poly(pts, layer, width, ids, key, fill="no"):
    xy = " ".join(f"(xy {f(x)} {f(y)})" for x, y in pts)
    return f'(fp_poly (pts {xy}) (stroke (width {width}) (type solid)) (fill {fill}) (layer {q(layer)}) (uuid {ids.q(*key)}))'


def _rect(x0, y0, x1, y1, layer, width, ids, key):
    return (f'(fp_rect (start {f(x0)} {f(y0)}) (end {f(x1)} {f(y1)}) (stroke (width {width}) (type solid)) (fill no) '
            f'(layer {q(layer)}) (uuid {ids.q(*key)}))')


def footprint_pec11r(ids):
    """Bourns PEC11R-4xxxF-Sxxxx (vertical, with push switch), land pattern from LCSC C143789 (EasyEDA
    SW-TH_PEC11R-4XXXF-SXXXX). Seen from the knob side (top): A-C-B row at +7.25 mm, switch pins at -7.25 mm,
    mounting lugs (MP, slotted) at +-5.7 mm. Body 12.5 x 13.4 mm."""
    name = "RotaryEncoder_Bourns_Vertical_PEC11R-4xxxF-Sxxxx"
    o = _fp_header(name, "Bourns PEC11R-4xxxF-Sxxxx incremental encoder, vertical, 11 mm, with switch "
                         "(land pattern from LCSC C143789)", "rotary encoder PEC11R EC11", "through_hole")
    o.append(_rect(-6.25, -6.7, 6.25, 6.7, "F.Fab", 0.1, ids, (name, "fab")))
    o.append(f'(fp_circle (center 0 0) (end 3 0) (stroke (width 0.1) (type solid)) (fill no) (layer "F.Fab") '
             f'(uuid {ids.q(name, "fab_shaft")}))')
    # silkscreen: body corners only (the pins sit on the body edge)
    for sx in (-1, 1):
        for sy in (-1, 1):
            x, y = 6.35 * sx, 6.8 * sy
            o.append(_poly([(x, y - 1.5 * sy), (x, y), (x - 1.5 * sx, y)], "F.SilkS", 0.12, ids,
                           (name, "silk", sx, sy)))
    # courtyard: body + pin rows + lugs (a cross shape keeps neighbouring encoders packed closely)
    cy = [(-3.65, -8.4), (3.65, -8.4), (3.65, -6.95), (6.5, -6.95), (6.5, -2.4), (7.15, -2.4), (7.15, 1.9),
          (6.5, 1.9), (6.5, 6.95), (3.65, 6.95), (3.65, 8.4), (-3.65, 8.4), (-3.65, 6.95), (-6.5, 6.95),
          (-6.5, 1.9), (-7.15, 1.9), (-7.15, -2.4), (-6.5, -2.4), (-6.5, -6.95), (-3.65, -6.95)]
    o.append(_poly(cy, "F.CrtYd", 0.05, ids, (name, "crtyd")))
    pads = [("S1", -2.5, -7.25), ("S2", 2.5, -7.25), ("A", -2.5, 7.25), ("C", 0, 7.25), ("B", 2.5, 7.25)]
    for num, x, y in pads:
        shape = "rect" if num == "A" else "circle"
        o.append(f'(pad {q(num)} thru_hole {shape} (at {f(x)} {f(y)}) (size 1.8 1.8) (drill 1.2) '
                 f'(layers "*.Cu" "*.Mask") (uuid {ids.q(name, "pad", num)}))')
    for i, x in enumerate((-5.7, 5.7)):
        o.append(f'(pad "MP" thru_hole oval (at {f(x)} -0.25) (size 2.4 3.8) (drill oval 1.4 2.8) '
                 f'(layers "*.Cu" "*.Mask") (uuid {ids.q(name, "pad", "MP", i)}))')
    o.append(f'(fp_text user "${{REFERENCE}}" (at 0 0 0) (layer "F.Fab") (uuid {ids.q(name, "fabref")}) '
             f'(effects (font (size 1 1) (thickness 0.15))))')
    return name, "\n".join(o) + ")\n"


def footprint_rj45_ckmtw(ids):
    """Ckmtw R-RJ45R08P-A004 (8P8C, right angle, unshielded, no LED), land pattern from LCSC C385834 (EasyEDA
    RJ45-TH_R-RJ45R08P-A004). Pins in two rows +-0.89 mm, 1.02 mm pitch; two plastic posts (NPTH 3.4 mm) at
    +3.43 mm; body 16 x 15.1 mm from -6.5 to +8.6 mm; the plug opening faces +Y."""
    name = "RJ45_Ckmtw_R-RJ45R08P-A004_Horizontal"
    o = _fp_header(name, "8P8C jack, right angle, unshielded, no LED, Ckmtw R-RJ45R08P-A004 "
                         "(land pattern from LCSC C385834); plug opening at +Y", "RJ45 8P8C", "through_hole")
    o.append(_rect(-8, -6.5, 8, 8.6, "F.Fab", 0.1, ids, (name, "fab")))
    for k, ((x1, y1), (x2, y2)) in enumerate((((-8.1, 1.2), (-8.1, -6.6)), ((-8.1, -6.6), (8.1, -6.6)),
                                              ((8.1, -6.6), (8.1, 1.2)))):
        o.append(f'(fp_line (start {f(x1)} {f(y1)}) (end {f(x2)} {f(y2)}) (stroke (width 0.12) (type solid)) '
                 f'(layer "F.SilkS") (uuid {ids.q(name, "silk", k)}))')
    o.append(_rect(-8.25, -6.75, 8.25, 8.85, "F.CrtYd", 0.05, ids, (name, "crtyd")))
    for n in range(1, 9):
        x = 3.57 - 1.02 * (n - 1)
        y = 0.89 if n % 2 else -0.89
        shape = "rect" if n == 1 else "circle"
        o.append(f'(pad "{n}" thru_hole {shape} (at {f(x)} {f(y)}) (size 1.5 1.5) (drill 0.9) '
                 f'(layers "*.Cu" "*.Mask") (uuid {ids.q(name, "pad", n)}))')
    for i, x in enumerate((-6.35, 6.35)):
        o.append(f'(pad "" np_thru_hole circle (at {f(x)} 3.43) (size 3.4 3.4) (drill 3.4) (layers "*.Cu" "*.Mask") '
                 f'(uuid {ids.q(name, "npth", i)}))')
    o.append(f'(fp_text user "${{REFERENCE}}" (at 0 2 0) (layer "F.Fab") (uuid {ids.q(name, "fabref")}) '
             f'(effects (font (size 1 1) (thickness 0.15))))')
    return name, "\n".join(o) + ")\n"


def footprint_hole_footswitch(ids):
    """Clearance hole for a 12 mm panel footswitch (M12 thread; 12.5 mm = ISO 273 fine series) that also holds
    the board. Fab ring and courtyard = the 20 mm nut / washer area, so no part goes under the nut (on Fab, not
    silkscreen: near the board edge the ring overhangs it)."""
    name = "Hole_M12_Footswitch"
    o = _fp_header(name, "12.5 mm hole for a M12 panel footswitch, 20 mm nut area", "hole footswitch M12",
                   "exclude_from_pos_files exclude_from_bom")
    for layer, key, w in (("F.Fab", "fab", 0.1), ("F.CrtYd", "crtyd", 0.05)):
        o.append(f'(fp_circle (center 0 0) (end 10 0) (stroke (width {w}) (type solid)) (fill no) (layer {q(layer)}) '
                 f'(uuid {ids.q(name, key)}))')
    o.append(f'(pad "" np_thru_hole circle (at 0 0) (size 12.5 12.5) (drill 12.5) (layers "*.Cu" "*.Mask") '
             f'(uuid {ids.q(name, "npth")}))')
    return name, "\n".join(o) + ")\n"


def footprint_wire_pads(ids, n=2):
    """Wire solder pads: n x 1.0 mm plated holes (1.7 mm pads, pin 1 square), 2.54 mm pitch - the copper of
    KiCad's PinHeader_1x0n_P2.54mm_Vertical without its 3D model (no header is fitted: wires are soldered here)."""
    name = f"WirePads_1x{n:02d}_P2.54mm_Drill1.0mm"
    o = _fp_header(name, f"{n} wire solder pads, 1.0 mm holes, 2.54 mm pitch (no part fitted)", "wire pad solder",
                   "through_hole exclude_from_pos_files exclude_from_bom")
    end = 2.54 * (n - 1)
    o.append(_rect(-1.38, -1.38, 1.38, end + 1.38, "F.SilkS", 0.12, ids, (name, "silk")))
    o.append(_rect(-1.77, -1.77, 1.77, end + 1.78, "F.CrtYd", 0.05, ids, (name, "crtyd")))
    for k in range(n):
        num, shape = str(k + 1), "rect" if k == 0 else "circle"
        o.append(f'(pad {q(num)} thru_hole {shape} (at 0 {f(2.54 * k)}) (size 1.7 1.7) (drill 1) '
                 f'(layers "*.Cu" "*.Mask") (uuid {ids.q(name, "pad", num)}))')
    return name, "\n".join(o) + ")\n"


def footprint_wire_pads3(ids):
    return footprint_wire_pads(ids, 3)


def footprint_hole_m6(ids):
    """Plain 6.4 mm hole (M6 clearance, ISO 273 fine) with no screw head or nut on the board: the courtyard is the
    hole itself (KiCad's MountingHole_6.4mm_M6 keeps a 13 mm head area)."""
    name = "Hole_6.4mm_M6_NoHead"
    o = _fp_header(name, "6.4 mm hole for M6, nothing fastened on the board", "hole M6",
                   "exclude_from_pos_files exclude_from_bom")
    for layer, key, r, w in (("F.Fab", "fab", 3.2, 0.1), ("F.CrtYd", "crtyd", 3.45, 0.05)):
        o.append(f'(fp_circle (center 0 0) (end {f(r)} 0) (stroke (width {w}) (type solid)) (fill no) '
                 f'(layer {q(layer)}) (uuid {ids.q(name, key)}))')
    o.append(f'(pad "" np_thru_hole circle (at 0 0) (size 6.4 6.4) (drill 6.4) (layers "*.Cu" "*.Mask") '
             f'(uuid {ids.q(name, "npth")}))')
    return name, "\n".join(o) + ")\n"


def footprint_hole_18(ids):
    """Plain 18 mm hole, nothing fastened on the board: courtyard = the hole + 0.25 mm."""
    name = "Hole_18mm_NoHead"
    o = _fp_header(name, "18 mm hole, nothing fastened on the board", "hole", "exclude_from_pos_files exclude_from_bom")
    for layer, key, r, w in (("F.Fab", "fab", 9.0, 0.1), ("F.CrtYd", "crtyd", 9.25, 0.05)):
        o.append(f'(fp_circle (center 0 0) (end {f(r)} 0) (stroke (width {w}) (type solid)) (fill no) '
                 f'(layer {q(layer)}) (uuid {ids.q(name, key)}))')
    o.append(f'(pad "" np_thru_hole circle (at 0 0) (size 18 18) (drill 18) (layers "*.Cu" "*.Mask") '
             f'(uuid {ids.q(name, "npth")}))')
    return name, "\n".join(o) + ")\n"


OWN_FOOTPRINTS = [footprint_pec11r, footprint_rj45_ckmtw, footprint_hole_footswitch, footprint_wire_pads,
                  footprint_wire_pads3, footprint_hole_m6, footprint_hole_18]


# ---------------------------------------------------------------- symbols
def _flatten(syms, name):
    """A library symbol with `extends` resolved: the base symbol's units under the derived name, properties
    overridden by the derived symbol."""
    s = syms[name]
    ext = kid(s, "extends")
    if not ext:
        return s
    base = [x if not isinstance(x, list) else x for x in _flatten(syms, ext[1].strip('"'))]
    import copy
    base = copy.deepcopy(base)
    bname = base[1].strip('"')
    base[1] = q(name)
    own = {p[1]: p for p in kids(s, "property")}
    out = []
    for item in base:
        if isinstance(item, list) and item[0] == "property" and item[1] in own:
            out.append(own[item[1]])
        elif isinstance(item, list) and item[0] == "symbol":
            unit = copy.deepcopy(item)
            unit[1] = q(name + unit[1].strip('"')[len(bname):])
            out.append(unit)
        else:
            out.append(item)
    return out


def symbol_7seg_ca():
    """One-digit 7-segment display, common anode, 0.56" 10-pin (FJ5161BH / 5161BS pinout: E1 D2 CA3 C4 DP5 B6 A7
    CA8 F9 G10). KiCad has no common-anode symbol with this pinout (SM420561N is the common-cathode one)."""
    name = "7SEG_CA_1DIGIT"
    font = "(effects (font (size 1.27 1.27)))"
    hide = "(effects (font (size 1.27 1.27)) (hide yes))"
    o = [f'(symbol {q(name)} (pin_names (offset 1.016)) (exclude_from_sim no) (in_bom yes) (on_board yes)',
         f'(property "Reference" "DS" (at 0 13.97 0) {font})',
         f'(property "Value" {q(name)} (at 0 -13.97 0) {font})',
         f'(property "Footprint" "" (at 0 0 0) {hide})',
         f'(property "Datasheet" "" (at 0 0 0) {hide})',
         f'(property "Description" "One digit 7-segment LED display, common anode, 0.56 in" (at 0 0 0) {hide})',
         f'(symbol "{name}_0_1" (rectangle (start -5.08 11.43) (end 5.08 -11.43) (stroke (width 0.254) '
         '(type default)) (fill (type background))))',
         f'(symbol "{name}_1_1"']
    segs = [("A", "7"), ("B", "6"), ("C", "4"), ("D", "2"), ("E", "1"), ("F", "9"), ("G", "10"), ("DP", "5")]
    pins = [(-7.62, 8.89 - 2.54 * k, 0, n, num) for k, (n, num) in enumerate(segs)]
    pins += [(7.62, 1.27, 180, "CA", "3"), (7.62, -1.27, 180, "CA", "8")]
    for x, y, a, n, num in pins:
        o.append(f'(pin passive line (at {f(x)} {f(y)} {a}) (length 2.54) (name {q(n)} {font}) '
                 f'(number {q(num)} {font}))')
    return "\n".join(o) + "))"


def symbol_pca9511a():
    """NXP PCA9511A hot-swappable I2C bus buffer, SO8 / TSSOP8 pinout (datasheet Rev 5.0, table 3): ENABLE 1,
    SCLOUT 2, SCLIN 3, GND 4, READY 5, SDAIN 6, SDAOUT 7, VCC 8. IN = backplane (connector) side, OUT = card side.
    KiCad has no symbol for it (LTC4300A-1 and TCA4307 share the pinout)."""
    name = "PCA9511A"
    font = "(effects (font (size 1.27 1.27)))"
    hide = "(effects (font (size 1.27 1.27)) (hide yes))"
    o = [f'(symbol {q(name)} (pin_names (offset 1.016)) (exclude_from_sim no) (in_bom yes) (on_board yes)',
         f'(property "Reference" "U" (at 0 10.16 0) {font})',
         f'(property "Value" {q(name)} (at 0 -10.16 0) {font})',
         f'(property "Footprint" "" (at 0 0 0) {hide})',
         '(property "Datasheet" "https://www.nxp.com/docs/en/data-sheet/PCA9511A.pdf" (at 0 0 0) '
         '(effects (font (size 1.27 1.27)) (hide yes)))',
         f'(property "Description" "Hot-swappable I2C-bus and SMBus buffer" (at 0 0 0) {hide})',
         f'(symbol "{name}_0_1" (rectangle (start -7.62 7.62) (end 7.62 -7.62) (stroke (width 0.254) '
         '(type default)) (fill (type background))))',
         f'(symbol "{name}_1_1"']
    pins = [(-10.16, 5.08, 0, "bidirectional", "SCLIN", "3"), (-10.16, 2.54, 0, "bidirectional", "SDAIN", "6"),
            (-10.16, -2.54, 0, "input", "ENABLE", "1"), (10.16, 5.08, 180, "bidirectional", "SCLOUT", "2"),
            (10.16, 2.54, 180, "bidirectional", "SDAOUT", "7"), (10.16, -2.54, 180, "open_collector", "READY", "5"),
            (0, 10.16, 270, "power_in", "VCC", "8"), (0, -10.16, 90, "power_in", "GND", "4")]
    for x, y, a, kind, n, num in pins:
        o.append(f'(pin {kind} line (at {f(x)} {f(y)} {a}) (length 2.54) (name {q(n)} {font}) '
                 f'(number {q(num)} {font}))')
    return "\n".join(o) + "))"


OWN_SYMBOLS = [symbol_7seg_ca, symbol_pca9511a]


def build_symbol_lib():
    share = kicad_share()
    out = ['(kicad_symbol_lib (version 20231120) (generator "pedalgen") (generator_version "8.0")']
    cache = {}
    for lib, name in SNAPSHOT_SYMBOLS:
        if lib not in cache:
            t = sexpr(open(os.path.join(share, "symbols", lib + ".kicad_sym"), encoding="utf-8").read(), keep_quotes=True)
            cache[lib] = {s[1].strip('"'): s for s in kids(t, "symbol")}
        out.append(dump(_flatten(cache[lib], name)))
    out += [fn() for fn in OWN_SYMBOLS]
    out.append(")")
    with open(SYMLIB, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    kicad_cli.upgrade("sym", SYMLIB)


def _stable_uuids(pretty):
    """kicad-cli fp upgrade gives items without a UUID (the properties of KiCad's library footprints, ours too) a
    random one on every run: replace every UUID in each footprint by one derived from the footprint name and the
    item's position in the file, so rebuilding the library gives identical files."""
    ns = uuid.UUID("9d6b2c4e-1f3a-4e5b-8c7d-0a1b2c3d4e5f")
    for fn in sorted(os.listdir(pretty)):
        if not fn.endswith(".kicad_mod"):
            continue
        path = os.path.join(pretty, fn)
        text = open(path, encoding="utf-8").read()
        n = iter(range(10 ** 6))
        text = re.sub(r'\(uuid "[0-9a-f-]{36}"\)', lambda m: f'(uuid "{uuid.uuid5(ns, fn + "/" + str(next(n)))}")', text)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def build_footprints():
    os.makedirs(PRETTY, exist_ok=True)
    share = kicad_share()
    for lib, name in SNAPSHOT_FOOTPRINTS:
        src = os.path.join(share, "footprints", lib + ".pretty", name + ".kicad_mod")
        data = open(src, "rb").read().replace(b"\r\n", b"\n")
        open(os.path.join(PRETTY, name + ".kicad_mod"), "wb").write(data)
    ids = Uuids("6a3c1f0e-8d2b-4b7a-9c55-0e1f2a3b4c5d")
    for fn in OWN_FOOTPRINTS:
        name, text = fn(ids)
        with open(os.path.join(PRETTY, name + ".kicad_mod"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    kicad_cli.upgrade("fp", PRETTY)
    _stable_uuids(PRETTY)


if __name__ == "__main__":
    build_footprints()
    build_symbol_lib()
    print("libraries written to", LIB_DIR)

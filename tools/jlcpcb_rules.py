"""Put JLCPCB's manufacturing limits into a KiCad project, so that a clean DRC means the board can be made.

    python jlcpcb_rules.py project.kicad_pro            # raises the project's minimums, writes project.kicad_dru
    python jlcpcb_rules.py project.kicad_pro --check    # only reports what is below the limits

The figures are for a standard 1-2 layer FR-4 board with 1 oz copper at the standard price, from
https://jlcpcb.com/capabilities/pcb-capabilities as read on 2026-11-10. Limits a design is stricter than stay as they
are: minimums are only raised. The custom rules go into a marked block at the start of the .kicad_dru, so the project's
own rules, which come later, keep priority and survive a reinstall.

It also writes the skill's layout rules (pcb-design.md, "Sensitive tracks") as DRC warnings: tracks of the Audio net
class at least 3 mm from the board edge (outside connector courtyards), and 1 mm from the Noisy class. --audio and
--noisy put nets in those classes by name pattern (stored in the .kicad_pro, shown in KiCad's Board Setup):

    python jlcpcb_rules.py project.kicad_pro --audio "/IN*,/OUT,/WET,/DRY" --noisy "/+9V_IN,/LED_*"

Two more classes, which freeroute.py routes by: --power (supply rails: tracks as wide as IPC-2221 asks for
--power-current with pcb-design.md's 1.5x margin, at least 0.5 mm, and 0.8/0.4 mm vias) and --hiz (high-impedance
nodes such as the guitar input after its clamp: one layer, no vias; DRC warns about a via on them):

    python jlcpcb_rules.py project.kicad_pro --power "/VCC,/V_EF" --power-current 0.5 --hiz "/JACK_IN,/IN"

A project with no net classes gets a Default class with KiCad's own sizes, so its other nets do not change. In Git
Bash, put MSYS_NO_PATHCONV=1 before the command, with this script's path as C:/... (it rewrites arguments that start
with /, and with the variable a /c/... path is not converted either); the script also undoes the rewrite.

Exit code: 0 when the project has the rules (with --check: nothing is below them), 1 otherwise.
"""
import argparse
import json
import math
import os
import re
import sys

MINIMUMS = {  # design_settings.rules of the .kicad_pro, in mm
    "min_track_width": 0.10,
    "min_clearance": 0.15,  # tracks may be 0.10 but SMD pads 0.15; a custom clearance rule would override the netclasses
    "min_via_diameter": 0.45,  # a smaller via costs more
    "min_through_hole_diameter": 0.20,  # a smaller hole costs more
    "min_via_annular_width": 0.075,  # via diameter = hole + 0.15, the preferred margin
    "min_hole_clearance": 0.20,
    "min_hole_to_hole": 0.20,
    "min_copper_edge_clearance": 0.20,  # routed edge; 0.4 for a V-cut panel
    "min_silk_clearance": 0.15,  # pad to silkscreen; KiCad applies it to the board edge only
    "min_text_height": 1.0,
    "min_text_thickness": 0.15,
}
ERRORS = ("annular_width", "clearance", "copper_edge_clearance", "drill_out_of_range", "hole_clearance", "hole_to_hole",
          "track_width")  # severities that must stop an order
BEGIN, END = "# begin jlcpcb_rules.py", "# end jlcpcb_rules.py"
RULES = f"""{BEGIN}
(rule "jlcpcb_pad_annular"
    (constraint annular_width (min 0.18mm))
    (condition "A.Type == 'Pad'"))
(rule "jlcpcb_pad_hole_to_hole"
    (constraint hole_to_hole (min 0.45mm))
    (condition "A.Type == 'Pad' && B.Type == 'Pad'"))
(rule "jlcpcb_npth"
    (constraint hole_size (min 0.5mm))
    (condition "A.Pad_Type == 'NPTH, mechanical'"))
(rule "jlcpcb_plated_slot"
    (constraint hole_size (min 0.5mm))
    (condition "A.Pad_Type == 'Through-hole' && A.Hole_Size_X != A.Hole_Size_Y"))
(rule "jlcpcb_npth_slot"
    (constraint hole_size (min 1mm))
    (condition "A.Pad_Type == 'NPTH, mechanical' && A.Hole_Size_X != A.Hole_Size_Y"))
{END}
"""
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n", re.S)

# The skill's layout rules (pcb-design.md, "Sensitive tracks") for the net classes Audio and Noisy. Warnings: they are
# about quality, not whether the board can be made. Pads are left out of the edge rule (a jack's pads sit at the edge
# by design), and so is copper inside a connector's courtyard (the track leaving a jack). Pad-to-pad pairs are left out
# of the noise rule: a resistor from a noisy net into an audio one has its two pads 1 mm apart by its nature.
AUDIO_EDGE, NOISE_GAP = 3.0, 1.0  # mm
LAYOUT_BEGIN, LAYOUT_END = "# begin pcb skill layout rules", "# end pcb skill layout rules"
LAYOUT = f"""{LAYOUT_BEGIN}
(rule "audio_from_edge"
    (severity warning)
    (constraint edge_clearance (min {AUDIO_EDGE:g}mm))
    (condition "A.hasNetclass('Audio') && (A.Type == 'Track' || A.Type == 'Via') && !A.intersectsCourtyard('J*')"))
(rule "audio_from_noise"
    (severity warning)
    (constraint clearance (min {NOISE_GAP:g}mm))
    (condition "A.hasNetclass('Audio') && B.hasNetclass('Noisy') && (A.Type != 'Pad' || B.Type != 'Pad')"))
(rule "hiz_no_via"
    (severity warning)
    (constraint disallow via)
    (condition "A.hasNetclass('HiZ')"))
{LAYOUT_END}
"""
_LAYOUT_BLOCK = re.compile(re.escape(LAYOUT_BEGIN) + r".*?" + re.escape(LAYOUT_END) + r"\n", re.S)
# Audio and Noisy: the layout rules above. Power: tracks as wide as the current needs (power_width), larger vias.
# HiZ: high-impedance nodes (the guitar input after its clamp), routed on one layer with no via (freeroute.py) and
# checked for vias by DRC.
CLASSES = ("Audio", "Noisy", "Power", "HiZ")
POWER_CURRENT = 1.0  # A, the default design current of the Power class
POWER_MIN_WIDTH = 0.5  # mm: below this a supply track is not worth a class (drop, robustness under a fault)
POWER_VIA = (0.8, 0.4)  # mm, diameter and drill


def power_width(current, rise=10.0, oz=1.0, margin=1.5, floor=POWER_MIN_WIDTH):
    """Track width in mm for `current` amperes on an outer layer by IPC-2221 (`rise` degrees C over ambient, `oz`
    ounce copper), with pcb-design.md's current margin, never under `floor`; rounded up to 0.05 mm."""
    area = (margin * current / (0.048 * rise ** 0.44)) ** (1 / 0.725)  # square mils
    width = area / (1.378 * oz) * 0.0254  # mils -> mm
    return round(max(floor, math.ceil(width / 0.05 - 1e-9) * 0.05), 2)


_MSYS = re.compile(r"^[A-Za-z]:[/\\](?:.*[/\\])?Git[/\\](.*)$")  # Git for Windows' root, wherever it is installed


def unmangle(pattern):
    """A net pattern as it was typed: Git Bash turns an argument that starts with / into a Windows path
    ('/IN' -> 'C:/Program Files/Git/IN'), and KiCad's net names start with /. Prints what it corrects."""
    m = _MSYS.match(pattern)
    if not m:
        return pattern
    fixed = "/" + m.group(1).replace("\\", "/")
    print(f"note: {pattern!r} is {fixed!r} rewritten by Git Bash's path conversion; using {fixed!r} "
          "(MSYS_NO_PATHCONV=1 before the command avoids it, with the script's path as C:/...)")
    return fixed


def set_classes(project, patterns, current=POWER_CURRENT):
    """Net classes Audio, Noisy, Power and HiZ from net-name patterns (KiCad wildcards, e.g. /IN*), copying the Default
    class's widths (Power: power_width(current) and POWER_VIA); their old patterns are replaced, every other class
    and pattern is kept."""
    net = project.setdefault("net_settings", {})
    net.setdefault("meta", {"version": 4})
    classes = net.get("classes") or []
    default = next((c for c in classes if c.get("name") == "Default"), None)
    if default is None:  # KiCad's own defaults, which a project with no net classes is routed with: writing others here
        # would change every net (0.25 mm tracks instead of 0.2 changed a whole board once)
        default = {"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
                   "priority": 2147483647}
        classes.insert(0, default)
    for k, name in enumerate(CLASSES):
        if name in patterns:
            classes = [c for c in classes if c.get("name") != name]
            cls = dict(default, name=name, priority=k)
            if name == "Power":
                cls.update(track_width=max(default.get("track_width", 0.2), power_width(current)),
                           via_diameter=max(default.get("via_diameter", 0.6), POWER_VIA[0]),
                           via_drill=max(default.get("via_drill", 0.3), POWER_VIA[1]))
            classes.append(cls)
    net["classes"] = classes
    kept = [p for p in net.get("netclass_patterns") or [] if p.get("netclass") not in patterns]
    net["netclass_patterns"] = kept + [{"netclass": name, "pattern": p} for name in CLASSES
                                       for p in patterns.get(name, [])]


def class_patterns(project):
    pats = (project.get("net_settings") or {}).get("netclass_patterns") or []
    return {p.get("netclass"): p.get("pattern") for p in pats}


def _settings(project):
    return project.setdefault("board", {}).setdefault("design_settings", {})


def problems(pro):
    """What in this project is below JLCPCB's limits: a list of sentences, empty when the rules are in place."""
    if not os.path.isfile(pro):
        return [f"{os.path.basename(pro)}: no project file, so KiCad checks with its default rules"]
    with open(pro, encoding="utf-8") as fh:
        project = json.load(fh)
    design = _settings(project)
    rules, severities = design.get("rules", {}), design.get("rule_severities", {})
    found = [f"{k} is {rules.get(k, 'not set')}, the limit is {v:g}" for k, v in MINIMUMS.items() if rules.get(k, 0) < v]
    found += [f"severity of {k} is {severities.get(k, 'not set')}, it must be error" for k in ERRORS if severities.get(k) != "error"]
    dru = os.path.splitext(pro)[0] + ".kicad_dru"
    text = ""
    if os.path.isfile(dru):
        with open(dru, encoding="utf-8") as fh:
            text = fh.read()
    if RULES not in text:
        found.append(f"{os.path.basename(dru)} does not have the current JLCPCB rules")
    if LAYOUT not in text:
        found.append(f"{os.path.basename(dru)} does not have the current layout rules")
    return found


def notes(pro):
    """What the layout rules lack, without making the project fail: a board may have no audio at all."""
    if not os.path.isfile(pro):
        return []
    with open(pro, encoding="utf-8") as fh:
        have = set(class_patterns(json.load(fh)))
    return [f"note: no net is in the {name} class, so the layout rules check nothing for it (--{name.lower()})"
            for name in ("Audio", "Noisy") if name not in have]  # Power and HiZ are options, not gaps


_NON_LATIN = ("CYRILLIC", "CJK", "ARABIC", "HEBREW", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "DEVANAGARI")


def non_latin(node, path="$"):
    """JSON paths of the strings (keys and values) with non-Latin letters, as repo_check.py --english sees them."""
    import unicodedata
    bad = lambda s: any(c.isalpha() and unicodedata.name(c, "").startswith(_NON_LATIN) for c in s)  # noqa: E731
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += [f"{path}.{k}"] if bad(k) else []
            out += non_latin(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += non_latin(v, f"{path}[{i}]")
    elif isinstance(node, str) and bad(node):
        out.append(path)
    return out


def install(pro, patterns=None, current=POWER_CURRENT):
    with open(pro, encoding="utf-8") as fh:
        project = json.load(fh)
    if patterns:
        set_classes(project, patterns, current)
    design = _settings(project)
    rules, severities = design.setdefault("rules", {}), design.setdefault("rule_severities", {})
    rules.update({k: max(rules.get(k, 0), v) for k, v in MINIMUMS.items()})
    severities.update(dict.fromkeys(ERRORS, "error"))
    with open(pro, "w", encoding="utf-8", newline="\n") as fh:
        # the file's own key order, and UTF-8 as KiCad writes it (Cyrillic names stay readable, not \u escapes)
        fh.write(json.dumps(project, indent=2, ensure_ascii=False) + "\n")
    for where in non_latin(project):
        print(f"note: non-Latin text in {os.path.basename(pro)} at {where}: now written as plain UTF-8, so it shows "
              "(an \\u escape hid it before); repo_check.py --english reports it")
    dru = os.path.splitext(pro)[0] + ".kicad_dru"
    text = ""
    if os.path.isfile(dru):
        with open(dru, encoding="utf-8") as fh:
            text = _LAYOUT_BLOCK.sub("", _BLOCK.sub("", fh.read()))
    head = re.match(r"\s*\(version \d+\)\s*", text)
    version, rest = (head.group().strip(), text[head.end():].lstrip("\n")) if head else ("(version 1)", text)
    with open(dru, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{version}\n\n{RULES}\n{LAYOUT}" + (f"\n{rest}" if rest else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help=".kicad_pro file")
    ap.add_argument("--check", action="store_true", help="only report, change nothing")
    ap.add_argument("--audio", help="comma-separated net-name patterns of the Audio class (e.g. /IN*,/OUT,/WET)")
    ap.add_argument("--noisy", help="comma-separated net-name patterns of the Noisy class (supply input, LEDs, relays, "
                                    "clocks)")
    ap.add_argument("--power", help="comma-separated net-name patterns of the Power class (supply rails): wider "
                                    "tracks and vias")
    ap.add_argument("--power-current", type=float, default=POWER_CURRENT,
                    help=f"design current of the Power class, A (default {POWER_CURRENT:g}): its track width")
    ap.add_argument("--hiz", help="comma-separated net-name patterns of the HiZ class (high-impedance nodes, the "
                                  "guitar input after its clamp): one layer, no vias")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected
    if not args.check:
        if not os.path.isfile(args.project):
            print(f"{args.project}: no such file")
            return 1
        patterns = {name: [unmangle(p.strip()) for p in value.split(",") if p.strip()]
                    for name, value in (("Audio", args.audio), ("Noisy", args.noisy), ("Power", args.power),
                                        ("HiZ", args.hiz)) if value is not None}
        install(args.project, patterns, args.power_current)
        if "Power" in patterns:
            print(f"Power class: tracks {power_width(args.power_current):g} mm for {args.power_current:g} A, vias "
                  f"{POWER_VIA[0]:g}/{POWER_VIA[1]:g} mm")
    found = problems(args.project)
    print("\n".join(found + notes(args.project)) if found else "\n".join(["ok: the project has JLCPCB's limits"]
                                                                          + notes(args.project)))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

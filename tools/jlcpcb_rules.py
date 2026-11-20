"""Put JLCPCB's manufacturing limits into a KiCad project, so that a clean DRC means the board can be made.

    python jlcpcb_rules.py project.kicad_pro            # raises the project's minimums, writes project.kicad_dru
    python jlcpcb_rules.py project.kicad_pro --check    # only reports what is below the limits

The figures are for a standard 1-2 layer FR-4 board with 1 oz copper at the standard price, from
https://jlcpcb.com/capabilities/pcb-capabilities as read on 2026-11-10. Limits a design is stricter than stay as they
are: minimums are only raised. The custom rules go into a marked block at the start of the .kicad_dru, so the project's
own rules, which come later, keep priority and survive a reinstall.

Exit code: 0 when the project has the rules (with --check: nothing is below them), 1 otherwise.
"""
import argparse
import json
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


def _settings(project):
    return project.setdefault("board", {}).setdefault("design_settings", {})


def problems(pro):
    """What in this project is below JLCPCB's limits: a list of sentences, empty when the rules are in place."""
    if not os.path.isfile(pro):
        return [f"{os.path.basename(pro)}: no project file, so KiCad checks with its default rules"]
    with open(pro, encoding="utf-8") as fh:
        design = _settings(json.load(fh))
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
    return found


def install(pro):
    with open(pro, encoding="utf-8") as fh:
        project = json.load(fh)
    design = _settings(project)
    rules, severities = design.setdefault("rules", {}), design.setdefault("rule_severities", {})
    rules.update({k: max(rules.get(k, 0), v) for k, v in MINIMUMS.items()})
    severities.update(dict.fromkeys(ERRORS, "error"))
    with open(pro, "w", encoding="utf-8", newline="\n") as fh:
        # the file's own key order, and UTF-8 as KiCad writes it (Cyrillic names stay readable, not \u escapes)
        fh.write(json.dumps(project, indent=2, ensure_ascii=False) + "\n")
    dru = os.path.splitext(pro)[0] + ".kicad_dru"
    text = ""
    if os.path.isfile(dru):
        with open(dru, encoding="utf-8") as fh:
            text = _BLOCK.sub("", fh.read())
    head = re.match(r"\s*\(version \d+\)\s*", text)
    version, rest = (head.group().strip(), text[head.end():]) if head else ("(version 1)", text)
    with open(dru, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{version}\n\n{RULES}" + (f"\n{rest}" if rest else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help=".kicad_pro file")
    ap.add_argument("--check", action="store_true", help="only report, change nothing")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected
    if not args.check:
        if not os.path.isfile(args.project):
            print(f"{args.project}: no such file")
            return 1
        install(args.project)
    found = problems(args.project)
    print("\n".join(found) if found else "ok: the project has JLCPCB's limits")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

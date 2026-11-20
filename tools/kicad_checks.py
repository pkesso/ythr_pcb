"""Run KiCad's ERC and DRC through kicad-cli and summarise the results by violation type.

    python kicad_checks.py erc project.kicad_sch
    python kicad_checks.py drc project.kicad_pcb            # zones refilled in memory, schematic parity included
    python kicad_checks.py drc project.kicad_pcb --strict   # warnings fail too
    python kicad_checks.py drc project.kicad_pcb --json     # machine-readable summary

Items excluded in KiCad (right click > Exclude) are not reported. The board file is never modified: --refill-zones
refills in memory only. Results are grouped by the violation `type` code from KiCad's JSON report, which does not
depend on the UI language; descriptions are KiCad's own text.

Exit code: 0 when there are no errors (no errors and no warnings with --strict), 1 otherwise, 2 if KiCad could not run.
"""
import argparse
import collections
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_cli  # noqa: E402


def erc(sch):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "erc.json")
        kicad_cli.run("sch", "erc", "--format", "json", "--severity-error", "--severity-warning", "-o", out, sch)
        d = json.load(open(out, encoding="utf-8"))
    return [v for sheet in d.get("sheets", []) for v in sheet.get("violations", [])]


def drc(pcb, parity=True, refill=True):
    args = ["pcb", "drc", "--format", "json", "--severity-error", "--severity-warning"]
    args += ["--schematic-parity"] if parity else []
    args += ["--refill-zones"] if refill else []
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "drc.json")
        kicad_cli.run(*args, "-o", out, pcb)
        d = json.load(open(out, encoding="utf-8"))
    return d.get("violations", []) + d.get("unconnected_items", []) + d.get("schematic_parity", [])


def summarise(items):
    """{(severity, type): [violation, ...]} sorted by severity then count."""
    groups = collections.defaultdict(list)
    for v in items:
        groups[(v["severity"], v["type"])].append(v)
    return dict(sorted(groups.items(), key=lambda kv: (kv[0][0] != "error", -len(kv[1]))))


def describe(v):
    where = " / ".join(f'{i["description"]} @({i["pos"]["x"]:.2f},{i["pos"]["y"]:.2f})' if "pos" in i else i["description"]
                       for i in v.get("items", []))
    return f'{v["description"]}: {where}'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=["erc", "drc"])
    ap.add_argument("file", help=".kicad_sch for erc, .kicad_pcb for drc")
    ap.add_argument("--strict", action="store_true", help="warnings fail too")
    ap.add_argument("--no-parity", action="store_true", help="drc: skip the schematic parity check")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("--max", type=int, default=10, help="examples printed per type (default 10)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected

    try:
        kicad_cli.ensure_ready()
        items = erc(args.file) if args.check == "erc" else drc(args.file, parity=not args.no_parity)
    except kicad_cli.KicadError as e:
        print(e)
        return 2
    groups = summarise(items)
    errors = sum(len(v) for (sev, _), v in groups.items() if sev == "error")
    warnings = sum(len(v) for (sev, _), v in groups.items() if sev != "error")

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings,
                          "types": [{"severity": s, "type": t, "count": len(v), "examples": [describe(x) for x in v[:args.max]]}
                                    for (s, t), v in groups.items()]}, indent=1, ensure_ascii=False))
    else:
        print(f"{args.check.upper()} {os.path.basename(args.file)}: {errors} errors, {warnings} warnings")
        for (sev, typ), vs in groups.items():
            print(f"  {sev:7} {typ:40} x{len(vs)}")
            for v in vs[:args.max]:
                print(f"            {describe(v)}")
            if len(vs) > args.max:
                print(f"            ... and {len(vs) - args.max} more")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())

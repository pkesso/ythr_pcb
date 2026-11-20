"""Repository validator for a generated KiCad project; meant to run as a git pre-commit hook too.

    python repo_check.py                    # all checks on the repository the current directory belongs to
    python repo_check.py drc assembly       # selected checks
    python repo_check.py --hook             # only checks relevant to the staged files (pre-commit)
    python repo_check.py --strict           # ERC/DRC warnings fail too
    python repo_check.py --english          # also reject non-Latin text (Cyrillic, CJK, ...) in text files

Checks:
  format    KiCad files are in KiCad 10 format (file version newer than KiCad 9's)
  paths     no absolute paths in KiCad files and library tables
  english   (with --english) no non-Latin letters in tracked text files
  python    Python files compile
  erc       KiCad ERC has no errors
  drc       KiCad DRC has no errors (zones refilled in memory, schematic parity)
  assembly  JLCPCB BOM and CPL match the board: parts, values, LCSC numbers, position, side (and rotation)
  rules     the skill's rules on the board: pads that cannot be right (SMD with a drill, through-hole without one,
            an SMD package with only through-hole pads), GND on a local label, no 0201; warns about 0402 parts and
            about references the last commit had and the board no longer has

Files are found automatically: the only .kicad_sch / .kicad_pcb at the repository root (or --sch / --pcb), and the
only CSV files anywhere in the repository, hidden directories aside, whose names contain BOM / CPL (or --bom / --cpl).
A check that lacks its files prints `skip`, which is not a pass. Checks read the working tree, not the staged versions.

Projects keep a copy of this script with kicad_cli.py, kicad_checks.py and kicad_gen.py in their tools/ directory,
kept up to date by sync_tools.py. Pre-commit hook (.githooks/pre-commit, enable with
`git config core.hooksPath .githooks`):
    #!/bin/sh
    # runs the project's copy of repo_check.py with the project's .venv
    root=$(git rev-parse --show-toplevel)
    py="$root/.venv/Scripts/python.exe"; [ -x "$py" ] || py="$root/.venv/bin/python"
    if [ ! -x "$py" ]; then
        echo "pre-commit: no .venv in $root - create it: python -m venv .venv, then .venv's pip install -r requirements.txt"
        exit 1
    fi
    exec "$py" "$root/tools/repo_check.py" --hook

Exit code: 0 when every check passes, 1 when one fails, 2 when the schematic or board to check is ambiguous.
"""
import argparse
import collections
import csv
import glob
import os
import re
import subprocess
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_checks  # noqa: E402
import kicad_cli  # noqa: E402
from kicad_gen import sexpr, kids, kid  # noqa: E402

KICAD_EXT = (".kicad_sch", ".kicad_pcb", ".kicad_sym", ".kicad_mod")
KICAD_FILES = KICAD_EXT + (".kicad_pro", "fp-lib-table", "sym-lib-table")
KICAD9_VERSION = {".kicad_sch": 20250114, ".kicad_sym": 20241209, ".kicad_pcb": 20241229, ".kicad_mod": 20241229}
FILE_VERSION = re.compile(rb"\(version (\d+)\)")
ABS_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|(?<![\w}$])/(?:home|Users|tmp|mnt)/")
NON_LATIN = ("CYRILLIC", "CJK", "ARABIC", "HEBREW", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "DEVANAGARI")


def git(root, *args):
    """File names from git; with -z they come as they are, without quoting of non-ASCII names."""
    out = subprocess.run(["git", *args, "-z"], cwd=root, capture_output=True, encoding="utf-8", check=True).stdout
    return [l for l in out.split("\0") if l]


def text_of(path):
    data = open(path, "rb").read()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class Result:
    def __init__(self):
        self.errors, self.warnings = [], []
        self.skipped = None


# ---------------------------------------------------------------- file checks
def check_format(ctx, files):
    r = Result()
    for f in files:
        ext = os.path.splitext(f)[1]
        if ext not in KICAD_EXT:
            continue
        m = FILE_VERSION.search(open(os.path.join(ctx.root, f), "rb").read(400))
        if not m or int(m.group(1)) <= KICAD9_VERSION[ext]:
            r.errors.append(f"{f}: not in KiCad 10 format ({'version ' + m.group(1).decode() if m else 'no version'})"
                            " - resave it with kicad-cli <sch|pcb|sym|fp> upgrade --force")
    return r


def check_paths(ctx, files):
    r = Result()
    for f in files:
        if f.endswith(KICAD_FILES):
            for n, line in enumerate((text_of(os.path.join(ctx.root, f)) or "").splitlines(), 1):
                if ABS_PATH.search(line):
                    r.errors.append(f"{f}:{n}: absolute path: {line.strip()[:120]}")
    return r


def check_english(ctx, files):
    r = Result()
    for f in files:
        for n, line in enumerate((text_of(os.path.join(ctx.root, f)) or "").splitlines(), 1):
            bad = [c for c in line if c.isalpha() and unicodedata.name(c, "").startswith(NON_LATIN)]
            if bad:
                r.errors.append(f"{f}:{n}: non-Latin text: {line.strip()[:120]}")
    return r


def check_python(ctx, files):
    r = Result()
    for f in files:
        if f.endswith(".py"):
            try:
                compile(open(os.path.join(ctx.root, f), "rb").read(), f, "exec")
            except SyntaxError as e:
                r.errors.append(f"{f}:{e.lineno}: {e.msg}")
    return r


# ---------------------------------------------------------------- KiCad checks
def _kicad(items, strict):
    r = Result()
    for (sev, typ), vs in kicad_checks.summarise(items).items():
        if sev == "error" or strict:
            r.errors += [f"{sev} {typ}: {kicad_checks.describe(v)}" for v in vs]
        else:
            r.warnings.append(f"{len(vs)} x {typ}")
    return r


def check_erc(ctx, files):
    return _kicad(kicad_checks.erc(ctx.sch), ctx.strict) if ctx.sch else _skip("no schematic")


def check_drc(ctx, files):
    return _kicad(kicad_checks.drc(ctx.pcb), ctx.strict) if ctx.pcb else _skip("no board")


def _skip(why):
    r = Result()
    r.skipped = why
    return r


# ---------------------------------------------------------------- assembly
def board_parts(pcb, field):
    """Assembled footprints (those with an LCSC number) -> value, lcsc, position, side, rotation."""
    t = sexpr(open(pcb, encoding="utf-8").read())
    parts = {}
    for fp in kids(t, "footprint"):
        props = {p[1]: p[2] for p in kids(fp, "property")}
        if not props.get(field):
            continue
        at = kid(fp, "at")
        parts[props["Reference"]] = dict(value=props.get("Value", ""), lcsc=props[field], x=float(at[1]), y=float(at[2]),
                                         rot=float(at[3]) if len(at) > 3 else 0.0,
                                         side={"F.Cu": "Top", "B.Cu": "Bottom"}[kid(fp, "layer")[1]])
    return parts


def mm(s):
    return float(s.strip().removesuffix("mm"))


def check_assembly(ctx, files):
    r = Result()
    if not ctx.pcb:
        return _skip("no board")
    # looked for only here, so that several BOMs in the repository fail this check, not every other one
    bom_path = find_one(ctx.root, ctx.bom, "**/*[Bb][Oo][Mm]*.csv", "BOM files")
    cpl_path = find_one(ctx.root, ctx.cpl, "**/*[Cc][Pp][Ll]*.csv", "CPL files")
    absent = [what for what, path in (("BOM", bom_path), ("CPL", cpl_path)) if not path]
    if absent:
        return _skip("no " + ", no ".join(absent))
    parts = board_parts(ctx.pcb, ctx.lcsc_field)
    bom, cpl = (os.path.relpath(p, ctx.root).replace(os.sep, "/") for p in (bom_path, cpl_path))
    seen = collections.Counter()
    for row in csv.DictReader(open(bom_path, encoding="utf-8-sig")):
        lcsc = row.get("LCSC Part #") or row.get("LCSC") or ""
        for ref in (x.strip() for x in row["Designator"].split(",") if x.strip()):
            seen[ref] += 1
            p = parts.get(ref)
            if not p:
                r.errors.append(f"{bom}: {ref} is not an assembled part on the board")
            elif lcsc != p["lcsc"]:
                r.errors.append(f"{bom}: {ref} LCSC {lcsc}, board has {p['lcsc']}")
            elif row.get("Comment") not in (None, p["value"]):
                r.errors.append(f"{bom}: {ref} value {row['Comment']}, board has {p['value']}")
    r.errors += [f"{bom}: {ref} listed {n} times" for ref, n in seen.items() if n > 1]
    r.errors += [f"{bom}: {ref} is missing" for ref in parts if ref not in seen]
    seen = collections.Counter()
    for row in csv.DictReader(open(cpl_path, encoding="utf-8-sig")):
        ref = row["Designator"]
        seen[ref] += 1
        p = parts.get(ref)
        if not p:
            r.errors.append(f"{cpl}: {ref} is not an assembled part on the board")
            continue
        # JLCPCB CPL uses KiCad position-file coordinates: the Y axis points up
        if abs(mm(row["Mid X"]) - p["x"]) > 0.001 or abs(mm(row["Mid Y"]) + p["y"]) > 0.001:
            r.errors.append(f"{cpl}: {ref} at ({row['Mid X']}, {row['Mid Y']}), board has ({p['x']:g}, {-p['y']:g})")
        if row["Layer"] != p["side"]:
            r.errors.append(f"{cpl}: {ref} on {row['Layer']}, board has {p['side']}")
        if ctx.rotation and (float(row["Rotation"]) - p["rot"]) % 360:
            r.errors.append(f"{cpl}: {ref} rotation {row['Rotation']}, board has {p['rot']:g} (use --no-rotation if the "
                            "CPL carries deliberate JLCPCB rotation offsets)")
    r.errors += [f"{cpl}: {ref} listed {n} times" for ref, n in seen.items() if n > 1]
    r.errors += [f"{cpl}: {ref} is missing" for ref in parts if ref not in seen]
    return r


# ---------------------------------------------------------------- the skill's rules on the board
SMD_NAME = re.compile(r"SMD|SOT-?\d|SOD-?\d|SOIC|SSOP|TSSOP|MSOP|[VWUX]?QFN|[VWUX]?DFN|VSON|WSON|QFP|BGA|"
                      r"_\d{4}_\d{4}Metric", re.I)
SIZE_0201 = re.compile(r"(?<!\d)0201(?!\d)|0603Metric", re.I)  # 0603Metric is 0201 imperial
SIZE_0402 = re.compile(r"(?<!\d)0402(?!\d)|1005Metric", re.I)
LOCAL_GND = re.compile(r"^/(?:.*/)?GND$")


def footprint_refs(board):
    return {next((p[2] for p in kids(fp, "property") if p[1] == "Reference"), "?"): fp
            for fp in kids(board, "footprint")}


def board_rules(text, head_text=None):
    """The skill's rules that can be read off the board file (`SKILL.md`, "Rules that save hours"; `pcb-design.md`):
    errors for pads that cannot be right, a local GND net and 0201 parts; warnings for 0402 parts and references that
    the last commit had and the board no longer has (unused parts are never removed unless the user asks)."""
    r = Result()
    board = sexpr(text)
    small = []
    for ref, fp in sorted(footprint_refs(board).items()):
        name = fp[1]
        pads = [p for p in kids(fp, "pad") if p[2] in ("smd", "thru_hole")]
        for p in pads:
            drill = kid(p, "drill")
            size = kid(p, "size")
            layers = " ".join(kid(p, "layers")[1:]) if kid(p, "layers") else ""
            if p[2] == "smd" and drill:
                r.errors.append(f"{ref} pad {p[1]}: SMD pad with a drill")
            if p[2] == "smd" and "Cu" not in layers and p[1]:  # unnumbered paste-only pads are paste windows
                r.errors.append(f"{ref} pad {p[1]}: SMD pad on no copper layer ({layers})")
            if p[2] == "thru_hole":
                d = [float(x) for x in (drill or [])[1:] if isinstance(x, str) and re.match(r"^[\d.]+$", x)]
                if not d or min(d) <= 0:
                    r.errors.append(f"{ref} pad {p[1]}: through-hole pad without a drill")
                elif size and min(d) >= min(float(size[1]), float(size[2])):
                    r.errors.append(f"{ref} pad {p[1]}: drill {min(d):g} mm not smaller than the pad "
                                    f"{float(size[1]):g} x {float(size[2]):g} mm")
        if pads and SMD_NAME.search(name) and not any(p[2] == "smd" for p in pads):
            r.errors.append(f"{ref} ({name}): an SMD package with only through-hole pads - a footprint generator that "
                            "writes every pad as thru_hole")
        if SIZE_0201.search(name):
            r.errors.append(f"{ref} ({name}): 0201 parts are never used (pcb-design.md)")
        elif SIZE_0402.search(name):
            small.append(ref)
    pad_nets = {n[-1] for fp in kids(board, "footprint") for p in kids(fp, "pad") for n in kids(p, "net") if len(n) > 1}
    nets = sorted({n[1] for n in kids(board, "net") if len(n) > 1} | pad_nets)
    r.errors += [f"net {n}: ground on a local label; GND must be the global net (footprints-and-parity.md)"
                 for n in nets if isinstance(n, str) and LOCAL_GND.match(n)]
    if small:
        r.warnings.append(f"0402 (only where space forces it): {', '.join(small)}")
    if head_text:
        gone = sorted(set(footprint_refs(sexpr(head_text))) - set(footprint_refs(board)), key=_ref_key)
        if gone:
            r.warnings.append(f"removed since the last commit: {', '.join(gone)} - unused parts stay unless the user "
                              "asked to remove them")
    return r


def _ref_key(ref):
    return re.sub(r"\d+", "", ref), int(re.sub(r"\D", "", ref) or 0)


def check_rules(ctx, files):
    if not ctx.pcb:
        return _skip("no board")
    rel = os.path.relpath(ctx.pcb, ctx.root).replace(os.sep, "/")
    head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ctx.root, capture_output=True, encoding="utf-8")
    return board_rules(open(ctx.pcb, encoding="utf-8").read(), head.stdout if head.returncode == 0 else None)


# ---------------------------------------------------------------- main
CHECKS = {  # name -> (function, which changed files make it relevant)
    "format": (check_format, lambda f: f.endswith(KICAD_FILES)),
    "paths": (check_paths, lambda f: f.endswith(KICAD_FILES)),
    "english": (check_english, lambda f: True),
    "python": (check_python, lambda f: f.endswith(".py")),
    "erc": (check_erc, lambda f: f.endswith((".kicad_sch", ".kicad_sym", ".kicad_pro"))),
    "drc": (check_drc, lambda f: f.endswith((".kicad_pcb", ".kicad_sch", ".kicad_mod", ".kicad_pro"))),
    "assembly": (check_assembly, lambda f: f.endswith((".kicad_pcb", ".csv"))),
    "rules": (check_rules, lambda f: f.endswith(".kicad_pcb")),
}


def find_one(root, explicit, pattern, what):
    if explicit:
        return os.path.abspath(explicit)
    found = sorted(glob.glob(os.path.join(glob.escape(root), pattern), recursive=True))
    if len(found) > 1:
        raise ValueError(f"several {what} found ({', '.join(os.path.relpath(x, root) for x in found)}); pass one explicitly")
    return found[0] if found else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checks", nargs="*", help=f"checks to run (default: all): {', '.join(CHECKS)}")
    ap.add_argument("--hook", action="store_true", help="only checks relevant to the staged files")
    ap.add_argument("--strict", action="store_true", help="ERC/DRC warnings fail too")
    ap.add_argument("--english", action="store_true", help="enable the english check")
    ap.add_argument("--sch"), ap.add_argument("--pcb"), ap.add_argument("--bom"), ap.add_argument("--cpl")
    ap.add_argument("--lcsc-field", default="LCSC", help="footprint field with the LCSC number (default LCSC)")
    ap.add_argument("--no-rotation", action="store_true", help="do not compare CPL rotations with the board")
    args = ap.parse_args()
    unknown = [c for c in args.checks if c not in CHECKS]
    if unknown:
        ap.error(f"unknown check(s): {', '.join(unknown)}")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # seen live when redirected

    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, encoding="utf-8").stdout.strip() or os.getcwd()
    try:
        ctx = argparse.Namespace(root=root, strict=args.strict, lcsc_field=args.lcsc_field, rotation=not args.no_rotation,
                                 sch=find_one(root, args.sch, "*.kicad_sch", "schematics"),
                                 pcb=find_one(root, args.pcb, "*.kicad_pcb", "boards"), bom=args.bom, cpl=args.cpl)
    except ValueError as e:
        print(e)
        return 2
    names = args.checks or [c for c in CHECKS if c != "english" or args.english]
    if os.path.isdir(os.path.join(root, ".git")) or subprocess.run(["git", "-C", root, "status"], capture_output=True).returncode == 0:
        files = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR") if args.hook else \
            [f for f in git(root, "ls-files", "-co", "--exclude-standard") if os.path.isfile(os.path.join(root, f))]
    else:
        files = [os.path.relpath(p, root) for p in glob.glob(os.path.join(glob.escape(root), "**", "*"), recursive=True) if os.path.isfile(p)]

    failed = False
    for name in names:
        func, relevant = CHECKS[name]
        targets = [f for f in files if relevant(f)]
        if args.hook and not targets:
            continue
        try:
            r = func(ctx, targets)
        except (kicad_cli.KicadError, OSError, KeyError, ValueError) as e:  # a check that cannot run must not pass
            r = Result()
            r.errors.append(f"could not run: {e}")
        extra = f"  ({', '.join(r.warnings)})" if r.warnings else ""
        extra += f"  ({r.skipped})" if r.skipped else ""
        print(f"{name:9} {'FAIL' if r.errors else 'skip' if r.skipped else 'ok'}{extra}")
        for e in r.errors[:30]:
            print(f"    {e}")
        if len(r.errors) > 30:
            print(f"    ... and {len(r.errors) - 30} more")
        failed |= bool(r.errors)
    if failed and args.hook:
        print("\ncommit blocked: fix the problems above, or exclude accepted ERC/DRC items in KiCad")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

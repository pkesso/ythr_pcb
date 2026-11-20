"""Find and run KiCad's command line tools.

Usage as a module:
    import kicad_cli
    kicad_cli.ensure_ready()               # KiCad 10+, English UI language; raises with an explanation otherwise
    kicad_cli.run("pcb", "drc", ...)       # run kicad-cli, raise on failure
    kicad_cli.upgrade("pcb", "board.kicad_pcb")   # resave in the current KiCad format, with LF line endings
    kicad_cli.kicad_file("ngspice.dll")    # a file in KiCad's bin directory
    kicad_cli.freerouting()                # the Freerouting program (the autorouter), or raise
    kicad_cli.run_process(cmd, timeout)    # run a program: stdin closed, its whole process tree killed on timeout
    kicad_cli.writable("out/board.kicad_pcb")   # create out/ and check the file can be written, before the long work
    kicad_cli.lower_priority("below")      # like nice 10 (Windows: below normal); inherited by every child process

Usage from the shell:
    python kicad_cli.py doctor             # report KiCad (where, version, English) and Freerouting

Freerouting is looked up in this order: the FREEROUTING environment variable (the program's path), `freerouting` on
PATH, then the default Windows installs: %LOCALAPPDATA%\\freerouting\\freerouting.exe (the per-user MSI) and
%ProgramFiles%\\freerouting\\freerouting.exe. The skill never downloads it: if it is missing, ask the user to install it.

KiCad's bin directory is looked up in this order:
  1. the KICAD_BIN environment variable;
  2. the directory of kicad-cli on PATH (Linux packages put it there);
  3. on Windows, the default install: %ProgramFiles%\\KiCad\\<newest version>\\bin.
No absolute path is hard-coded: on macOS, or with KiCad installed elsewhere, set KICAD_BIN.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

MIN_MAJOR = 10
CLI = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
# kicad-cli localises the top-level --help text with KiCad's UI language (subcommand help stays English)
ENGLISH_MARKER = "Shows help message and exits"


class KicadError(RuntimeError):
    pass


def _version_key(path):
    m = re.search(r"(\d+)\.(\d+)", path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def candidate_dirs():
    if os.environ.get("KICAD_BIN"):
        yield os.environ["KICAD_BIN"]
        return
    on_path = shutil.which("kicad-cli")
    if on_path:
        yield os.path.dirname(on_path)
    if os.environ.get("ProgramFiles"):
        dirs = glob.glob(os.path.join(os.environ["ProgramFiles"], "KiCad", "*", "bin"))
        yield from sorted(dirs, key=_version_key, reverse=True)


def kicad_bin():
    """KiCad's bin directory (see the module docstring for the search order)."""
    for d in candidate_dirs():
        if os.path.isfile(os.path.join(d, CLI)):
            return d
    if os.environ.get("KICAD_BIN"):
        raise KicadError(f"KICAD_BIN={os.environ['KICAD_BIN']} does not contain {CLI}")
    raise KicadError(f"KiCad not found. Install KiCad {MIN_MAJOR} or set KICAD_BIN to its bin directory.")


def kicad_file(name):
    """Path of a file in KiCad's bin directory, e.g. ngspice.dll."""
    path = os.path.join(kicad_bin(), name)
    if not os.path.isfile(path):
        raise KicadError(f"{path} not found")
    return path


KICAD_TIMEOUT = 900  # s: kicad-cli calls (a DRC with zone refill on a big board takes a minute)


def kill_tree(pid):
    """Stop a process and everything it started (a shell's children, KiCad's Python, Freerouting's JVM)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


HEARTBEAT = 30  # s without a line of output after which a waiting script says it is still waiting, and on what


def short_name(cmd):
    """The program and its first plain arguments, for progress lines: 'kicad-cli pcb render'."""
    parts = cmd.split() if isinstance(cmd, str) else [str(c) for c in cmd]
    words = [os.path.splitext(os.path.basename(parts[0]))[0]] if parts else ["?"]
    for a in parts[1:]:
        if a.startswith("-") or os.sep in a or "/" in a or "." in a or len(words) == 3:
            break
        words.append(a)
    return " ".join(words)


def run_process(cmd, timeout, shell=False, echo=None, echo_filter=None, heartbeat=True, **kw):
    """subprocess.run for every external program the scripts start: stdin closed (nothing can wait for input), and on
    timeout the whole process tree is killed, not only the shell, before TimeoutError is raised. kw goes to Popen
    (encoding, errors, cwd, env).

    echo: a prefix (e.g. "  freerouting: "), to also print the program's output line by line as it comes, so a long
    run can be watched live; the output is captured all the same. Text mode only (pass encoding). echo_filter: a
    function from a line to the text to show, or None to leave the line out (it is still captured).

    heartbeat: after HEARTBEAT seconds with nothing printed, a line says the program is still running and for how
    long, and again every HEARTBEAT seconds, so a long wait is never taken for a hang."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    env = kw.pop("env", None) or os.environ
    env = {**env, "PYTHONUNBUFFERED": "1"}  # a Python child shows its output as it goes, not when it ends
    p = subprocess.Popen(cmd, shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, **group, **kw)
    what = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    start = time.monotonic()
    deadline = start + timeout
    last = [start]  # when the last line was shown

    def beat():
        now = time.monotonic()
        if heartbeat and now - last[0] >= HEARTBEAT - 0.5:
            print(f"  ... {short_name(cmd)} still running, {now - start:.0f} s", flush=True)
            last[0] = now

    def timed_out():
        kill_tree(p.pid)
        raise TimeoutError(f"{what} did not finish in {timeout:g} s")

    if echo is None:
        while True:
            try:
                out, err = p.communicate(timeout=max(0.01, min(HEARTBEAT, deadline - time.monotonic())))
                return subprocess.CompletedProcess(cmd, p.returncode, out, err)
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    try:
                        timed_out()
                    finally:
                        p.communicate()
                beat()

    import threading
    chunks = {"out": [], "err": []}

    def pump(stream, key):
        for line in stream:
            chunks[key].append(line)
            shown = echo_filter(line.rstrip("\r\n")) if echo_filter else line.rstrip("\r\n")
            if shown is not None:
                print(echo + shown, flush=True)
                last[0] = time.monotonic()

    readers = [threading.Thread(target=pump, args=(s, k), daemon=True)
               for s, k in ((p.stdout, "out"), (p.stderr, "err"))]
    for t in readers:
        t.start()
    try:
        while True:
            try:
                p.wait(timeout=max(0.01, min(5, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    try:
                        timed_out()
                    finally:
                        p.wait()
                beat()
    finally:
        for t in readers:
            t.join(5)  # a grandchild still holding the pipe must not keep us here
    return subprocess.CompletedProcess(cmd, p.returncode, "".join(chunks["out"]), "".join(chunks["err"]))


def lower_priority(level="below"):
    """Lower this process's CPU priority, like `nice`; everything it starts afterwards (KiCad's Python, Freerouting's
    JVM, ngspice) inherits it. level: "below" (below normal, nice 10), "idle" (nice 19) or "normal" (left as it is).
    Never raises the priority: a process already lower stays where it is."""
    if level == "normal":
        return
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetPriorityClass.argtypes = [ctypes.c_void_p]
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        h = k.GetCurrentProcess()
        want = {"below": 0x4000, "idle": 0x40}[level]  # BELOW_NORMAL_PRIORITY_CLASS, IDLE_PRIORITY_CLASS
        rank = {0x40: 0, 0x4000: 1, 0x20: 2, 0x8000: 3, 0x80: 4, 0x100: 5}  # idle < below < normal < above < high
        if rank.get(k.GetPriorityClass(h), 2) > rank[want]:
            k.SetPriorityClass(h, want)
    else:
        want = {"below": 10, "idle": 19}[level]
        now = os.nice(0)
        if now < want:
            os.nice(want - now)


def add_priority_argument(ap):
    """--priority for the scripts that run long, CPU-heavy work (routing, searches, renders, DRC)."""
    ap.add_argument("--priority", choices=("below", "idle", "normal"), default="below",
                    help="CPU priority of this run and everything it starts (default below: below normal, like nice "
                         "10, so the machine stays responsive; idle can stall under other load and hit the timeouts)")


def writable(path, directory=False):
    """Make sure a result can be saved before the long work that produces it: create its directory (or the directory
    itself with directory=True) and try writing there; an existing file must be openable for writing (not read-only,
    not locked by KiCad or a viewer). The file itself is not changed. Raises OSError with the reason; returns path.
    An existing directory is checked as a directory without directory=True (opening it as a file failed)."""
    path = os.path.abspath(path)
    directory = directory or os.path.isdir(path)
    d = path if directory else os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    import tempfile
    fd, probe = tempfile.mkstemp(dir=d, prefix=".write-check-")
    os.close(fd)
    os.remove(probe)
    if not directory and os.path.exists(path):
        with open(path, "r+b"):
            pass
    return path


def _raw(*args):
    try:
        return run_process([os.path.join(kicad_bin(), CLI), *args], KICAD_TIMEOUT)
    except TimeoutError as e:
        raise KicadError(str(e))


def run(*args):
    """Run kicad-cli; raise KicadError with its output if it fails."""
    p = _raw(*args)
    if p.returncode:
        out = (p.stdout + p.stderr).decode(errors="replace")
        raise KicadError(f"kicad-cli {' '.join(args)} failed:\n{out}")
    return p


def version():
    """KiCad version as a tuple of ints, e.g. (10, 0, 2)."""
    out = run("version").stdout.decode().strip()
    return tuple(int(x) for x in re.findall(r"\d+", out)[:3])


def is_english():
    """True when kicad-cli (and so KiCad's UI language setting) is English."""
    p = _raw("--help")
    return ENGLISH_MARKER in (p.stdout + p.stderr).decode(errors="replace")


def ensure_ready():
    """Check that KiCad >= MIN_MAJOR is installed and set to English; raise KicadError with what to do."""
    v = version()
    if v[0] < MIN_MAJOR:
        raise KicadError(f"KiCad {'.'.join(map(str, v))} found in {kicad_bin()}; KiCad {MIN_MAJOR} or newer is needed.")
    if not is_english():
        raise KicadError("KiCad is not set to English, so kicad-cli reports are localised. Ask the user to switch it: "
                         "KiCad main window > Preferences > Set Language > English, then restart KiCad.")
    return v


def freerouting():
    """Path of the Freerouting program (see the module docstring for the search order)."""
    if os.environ.get("FREEROUTING"):
        if os.path.isfile(os.environ["FREEROUTING"]):
            return os.environ["FREEROUTING"]
        raise KicadError(f"FREEROUTING={os.environ['FREEROUTING']} is not a file")
    found = shutil.which("freerouting")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        path = os.path.join(base or "", "freerouting", "freerouting.exe")
        if base and os.path.isfile(path):
            return path
    raise KicadError("Freerouting not found. Ask the user to install it (https://github.com/freerouting/freerouting/"
                     "releases; on Windows the MSI) or to set FREEROUTING to the program's path; don't download it.")


def freerouting_version():
    """Freerouting's version as a string, from the first line it prints."""
    try:
        p = run_process([freerouting(), "--help", "-l", "en"], 120)
    except TimeoutError as e:
        raise KicadError(str(e))
    m = re.search(r"Freerouting(?: CLI)? v(\d+\.\d+\.\d+)", (p.stdout + p.stderr).decode(errors="replace"))
    if not m:
        raise KicadError(f"{freerouting()} did not report a version")
    return m.group(1)


def to_lf(path):
    """kicad-cli writes CRLF on Windows; keep repositories on LF."""
    data = open(path, "rb").read()
    if b"\r\n" in data:
        open(path, "wb").write(data.replace(b"\r\n", b"\n"))


def upgrade(kind, path):
    """Resave a KiCad file or library in the current format. kind: sch, pcb, sym or fp (fp takes a .pretty dir)."""
    run(kind, "upgrade", "--force", path)
    files = glob.glob(os.path.join(path, "*.kicad_mod")) if os.path.isdir(path) else [path]
    for f in files:
        to_lf(f)


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "doctor":
        print(__doc__)
        return 2
    try:
        d = kicad_bin()
        print(f"KiCad bin:  {d}")
        v = version()
        print(f"Version:    {'.'.join(map(str, v))}" + ("" if v[0] >= MIN_MAJOR else f"  (need {MIN_MAJOR}+)"))
        eng = is_english()
        print(f"Language:   {'English' if eng else 'NOT English - switch KiCad to English (Preferences > Set Language)'}")
        ngspice = os.path.join(d, "ngspice.dll" if os.name == "nt" else "libngspice.so")
        print(f"ngspice:    {ngspice if os.path.isfile(ngspice) else 'not in the bin directory'}")
    except KicadError as e:
        print(e)
        return 1
    try:
        print(f"Freerouting: {freerouting()} (v{freerouting_version()})")
    except (KicadError, OSError) as e:
        print(f"Freerouting: {e}")
        return 1
    return 0 if v[0] >= MIN_MAJOR and eng else 1


if __name__ == "__main__":
    sys.exit(main())

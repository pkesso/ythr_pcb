"""Claude Code PreToolUse hook for board projects: stops shell commands that break the skill's rules before they run.

Reads the hook's JSON from stdin (tool_name Bash or PowerShell, tool_input.command) and prints a decision:

- denied: Python started interactively - `python -` (with a heredoc or a pipe), Python with no script (`python`,
  `cat x | python`, `python < file`, `python <<EOF`), `python -i`. Such a process can wait for input forever.
  `python file.py`, `python -c "..."`, `python -m module`, `python -V` pass.
- warned (the command still runs): `pip install` not through a .venv's Python; a long run of the skill's scripts
  (freeroute, place_search, views, gerber, determinism) piped through tail, head or grep, which hides its progress.

sync_tools.py copies this file into the project's tools/ and adds the hook to the project's .claude/settings.json.
Test it by hand:  echo '{"tool_name":"Bash","tool_input":{"command":"python -"}}' | python guard_shell.py
Exit code 0 always; a failure inside the hook exits 1, which Claude Code reports without blocking the command.
"""
import json
import os
import re
import shlex
import sys

PYTHON = re.compile(r"^(?:python(?:w)?(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?$", re.I)
PIP = re.compile(r"^pip(?:\d+(?:\.\d+)*)?(?:\.exe)?$", re.I)
SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", ")", "{", "}", "\n", "!", "then", "do", "else", "elif"}
WRAPPERS = {"nice", "timeout", "nohup", "exec", "time", "command", "env", "stdbuf", "sudo"}
OPTION_ARG = {"-W", "-X"}  # options of python that take the next word as their value
HARMLESS = {"-V", "-VV", "--version", "-h", "--help", "-?"}
LONG_RUNS = {"freeroute.py", "place_search.py", "views.py", "gerber.py", "determinism.py"}
FILTERS = {"tail", "head", "grep", "Select-Object", "Select-String"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command):
    """The command without the bodies of its heredocs, which are data and may mention anything."""
    out, end = [], None
    for line in command.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = HEREDOC.search(line)
        if m:
            end = m.group(2)
    return "\n".join(out)


def words(command):
    """Shell words and separators, quotes removed; a newline is a separator."""
    lexer = shlex.shlex(command.replace("\n", " \n "), posix=False, punctuation_chars=";&|()<>")
    lexer.whitespace = " \t\r"
    lexer.wordchars += "\\:$%{}[]=+,@~*?!#^."
    out = []
    try:
        for w in lexer:
            if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"":
                w = w[1:-1]
            out.append(w)
    except ValueError:  # an unclosed quote: a partial word list is still worth checking
        pass
    return out


def segments(ws):
    """Simple commands as (words, separator before it)."""
    cur, before, out = [], ";", []
    for w in ws:
        if w in SEPARATORS:
            if cur:
                out.append((cur, before))
            cur, before = [], w
        else:
            cur.append(w)
    if cur:
        out.append((cur, before))
    return out


def command_words(seg):
    """The words of a simple command from the program on: assignments and wrappers (nice -n 10, timeout 900, env)
    skipped."""
    i = 0
    while i < len(seg):
        w = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif os.path.basename(w) in WRAPPERS:
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or re.match(r"^\d+[smhd]?$", seg[i])
                                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i])):
                i += 2 if seg[i] in ("-n", "-s", "-k", "--signal", "--kill-after") else 1
        else:
            return seg[i:]
    return []


def program(w):
    return re.split(r"[\\/]", w)[-1]


def python_problem(args):
    """Why this run of Python is interactive, or None when it names a script, -c or -m."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("<", "<<", "<<<"):
            return "reads its program from standard input (a redirect or heredoc)"
        if a == "-":
            return "`python -` reads its program from standard input"
        if a in HARMLESS:
            return None
        if a == "-i" or (re.match(r"^-[A-Za-z]+$", a) and "i" in a[1:] and "c" not in a and "m" not in a):
            return "`-i` starts the interactive interpreter"
        if a in ("-c", "-m") or re.match(r"^-[A-Za-z]*[cm]", a):
            return None
        if a in OPTION_ARG:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return None  # a script
    return "Python with no script waits for input"


def check(command):
    """(denials, warnings) for one shell command."""
    deny, warn = [], []
    segs = segments(words(strip_heredocs(command)))
    pipeline_long = None
    for seg, before in segs:
        cmd = command_words(seg)
        if before != "|":
            pipeline_long = None
        if not cmd:
            continue
        name, args = program(cmd[0]), cmd[1:]
        if before == "|" and pipeline_long and name in FILTERS:
            warn.append(f"`{pipeline_long}` piped through `{name}`: the user sees nothing until it ends. Let it print "
                        "in full (run it in the background; its output file is the log) and filter the file afterwards.")
        if PYTHON.match(name):
            why = python_problem(args)
            if why:
                deny.append(f"{why}: `{' '.join(cmd)[:80]}`. Never start Python interactively here (it can wait "
                            "forever). Write the code to a .py file and run `python file.py`, or use `python -c \"...\"`.")
            elif args[:2] == ["-m", "pip"] and "install" in args and ".venv" not in cmd[0] and "venv" not in cmd[0]:
                warn.append("`pip install` through a Python that is not a .venv's: install only into the project's "
                            ".venv (or the skill's own environment).")
            script = next((program(a) for a in args if a.endswith(".py")), None)
            if script in LONG_RUNS:
                pipeline_long = script
        elif PIP.match(name) and "install" in args and "venv" not in cmd[0]:
            warn.append("`pip install` with a bare pip may install outside the .venv: use the .venv's Python, "
                        "`.venv/Scripts/python -m pip install ...`.")
    return deny, warn


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command") or ""
    deny, warn = check(command)
    if deny:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": "pcb skill: " + " ".join(deny)}}
    elif warn:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "pcb skill: " + " ".join(warn)}}
    else:
        return 0
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

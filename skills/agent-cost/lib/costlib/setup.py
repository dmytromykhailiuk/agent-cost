import difflib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import core

BEGIN = "// agent-cost:begin"
END = "// agent-cost:end"
SNIPPET = core.SKILL_DIR / "assets" / "dashboard-snippet.js"
RECENT_HOOK_S = 24 * 3600
LABEL_ANCHOR = "[${prof.name}]${R} ${B}${ACCENT}◆"
LABEL_PATCHED = "[${prof.name}]${R} ${ac && ac.label ? ac.label + ' ' : ''}${B}${ACCENT}◆"
PROFILE_RE = re.compile(r"^(?P<indent>[ \t]*)const prof = claudeProfile\(\);[^\n]*$", re.M)
COST_RE = re.compile(r"^(?P<indent>[ \t]*)if \(d\.cost\?\.total_cost_usd != null\) parts\.push\([^\n]*\);[ \t]*$", re.M)
PUSH_RE = re.compile(r"^(?P<indent>[ \t]*)lines\.push\(wrap\(parts, SEP\)\);[ \t]*$", re.M)
LINE_PUSH = "if (ac && ac.line) lines.push(ac.line);"
LINE_PUSH_RE = re.compile(r"\n[ \t]*" + re.escape(LINE_PUSH))
SNIPPET_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n\n?", re.S)
OLD_COST_RE = re.compile(r"^(?P<first>[ \t]*)if \(ac && ac\.segment\) parts\.push\(ac\.segment\);\n(?P<indent>[ \t]*)else (?=if \(d\.cost)", re.M)
NEW_COST_RE = re.compile(r"^([ \t]*)if \(!\(ac && ac\.line\)\) (?=if \(d\.cost)", re.M)
AC_DECL_RE = re.compile(r"\n[ \t]*const ac = agentCost\(d\);")
FUNC_RE = re.compile(r"^function claudeProfile\(\) \{", re.M)


class PatchError(Exception):
    pass


def default_dashboard():
    return Path.home() / ".claude" / "statusline" / "dashboard.js"


def _one(regex, text, what):
    matches = list(regex.finditer(text))
    if len(matches) != 1:
        raise PatchError(f"anchor not found ({len(matches)} matches): {what}")
    return matches[0]


def is_patched(text):
    return BEGIN in text


def is_current(text):
    return is_patched(text) and LINE_PUSH in text


def revert_patch(text):
    out = SNIPPET_RE.sub("", text)
    out = OLD_COST_RE.sub(r"\g<first>", out)
    out = NEW_COST_RE.sub(r"\g<1>", out)
    out = LINE_PUSH_RE.sub("", out)
    out = AC_DECL_RE.sub("", out)
    return out.replace(LABEL_PATCHED, LABEL_ANCHOR)


def apply_patch(text):
    if is_current(text):
        return text
    text = revert_patch(text)
    snippet = SNIPPET.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
    func = _one(FUNC_RE, text, "function claudeProfile()")
    prof = _one(PROFILE_RE, text, "const prof = claudeProfile();")
    cost = _one(COST_RE, text, "if (d.cost?.total_cost_usd != null) parts.push(...)")
    push = _one(PUSH_RE, text, "lines.push(wrap(parts, SEP));")
    if text.count(LABEL_ANCHOR) != 1:
        raise PatchError(f"anchor not found ({text.count(LABEL_ANCHOR)} matches): profile label template")
    if not (func.start() < prof.start() < cost.start() < push.start()):
        raise PatchError("anchors are in an unexpected order")
    cost_new = f"{cost.group('indent')}if (!(ac && ac.line)) {cost.group(0).strip()}"
    prof_new = f"{prof.group(0)}\n{prof.group('indent')}const ac = agentCost(d);"
    push_new = f"{push.group(0)}\n{push.group('indent')}{LINE_PUSH}"
    edits = [(func.start(), func.start(), snippet), (prof.start(), prof.end(), prof_new),
             (cost.start(), cost.end(), cost_new), (push.start(), push.end(), push_new)]
    out = text
    for start, end, repl in sorted(edits, reverse=True):
        out = out[:start] + repl + out[end:]
    return out.replace(LABEL_ANCHOR, LABEL_PATCHED, 1)


def _diff(old, new, name):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"{name}", f"{name} (agent-cost)"))


def node_check(path):
    try:
        res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def cmd_setup(args):
    target = Path(getattr(args, "dashboard", None) or default_dashboard()).expanduser()
    try:
        old = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"agent-cost setup: cannot read {target}: {exc}", file=sys.stderr)
        return 1
    try:
        new = apply_patch(old)
    except PatchError as exc:
        print(f"agent-cost setup: {exc}", file=sys.stderr)
        return 1
    if new == old:
        if getattr(args, "apply", False):
            core.write_cli_pointer()
        print(f"already applied: {target}")
        return 0
    if not getattr(args, "apply", False):
        sys.stdout.write(_diff(old, new, str(target)))
        print(f"\n(dry run) re-run with --apply to patch {target}")
        return 0
    backup = target.with_name(f"{target.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)
    target.write_text(new, encoding="utf-8")
    ok, message = node_check(target)
    if not ok:
        shutil.copy2(backup, target)
        print(f"agent-cost setup: node --check failed, restored {target}\n{message}", file=sys.stderr)
        return 1
    core.write_cli_pointer()
    print(f"patched: {target}\nbackup:  {backup}")
    return 0


def _hook_recency():
    newest, count = None, 0
    base = core.state_dir() / "sessions"
    files = [p for p in base.glob("*/*.json") if not p.name.endswith(".live.json")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:50]:
        rec = core.read_json(path, {}) or {}
        events = rec.get("hook_events")
        if not isinstance(events, dict):
            continue
        for value in events.values():
            ts = core.parse_ts(value)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        count += 1
    return newest, count


def cmd_doctor(args):
    problems = 0

    def line(ok, label, detail):
        nonlocal problems
        if not ok:
            problems += 1
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {detail}")

    v = sys.version_info
    line(v >= (3, 10), "python", f"{v.major}.{v.minor}.{v.micro}")
    state = core.state_dir()
    line(True, "state dir", str(state))
    pointer = core.read_json(state / "cli.json", {}) or {}
    path = pointer.get("path")
    line(bool(path) and Path(path).is_file(), "cli.json pointer", path or "missing")
    newest, count = _hook_recency()
    if newest is None:
        line(False, "hooks", "no hook events recorded yet")
    else:
        age = core.now() - newest
        line(age <= RECENT_HOOK_S, "hooks", f"last event {core.iso(newest)} ({int(age // 60)} min ago, {count} sessions)")
    dash = Path(getattr(args, "dashboard", None) or default_dashboard()).expanduser()
    try:
        patched = is_patched(dash.read_text(encoding="utf-8"))
        line(patched, "dashboard.js", f"{dash} {'has' if patched else 'lacks'} the agent-cost snippet")
    except OSError:
        line(False, "dashboard.js", f"{dash} not readable")
    return 1 if problems else 0

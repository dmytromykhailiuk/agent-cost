import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from . import aggregate, auxlink, core, registry, scan

PLACEHOLDER = "/*__DATA__*/null"
KEEP_REPORTS = 20
PAYLOAD_DAYS = 90
REFRESH_WINDOW = "2d"
FIRST_RUN_WINDOW = f"{PAYLOAD_DAYS}d"
DEFAULT_VISIBLE_ROOTS = ("~/Desktop",)
DEFAULT_PROJECT_ROOTS = (("projects", "~/projects"), ("projects", "~/Desktop/projects"),
                         ("npm-packages", "~/Desktop/npm-packages"), ("code", "~/code"), ("src", "~/src"),
                         ("dev", "~/dev"), ("work", "~/work"), ("claude", "~/.claude"))


def template_path():
    return Path(core.SKILL_DIR) / "assets" / "report.html"


def inject(template, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")
    if PLACEHOLDER not in template:
        raise ValueError("template has no %s placeholder" % PLACEHOLDER)
    return template.replace(PLACEHOLDER, payload, 1)


def config_path():
    return core.config_path()


def project_roots():
    cfg = core.read_json(config_path(), {})
    raw = cfg.get("project_roots") if isinstance(cfg, dict) else None
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict) or not item.get("path"):
                continue
            path = str(Path(str(item["path"])).expanduser())
            out.append({"name": item.get("name") or Path(path).name, "path": path})
        return out
    out = []
    for name, path in DEFAULT_PROJECT_ROOTS:
        p = Path(path).expanduser()
        if p.is_dir():
            out.append({"name": name, "path": str(p)})
    return out


def visible_roots():
    cfg = core.read_json(config_path(), {})
    raw = cfg.get("visible_roots") if isinstance(cfg, dict) else None
    if not isinstance(raw, list):
        raw = list(DEFAULT_VISIBLE_ROOTS)
    out = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(auxlink._resolved(item.strip()))
    return out


def root_cwd(data):
    sessions = data.get("sessions") or []
    root_sid = data.get("root_session_id")
    root = next((s for s in sessions if s.get("session_id") == root_sid), sessions[0] if sessions else {})
    return root.get("cwd")


def is_hidden(cwd, vroots):
    if not vroots:
        return False
    if not cwd:
        return True
    path = auxlink._resolved(cwd)
    return not any(auxlink.inside(path, v) for v in vroots)


def mark_hidden(roots, vroots=None):
    vroots = visible_roots() if vroots is None else vroots
    return [dict(r, hidden=is_hidden(root_cwd(r), vroots)) for r in roots]


def _quiet_scan(since):
    scan.run_scan(window=scan.parse_since(since))


def _recent_root_dirs(cutoff):
    base = core.state_dir() / "sessions"
    out = []
    for d in base.glob("*"):
        if not (d.is_dir() and registry.valid_sid(d.name)):
            continue
        try:
            if any(p.stat().st_mtime >= cutoff for p in d.glob("*.json")):
                out.append(d.name)
        except OSError:
            continue
    return out


def refresh(since=REFRESH_WINDOW, cutoff=None):
    started = core.now()
    if cutoff is None:
        if not any(core.roots_dir().glob("*.json")):
            since = FIRST_RUN_WINDOW
        cutoff = started - scan.parse_since(since)
    written = set()
    try:
        written |= scan.run_scan(cutoff=cutoff)["roots"]
    except Exception as exc:
        print(f"agent-cost: scan failed: {exc}", file=sys.stderr)
    for root in _recent_root_dirs(cutoff):
        if root in written:
            continue
        path = core.roots_dir() / f"{root}.json"
        try:
            if path.is_file() and path.stat().st_mtime >= started:
                continue
            aggregate.write_root(root)
            written.add(root)
        except (OSError, ValueError):
            continue
    return written


def load_roots(days=PAYLOAD_DAYS):
    cutoff = None if days is None else core.now() - days * 86400
    out = []
    for summary in aggregate.list_roots():
        last = core.parse_ts(summary.get("last_activity_at") or summary.get("started_at"))
        if cutoff is not None and (last is None or last < cutoff):
            continue
        data = core.read_json(summary["path"])
        if isinstance(data, dict):
            out.append(data)
    return out


def env_session():
    for key in ("AGENT_COST_SESSION", "CLAUDE_CODE_SESSION_ID"):
        sid = os.environ.get(key)
        if registry.valid_sid(sid):
            return sid
    return None


def resolve_focus(sid):
    if not registry.valid_sid(sid):
        return None, None
    root = registry.find_root(sid)
    if root is None and (core.roots_dir() / f"{sid}.json").is_file():
        root = sid
    return root, sid if root else None


def _latest_root():
    roots = aggregate.list_roots()
    vroots = visible_roots()
    shown = [r for r in roots if not is_hidden(r.get("cwd"), vroots)]
    pick = shown or roots
    return pick[0]["root_session_id"] if pick else None


def _report_path(out):
    if out:
        path = Path(out).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    base = core.reports_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"agent-cost-{stamp}.html"
    n = 1
    while path.exists():
        path = base / f"agent-cost-{stamp}-{n}.html"
        n += 1
    return path.resolve()


def prune_reports(keep=KEEP_REPORTS):
    files = sorted(core.reports_dir().glob("agent-cost-*.html"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def build_payload(focus_root, roots, now=None, vroots=None):
    vroots = visible_roots() if vroots is None else vroots
    return {
        "generated_at": core.iso(core.now() if now is None else now),
        "focus_root_session_id": focus_root,
        "project_roots": project_roots(),
        "visible_roots": vroots,
        "roots": roots,
    }


def collect(focus_root=None, days=PAYLOAD_DAYS):
    focus_root = focus_root or _latest_root()
    roots = load_roots(days)
    if focus_root and not any(r.get("root_session_id") == focus_root for r in roots):
        data = core.read_json(core.roots_dir() / f"{focus_root}.json")
        if isinstance(data, dict):
            roots.append(data)
    vroots = visible_roots()
    return build_payload(focus_root, mark_hidden(roots, vroots), vroots=vroots)


def cmd_report(args):
    wanted = getattr(args, "session", None)
    if wanted and not registry.valid_sid(wanted):
        print(f"agent-cost report: invalid session id {wanted!r}", file=sys.stderr)
        return 2
    refresh()
    if not (args.latest or args.all):
        wanted = wanted or env_session()
    focus_root, focus_sid = resolve_focus(wanted) if wanted else (None, None)
    if wanted and focus_root is None and getattr(args, "session", None):
        _quiet_scan(FIRST_RUN_WINDOW)
        focus_root, focus_sid = resolve_focus(wanted)
        if focus_root is None:
            print(f"agent-cost report: session {wanted} not found", file=sys.stderr)
            return 1
    if focus_root is None:
        focus_root = focus_sid = _latest_root()

    payload = collect(focus_root, None if args.all else PAYLOAD_DAYS)
    roots = payload["roots"]
    path = _report_path(args.out)
    path.write_text(inject(template_path().read_text(encoding="utf-8"), payload), encoding="utf-8")
    prune_reports()

    focus = next((r for r in roots if r.get("root_session_id") == focus_root), None)
    if focus:
        t = focus.get("totals") or {}
        print(f"agent-cost report: {len(roots)} roots, focus {focus_root[:8]} ${t.get('cost_usd') or 0:.2f} "
              f"({max(0, (t.get('sessions') or 1) - 1)} herdr agents, {t.get('builtin_agents') or 0} builtin)")
    else:
        print(f"agent-cost report: {len(roots)} roots, no focus session")
    if not args.no_open:
        anchor = focus_sid or focus_root
        try:
            webbrowser.open(path.as_uri() + (f"#{anchor}" if anchor else ""))
        except Exception:
            pass
    print(str(path))
    return 0


def fmt_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_time(value):
    ts = core.parse_ts(value)
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_usd(value):
    return "-" if value is None else f"${value:.2f}"


def _list_time(t, duration):
    if not isinstance(t, dict) or not isinstance(t.get("active_s"), (int, float)):
        return None
    out = {k: int(t.get(k) or 0) for k in ("wall_s", "active_s", "waiting_s", "idle_s")}
    if duration is not None and duration > out["wall_s"]:
        out["idle_s"] += duration - out["wall_s"]
        out["wall_s"] = duration
    return out


def _list_rows(limit, show_all=False):
    rows = []
    vroots = [] if show_all else visible_roots()
    shown = [s for s in aggregate.list_roots() if not is_hidden(s.get("cwd"), vroots)]
    for s in shown[: max(0, limit)]:
        start = core.parse_ts(s.get("started_at"))
        last = core.parse_ts(s.get("last_activity_at"))
        if s.get("state") == "live":
            last = max(last or 0, core.now())
        duration = int(last - start) if start is not None and last is not None and last >= start else None
        t = _list_time(s.get("time"), duration)
        if t:
            duration = t["wall_s"]
        rows.append({
            "root_session_id": s["root_session_id"],
            "started_at": s.get("started_at"),
            "project": Path(s["cwd"]).name if s.get("cwd") else None,
            "cwd": s.get("cwd"),
            "title": s.get("title"),
            "state": s.get("state"),
            "duration_s": duration,
            "time": t,
            "cost_usd": s.get("cost_usd"),
            "herdr_agents": max(0, (s.get("sessions") or 1) - 1),
            "builtin_agents": s.get("builtin_agents") or 0,
        })
    return rows


def cmd_list(args):
    show_all = bool(getattr(args, "all", False))
    rows = _list_rows(args.limit, show_all)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no sessions recorded")
        return 0
    table = [("START", "PROJECT", "STATE", "DURATION", "ACTIVE", "WAITING", "IDLE", "TOTAL", "AGENTS", "SESSION")]
    for r in rows:
        agents = str(r["herdr_agents"]) + (f"+{r['builtin_agents']}b" if r["builtin_agents"] else "")
        t = r["time"] or {}
        table.append((fmt_time(r["started_at"]), r["project"] or "-", r["state"] or "-",
                      fmt_duration(r["duration_s"]), fmt_duration(t.get("active_s")), fmt_duration(t.get("waiting_s")),
                      fmt_duration(t.get("idle_s")), fmt_usd(r["cost_usd"]), agents, r["root_session_id"]))
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    right = {3, 4, 5, 6, 7, 8}
    for row in table:
        cells = [c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(row)]
        print("  ".join(cells).rstrip())
    if not show_all:
        vroots = visible_roots()
        hidden = sum(1 for s in aggregate.list_roots() if is_hidden(s.get("cwd"), vroots))
        if hidden:
            print(f"({hidden} hidden roots outside visible_roots; --all shows them)")
    return 0


def load_root_data(root):
    if registry.session_ids(root):
        return aggregate.build_root(root)
    data = core.read_json(core.roots_dir() / f"{root}.json")
    return data if isinstance(data, dict) else None


def _label(s):
    if s.get("kind") == "main" or s.get("parent_session_id") is None:
        return "main"
    role, hid = s.get("role"), s.get("herdr_id")
    if role and hid and role != hid and not hid.startswith(role):
        return f"{role} ({hid})"
    return hid or role or "herdr"


def _model(m, effort):
    m = (m or "?").replace("claude-", "")
    return f"{m}/{effort}" if effort else m


def fmt_split(t):
    if not isinstance(t, dict):
        return ""
    waiting = f", waiting {fmt_duration(t.get('waiting_s'))}" if t.get("waiting_s") else ""
    return f" (active {fmt_duration(t.get('active_s'))}{waiting}, idle {fmt_duration(t.get('idle_s'))})"


def _line(prefix, name, model, effort, started, duration, cost, extra="", t=None):
    return (f"{prefix}{name}  {_model(model, effort)}  {fmt_time(started)}  {fmt_duration(duration)}{fmt_split(t)}"
            f"  {fmt_usd(cost)}{extra}")


def render_tree(data):
    sessions = data.get("sessions") or []
    root_sid = data.get("root_session_id")
    t = data.get("totals") or {}
    root = next((s for s in sessions if s.get("session_id") == root_sid), sessions[0] if sessions else {})
    lines = [f"{root_sid}  {root.get('cwd') or '-'}  {root.get('state') or '-'}  total {fmt_usd(t.get('cost_usd'))}"
             f"  (main {fmt_usd(t.get('own_cost_usd'))}, agents {fmt_usd(t.get('agents_cost_usd'))})"]
    tt = t.get("time")
    if isinstance(tt, dict):
        lines.append(f"time: wall {fmt_duration(tt.get('wall_s'))}, active {fmt_duration(tt.get('active_s'))}, "
                     f"waiting {fmt_duration(tt.get('waiting_s'))}, idle {fmt_duration(tt.get('idle_s'))}; "
                     f"agents active {fmt_duration(tt.get('agents_active_s'))}")
    elif "time" in t:
        lines.append("time: unknown")
    children = {}
    ids = {s.get("session_id") for s in sessions}
    for s in sessions:
        parent = s.get("parent_session_id")
        if s is not root and parent not in ids:
            parent = root.get("session_id")
        if s is not root:
            children.setdefault(parent, []).append(s)

    def walk(s, prefix, branch, seen):
        sid = s.get("session_id")
        extra = f"  (subtree {fmt_usd(s.get('subtree_cost_usd'))})" if children.get(sid) else ""
        lines.append(_line(prefix + branch, _label(s), s.get("model"), s.get("effort"), s.get("started_at"),
                           s.get("duration_s"), (s.get("cost") or {}).get("effective_usd"), extra, s.get("time")))
        if sid in seen:
            return
        seen = seen | {sid}
        sub = prefix + ("   " if branch == "└─ " else "│  " if branch else "")
        kids = [("s", c) for c in children.get(sid, [])] + [("b", b) for b in s.get("builtin_agents") or []]
        for i, (kind, item) in enumerate(kids):
            br = "└─ " if i == len(kids) - 1 else "├─ "
            if kind == "s":
                walk(item, sub, br, seen)
            else:
                name = f"[{item.get('agent_type') or 'agent'}] {item.get('description') or ''}".rstrip()
                lines.append(_line(sub + br, name, item.get("model"), item.get("effort"), item.get("started_at"),
                                   item.get("duration_s"), item.get("cost_usd"), "  incl.", item.get("time")))

    if root:
        walk(root, "", "", frozenset())
    return "\n".join(lines)


def cmd_show(args):
    if args.sid and not registry.valid_sid(args.sid):
        print(f"agent-cost show: invalid session id {args.sid!r}", file=sys.stderr)
        return 2
    if args.sid and not args.latest:
        root, _ = resolve_focus(args.sid)
    else:
        root = _latest_root()
    data = load_root_data(root) if root else None
    if not data:
        print(f"agent-cost show: session {args.sid or '(latest)'} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_tree(data))
    return 0

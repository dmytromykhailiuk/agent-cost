import json
import os
import time

from . import core, registry

R = "\x1b[0m"
DIM = "\x1b[38;2;110;110;120m"
TEXT = "\x1b[38;2;200;200;210m"
ACCENT = "\x1b[38;2;120;170;255m"
ORANGE = "\x1b[38;2;245;150;70m"
GREEN = "\x1b[38;2;80;200;120m"


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _str(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("id", "level", "display_name", "name"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


def _fmt_duration(seconds):
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def _fmt_start(ts):
    lt = time.localtime(ts)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d", time.localtime()):
        return time.strftime("%H:%M", lt)
    return time.strftime("%d.%m %H:%M", lt)


def _money(value):
    return f"${value:.2f}"


def _write_live(root, sid, payload, cost, now):
    model = _str(payload.get("model"))
    effort = _str(payload.get("effort"))
    duration = _num((payload.get("cost") or {}).get("total_duration_ms"))
    wanted = {"session_id": sid, "reported_usd": cost, "model": model, "effort": effort,
              "duration_ms": duration}
    current = registry.load_live(root, sid) or {}
    if all(current.get(k) == v for k, v in wanted.items()):
        return
    core.write_json_atomic(registry.live_path(root, sid), dict(wanted, ts=now))


def _compute(payload, env):
    sid = payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
    if not registry.valid_sid(sid):
        return None
    foreign = registry.foreign_env(sid, env)
    ident, _ = registry.resolve(sid, payload, env)
    root = ident.get("root_session_id") or sid
    now = core.now()
    cost_block = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    stdin_cost = _num(cost_block.get("total_cost_usd"))
    if stdin_cost is not None and not foreign:
        _write_live(root, sid, payload, stdin_cost, now)

    from . import aggregate

    summary = aggregate.root_live_summary(root, now=now)
    own = dict(summary.get("own_usd") or {})
    recorded_self = own.get(sid, 0.0)
    self_cost = max(recorded_self, stdin_cost or 0.0)
    total = (summary.get("total_usd") or 0.0) - recorded_self + self_cost
    root_own = self_cost if sid == root else own.get(root, 0.0)
    agents_usd = max(0.0, total - root_own)
    agents_count = int(summary.get("agents_count") or 0) + int(summary.get("builtin_count") or 0)

    started = summary.get("started_at")
    duration_ms = _num(cost_block.get("total_duration_ms"))
    if started is None and duration_ms is not None:
        started = now - duration_ms / 1000.0
    duration = None
    if started is not None:
        last = summary.get("last_activity_at")
        end = last if summary.get("state") == "ended" and last is not None else now
        duration = max(0.0, end - started)

    parent = ident.get("parent_session_id")
    is_child = ((registry.valid_sid(parent) and parent != sid) or ident.get("kind") in ("herdr", "headless")
                or bool(ident.get("purpose")))
    role = ident.get("role") if ident.get("role") != "main" else None
    return {"is_child": is_child, "depth": ident.get("depth"), "role": role, "purpose": ident.get("purpose"),
            "herdr_id": ident.get("herdr_id"), "total": total, "self": self_cost,
            "agents_usd": agents_usd, "agents_count": agents_count, "started": started,
            "duration": duration}


def _render(c):
    sep = f" {DIM}·{R} "
    parts = []
    when = []
    if c["started"] is not None:
        when.append(f"{TEXT}{_fmt_start(c['started'])}{R}")
    if c["duration"] is not None:
        when.append(f"{TEXT}{_fmt_duration(c['duration'])}{R}")
    if when:
        parts.append(f"{DIM}session{R} " + sep.join(when))
    parts.append(f"{DIM}Σ{R} {TEXT}{_money(c['total'])}{R}")
    if c["is_child"]:
        parts.append(f"{DIM}self{R} {TEXT}{_money(c['self'])}{R}")
    elif c["agents_count"] > 0:
        parts.append(f"{DIM}main{R} {TEXT}{_money(c['self'])}{R}")
        parts.append(f"{DIM}agents{R} {TEXT}{_money(c['agents_usd'])}{R} {DIM}({c['agents_count']}){R}")
    line = sep.join(parts)
    label = ""
    if c["is_child"]:
        name = c.get("role") or c.get("purpose") or c.get("herdr_id") or "agent"
        depth = c.get("depth")
        tail = f" d{depth}" if isinstance(depth, int) else ""
        label = f"{ORANGE}[sub:{name}{tail}]{R}"
    return label, line


def run(stdin_text, as_json):
    try:
        payload = json.loads(stdin_text or "")
        if not isinstance(payload, dict):
            return ""
        computed = _compute(payload, os.environ)
        if computed is None:
            return ""
        label, line = _render(computed)
        if as_json:
            return json.dumps({"label": label, "line": line}, ensure_ascii=False)
        return f"{label} {line}".strip()
    except BaseException:
        return ""

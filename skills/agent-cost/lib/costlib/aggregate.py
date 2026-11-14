from pathlib import Path

from . import auxlink, core, pricing, registry, transcripts

SCHEMA = 1
IDLE_AFTER_S = 30 * 60
RESUME_GRACE_S = 60


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _round(value, digits=6):
    return None if value is None else round(value, digits)


def _max(*values):
    nums = [v for v in (_num(x) for x in values) if v is not None]
    return max(nums) if nums else None


def _min(*values):
    nums = [v for v in (_num(x) for x in values) if v is not None]
    return min(nums) if nums else None


def add_by_model(dst, src):
    for model, row in (src or {}).items():
        if not isinstance(row, dict):
            continue
        b = dst.setdefault(model, {k: 0 for k in pricing.TOKEN_KEYS} | {"requests": 0, "cost_usd": 0.0})
        for k in pricing.TOKEN_KEYS + ("requests",):
            b[k] = int(b.get(k) or 0) + int(row.get(k) or 0)
        cost = _num(row.get("cost_usd"))
        if cost is not None:
            b["cost_usd"] = round((b.get("cost_usd") or 0.0) + cost, 8)
    return dst


def estimate(transcript_path, with_builtins=True):
    s = transcripts.summarize(transcript_path)
    builtins = transcripts.builtin_agents(transcript_path) if with_builtins else []
    by_model = add_by_model({}, s["by_model"])
    builtins_usd = 0.0
    for b in builtins:
        add_by_model(by_model, b.get("by_model"))
        builtins_usd += _num(b.get("cost_usd")) or 0.0
    own = _num(s["estimated_usd"])
    total = None if own is None and not builtins else (own or 0.0) + builtins_usd
    cost_state = s.get("cost_state") or {}
    return {
        "summary": s,
        "builtins": builtins,
        "cache": {
            "estimated_usd": _round(total),
            "own_estimated_usd": _round(own),
            "builtins_usd": _round(builtins_usd),
            "builtin_count": len(builtins),
            "by_model": by_model,
            "first_ts": s["first_ts"],
            "last_ts": s["last_ts"],
            "active_s": _round(s["time"]["active_s"], 3),
            "gaps": [[_round(a, 3), _round(b, 3)] for a, b in s["gaps"]],
            "builtin_spans": [[_round(b.get("started_at"), 3), _round(b.get("ended_at"), 3)] for b in builtins],
            "updated_at": core.now(),
        },
        "cost_state_usd": _num(cost_state.get("totalCostUSD")),
    }


def pick_reported(live, cost_state_usd):
    candidates = []
    if isinstance(live, dict) and _num(live.get("reported_usd")) is not None:
        candidates.append((_num(live["reported_usd"]), "statusline"))
    if _num(cost_state_usd) is not None:
        candidates.append((_num(cost_state_usd), "cost-state"))
    if not candidates:
        return None, None
    return max(candidates, key=lambda c: c[0])


def session_state(ended_at, last_activity_at, now):
    if ended_at is not None:
        return "ended"
    if last_activity_at is not None and now - last_activity_at > IDLE_AFTER_S:
        return "idle"
    return "live"


def _times(rec, first_ts, last_ts, live, now):
    started = _min(rec.get("started_at"), first_ts)
    live_ts = core.parse_ts(live.get("ts")) if isinstance(live, dict) else None
    last = _max(last_ts, live_ts, rec.get("last_activity_at"), started)
    ended = _num(rec.get("ended_at"))
    if ended is not None and last is not None and last > ended + RESUME_GRACE_S:
        ended = None
    state = session_state(ended, last, now)
    end = ended if ended is not None else (now if state == "live" else last)
    duration = max(0, int(round(end - started))) if started is not None and end is not None else None
    return started, last, ended, duration, state


def time_out(wall_s, active_s, waiting_s=0):
    if active_s is None:
        return None
    wall = max(0, int(round(wall_s or 0)))
    active = min(wall, max(0, int(round(active_s or 0))))
    waiting = min(wall - active, max(0, int(round(waiting_s or 0))))
    return {"wall_s": wall, "active_s": active, "waiting_s": waiting, "idle_s": wall - active - waiting}


def _span(first, last):
    first, last = _num(first), _num(last)
    return [first, last] if first is not None and last is not None and last > first else None


def descendants(sid, parent_of):
    kids = {}
    for child, parent in parent_of.items():
        kids.setdefault(parent, []).append(child)
    out, stack, seen = [], [sid], {sid}
    while stack:
        for child in kids.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                out.append(child)
                stack.append(child)
    return out


def builtin_link(b):
    hit = b.get("prompt_run")
    name = str(b.get("agent_type") or "").rsplit(":", 1)[-1] or None
    purpose = auxlink.classify({"kind": "builtin", "purpose": auxlink.ROLE_PURPOSE.get(hit[1]) if hit else None,
                                "agent_name": name, "agent": name,
                                "task": {"prompt_excerpt": b.get("prompt_excerpt")}})
    return purpose, (hit[0] if hit else None)


def _transcript(rec):
    tp = rec.get("transcript_path")
    if tp and Path(tp).expanduser().is_file():
        return str(Path(tp).expanduser())
    found = transcripts.find_transcript(rec.get("session_id"))
    return str(found) if found else None


def _builtin_out(b):
    purpose, run_id = builtin_link(b)
    t = b.get("time") or {}
    return {
        "agent_id": b.get("agent_id"),
        "agent_type": b.get("agent_type"),
        "description": b.get("description"),
        "prompt_excerpt": b.get("prompt_excerpt"),
        "result_excerpt": b.get("result_excerpt"),
        "purpose": purpose,
        "run_id": run_id,
        "model": b.get("model"),
        "effort": b.get("effort"),
        "started_at": core.iso(b.get("started_at")),
        "ended_at": core.iso(b.get("ended_at")),
        "duration_s": None if b.get("duration_s") is None else int(round(b["duration_s"])),
        "time": time_out(t.get("wall_s"), t.get("active_s")),
        "cost_usd": _round(_num(b.get("cost_usd"))),
        "by_model": b.get("by_model") or {},
        "included_in_parent": True,
    }


def _session_out(rec, root, now):
    sid = rec["session_id"]
    synthetic = rec.get("synthetic")
    tp = None if synthetic else _transcript(rec)
    if tp:
        est = estimate(tp)
        summary, builtins, cache = est["summary"], est["builtins"], est["cache"]
        cost_state_usd = est["cost_state_usd"] if est["cost_state_usd"] is not None else rec.get("cost_state_usd")
    else:
        summary, builtins = {}, []
        cache = rec.get("estimate") or {}
        cost_state_usd = rec.get("cost_state_usd")
    live = registry.load_live(root, sid)
    estimated = _num(cache.get("estimated_usd"))
    reported, source = pick_reported(live, cost_state_usd)
    if synthetic and _num(rec.get("reported_usd")) is not None:
        reported, source = _num(rec["reported_usd"]), synthetic
    effective = _max(reported, estimated) or 0.0
    started, last, ended, duration, state = _times(rec, cache.get("first_ts"), cache.get("last_ts"), live, now)
    task = dict(rec.get("task") or {})
    task.setdefault("description", None)
    if not task.get("prompt_excerpt"):
        task["prompt_excerpt"] = summary.get("first_user_prompt")
    by_model = {m: dict(r) | {"cost_usd": _round(_num(r.get("cost_usd")))} for m, r in (cache.get("by_model") or {}).items()}
    if synthetic:
        active, gaps = duration, []
        span = _span(started, ended if ended is not None else last)
    elif summary:
        active, gaps = summary["time"]["active_s"], summary.get("gaps") or []
        span = _span(summary.get("first_ts"), summary.get("last_ts"))
    else:
        active, gaps = _num(cache.get("active_s")), cache.get("gaps")
        span = _span(cache.get("first_ts") or started, cache.get("last_ts") or last)
    run = summary.get("prompt_run") if summary else None
    run_id = rec.get("run_id") or (run[0] if run else None)
    if not run_id and str(sid).startswith(auxlink.TRIAGE_PREFIX):
        run_id = str(sid)[len(auxlink.TRIAGE_PREFIX):]
    return {
        "session_id": sid,
        "kind": rec.get("kind") or "main",
        "parent_session_id": rec.get("parent_session_id"),
        "depth": int(rec.get("depth") or 0),
        "herdr_id": rec.get("herdr_id"),
        "role": rec.get("role"),
        "purpose": auxlink.classify(rec | {"cwd": rec.get("cwd") or summary.get("cwd"),
                                           "agent_name": summary.get("agent_name"),
                                           "task": rec.get("task") or {"prompt_excerpt": summary.get("first_user_prompt")}}),
        "run_id": run_id,
        "headless": bool(rec.get("headless") or summary.get("headless")),
        "entrypoint": rec.get("entrypoint") or summary.get("entrypoint"),
        "cwd": rec.get("cwd") or summary.get("cwd"),
        "model": summary.get("model") or rec.get("model") or (live or {}).get("model"),
        "effort": rec.get("effort") or (live or {}).get("effort") or summary.get("effort"),
        "started_at": core.iso(started),
        "last_activity_at": core.iso(last),
        "ended_at": core.iso(ended),
        "duration_s": duration,
        "time": time_out(duration, active),
        "state": state,
        "task": {"description": task.get("description"), "prompt_excerpt": task.get("prompt_excerpt")},
        "result_excerpt": rec.get("result_excerpt") or summary.get("last_assistant_text"),
        "transcript_path": tp or rec.get("transcript_path"),
        "cost": {
            "effective_usd": _round(effective),
            "reported_usd": _round(reported),
            "reported_source": source,
            "estimated_usd": _round(estimated),
            "by_model": by_model,
        },
        "subtree_cost_usd": None,
        "builtin_agents": [_builtin_out(b) for b in builtins],
        "_started": started,
        "_wall": duration,
        "_active": active,
        "_gaps": gaps,
        "_builtin_spans": [x for x in (_span(b.get("started_at"), b.get("ended_at")) for b in builtins) if x],
        "_span": span,
    }


def _apply_waiting(sessions):
    by_id = {s["session_id"]: s for s in sessions}
    parent_of = {s["session_id"]: s["parent_session_id"] for s in sessions if s["parent_session_id"]}
    for s in sessions:
        spans = list(s["_builtin_spans"])
        for d in descendants(s["session_id"], parent_of):
            spans.extend(([by_id[d]["_span"]] if by_id[d]["_span"] else []) + by_id[d]["_builtin_spans"])
        waiting = transcripts.waiting_s(s["_gaps"], spans) if s["_gaps"] else 0
        s["time"] = time_out(s["_wall"], s["_active"], waiting)


def _subtree(sessions):
    children = {}
    for s in sessions:
        children.setdefault(s["parent_session_id"], []).append(s)
    memo = {}

    def total(s, seen):
        sid = s["session_id"]
        if sid in memo:
            return memo[sid]
        if sid in seen:
            return 0.0
        seen = seen | {sid}
        value = (s["cost"]["effective_usd"] or 0.0) + sum(total(c, seen) for c in children.get(sid, []))
        memo[sid] = value
        return value

    for s in sessions:
        s["subtree_cost_usd"] = _round(total(s, frozenset()))


def build_root(root_sid, now=None):
    now = core.now() if now is None else now
    records = registry.load_root_sessions(root_sid)
    sessions = [_session_out(r, root_sid, now) for r in records]
    sessions.sort(key=lambda s: (s["_started"] is None, s["_started"] or 0, s["depth"], s["session_id"]))
    _apply_waiting(sessions)
    for s in sessions:
        for k in ("_started", "_wall", "_active", "_gaps", "_span", "_builtin_spans"):
            s.pop(k, None)
    _subtree(sessions)
    total = sum(s["cost"]["effective_usd"] or 0.0 for s in sessions)
    root = next((s for s in sessions if s["session_id"] == root_sid), None)
    own = (root["cost"]["effective_usd"] or 0.0) if root else 0.0
    by_model = {}
    for s in sessions:
        add_by_model(by_model, s["cost"]["by_model"])
    for row in by_model.values():
        row["cost_usd"] = _round(row.get("cost_usd"))
    main_time = (root or {}).get("time")
    agents_active = sum((s["time"] or {}).get("active_s") or 0 for s in sessions if s is not root)
    agents_active += sum((b["time"] or {}).get("active_s") or 0 for s in sessions for b in s["builtin_agents"])
    return {
        "schema": SCHEMA,
        "generated_at": core.iso(now),
        "root_session_id": root_sid,
        "totals": {
            "cost_usd": _round(total),
            "own_cost_usd": _round(own),
            "agents_cost_usd": _round(total - own),
            "sessions": len(sessions),
            "builtin_agents": sum(len(s["builtin_agents"]) for s in sessions),
            "by_model": by_model,
            "time": dict(main_time, agents_active_s=agents_active) if main_time else None,
        },
        "sessions": sessions,
    }


def root_path(root_sid):
    if not registry.valid_sid(root_sid):
        raise ValueError("invalid session id")
    return core.roots_dir() / f"{root_sid}.json"


def _same(old, new):
    if not isinstance(old, dict):
        return False
    return {k: v for k, v in old.items() if k != "generated_at"} == {k: v for k, v in new.items() if k != "generated_at"}


def write_root(root_sid, now=None):
    data = build_root(root_sid, now=now)
    path = root_path(root_sid)
    if not _same(core.read_json(path), data):
        core.write_json_atomic(path, data)
    return path


def _summary(data, path):
    sessions = data.get("sessions") or []
    root_sid = data.get("root_session_id")
    root = next((s for s in sessions if s.get("session_id") == root_sid), sessions[0] if sessions else {})
    starts = [core.parse_ts(s.get("started_at")) for s in sessions]
    lasts = [core.parse_ts(s.get("ended_at") or s.get("last_activity_at")) for s in sessions]
    totals = data.get("totals") or {}
    task = root.get("task") or {}
    return {
        "root_session_id": root_sid,
        "generated_at": data.get("generated_at"),
        "cwd": root.get("cwd"),
        "title": task.get("description") or task.get("prompt_excerpt"),
        "state": root.get("state"),
        "headless": bool(root.get("headless")),
        "started_at": core.iso(_min(*starts)),
        "last_activity_at": core.iso(_max(*lasts)),
        "cost_usd": totals.get("cost_usd"),
        "time": totals.get("time"),
        "sessions": totals.get("sessions"),
        "builtin_agents": totals.get("builtin_agents"),
        "path": str(path),
    }


def list_roots():
    out = []
    for path in core.roots_dir().glob("*.json"):
        data = core.read_json(path)
        if isinstance(data, dict) and data.get("root_session_id"):
            out.append(_summary(data, path))
    out.sort(key=lambda r: core.parse_ts(r.get("last_activity_at") or r.get("started_at")) or 0, reverse=True)
    return out


def root_live_summary(root_sid, now=None):
    now = core.now() if now is None else now
    own, starts, lasts = {}, [], []
    builtin_count = agents_count = 0
    root_rec = root_active = root_gaps = None
    spans = []
    for rec in registry.load_root_sessions(root_sid):
        sid = rec["session_id"]
        live = registry.load_live(root_sid, sid)
        cache = rec.get("estimate") or {}
        reported, _ = pick_reported(live, rec.get("cost_state_usd"))
        if rec.get("synthetic") and _num(rec.get("reported_usd")) is not None:
            reported = _num(rec["reported_usd"])
        own[sid] = _round(_max(reported, cache.get("estimated_usd")) or 0.0)
        builtin_count += int(cache.get("builtin_count") or 0)
        live_ts = core.parse_ts(live.get("ts")) if live else None
        starts.append(_min(rec.get("started_at"), cache.get("first_ts")))
        lasts.append(_max(rec.get("last_activity_at"), cache.get("last_ts"), live_ts))
        spans.extend(x for x in (_span(*b) for b in cache.get("builtin_spans") or [] if len(b) == 2) if x)
        if sid == root_sid:
            root_rec = rec
            root_active = _num(cache.get("active_s"))
            root_gaps = cache.get("gaps")
        else:
            agents_count += 1
            if rec.get("synthetic"):
                spans.append(_span(rec.get("started_at"), rec.get("ended_at")))
            else:
                spans.append(_span(_min(cache.get("first_ts"), rec.get("started_at")),
                                   _max(cache.get("last_ts"), live_ts, rec.get("last_activity_at"))))
    total = sum(own.values())
    root_own = own.get(root_sid, 0.0)
    last = _max(*lasts)
    ended = _num((root_rec or {}).get("ended_at"))
    if ended is not None and last is not None and last > ended + RESUME_GRACE_S:
        ended = None
    return {
        "root_session_id": root_sid,
        "total_usd": _round(total),
        "own_usd": own,
        "agents_usd": _round(total - root_own),
        "agents_count": agents_count,
        "builtin_count": builtin_count,
        "started_at": _min(*starts),
        "last_activity_at": last,
        "state": session_state(ended, last, now) if own else None,
        "active_s": root_active,
        "waiting_s": None if root_active is None or root_gaps is None else transcripts.waiting_s(root_gaps, spans),
    }

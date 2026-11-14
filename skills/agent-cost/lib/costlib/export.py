import json

from . import aggregate, auxlink, core, registry, transcripts

CONTRACT = 1
EXPORT_PURPOSES = ("verify:triage", "verify:review", "verify:qa", "verify:retro", "vault")


def _ms(seconds):
    return max(0, int(round((seconds or 0) * 1000)))


def _time(wall_s, active_s, waiting_s=0):
    wall = _ms(wall_s)
    active = min(wall, _ms(active_s))
    waiting = min(wall - active, _ms(waiting_s))
    return {"wall_ms": wall, "active_ms": active, "waiting_ms": waiting, "idle_ms": wall - active - waiting}


def _tokens(row):
    row = row or {}
    return {"input": int(row.get("input") or 0), "output": int(row.get("output") or 0),
            "cache_creation": int(row.get("cache_write_5m") or 0) + int(row.get("cache_write_1h") or 0),
            "cache_read": int(row.get("cache_read") or 0)}


def _usd(value):
    return round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _numbers(summary):
    models, total = {}, {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for model, row in (summary.get("by_model") or {}).items():
        tokens = _tokens(row)
        models[model] = {"usd": _usd(row.get("cost_usd")), "tokens": tokens}
        for k in total:
            total[k] += tokens[k]
    t = summary["time"]
    return {"usd": _usd(summary.get("estimated_usd")), "tokens": total, "models": models,
            "time": _time(t["wall_s"], t["active_s"])}


def _waiting(gaps, sid, parent_of, by_id):
    spans = [by_id[d]["_span"] for d in aggregate.descendants(sid, parent_of) if by_id[d]["_span"]]
    return transcripts.waiting_s(gaps, spans)


def _purpose(value):
    return value if value in EXPORT_PURPOSES else None


def _active(summary):
    return summary.get("first_ts") is not None


def _children(records):
    out = {}
    for rec in records:
        out.setdefault(rec.get("parent_session_id"), []).append(rec)
    return out


def _descendants(sid, records):
    kids = _children(records)
    out, stack, seen = [], [sid], {sid}
    while stack:
        cur = stack.pop()
        for rec in kids.get(cur, []):
            child = rec["session_id"]
            if child not in seen:
                seen.add(child)
                out.append(rec)
                stack.append(child)
    return out


def _builtin_entries(parent_sid, transcript, window):
    out = []
    for b in transcripts.builtin_agents(transcript, use_cache=False):
        s = transcripts.summarize_window(b["transcript_path"], *window, main_only=False)
        if not _active(s):
            continue
        purpose, run_id = aggregate.builtin_link(b)
        n = _numbers(s)
        out.append({"session_id": f"agent-{b['agent_id']}", "parent_session_id": parent_sid, "herdr_id": None,
                    "kind": "builtin", "purpose": _purpose(purpose), "run_id": run_id,
                    "model": s.get("model") or b.get("model"), "usd": n["usd"], "tokens": n["tokens"],
                    "time": n["time"], "_start": s["first_ts"], "_span": (s["first_ts"], s["last_ts"]),
                    "_gaps": s["gaps"], "_t": s["time"]})
    return out


def _synthetic(sid, parent, run_id, model, usd, started, duration_s, window):
    end = (started or 0) + (duration_s or 0)
    since, until = window
    if started is not None and ((since is not None and end < since) or (until is not None and started > until)):
        return None
    return {"session_id": sid, "parent_session_id": parent, "herdr_id": None, "kind": "synthetic",
            "purpose": "verify:triage", "run_id": run_id, "model": model, "usd": _usd(usd),
            "tokens": _tokens({}), "time": _time(duration_s, duration_s), "_start": started,
            "_span": (started, end) if started is not None else None, "_gaps": [], "_t": None}


def _session_entry(rec, vloop, window):
    sid = rec["session_id"]
    if rec.get("synthetic"):
        run_id = sid[len(auxlink.TRIAGE_PREFIX):] if sid.startswith(auxlink.TRIAGE_PREFIX) else rec.get("run_id")
        started, ended = aggregate._num(rec.get("started_at")), aggregate._num(rec.get("ended_at"))
        duration = (ended - started) if started is not None and ended is not None else 0
        return _synthetic(sid, rec.get("parent_session_id"), run_id, rec.get("model"), rec.get("reported_usd"),
                          started, duration, window), None
    tp = aggregate._transcript(rec)
    s = transcripts.summarize_window(tp, *window) if tp else None
    if not s or not _active(s):
        return None, tp
    linked = vloop.agents.get(sid)
    run = s.get("prompt_run")
    run_id = (linked[0] if linked else None) or (run[0] if run else None)
    purpose = auxlink.classify(rec | {"cwd": rec.get("cwd") or s.get("cwd"), "agent_name": s.get("agent_name"),
                                      "task": {"prompt_excerpt": ((rec.get("task") or {}).get("prompt_excerpt"))
                                               or s.get("first_user_prompt")}})
    kind = rec.get("kind") if rec.get("kind") in ("herdr", "headless") else ("herdr" if rec.get("herdr_id") else "headless")
    n = _numbers(s)
    return {"session_id": sid, "parent_session_id": rec.get("parent_session_id"), "herdr_id": rec.get("herdr_id"),
            "kind": kind, "purpose": _purpose(purpose), "run_id": run_id,
            "model": s.get("model") or rec.get("model"), "usd": n["usd"], "tokens": n["tokens"],
            "time": n["time"], "_start": s["first_ts"], "_span": (s["first_ts"], s["last_ts"]),
            "_gaps": s["gaps"], "_t": s["time"]}, tp


def build(sid, since=None, until=None, now=None):
    now = core.now() if now is None else now
    window = (since, until)
    root = registry.find_root(sid, write_index=False)
    records = registry.load_root_sessions(root) if root else []
    rec = next((r for r in records if r["session_id"] == sid), None)
    main_tp = aggregate._transcript(rec) if rec else None
    if main_tp is None and rec is None:
        found = transcripts.find_transcript(sid)
        main_tp = str(found) if found else None
    if rec is None and main_tp is None:
        return None
    main = transcripts.summarize_window(main_tp, *window) if main_tp else transcripts._result(sid, transcripts._empty_state())
    n = _numbers(main)
    agents = _builtin_entries(sid, main_tp, window) if main_tp else []
    vloop = auxlink.load_vloop()
    seen = set()
    for child in _descendants(sid, records):
        seen.add(child["session_id"])
        entry, tp = _session_entry(child, vloop, window)
        if entry:
            agents.append(entry)
        if tp:
            agents.extend(_builtin_entries(child["session_id"], tp, window))
    owners = {sid} | seen
    for e in vloop.triage_entries():
        if e["session_id"] in seen or e["parent_session_id"] not in owners:
            continue
        entry = _synthetic(e["session_id"], e["parent_session_id"], e["run_id"], e["model"], e["cost_usd"],
                           e["started_at"], e["duration_s"], window)
        if entry:
            agents.append(entry)
    parent_of = {a["session_id"]: a["parent_session_id"] for a in agents}
    by_id = {a["session_id"]: a for a in agents}
    for a in agents:
        if a["_gaps"]:
            waiting = _waiting(a["_gaps"], a["session_id"], parent_of, by_id)
            a["time"] = _time(a["_t"]["wall_s"], a["_t"]["active_s"], waiting)
    if main.get("gaps"):
        t = main["time"]
        n["time"] = _time(t["wall_s"], t["active_s"], _waiting(main["gaps"], sid, parent_of, by_id))
    agents.sort(key=lambda a: (a["_start"] is None, a["_start"] or 0, a["session_id"]))
    for a in agents:
        for k in ("_start", "_span", "_gaps", "_t"):
            a.pop(k, None)
    return {
        "contract": CONTRACT,
        "generated_at": core.iso(now),
        "session_id": sid,
        "window": {"since": core.iso(since), "until": core.iso(until)},
        "main": {"session_id": sid, "usd": n["usd"], "tokens": n["tokens"], "models": n["models"], "time": n["time"]},
        "agents": agents,
    }


def _error(message, code):
    print(json.dumps({"error": message}))
    return code


def cmd_export(args):
    bounds = []
    for name in ("since", "until"):
        raw = getattr(args, name)
        ts = None if raw is None else core.parse_ts(raw)
        if raw is not None and ts is None:
            return _error(f"invalid --{name} {raw!r}", 2)
        bounds.append(ts)
    if not registry.valid_sid(args.session):
        return _error(f"unknown session {args.session!r}", 1)
    data = build(args.session, *bounds)
    if data is None:
        return _error(f"unknown session {args.session!r}", 1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0

import json
import os

from . import aggregate, core, registry, transcripts

EXCERPT = 600
EVENTS = ("session-start", "prompt", "stop", "subagent-stop", "session-end")


def _payload(stdin_text):
    try:
        data = json.loads(stdin_text or "")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _str(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("id", "level", "value", "name"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


def _context(payload, env, event):
    sid = payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
    if not registry.valid_sid(sid):
        return None, None
    clear = event == "session-start" and payload.get("source") == "clear"
    return registry.resolve(sid, payload, env, clear=clear)


def _base(ident, payload, event, now):
    rec = dict(ident)
    rec["cwd"] = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    tp = payload.get("transcript_path")
    rec["transcript_path"] = tp if isinstance(tp, str) and tp else None
    rec["last_activity_at"] = now
    rec["hook_events"] = {event: now}
    return rec


def _herdr_details(rec, existing, ident, env):
    if ident.get("kind") != "herdr":
        return
    meta = registry.read_herdr_meta(env.get("HERDR_AGENT_DIR"))
    if meta and meta.get("session_id") not in (None, ident["session_id"]):
        meta = None
    if not meta:
        meta = registry.read_launch_record(ident["session_id"], env)
        if meta:
            meta = dict(meta, role=None, _prompt=None)
    if not meta:
        return
    herdr = {k: meta.get(k) for k in ("id", "agent", "delegate", "read_only", "created_at", "workspace_id",
                                      "tab_id", "pane_id")}
    rec["herdr"] = {k: v for k, v in herdr.items() if v is not None}
    if meta.get("role") and not existing.get("role"):
        rec["role"] = meta["role"]
    if not existing.get("herdr_id") and meta.get("id"):
        rec["herdr_id"] = meta["id"]
    if not existing.get("model") and _str(meta.get("model")):
        rec["model"] = _str(meta.get("model"))
    if not existing.get("effort") and _str(meta.get("effort")):
        rec["effort"] = _str(meta.get("effort"))
    task = dict(existing.get("task") or {})
    if not task.get("description"):
        task["description"] = meta.get("agent") or meta.get("role") or rec.get("role") or None
    if not task.get("prompt_excerpt") and meta.get("_prompt"):
        task["prompt_excerpt"] = core.excerpt(meta["_prompt"], EXCERPT) or None
    rec["task"] = task


def _estimate(rec, existing):
    tp = rec.get("transcript_path") or existing.get("transcript_path")
    if not tp:
        return None
    est = aggregate.estimate(tp)
    rec["estimate"] = est["cache"]
    if est["cost_state_usd"] is not None:
        rec["cost_state_usd"] = est["cost_state_usd"]
    s = est["summary"]
    if s.get("first_ts") is not None:
        rec["started_at"] = s["first_ts"]
    if not existing.get("cwd") and not rec.get("cwd"):
        rec["cwd"] = s.get("cwd")
    if s.get("model"):
        rec["transcript_model"] = s["model"]
    return s


def _session_start(payload, env, now):
    ident, existing = _context(payload, env, "session-start")
    if not ident:
        return
    rec = _base(ident, payload, "session-start", now)
    rec["started_at"] = now
    rec["source"] = _str(payload.get("source"))
    entrypoint = _str(env.get("CLAUDE_CODE_ENTRYPOINT"))
    if entrypoint:
        rec["entrypoint"] = entrypoint
        rec["headless"] = transcripts.is_headless(entrypoint)
    rec["_unset"] = ["ended_at", "end_reason"]
    model = _str(payload.get("model"))
    if model:
        rec["model"] = model
    effort = _str(payload.get("effort")) or _str(env.get("CLAUDE_EFFORT"))
    if effort:
        rec["effort"] = effort
    _herdr_details(rec, existing, ident, env)
    registry.save_session(rec)
    core.write_cli_pointer()


def _prompt(payload, env, now):
    ident, existing = _context(payload, env, "prompt")
    if not ident:
        return
    rec = _base(ident, payload, "prompt", now)
    if not existing:
        rec["started_at"] = now
    task = existing.get("task") or {}
    text = payload.get("prompt")
    if not task.get("prompt_excerpt") and isinstance(text, str) and text.strip():
        rec["task"] = {"description": task.get("description"), "prompt_excerpt": core.excerpt(text, EXCERPT)}
    effort = _str(env.get("CLAUDE_EFFORT"))
    if effort:
        rec["effort"] = effort
    rec["_unset"] = ["ended_at", "end_reason"]
    registry.save_session(rec)


def _finish(payload, env, now, event):
    ident, existing = _context(payload, env, event)
    if not ident:
        return
    rec = _base(ident, payload, event, now)
    if not existing:
        rec["started_at"] = now
    if event == "stop":
        _herdr_details(rec, existing, ident, env)
    summary = _estimate(rec, existing)
    if event == "stop":
        text = payload.get("last_assistant_message")
        if not (isinstance(text, str) and text.strip()) and summary:
            text = summary.get("last_assistant_text")
        if isinstance(text, str) and text.strip():
            rec["result_excerpt"] = core.excerpt(text, EXCERPT)
    if event == "session-end":
        rec["ended_at"] = now
        rec["end_reason"] = _str(payload.get("reason"))
    registry.save_session(rec)
    aggregate.write_root(ident["root_session_id"])


def run(event, stdin_text, env=None):
    try:
        env = os.environ if env is None else env
        payload = _payload(stdin_text)
        now = core.now()
        if event == "session-start":
            _session_start(payload, env, now)
        elif event == "prompt":
            _prompt(payload, env, now)
        elif event in ("stop", "subagent-stop", "session-end"):
            _finish(payload, env, now, event)
    except BaseException:
        pass
    return 0

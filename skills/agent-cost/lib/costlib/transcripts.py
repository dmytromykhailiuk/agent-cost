import copy
import glob
import hashlib
import json
import os
import re
from pathlib import Path

from . import auxlink, core, pricing

PARSER_VERSION = 4
HEAD_BYTES = 4096
EXCERPT = 600

_HERDR_STATUS_RE = re.compile(r"\[herdr-agent\] id=([A-Za-z0-9][\w.-]*)((?:[ \t]+\w+=\S+)*)")
_HERDR_SPAWN_RE = re.compile(r"\[herdr-agent\] spawned ([A-Za-z0-9][\w.-]*) in tab ([A-Za-z0-9][\w:.-]*?)(?=[;,\s]|$)")
_KV_RE = re.compile(r"(\w+)=(\S+)")
_SID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CMD_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
_LOCAL_COMMANDS = {"/clear", "/compact", "/model", "/effort", "/resume", "/config", "/login", "/logout", "/status",
                   "/cost", "/help", "/exit", "/rename", "/fast", "/permissions", "/mcp", "/memory", "/context",
                   "/plugin", "/hooks", "/agents", "/doctor", "/init", "/theme", "/statusline", "/usage"}
_SKIP_PREFIXES = ("<local-command-", "caveat:", "<task-notification", "<system-reminder", "[request interrupted")


def is_headless(entrypoint):
    return isinstance(entrypoint, str) and entrypoint.startswith("sdk")


def _int(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def usage_numbers(usage):
    usage = usage if isinstance(usage, dict) else {}
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    cc = usage.get("cache_creation")
    if isinstance(cc, dict) and ("ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc):
        w5, w1 = _int(cc.get("ephemeral_5m_input_tokens")), _int(cc.get("ephemeral_1h_input_tokens"))
    else:
        w5, w1 = cache_write, 0
    return {
        "input": _int(usage.get("input_tokens")),
        "output": _int(usage.get("output_tokens")),
        "cache_read": _int(usage.get("cache_read_input_tokens")),
        "cache_write_5m": w5,
        "cache_write_1h": w1,
    }


def _empty_state():
    return {
        "session_id": None,
        "cwd": None,
        "first_ts": None,
        "last_ts": None,
        "entries": {},
        "first_user_prompt": None,
        "last_assistant_text": None,
        "agent_name": None,
        "custom_title": None,
        "model": None,
        "effort": None,
        "cost_state": None,
        "entrypoint": None,
        "herdr": {},
        "lines": 0,
        "prompt_run": None,
        "idle_gap_s": core.idle_gap_s(),
        "prev_ts": None,
        "active_s": 0.0,
        "gaps": [],
    }


def _text_blocks(content):
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append(block["text"])
            elif isinstance(block, str):
                out.append(block)
    return out


def _user_prompt(rec, msg):
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("toolUseResult") is not None:
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    text = "\n".join(_text_blocks(content)).strip()
    if not text:
        return None
    name = _CMD_NAME_RE.search(text)
    if name:
        args = _CMD_ARGS_RE.search(text)
        command = name.group(1).strip()
        if command in _LOCAL_COMMANDS:
            return None
        text = (command + " " + (args.group(1) if args else "")).strip()
    if text.lower().startswith(_SKIP_PREFIXES):
        return None
    return core.excerpt(text, EXCERPT) or None


def _human_input(rec, msg):
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("toolUseResult") is not None:
        return False
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = "\n".join(_text_blocks(content)).strip()
    if not text:
        return False
    return bool(_CMD_NAME_RE.search(text)) or not text.lower().startswith(_SKIP_PREFIXES)


def _tick(state, ts, human):
    prev = state.get("prev_ts")
    if prev is not None and ts > prev:
        if not human and ts - prev <= state["idle_gap_s"]:
            state["active_s"] += ts - prev
        else:
            gaps = state.setdefault("gaps", [])
            if gaps and gaps[-1][1] == prev:
                gaps[-1][1] = ts
            else:
                gaps.append([prev, ts])
    if prev is None or ts > prev:
        state["prev_ts"] = ts


def _tool_result_texts(content):
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                out.append(inner)
            else:
                out.extend(_text_blocks(inner))
    return out


def parse_herdr_links(text, links=None):
    links = {} if links is None else links
    if not text or "[herdr-agent]" not in text:
        return links
    for m in _HERDR_SPAWN_RE.finditer(text):
        link = links.setdefault(m.group(1), {"herdr_id": m.group(1), "session_id": None, "tab": None, "status": None})
        link["tab"] = m.group(2)
        link["status"] = link["status"] or "spawned"
    for m in _HERDR_STATUS_RE.finditer(text):
        fields = dict(_KV_RE.findall(m.group(2)))
        link = links.setdefault(m.group(1), {"herdr_id": m.group(1), "session_id": None, "tab": None, "status": None})
        if fields.get("status"):
            link["status"] = fields["status"]
        if fields.get("tab"):
            link["tab"] = fields["tab"]
        sid = fields.get("session")
        if sid and _SID_RE.match(sid):
            link["session_id"] = sid
    return links


def _apply(state, rec, idx, main_only, window=None):
    rtype = rec.get("type")
    if rtype == "agent-name":
        if rec.get("agentName"):
            state["agent_name"] = rec["agentName"]
        return
    if rtype == "custom-title":
        if rec.get("customTitle"):
            state["custom_title"] = rec["customTitle"]
        return
    if rtype == "cost-state":
        state["cost_state"] = rec
        return
    if rtype not in ("user", "assistant", "system", "attachment"):
        return
    if main_only and rec.get("isSidechain") is True:
        return
    ts = core.parse_ts(rec.get("timestamp"))
    if window is not None and (ts is None or (window[0] is not None and ts < window[0])
                               or (window[1] is not None and ts > window[1])):
        return
    if state["session_id"] is None and rec.get("sessionId"):
        state["session_id"] = rec["sessionId"]
    if state["cwd"] is None and rec.get("cwd"):
        state["cwd"] = rec["cwd"]
    if state.get("entrypoint") is None and isinstance(rec.get("entrypoint"), str):
        state["entrypoint"] = rec["entrypoint"]
    msg = rec.get("message")
    if ts is not None:
        _tick(state, ts, rtype == "user" and _human_input(rec, msg))
        if state["first_ts"] is None or ts < state["first_ts"]:
            state["first_ts"] = ts
        if state["last_ts"] is None or ts > state["last_ts"]:
            state["last_ts"] = ts
    if not isinstance(msg, dict):
        return
    if rtype == "user":
        if state["first_user_prompt"] is None:
            state["first_user_prompt"] = _user_prompt(rec, msg)
            if state["first_user_prompt"]:
                state["prompt_run"] = auxlink.prompt_run("\n".join(_text_blocks(msg.get("content"))))
        for text in _tool_result_texts(msg.get("content")):
            parse_herdr_links(text, state["herdr"])
        return
    if rtype != "assistant":
        return
    effort = rec.get("perTurnEffort") or rec.get("effort")
    if isinstance(effort, str) and effort:
        state["effort"] = effort
    model = msg.get("model")
    if not model or model == "<synthetic>":
        return
    state["model"] = model
    text = "\n".join(_text_blocks(msg.get("content"))).strip()
    if text:
        state["last_assistant_text"] = core.excerpt(text, EXCERPT)
    usage = msg.get("usage")
    if isinstance(usage, dict):
        key = msg.get("id") or rec.get("requestId") or rec.get("uuid") or f"#{idx}"
        n = usage_numbers(usage)
        state["entries"][key] = [model] + [n[k] for k in pricing.TOKEN_KEYS]


def _feed(state, raw_lines, main_only, window=None):
    for raw in raw_lines:
        idx = state["lines"]
        state["lines"] += 1
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            _apply(state, rec, idx, main_only, window)


def _result(path, state):
    by_model = {}
    for row in state["entries"].values():
        model = row[0]
        n = dict(zip(pricing.TOKEN_KEYS, row[1:]))
        b = by_model.setdefault(model, {k: 0 for k in pricing.TOKEN_KEYS} | {"requests": 0, "cost_usd": 0.0})
        for k in pricing.TOKEN_KEYS:
            b[k] += n[k]
        b["requests"] += 1
        cost = pricing.request_cost(model, n)
        b["cost_usd"] = None if cost is None or b["cost_usd"] is None else b["cost_usd"] + cost
    total, by_model, unknown = pricing.price_by_model(by_model)
    session_id = state["session_id"] or Path(path).stem
    return {
        "path": str(path),
        "session_id": session_id,
        "cwd": state["cwd"],
        "first_ts": state["first_ts"],
        "last_ts": state["last_ts"],
        "by_model": by_model,
        "estimated_usd": total,
        "unknown_models": unknown,
        "first_user_prompt": state["first_user_prompt"],
        "last_assistant_text": state["last_assistant_text"],
        "agent_name": state["agent_name"],
        "custom_title": state["custom_title"],
        "model": state["model"],
        "effort": state["effort"],
        "cost_state": state["cost_state"],
        "entrypoint": state.get("entrypoint"),
        "headless": is_headless(state.get("entrypoint")),
        "herdr_links": list(state["herdr"].values()),
        "prompt_run": list(state["prompt_run"]) if state.get("prompt_run") else None,
        "time": time_split(state["first_ts"], state["last_ts"], state.get("active_s")),
        "gaps": [list(g) for g in state.get("gaps") or []],
    }


def time_split(first_ts, last_ts, active_s, waiting_s=0.0):
    if first_ts is None or last_ts is None:
        return {"wall_s": 0.0, "active_s": 0.0, "waiting_s": 0.0, "idle_s": 0.0}
    wall = max(0.0, last_ts - first_ts)
    active = min(wall, max(0.0, float(active_s or 0.0)))
    waiting = min(wall - active, max(0.0, float(waiting_s or 0.0)))
    return {"wall_s": wall, "active_s": active, "waiting_s": waiting, "idle_s": wall - active - waiting}


def merge_spans(spans):
    clean = sorted((float(a), float(b)) for a, b in (tuple(x) for x in spans or [] if x and len(x) == 2)
                   if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > a)
    out = []
    for a, b in clean:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def waiting_s(gaps, spans):
    merged = merge_spans(spans)
    total = 0.0
    for g in gaps or []:
        if not g or len(g) != 2:
            continue
        g0, g1 = g
        for a, b in merged:
            if b <= g0:
                continue
            if a >= g1:
                break
            total += min(b, g1) - max(a, g0)
    return total


def _cache_path(path, main_only):
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
    suffix = "" if main_only else ".all"
    return core.cache_dir() / "offsets" / f"{digest}{suffix}.json"


def _split(data):
    *complete, tail = data.split(b"\n")
    return complete, tail


def summarize_window(path, since=None, until=None, *, main_only=True):
    path = Path(path).expanduser()
    state = _empty_state()
    try:
        with open(path, "rb") as f:
            _feed(state, f, main_only, (since, until))
    except OSError:
        pass
    return _result(path, state)


def summarize(path, *, main_only=True, use_cache=True):
    path = Path(path).expanduser()
    try:
        st = path.stat()
    except OSError:
        return _result(path, _empty_state())
    cache_file = _cache_path(path.absolute(), main_only) if use_cache else None
    cached = core.read_json(cache_file) if cache_file else None
    state, offset, head = _empty_state(), 0, None
    try:
        with open(path, "rb") as f:
            if isinstance(cached, dict) and cached.get("version") == PARSER_VERSION:
                c_off = cached.get("offset")
                if isinstance(c_off, int) and 0 < c_off <= st.st_size:
                    head = hashlib.sha1(f.read(min(c_off, HEAD_BYTES))).hexdigest()
                    if (head == cached.get("head") and isinstance(cached.get("state"), dict)
                            and cached["state"].get("idle_gap_s") == core.idle_gap_s()):
                        state, offset = cached["state"], c_off
            f.seek(offset)
            data = f.read()
    except OSError:
        return _result(path, _empty_state())
    complete, tail = _split(data)
    _feed(state, complete, main_only)
    new_offset = offset + sum(len(line) + 1 for line in complete)
    if cache_file and new_offset > 0 and (new_offset != offset or not cached):
        try:
            with open(path, "rb") as f:
                head = hashlib.sha1(f.read(min(new_offset, HEAD_BYTES))).hexdigest()
            core.write_json_atomic(cache_file, {
                "version": PARSER_VERSION,
                "path": str(path.absolute()),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "offset": new_offset,
                "head": head,
                "main_only": main_only,
                "state": state,
            })
        except OSError:
            pass
    if tail.strip():
        state = copy.deepcopy(state)
        _feed(state, [tail], main_only)
    return _result(path, state)


def _agent_meta(jsonl):
    meta = core.read_json(jsonl.with_name(jsonl.name[: -len(".jsonl")] + ".meta.json"), {})
    return meta if isinstance(meta, dict) else {}


def builtin_agents(main_transcript_path, use_cache=True):
    if not main_transcript_path:
        return []
    main = Path(main_transcript_path).expanduser()
    out = []
    for f in sorted((main.parent / main.stem / "subagents").glob("agent-*.jsonl")):
        meta = _agent_meta(f)
        s = summarize(f, main_only=False, use_cache=use_cache)
        start, end = s["first_ts"], s["last_ts"]
        out.append({
            "agent_id": f.stem[len("agent-"):],
            "agent_type": meta.get("agentType"),
            "description": meta.get("description"),
            "model": s["model"] or meta.get("pickedEffortModel") or meta.get("model"),
            "effort": s["effort"] or meta.get("effort"),
            "spawn_depth": meta.get("spawnDepth"),
            "tool_use_id": meta.get("toolUseId"),
            "finished": meta.get("finished"),
            "transcript_path": str(f),
            "started_at": start,
            "ended_at": end,
            "duration_s": (end - start) if start is not None and end is not None else None,
            "prompt_excerpt": s["first_user_prompt"],
            "prompt_run": s["prompt_run"],
            "time": s["time"],
            "result_excerpt": s["last_assistant_text"],
            "by_model": s["by_model"],
            "cost_usd": s["estimated_usd"],
        })
    out.sort(key=lambda a: (a["started_at"] is None, a["started_at"] or 0))
    return out


def _config_dirs():
    dirs = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs.append(env)
    dirs.append(str(Path.home() / ".claude"))
    return dirs


def find_transcript(session_id, config_dirs=None):
    if not session_id or "/" in str(session_id):
        return None
    seen, hits = set(), []
    for d in config_dirs or _config_dirs():
        root = Path(d).expanduser()
        if str(root) in seen:
            continue
        seen.add(str(root))
        hits.extend(glob.glob(str(root / "projects" / "*" / f"{glob.escape(str(session_id))}.jsonl")))
    if not hits:
        return None
    return Path(max(hits, key=lambda h: os.path.getmtime(h)))

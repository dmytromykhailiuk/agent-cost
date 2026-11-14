import json
import os
import re
from pathlib import Path

from . import core

ROLE_PURPOSE = {"triage": "verify:triage", "reviewer": "verify:review", "review": "verify:review",
                "qa": "verify:qa", "retro": "verify:retro"}
HERDR_PREFIX_PURPOSE = (("verify-triage", "verify:triage"), ("verify-review", "verify:review"),
                        ("verify-qa", "verify:qa"), ("vault-retro", "verify:retro"), ("verify-retro", "verify:retro"))
PURPOSES = ("verify:triage", "verify:review", "verify:qa", "verify:retro", "vault", "harness", "task")
TRIAGE_PREFIX = "vloop-triage-"

_RUN_PATH_RE = re.compile(r"/runs/(\d{8}-\d{6}-[A-Za-z0-9_.-]+?)/(?:attempt-\d+/(triage|reviewer|qa)\b|(retro)/)")
_SID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SCRATCH_REF_RE = re.compile(r"/claude-\d+/[^/\s]+/(" + _SID + r")(?:/|\b)")
_VALID_SID_RE = re.compile(r"^" + _SID + r"$")


def _sid(value):
    return value if isinstance(value, str) and _VALID_SID_RE.match(value) else None


def vloop_state_dirs():
    raw = os.environ.get("AGENT_COST_VLOOP_DIRS")
    if raw is not None:
        cands = [c for c in raw.split(os.pathsep) if c]
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        cands = [os.environ.get("VERIFY_STATE_DIR") or "", str(base / "agent-verify-loop"), str(base / "claude-verify")]
    out, seen = [], set()
    for c in cands:
        if not c:
            continue
        p = Path(c).expanduser()
        try:
            key = str(p.resolve())
        except OSError:
            continue
        if key not in seen and p.is_dir():
            seen.add(key)
            out.append(p)
    return out


def _jsonl(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _codex(model):
    m = str(model or "").lower()
    return m.startswith(("gpt", "o1", "o3", "o4", "codex"))


class VloopIndex:
    def __init__(self):
        self.runs = {}
        self.agents = {}
        self.parents = {}
        self.triage = {}

    def run_owner(self, run_id):
        return self.runs.get(run_id)

    def link_for(self, sid):
        hit = self.agents.get(sid)
        if hit:
            run_id, role, transport = hit
            owner = self.runs.get(run_id)
            if owner and owner != sid:
                return owner, role, transport
        return None

    def _agent(self, sid, run_id, role, transport=None):
        sid = _sid(sid)
        if not sid or not run_id or role not in ROLE_PURPOSE:
            return
        cur = self.agents.get(sid)
        self.agents[sid] = (run_id, role, transport or (cur[2] if cur else None))

    def _triage(self, run_id, attempt, cost, model, started, duration, platform=None, source="run"):
        if not run_id or cost is None or cost <= 0 or _codex(model) or (platform and platform != "claude"):
            return
        entry = self.triage.setdefault(run_id, {"run_id": run_id, "attempts": {}, "ledger_usd": None,
                                                "started_at": None, "duration_s": None, "model": None})
        if source == "ledger":
            entry["ledger_usd"] = cost
            entry["started_at"] = entry["started_at"] or started
            return
        entry["attempts"][attempt] = cost
        if started is not None and (entry["started_at"] is None or started < entry["started_at"]):
            entry["started_at"] = started
        if duration is not None:
            entry["duration_s"] = (entry["duration_s"] or 0) + duration
        entry["model"] = entry["model"] or model

    def triage_entries(self):
        out = []
        for run_id, e in self.triage.items():
            owner = self.runs.get(run_id)
            if not owner:
                continue
            cost = sum(e["attempts"].values()) if e["attempts"] else e["ledger_usd"]
            if not cost:
                continue
            out.append({"session_id": TRIAGE_PREFIX + run_id, "run_id": run_id, "parent_session_id": owner,
                        "cost_usd": round(cost, 6), "started_at": e["started_at"], "duration_s": e["duration_s"],
                        "model": e["model"], "attempts": max(1, len(e["attempts"]))})
        return out


def _load_run(index, run_dir):
    ctx = core.read_json(run_dir / "context.json")
    if not isinstance(ctx, dict):
        return
    run_id = ctx.get("run_id") or run_dir.name
    owner = _sid(ctx.get("session_id"))
    if owner:
        index.runs[run_id] = owner
    platform = ctx.get("platform")
    for att in ctx.get("attempts") or []:
        if not isinstance(att, dict):
            continue
        for role, info in (att.get("agents") or {}).items():
            if isinstance(info, dict):
                index._agent(info.get("session_id"), run_id, role, info.get("transport"))
        n = att.get("n")
        tri = core.read_json(run_dir / f"attempt-{n}" / "triage.json")
        agent = tri.get("agent") if isinstance(tri, dict) and isinstance(tri.get("agent"), dict) else None
        if agent and tri.get("source") in ("triage", "fallback"):
            index._triage(run_id, n, _num(agent.get("cost_usd")), agent.get("model"),
                          core.parse_ts(att.get("begin_at")), _num(agent.get("duration_s")), platform)
    retro = ctx.get("retro_agent")
    if isinstance(retro, dict):
        index._agent(retro.get("session_id"), run_id, "retro", retro.get("transport"))


def load_vloop(dirs=None):
    index = VloopIndex()
    for base in vloop_state_dirs() if dirs is None else [Path(d) for d in dirs]:
        for rec in _jsonl(base / "ledger.jsonl"):
            run_id, owner = rec.get("run_id"), _sid(rec.get("session_id"))
            if run_id and owner:
                index.runs.setdefault(run_id, owner)
            cost = _num((rec.get("cost_usd") or {}).get("triage")) if isinstance(rec.get("cost_usd"), dict) else None
            ts = core.parse_ts(rec.get("ts"))
            minutes = rec.get("minutes") if isinstance(rec.get("minutes"), dict) else {}
            verify_min = _num(minutes.get("verify")) or _num(minutes.get("wall"))
            started = ts - verify_min * 60 if ts is not None and verify_min is not None else ts
            index._triage(run_id, None, cost, None, started, None, rec.get("platform"), source="ledger")
        sessions = base / "sessions"
        for path in sessions.glob("*.events.jsonl"):
            for ev in _jsonl(path):
                run_id, owner = ev.get("run"), _sid(ev.get("sid"))
                if run_id and owner:
                    index.runs.setdefault(run_id, owner)
                data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                if ev.get("event") == "agent_done":
                    index._agent(data.get("session_id"), run_id, data.get("role"), data.get("transport"))
                elif ev.get("event") == "retro_done":
                    index._agent(data.get("session_id"), run_id, "retro", data.get("transport"))
        for path in sessions.glob("*.json"):
            st = core.read_json(path)
            if not isinstance(st, dict):
                continue
            sid, parent = _sid(st.get("session_id")), _sid(st.get("parent_session_id"))
            if sid and parent and sid != parent:
                index.parents[sid] = parent
        for run_dir in (base / "runs").glob("*"):
            if run_dir.is_dir():
                _load_run(index, run_dir)
    return index


def prompt_run(text):
    m = _RUN_PATH_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.group(2) or m.group(3)


def scratch_refs(text):
    return [m.group(1) for m in _SCRATCH_REF_RE.finditer(text or "")]


def harness_dirs():
    return [_resolved(r) for r in (os.environ.get("CLAUDE_CONFIG_DIR"), "~/.claude") if r]


def inside(path, base):
    if not path or not base:
        return False
    path, base = str(path).rstrip("/"), str(base).rstrip("/")
    return path == base or path.startswith(base + "/")


def _resolved(path):
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(path)


def classify(rec):
    if not isinstance(rec, dict) or rec.get("kind") in (None, "main"):
        return None
    if rec.get("purpose") in PURPOSES:
        return rec["purpose"]
    for name in (rec.get("herdr_id"), rec.get("role"), rec.get("agent_name")):
        if not isinstance(name, str):
            continue
        for prefix, purpose in HERDR_PREFIX_PURPOSE:
            if name.startswith(prefix):
                return purpose
    hit = prompt_run(((rec.get("task") or {}).get("prompt_excerpt")) or "")
    if hit:
        return ROLE_PURPOSE[hit[1]]
    for name in (rec.get("herdr_id"), rec.get("role"), rec.get("agent")):
        if isinstance(name, str) and (name.startswith("vault") or "vault-keeper" in name):
            return "vault"
    prompt = ((rec.get("task") or {}).get("prompt_excerpt")) or ""
    if prompt.startswith("Operation:") and "vault" in prompt:
        return "vault"
    cwd = rec.get("cwd")
    if cwd and any(inside(_resolved(cwd), h) for h in harness_dirs()):
        return "harness"
    return "task"

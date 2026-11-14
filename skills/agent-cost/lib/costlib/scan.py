import calendar
import os
import re
import sys
import time
import tempfile
from pathlib import Path

from . import aggregate, auxlink, core, registry, transcripts

_SINCE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_IDENT_KEYS = ("root_session_id", "parent_session_id", "depth", "kind", "herdr_id", "role", "purpose")
_REFRESH_KEYS = ("estimate", "cost_state_usd", "transcript_model", "headless", "entrypoint")
_SCRATCH_RE = re.compile(r"/claude-\d+/[^/]+/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)")
TASK_OUTPUT_MAX = 4 * 1024 * 1024
_TASK_FILE = "Your task file is "
RUN_SLACK_S = 120


def parse_since(text):
    m = _SINCE_RE.match(text or "")
    if not m:
        return None
    return float(m.group(1)) * _UNIT_S[m.group(2).lower()]


def config_dirs(given=None):
    if given:
        raw = list(given)
    else:
        raw = []
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            raw.append(os.environ["CLAUDE_CONFIG_DIR"])
        raw.append(str(Path.home() / ".claude"))
        profiles = core.read_json(Path.home() / ".claude" / "shared" / "claude-profiles.json", {}) or {}
        for prof in (profiles.get("profiles") or {}).values():
            if isinstance(prof, dict) and prof.get("config_dir"):
                raw.append(prof["config_dir"])
    out, seen = [], set()
    for d in raw:
        p = Path(d).expanduser()
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_transcripts(dirs, cutoff):
    best = {}
    for d in dirs:
        for path in (d / "projects").glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            sid = path.stem
            if sid not in best or mtime > best[sid][0]:
                best[sid] = (mtime, path)
    return {sid: path for sid, (_, path) in best.items()}


def role_of(herdr_id):
    if not herdr_id:
        return None
    head, sep, tail = herdr_id.rpartition("-")
    if sep and head and any(c.isdigit() for c in tail):
        return head
    return herdr_id


def tmp_bases():
    raw = os.environ.get("AGENT_COST_TMP_DIRS")
    cands = raw.split(os.pathsep) if raw else ["/private/tmp", "/tmp", tempfile.gettempdir(),
                                               os.environ.get("CLAUDE_CODE_TMPDIR") or ""]
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


def task_output_links(transcript_path, sid, links=None):
    links = {} if links is None else links
    slug = Path(transcript_path).parent.name
    for base in tmp_bases():
        for out in (base / f"claude-{os.getuid()}" / slug / sid / "tasks").glob("*.output"):
            try:
                if out.stat().st_size > TASK_OUTPUT_MAX:
                    continue
                text = out.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            transcripts.parse_herdr_links(text, links)
    return links


def scratchpad_owner(cwd):
    m = _SCRATCH_RE.search(cwd or "")
    return m.group(1) if m else None


def _aux_link(sid, s, vloop):
    headless = bool(s.get("headless"))
    kind = "headless" if headless else "herdr"
    hid = None if headless else s.get("agent_name")
    hit = vloop.link_for(sid) if vloop else None
    if hit:
        owner, role, transport = hit
        k = "herdr" if transport == "herdr" else ("headless" if headless or transport == "headless" else kind)
        return owner, hid if k == "herdr" else None, k, auxlink.ROLE_PURPOSE[role], role
    prompt = s.get("first_user_prompt") or ""
    run = auxlink.prompt_run(prompt) if (headless or s.get("agent_name") or prompt.startswith(_TASK_FILE)) else None
    owner = vloop.run_owner(run[0]) if run and vloop else None
    if owner and owner != sid:
        return owner, hid, kind, auxlink.ROLE_PURPOSE[run[1]], run[1]
    parent = vloop.parents.get(sid) if vloop else None
    if parent and parent != sid:
        return parent, hid, "herdr", None, None
    if not (headless or s.get("agent_name")):
        return None
    owner = scratchpad_owner(s.get("cwd"))
    if registry.valid_sid(owner) and owner != sid:
        return owner, hid, kind, None, None
    for ref in auxlink.scratch_refs(prompt):
        if ref != sid:
            return ref, hid, kind, None, None
    return None


def _run_stamp(run_id):
    try:
        return calendar.timegm(time.strptime(run_id[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def resolve_runs(vloop, summaries, paths):
    pending = {}
    for sid, s in summaries.items():
        prompt = s.get("first_user_prompt") or ""
        run = auxlink.prompt_run(prompt)
        if run and not vloop.run_owner(run[0]):
            pending.setdefault(run[0], set()).add(sid)
    if not pending:
        return {}
    cache_path = core.cache_dir() / "vloop-runs.json"
    cache = core.read_json(cache_path, {}) or {}
    found = {}
    for run_id, aux in pending.items():
        owner = cache.get(run_id)
        stamp = _run_stamp(run_id)
        if owner is None and stamp is not None:
            needle = run_id.encode("utf-8")
            best = None
            for sid, s in summaries.items():
                if sid in aux or s.get("headless") or auxlink.prompt_run(s.get("first_user_prompt") or ""):
                    continue
                first, last = s.get("first_ts"), s.get("last_ts")
                if first is None or last is None or first > stamp + RUN_SLACK_S or last < stamp - RUN_SLACK_S:
                    continue
                try:
                    hits = Path(paths[sid]).read_bytes().count(needle)
                except (OSError, KeyError):
                    continue
                if hits and (best is None or hits > best[0]):
                    best = (hits, sid)
            owner = best[1] if best else None
            if owner:
                cache[run_id] = owner
        if owner:
            vloop.runs[run_id] = owner
            found[run_id] = owner
    if found:
        try:
            core.write_json_atomic(cache_path, cache)
        except OSError:
            pass
    return found


def build_links(summaries, paths=None, vloop=None):
    paths = paths or {}
    by_name = {}
    for sid, s in summaries.items():
        if s["agent_name"]:
            by_name.setdefault(s["agent_name"], []).append(sid)
    claims = {}
    for psid in summaries:
        links = {l["herdr_id"]: dict(l) for l in summaries[psid]["herdr_links"]}
        if psid in paths:
            for hid, l in task_output_links(paths[psid], psid).items():
                cur = links.setdefault(hid, l)
                if not cur.get("session_id") and l.get("session_id"):
                    cur["session_id"] = l["session_id"]
        for link in links.values():
            hid = link["herdr_id"]
            csid = link.get("session_id")
            if csid not in summaries:
                candidates = [c for c in by_name.get(hid, []) if c != psid]
                csid = candidates[0] if len(candidates) == 1 else None
            if csid and csid != psid:
                claims.setdefault(csid, []).append((psid, hid))
    parent_of = {}
    for csid, options in claims.items():
        started = summaries[csid]["first_ts"]
        earlier = [o for o in options if started is None or (summaries[o[0]]["first_ts"] or 0) <= started]
        options = earlier or options
        options.sort(key=lambda o: (summaries[o[0]]["last_ts"] or 0, o[0]))
        parent_of[csid] = (options[-1][0], options[-1][1], "herdr")
    for sid, s in summaries.items():
        launch = registry.read_launch_record(sid)
        if launch and registry.valid_sid(launch.get("parent_session_id")) and launch["parent_session_id"] != sid:
            parent_of[sid] = (launch["parent_session_id"], launch.get("id"), "herdr")
            continue
        if sid not in parent_of:
            link = _aux_link(sid, s, vloop)
            if link:
                parent_of[sid] = link
    return parent_of


def _chain(sid, parent_of):
    chain, seen = [sid], {sid}
    while chain[-1] in parent_of:
        nxt = parent_of[chain[-1]][0]
        if nxt in seen:
            parent_of.pop(chain[-1])
            break
        seen.add(nxt)
        chain.append(nxt)
    return chain


def _existing(sid):
    root = registry.find_root(sid)
    return (root, registry.load_session(root, sid) or {}) if root else (None, {})


def _fill_gaps(rec, existing):
    out = {}
    for key, value in rec.items():
        if value is None:
            continue
        cur = existing.get(key)
        if key in _REFRESH_KEYS or cur is None:
            out[key] = value
        elif isinstance(value, dict) and isinstance(cur, dict):
            sub = {k: v for k, v in value.items() if v is not None and cur.get(k) is None}
            if sub:
                out[key] = sub
    return out


def _record(sid, s, tp, link, root, depth):
    kind = link[2] if link else "main"
    if not link:
        role = "main"
    elif len(link) > 4 and link[4]:
        role = link[4]
    else:
        role = "headless" if kind == "headless" else (role_of(link[1]) or "agent")
    rec = {
        "session_id": sid,
        "root_session_id": root,
        "parent_session_id": link[0] if link else None,
        "depth": depth,
        "kind": kind,
        "herdr_id": link[1] if link else None,
        "role": role,
        "cwd": s["cwd"],
        "model": s["model"],
        "effort": s["effort"],
        "entrypoint": s.get("entrypoint"),
        "headless": bool(s.get("headless")),
        "transcript_path": str(tp),
        "started_at": s["first_ts"],
        "last_activity_at": s["last_ts"],
        "result_excerpt": s["last_assistant_text"],
        "source": "scan",
    }
    task = {"prompt_excerpt": s["first_user_prompt"]}
    if link and kind == "herdr":
        task["description"] = role
    rec["task"] = task
    if link:
        rec["purpose"] = (link[3] if len(link) > 3 else None) or auxlink.classify(rec | {"agent_name": s.get("agent_name")})
    cs = s.get("cost_state")
    if isinstance(cs, dict) and s["last_ts"] is not None:
        rec["ended_at"] = s["last_ts"]
    return rec


def _resolve_parent(psid, resolved):
    if psid in resolved:
        return resolved[psid]
    root = registry.find_root(psid)
    if not root:
        return None
    rec = registry.load_session(root, psid) or {}
    return root, int(rec.get("depth") or 0)


def _triage_sessions(vloop, cutoff, resolved, stats):
    for e in vloop.triage_entries():
        started = e["started_at"]
        if started is None or started + 86400 < cutoff:
            continue
        target = _resolve_parent(e["parent_session_id"], resolved)
        if not target:
            continue
        root, depth = target[0], target[1] + 1
        ended = started + (e["duration_s"] or 0)
        rec = {"session_id": e["session_id"], "root_session_id": root, "parent_session_id": e["parent_session_id"],
               "depth": depth, "kind": "headless", "herdr_id": None, "role": "triage", "purpose": "verify:triage",
               "synthetic": "vloop-triage", "reported_usd": e["cost_usd"], "model": e["model"], "headless": True,
               "started_at": started, "last_activity_at": ended, "ended_at": ended, "source": "vloop",
               "task": {"description": "verify-loop triage",
                        "prompt_excerpt": f"vloop run {e['run_id']} ({e['attempts']} attempt(s), claude -p)"}}
        existing = registry.load_session(root, e["session_id"])
        if existing and all(existing.get(k) == v for k, v in rec.items() if k != "task"):
            continue
        old_root = registry.find_root(e["session_id"])
        if old_root and old_root != root:
            try:
                (core.state_dir() / "sessions" / old_root / f"{e['session_id']}.json").unlink()
            except OSError:
                pass
            stats["roots"].add(old_root)
        registry.save_session(rec)
        stats["roots"].add(root)
        stats["triage"] += 1
        stats["purposes"]["verify:triage"] = stats["purposes"].get("verify:triage", 0) + 1


def run_scan(window=None, cutoff=None, dirs=None):
    if cutoff is None:
        cutoff = core.now() - (window if window is not None else 30 * 86400)
    found = find_transcripts(config_dirs(dirs), cutoff)
    summaries = {sid: transcripts.summarize(path) for sid, path in found.items()}
    summaries = {sid: s for sid, s in summaries.items() if registry.valid_sid(sid)}
    vloop = auxlink.load_vloop()
    resolve_runs(vloop, summaries, found)
    parent_of = build_links(summaries, found, vloop)
    order = {sid: len(_chain(sid, parent_of)) for sid in summaries}

    stats = {"transcripts": len(found), "sessions": 0, "linked": 0, "relinked": 0, "headless": 0,
             "builtins": 0, "roots": set(), "purposes": {}, "triage": 0}
    resolved = {}
    for sid in sorted(summaries, key=lambda x: (order[x], x)):
        s = summaries[sid]
        old_root, existing = _existing(sid)
        link = parent_of.get(sid)
        if old_root and old_root != sid:
            link = None
        elif not link and registry.valid_sid(existing.get("parent_session_id")) and existing["parent_session_id"] != sid:
            kind = existing.get("kind") if existing.get("kind") in ("herdr", "headless") else "herdr"
            link = (existing["parent_session_id"], existing.get("herdr_id"), kind, existing.get("purpose"),
                    existing.get("role"))
        target = _resolve_parent(link[0], resolved) if link else None
        if link and (target is None or target[0] == sid):
            link = target = None
        if link:
            root, depth = target[0], target[1] + 1
        else:
            root, depth = old_root or sid, int(existing.get("depth") or 0)
        rec = _record(sid, s, found[sid], link, root, depth)
        if not link and old_root:
            for key in _IDENT_KEYS:
                if existing.get(key) is not None:
                    rec[key] = existing[key]
        est = aggregate.estimate(found[sid])
        rec["estimate"] = est["cache"]
        if est["cost_state_usd"] is not None:
            rec["cost_state_usd"] = est["cost_state_usd"]
        if s["model"]:
            rec["transcript_model"] = s["model"]
        merged = _fill_gaps(rec, existing)
        if link:
            for key in _IDENT_KEYS:
                merged[key] = rec[key]
            if old_root == sid:
                registry.move_root(old_root, root, depth)
                stats["roots"].discard(old_root)
                stats["relinked"] += 1
        merged["session_id"] = sid
        merged["root_session_id"] = root
        try:
            registry.save_session(merged)
        except ValueError:
            continue
        resolved[sid] = (root, int((registry.load_session(root, sid) or {}).get("depth") or depth))
        stats["roots"].add(root)
        stats["sessions"] += 1
        stats["builtins"] += est["cache"]["builtin_count"]
        stats["linked"] += 1 if (link or (old_root and old_root != sid)) else 0
        stats["headless"] += 1 if s.get("headless") else 0
        if link and merged.get("purpose"):
            stats["purposes"][merged["purpose"]] = stats["purposes"].get(merged["purpose"], 0) + 1

    _triage_sessions(vloop, cutoff, resolved, stats)
    total = 0.0
    for root in sorted(stats["roots"]):
        if not registry.session_ids(root):
            try:
                aggregate.root_path(root).unlink()
            except (OSError, ValueError):
                pass
            continue
        try:
            path = aggregate.write_root(root)
        except (OSError, ValueError):
            continue
        data = core.read_json(path, {}) or {}
        total += (data.get("totals") or {}).get("cost_usd") or 0.0
    stats["total_usd"] = total
    return stats


def cmd_scan(args):
    window = parse_since(getattr(args, "since", "30d"))
    if window is None:
        print(f"agent-cost scan: invalid --since {args.since!r} (use e.g. 30d, 12h)", file=sys.stderr)
        return 2
    st = run_scan(window=window, dirs=getattr(args, "config_dir", None))
    print(f"scanned {st['transcripts']} transcripts: {len(st['roots'])} roots, {st['sessions']} sessions, "
          f"{st['linked']} children linked ({st['relinked']} relinked), {st['headless']} headless, "
          f"{st['builtins']} builtin agents, total ${st['total_usd']:.2f}")
    if st["purposes"]:
        print("linked by purpose: " + ", ".join(f"{k} {v}" for k, v in sorted(st["purposes"].items())))
    return 0

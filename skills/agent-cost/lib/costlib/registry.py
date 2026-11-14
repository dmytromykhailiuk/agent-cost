import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path

from . import auxlink, core

SCHEMA = 1
PROMPT_EXCERPT = 600
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HERDR_ID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z][A-Za-z0-9_]*)*?)-[0-9a-f]{4,}$")
_MIN_KEYS = ("started_at",)
_MAX_KEYS = ("last_activity_at",)


def valid_sid(sid):
    return isinstance(sid, str) and bool(_SID_RE.match(sid))


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def herdr_state_dir(env=None):
    env = os.environ if env is None else env
    if env.get("HERDR_AGENTS_STATE_DIR"):
        return Path(env["HERDR_AGENTS_STATE_DIR"]).expanduser()
    base = env.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "herdr-agents"


def _flag(args, name):
    if not isinstance(args, list):
        return None
    for i, tok in enumerate(args):
        if not isinstance(tok, str):
            continue
        if tok == name and i + 1 < len(args) and isinstance(args[i + 1], str):
            return args[i + 1]
        if tok.startswith(name + "="):
            return tok.partition("=")[2]
    return None


def read_herdr_meta(agent_dir):
    if not agent_dir:
        return None
    d = Path(agent_dir).expanduser()
    meta = core.read_json(d / "meta.json")
    if not isinstance(meta, dict):
        return None
    out = dict(meta)
    try:
        out["_prompt"] = (d / "prompt.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        out["_prompt"] = None
    return out


def read_launch_record(sid, env=None):
    if not valid_sid(sid):
        return None
    rec = core.read_json(herdr_state_dir(env) / "launches" / f"{sid}.json")
    if not isinstance(rec, dict):
        return None
    renv = rec.get("env") if isinstance(rec.get("env"), dict) else {}
    args = rec.get("args")
    return {
        "id": renv.get("HERDR_AGENT_ID") or rec.get("name"),
        "parent_session_id": renv.get("HERDR_PARENT_SESSION_ID"),
        "root_session_id": renv.get("HERDR_ROOT_SESSION_ID"),
        "depth": renv.get("HERDR_AGENT_DEPTH"),
        "model": _flag(args, "--model"),
        "effort": _flag(args, "--effort"),
        "agent": _flag(args, "--agent"),
        "cwd": rec.get("cwd"),
        "created_at": rec.get("created_at"),
        "agent_dir": rec.get("agent_dir") or renv.get("HERDR_AGENT_DIR"),
    }


def foreign_env(sid, env):
    own = env.get("CLAUDE_CODE_SESSION_ID")
    return bool(own) and own != sid


def own_root_identity(sid):
    return {"session_id": sid, "root_session_id": sid, "parent_session_id": None, "depth": 0,
            "kind": "main", "herdr_id": None, "role": "main", "purpose": None}


def identity(hook_input, env=None):
    env = os.environ if env is None else env
    hook_input = hook_input if isinstance(hook_input, dict) else {}
    sid = hook_input.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
    sid = sid if valid_sid(sid) else None
    parent = env.get("HERDR_PARENT_SESSION_ID") or None
    root = env.get("HERDR_ROOT_SESSION_ID") or None
    herdr_id = env.get("HERDR_AGENT_ID") or None
    depth = _int(env.get("HERDR_AGENT_DEPTH"))
    role = None
    meta = read_herdr_meta(env.get("HERDR_AGENT_DIR"))
    owner = meta.get("session_id") if meta else None
    if sid and valid_sid(owner) and owner != sid:
        if herdr_id and herdr_id != meta.get("id"):
            meta = None
        else:
            vrole = env.get("VLOOP_GUARD_ROLE") or None
            return {"session_id": sid, "root_session_id": root if valid_sid(root) else owner,
                    "parent_session_id": owner, "depth": (_int(meta.get("depth"), depth or 0) or 0) + 1,
                    "kind": "herdr", "herdr_id": None, "role": vrole or "nested",
                    "purpose": auxlink.ROLE_PURPOSE.get(vrole)}
    if not (parent or herdr_id) and sid:
        launch = read_launch_record(sid, env)
        if launch and launch.get("parent_session_id"):
            parent = launch["parent_session_id"]
            root = launch.get("root_session_id")
            herdr_id = launch.get("id")
            depth = _int(launch.get("depth"))
    if meta:
        herdr_id = herdr_id or meta.get("id")
        parent = parent or meta.get("parent_session_id")
        depth = depth if depth is not None else _int(meta.get("depth"))
        role = meta.get("role")
    if not role and herdr_id:
        m = _HERDR_ID_RE.match(herdr_id)
        role = m.group(1) if m else None
    is_herdr = bool(parent or herdr_id) and parent != sid
    if not is_herdr:
        vparent = env.get("VLOOP_PARENT_SESSION_ID")
        if sid and valid_sid(vparent) and vparent != sid:
            vrole = env.get("VLOOP_GUARD_ROLE") or None
            proot = find_root(vparent)
            prec = load_session(proot, vparent) if proot else None
            return {"session_id": sid, "root_session_id": proot or vparent, "parent_session_id": vparent,
                    "depth": int((prec or {}).get("depth") or 0) + 1, "kind": "headless", "herdr_id": None,
                    "role": vrole or "headless", "purpose": auxlink.ROLE_PURPOSE.get(vrole)}
        return {"session_id": sid, "root_session_id": sid, "parent_session_id": None, "depth": 0,
                "kind": "main", "herdr_id": None, "role": "main"}
    if not valid_sid(root):
        root = parent if valid_sid(parent) else sid
    if depth is None:
        depth = 1
    return {"session_id": sid, "root_session_id": root, "parent_session_id": parent, "depth": depth,
            "kind": "herdr", "herdr_id": herdr_id, "role": role}


_HIERARCHY_KEYS = ("parent_session_id", "depth", "kind", "herdr_id", "role", "purpose")


def resolve(sid, payload, env, clear=False):
    root = find_root(sid)
    if root:
        record = load_session(root, sid) or {}
        ident = {k: record.get(k) for k in _HIERARCHY_KEYS}
        ident.update(session_id=sid, root_session_id=root)
        return ident, record
    if foreign_env(sid, env):
        ident = own_root_identity(sid)
    else:
        ident = identity(dict(payload or {}, session_id=sid), env)
    if clear:
        ident.update(root_session_id=sid, parent_session_id=None, depth=0, kind="main", role="main")
    return ident, {}


def session_path(root, sid):
    if not (valid_sid(root) and valid_sid(sid)):
        raise ValueError("invalid session id")
    return core.sessions_dir(root) / f"{sid}.json"


def live_path(root, sid):
    return core.sessions_dir(root) / f"{sid}.live.json"


def load_session(root, sid):
    if not (valid_sid(root) and valid_sid(sid)):
        return None
    data = core.read_json(core.state_dir() / "sessions" / root / f"{sid}.json")
    return data if isinstance(data, dict) else None


def load_live(root, sid):
    if not (valid_sid(root) and valid_sid(sid)):
        return None
    data = core.read_json(core.state_dir() / "sessions" / root / f"{sid}.live.json")
    return data if isinstance(data, dict) else None


def merge(old, new):
    out = dict(old or {})
    for key, value in (new or {}).items():
        if value is None:
            continue
        cur = out.get(key)
        if isinstance(value, dict) and isinstance(cur, dict):
            out[key] = merge(cur, value)
        elif key in _MIN_KEYS and isinstance(cur, (int, float)) and isinstance(value, (int, float)):
            out[key] = min(cur, value)
        elif key in _MAX_KEYS and isinstance(cur, (int, float)) and isinstance(value, (int, float)):
            out[key] = max(cur, value)
        else:
            out[key] = value
    return out


@contextmanager
def _locked(path):
    lock = path.with_name(path.name + ".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def save_session(record):
    sid = record.get("session_id")
    root = record.get("root_session_id")
    path = session_path(root, sid)
    with _locked(path):
        current = core.read_json(path, {})
        current = current if isinstance(current, dict) else {}
        for key in record.get("_unset") or ():
            current.pop(key, None)
        merged = merge(current, {k: v for k, v in record.items() if k != "_unset"})
        merged["schema"] = SCHEMA
        merged["updated_at"] = core.now()
        core.write_json_atomic(path, merged)
    _index_put(sid, root)
    return merged


def _index_dir():
    return core.state_dir() / "index"


def _index_put(sid, root):
    try:
        path = _index_dir() / sid
        if path.is_file() and path.read_text(encoding="utf-8").strip() == root:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{sid}.{os.getpid()}.tmp")
        tmp.write_text(root, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def find_root(sid):
    if not valid_sid(sid):
        return None
    try:
        root = (_index_dir() / sid).read_text(encoding="utf-8").strip()
        if valid_sid(root) and (core.state_dir() / "sessions" / root / f"{sid}.json").is_file():
            return root
    except OSError:
        pass
    base = core.state_dir() / "sessions"
    hits = sorted(base.glob(f"*/{sid}.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not hits:
        return None
    root = hits[0].parent.name
    _index_put(sid, root)
    return root


def session_ids(root):
    if not valid_sid(root):
        return []
    d = core.state_dir() / "sessions" / root
    out = []
    for p in sorted(d.glob("*.json")):
        name = p.name
        if name.endswith(".live.json"):
            continue
        sid = name[: -len(".json")]
        if valid_sid(sid):
            out.append(sid)
    return out


def load_root_sessions(root):
    out = []
    for sid in session_ids(root):
        rec = load_session(root, sid)
        if rec:
            rec.setdefault("session_id", sid)
            rec.setdefault("root_session_id", root)
            out.append(rec)
    return out


def _unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


def move_root(old_root, new_root, depth_shift=0):
    if not (valid_sid(old_root) and valid_sid(new_root)) or old_root == new_root:
        return []
    src = core.state_dir() / "sessions" / old_root
    dst = core.sessions_dir(new_root)
    moved = []
    for sid in session_ids(old_root):
        path = src / f"{sid}.json"
        target = dst / f"{sid}.json"
        with _locked(target):
            rec = core.read_json(path, {})
            rec = rec if isinstance(rec, dict) else {}
            current = core.read_json(target, {})
            rec = merge(rec, current if isinstance(current, dict) else {})
            rec["root_session_id"] = new_root
            rec["depth"] = int(rec.get("depth") or 0) + depth_shift
            rec["updated_at"] = core.now()
            core.write_json_atomic(target, rec)
        live = src / f"{sid}.live.json"
        if live.is_file():
            try:
                os.replace(live, dst / live.name)
            except OSError:
                pass
        _unlink(path)
        _unlink(path.with_name(path.name + ".lock"))
        _index_put(sid, new_root)
        moved.append(sid)
    for leftover in src.glob("*.lock"):
        _unlink(leftover)
    try:
        src.rmdir()
    except OSError:
        pass
    _unlink(core.roots_dir() / f"{old_root}.json")
    return moved

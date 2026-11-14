import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
PLUGIN_ROOT = SKILL_DIR.parent.parent

_TAG_RE = re.compile(r"</?pasted_content[^>]*>", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _ensure(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir():
    override = os.environ.get("AGENT_COST_STATE_DIR")
    if override:
        return _ensure(Path(override).expanduser())
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return _ensure(Path(base) / "agent-cost")


def sessions_dir(root_sid):
    return _ensure(state_dir() / "sessions" / str(root_sid))


def roots_dir():
    return _ensure(state_dir() / "roots")


def cache_dir():
    return _ensure(state_dir() / "cache")


def reports_dir():
    return _ensure(state_dir() / "reports")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def now():
    return time.time()


def iso(ts):
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_ts(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    if num != num or num in (float("inf"), float("-inf")):
        return None
    if abs(num) >= 1e11:
        num /= 1000.0
    return num


def excerpt(text, limit=600):
    if not text:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", str(text))).strip()
    if len(text) > limit:
        return text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def plugin_version():
    manifest = read_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json", {}) or {}
    return manifest.get("version") or "unknown"


def write_cli_pointer():
    path = state_dir() / "cli.json"
    current = read_json(path, {}) or {}
    wanted = {"path": str(SKILL_DIR.absolute() / "bin" / "agent-cost"), "version": plugin_version()}
    if current.get("path") == wanted["path"] and current.get("version") == wanted["version"]:
        return False
    wanted["updated_at"] = iso(now())
    write_json_atomic(path, wanted)
    return True

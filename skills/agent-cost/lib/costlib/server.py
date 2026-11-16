import hashlib
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib.parse import parse_qs

from . import aggregate, core, registry, report

HOST = "127.0.0.1"
HOST_ENV = "AGENT_COST_HOST"
ALLOWED_HOSTS_ENV = "AGENT_COST_ALLOWED_HOSTS"
WILDCARD_HOSTS = ("", "0.0.0.0", "::")
DEFAULT_PORT = 47821
IDLE_TIMEOUT_S = 2 * 3600
REFRESH_EVERY_S = 10
REFRESH_OVERLAP_S = 60
VERSION_MEMO_S = 1.0
START_WAIT_S = 15.0
HISTORY = 64


def _host_name(value):
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return (value[1:end], value[end + 1:].lstrip(":")) if end > 0 else (value, "")
    if value.count(":") == 1:
        name, _, port = value.partition(":")
        return name, port
    return value, ""


def parse_hosts(values):
    out = []
    for value in values or ():
        for part in str(value).split(","):
            name = _host_name(part)[0] if part.strip() else ""
            if name and name not in out:
                out.append(name)
    return out


def resolve_host(flag=None):
    return (flag if flag is not None else os.environ.get(HOST_ENV) or HOST).strip()


def resolve_allowed(flags=None):
    return parse_hosts(flags if flags else [os.environ.get(ALLOWED_HOSTS_ENV, "")])


def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_host(host):
    if host in WILDCARD_HOSTS:
        return "::1" if host == "::" else HOST
    return host


def url_host(host):
    host = local_host(host)
    return f"[{host}]" if ":" in host else host


def exposure_warning(host):
    if is_loopback(host):
        return None
    shown = "all interfaces" if host in WILDCARD_HOSTS else host
    return (f"agent-cost serve: WARNING listening on {shown}; the page has no auth and is reachable by anyone "
            "who can reach the port")


def state_file():
    return core.state_dir() / "server.json"


def log_file():
    return core.state_dir() / "server.log"


def log(msg):
    try:
        with open(log_file(), "a", encoding="utf-8") as f:
            f.write(f"{core.iso(core.now())} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


class IdleTracker:
    def __init__(self, timeout=IDLE_TIMEOUT_S, clock=time.monotonic):
        self.timeout = timeout
        self.clock = clock
        self.last = clock()

    def touch(self):
        self.last = self.clock()

    def expired(self):
        return self.clock() - self.last >= self.timeout


class RootCache:
    def __init__(self):
        self.entries = {}

    def update(self, vroots):
        seen = set()
        for path in core.roots_dir().glob("*.json"):
            sid = path.stem
            if not registry.valid_sid(sid):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            key = (st.st_mtime_ns, st.st_size)
            cur = self.entries.get(sid)
            if cur is None or cur["key"] != key:
                data = core.read_json(path)
                if not isinstance(data, dict) or not data.get("root_session_id"):
                    continue
                summary = aggregate._summary(data, path)
                cur = {"key": key, "data": data,
                       "last": core.parse_ts(summary.get("last_activity_at") or summary.get("started_at")),
                       "cwd": summary.get("cwd")}
                self.entries[sid] = cur
            cur["hidden"] = report.is_hidden(cur["cwd"], vroots)
            seen.add(sid)
        for sid in list(self.entries):
            if sid not in seen:
                del self.entries[sid]

    def versions(self, cutoff, focus=None):
        out = {}
        for sid, e in self.entries.items():
            if sid != focus and (e["last"] is None or e["last"] < cutoff):
                continue
            out[sid] = f"{e['key'][0]:x}-{e['key'][1]:x}" + ("-h" if e["hidden"] else "")
        return out

    def latest(self):
        shown = [(e["last"] or 0, sid) for sid, e in self.entries.items() if not e["hidden"]]
        pool = shown or [(e["last"] or 0, sid) for sid, e in self.entries.items()]
        return max(pool)[1] if pool else None

    def root(self, sid):
        e = self.entries.get(sid)
        return dict(e["data"], hidden=e["hidden"]) if e else None


class App:
    def __init__(self, focus=None, idle=None, refresh_every=REFRESH_EVERY_S):
        self.focus = focus
        self.idle = idle or IdleTracker()
        self.refresh_every = refresh_every
        self.refreshes = 0
        self.last_refresh_at = None
        self.version = core.plugin_version()
        self.started_at = core.now()
        self._lock = threading.Lock()
        self._snap = (0.0, None)
        self._payload = (None, None)
        self._cache = RootCache()
        self._history = OrderedDict()
        self._stop = threading.Event()
        self._cutoff = None

    def snapshot(self):
        with self._lock:
            at, snap = self._snap
            if snap is not None and time.monotonic() - at < VERSION_MEMO_S:
                return snap
            vroots = report.visible_roots()
            proots = report.project_roots()
            self._cache.update(vroots)
            focus = self.focus or self._cache.latest()
            versions = self._cache.versions(core.now() - report.PAYLOAD_DAYS * 86400, focus)
            cfg = {"visible_roots": vroots, "project_roots": proots, "focus": focus}
            digest = hashlib.sha1(json.dumps([cfg, sorted(versions.items())]).encode("utf-8")).hexdigest()[:16]
            snap = {"token": digest, "versions": versions, "cfg": cfg}
            self._history[digest] = snap
            self._history.move_to_end(digest)
            while len(self._history) > HISTORY:
                self._history.popitem(last=False)
            self._snap = (time.monotonic(), snap)
            return snap

    def token(self):
        return self.snapshot()["token"]

    def invalidate(self):
        with self._lock:
            self._snap = (0.0, None)

    def _meta(self, snap, incremental):
        cfg = snap["cfg"]
        return {"generated_at": core.iso(core.now()), "focus_root_session_id": cfg["focus"],
                "project_roots": cfg["project_roots"], "visible_roots": cfg["visible_roots"],
                "version": snap["token"], "live": True, "incremental": incremental}

    def _roots(self, sids):
        with self._lock:
            return [r for r in (self._cache.root(sid) for sid in sids) if r]

    def payload_bytes(self, since=None):
        snap = self.snapshot()
        with self._lock:
            base = self._history.get(since) if since else None
        if base is not None and base["cfg"] == snap["cfg"]:
            cur, old = snap["versions"], base["versions"]
            payload = self._meta(snap, True)
            payload["base"] = since
            payload["roots"] = self._roots([sid for sid, v in cur.items() if old.get(sid) != v])
            payload["removed"] = sorted(sid for sid in old if sid not in cur)
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            if self._payload[0] == snap["token"]:
                return self._payload[1]
        payload = self._meta(snap, False)
        payload["roots"] = self._roots(snap["versions"])
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            self._payload = (snap["token"], body)
        return body

    def refresh_once(self):
        started = core.now()
        cutoff = None if self._cutoff is None else self._cutoff - REFRESH_OVERLAP_S
        written = report.refresh(cutoff=cutoff)
        self._cutoff = started
        self.last_refresh_at = core.now()
        if written:
            self.refreshes += 1
            self.invalidate()
        return written

    def refresher(self):
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                log("refresh failed:\n" + traceback.format_exc())
            self._stop.wait(self.refresh_every)

    def stop(self):
        self._stop.set()


def page_bytes():
    return report.template_path().read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-cost"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def _host_ok(self, value):
        value = value.strip().lower()
        port = self.server.server_address[1]
        if value in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return True
        if ":" in self.server.server_address[0] and value == f"[::1]:{port}":
            return True
        return bool(value) and _host_name(value)[0] in self.server.extra_hosts

    def _request_ok(self):
        if not self._host_ok(self.headers.get("Host") or ""):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        origin = origin.strip().lower()
        return origin.startswith("http://") and self._host_ok(origin[len("http://"):])

    def do_GET(self):
        if not self._request_ok():
            try:
                self._json({"error": "forbidden host"}, 403)
            except OSError:
                pass
            return
        app = self.server.app
        app.idle.touch()
        path, _, query = self.path.split("#", 1)[0].partition("?")
        params = parse_qs(query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, page_bytes(), "text/html; charset=utf-8")
            elif path == "/api/data":
                since = (params.get("since") or [None])[0]
                self._send(200, app.payload_bytes(since), "application/json; charset=utf-8")
            elif path == "/api/version":
                snap = app.snapshot()
                body = {"version": snap["token"], "token": snap["token"], "count": len(snap["versions"]),
                        "refreshes": app.refreshes, "last_refresh_at": core.iso(app.last_refresh_at)}
                if (params.get("roots") or ["0"])[0] not in ("0", ""):
                    body["roots"] = snap["versions"]
                self._json(body)
            elif path == "/api/health":
                self._json({"ok": True, "pid": os.getpid(), "version": app.version,
                            "started_at": core.iso(app.started_at), "port": self.server.server_address[1]})
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log(f"GET {path} failed:\n" + traceback.format_exc())
            try:
                self._json({"error": "internal"}, 500)
            except OSError:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    extra_hosts = frozenset()


class Server6(Server):
    address_family = socket.AF_INET6


def bind(port, host=HOST, allowed=()):
    cls = Server6 if ":" in host else Server
    extra = set(allowed)
    if host not in WILDCARD_HOSTS + (HOST, "localhost", "::1"):
        extra.add(host.lower())
    for candidate in ([port] if port else []) + [0]:
        try:
            httpd = cls((host, candidate), Handler)
        except OSError:
            continue
        httpd.extra_hosts = frozenset(extra)
        return httpd
    raise OSError("no free port")


def _idle_watch(httpd, app, every):
    while not app._stop.wait(every):
        if app.idle.expired():
            log("idle timeout, shutting down")
            httpd.shutdown()
            return


def run_foreground(port=DEFAULT_PORT, focus=None, idle_timeout=IDLE_TIMEOUT_S, refresh_every=REFRESH_EVERY_S,
                   ready=None, write_state=True, host=HOST, allowed=()):
    httpd = bind(port, host, allowed)
    app = App(focus=focus, idle=IdleTracker(idle_timeout), refresh_every=refresh_every)
    httpd.app = app
    actual = httpd.server_address[1]
    info = {"pid": os.getpid(), "port": actual, "started_at": core.iso(app.started_at), "version": app.version,
            "host": host, "allowed_hosts": sorted(allowed), "url": f"http://{url_host(host)}:{actual}/"}
    if write_state:
        core.write_json_atomic(state_file(), info)
    log(f"listening on {info['url']} (version {app.version})")
    threads = [threading.Thread(target=app.refresher, name="refresher", daemon=True),
               threading.Thread(target=_idle_watch, args=(httpd, app, min(30.0, max(idle_timeout / 4, 0.05))),
                                name="idle", daemon=True)]
    for t in threads:
        t.start()
    if ready is not None:
        ready(httpd)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.stop()
        httpd.server_close()
        if write_state:
            cur = core.read_json(state_file(), {}) or {}
            if cur.get("pid") == os.getpid():
                try:
                    state_file().unlink()
                except OSError:
                    pass
        log("stopped")
    return info


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def health(port, timeout=1.5, host=HOST):
    try:
        with urllib.request.urlopen(f"http://{url_host(host)}:{port}/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) and data.get("ok") else None
    except (OSError, ValueError):
        return None


def running():
    info = core.read_json(state_file())
    if not isinstance(info, dict) or not _pid_alive(info.get("pid")):
        return None
    h = health(info.get("port"), host=info.get("host") or HOST)
    if not h or h.get("pid") != info.get("pid"):
        return None
    info["version"] = h.get("version")
    return info


def stop_server(info=None, wait=5.0):
    info = info or running()
    if not info:
        stale = core.read_json(state_file())
        if isinstance(stale, dict) and not _pid_alive(stale.get("pid")):
            try:
                state_file().unlink()
            except OSError:
                pass
        return False
    try:
        os.kill(info["pid"], signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and _pid_alive(info["pid"]):
        try:
            os.waitpid(info["pid"], os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    cur = core.read_json(state_file(), {}) or {}
    if cur.get("pid") == info["pid"]:
        try:
            state_file().unlink()
        except OSError:
            pass
    return True


def _cli_path():
    return Path(core.SKILL_DIR) / "bin" / "agent-cost"


def spawn(port, host=HOST, allowed=()):
    log_path = log_file()
    cmd = [sys.executable, str(_cli_path()), "serve", "--foreground", "--port", str(port), "--host", host]
    for name in allowed:
        cmd += ["--allow-host", name]
    with open(log_path, "a", encoding="utf-8") as out, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(cmd, stdin=devnull, stdout=out, stderr=out, start_new_session=True,
                                close_fds=True, cwd=str(core.state_dir()))
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        info = core.read_json(state_file())
        if isinstance(info, dict) and info.get("pid") == proc.pid and health(info.get("port"), 0.5, host):
            return info
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return None


def url_for(info, sid=None):
    return f"http://{url_host(info.get('host') or HOST)}:{info['port']}/" + (f"#{sid}" if sid else "")


def _focus_sid(args):
    sid = getattr(args, "session", None) or report.env_session()
    return sid if registry.valid_sid(sid) else None


def _ensure_running(port, host=HOST, allowed=()):
    info = running()
    how = "started"
    if info and (info.get("version") != core.plugin_version() or (info.get("host") or HOST) != host
                 or sorted(info.get("allowed_hosts") or []) != sorted(allowed)):
        stop_server(info)
        info, how = None, "restarted"
    if info:
        return info, "running"
    info = spawn(port, host, allowed)
    return info, how


def _terminate(*_):
    raise KeyboardInterrupt


def cmd_serve(args):
    wanted = getattr(args, "session", None)
    if wanted and not registry.valid_sid(wanted):
        print(f"agent-cost serve: invalid session id {wanted!r}", file=sys.stderr)
        return 2
    port = args.port if args.port is not None else DEFAULT_PORT
    host = resolve_host(getattr(args, "host", None))
    allowed = resolve_allowed(getattr(args, "allow_host", None))
    if args.stop:
        print("agent-cost serve: stopped" if stop_server() else "agent-cost serve: not running")
        return 0
    if args.status:
        info = running()
        if not info:
            print("agent-cost serve: not running")
            return 1
        print(f"agent-cost serve: running pid {info['pid']} version {info.get('version')}")
        print(url_for(info))
        return 0
    if args.foreground:
        def ready(httpd):
            print(f"agent-cost serve: listening pid {os.getpid()}", flush=True)
            warning = exposure_warning(host)
            if warning:
                print(warning, file=sys.stderr, flush=True)
            print(f"http://{url_host(host)}:{httpd.server_address[1]}/", flush=True)

        try:
            signal.signal(signal.SIGTERM, _terminate)
        except ValueError:
            pass
        try:
            run_foreground(port=port, ready=ready, host=host, allowed=allowed)
        except KeyboardInterrupt:
            pass
        return 0

    info, how = _ensure_running(port, host, allowed)
    if not info:
        print(f"agent-cost serve: server failed to start, see {log_file()}", file=sys.stderr)
        return 1
    sid = _focus_sid(args)
    url = url_for(info, sid)
    warning = exposure_warning(info.get("host") or HOST)
    if warning:
        print(warning, file=sys.stderr)
    print(f"agent-cost serve: {how} pid {info['pid']} on port {info['port']} (live, refresh every {REFRESH_EVERY_S}s)")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(url)
    return 0


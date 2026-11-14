import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import aggregate, core, registry, report, server  # noqa: E402

ROOT = "cccccccc-0000-4000-8000-000000000001"
CHILD = "cccccccc-0000-4000-8000-000000000002"


def serve_args(**kw):
    base = dict(port=0, session=None, no_open=True, stop=False, status=False, foreground=False)
    base.update(kw)
    return argparse.Namespace(**base)


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


class ServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / "claude").mkdir()
        env = {
            "AGENT_COST_STATE_DIR": str(base / "state"),
            "AGENT_COST_CONFIG": str(base / "config.json"),
            "CLAUDE_CONFIG_DIR": str(base / "claude"),
            "AGENT_COST_TMP_DIRS": str(base),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("AGENT_COST_SESSION", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(key, None)

    def seed(self):
        registry.save_session({"session_id": ROOT, "root_session_id": ROOT, "kind": "main", "depth": 0,
                               "cwd": "/tmp/p", "started_at": core.now() - 600, "last_activity_at": core.now() - 60})
        aggregate.write_root(ROOT)


class ForegroundServerTest(ServerBase):
    def setUp(self):
        super().setUp()
        self.seed()
        memo = mock.patch.object(server, "VERSION_MEMO_S", 0)
        memo.start()
        self.addCleanup(memo.stop)
        started = threading.Event()
        holder = {}

        def ready(httpd):
            holder["httpd"] = httpd
            started.set()

        self.thread = threading.Thread(target=server.run_foreground,
                                       kwargs=dict(port=0, ready=ready, refresh_every=3600), daemon=True)
        self.thread.start()
        self.assertTrue(started.wait(10))
        self.httpd = holder["httpd"]
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.shutdown)
        deadline = time.monotonic() + 10
        while self.httpd.app.last_refresh_at is None and time.monotonic() < deadline:
            time.sleep(0.02)

    def shutdown(self):
        self.httpd.shutdown()
        self.thread.join(10)

    def test_binds_loopback_and_writes_state(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        info = core.read_json(server.state_file())
        self.assertEqual((info["pid"], info["port"]), (os.getpid(), self.port))
        self.assertEqual(info["version"], core.plugin_version())

    def test_health(self):
        code, ctype, body = get(self.port, "/api/health")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual((data["ok"], data["pid"], data["port"]), (True, os.getpid(), self.port))

    def test_data_shape(self):
        _, _, body = get(self.port, "/api/data")
        data = json.loads(body)
        self.assertEqual(set(data) >= {"generated_at", "focus_root_session_id", "project_roots", "roots", "version"}, True)
        self.assertTrue(data["live"])
        self.assertEqual([r["root_session_id"] for r in data["roots"]], [ROOT])
        self.assertEqual(data["focus_root_session_id"], ROOT)

    def test_page_is_template_with_live_bootstrap(self):
        code, ctype, body = get(self.port, "/")
        text = body.decode("utf-8")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(report.PLACEHOLDER, text)
        self.assertIn("/api/version", text)
        self.assertIn("/api/data", text)

    def test_version_changes_after_root_rebuild(self):
        before = json.loads(get(self.port, "/api/version")[2])["version"]
        self.assertEqual(json.loads(get(self.port, "/api/version")[2])["version"], before)
        time.sleep(0.01)
        registry.save_session({"session_id": CHILD, "root_session_id": ROOT, "parent_session_id": ROOT,
                               "kind": "herdr", "depth": 1})
        aggregate.write_root(ROOT)
        after = json.loads(get(self.port, "/api/version")[2])["version"]
        self.assertNotEqual(before, after)

    def test_data_follows_refresh(self):
        registry.save_session({"session_id": CHILD, "root_session_id": ROOT, "parent_session_id": ROOT,
                               "kind": "herdr", "depth": 1, "started_at": core.now() - 30,
                               "last_activity_at": core.now()})
        self.httpd.app.refresh_once()
        data = json.loads(get(self.port, "/api/data")[2])
        root = data["roots"][0]
        self.assertEqual({s["session_id"] for s in root["sessions"]}, {ROOT, CHILD})

    def raw(self, path, headers):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("GET", path, skip_host=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def test_host_header_allowlist(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}"):
            self.assertEqual(self.raw("/api/data", {"Host": host})[0], 200, host)
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}",
                     f"[::1]:{self.port}"):
            for path in ("/", "/api/data", "/api/version", "/api/health", "/nope"):
                self.assertEqual(self.raw(path, {"Host": host})[0], 403, (host, path))
        self.assertEqual(self.raw("/api/data", {})[0], 403)

    def test_no_cors_and_cross_origin_rejected(self):
        host = f"127.0.0.1:{self.port}"
        code, headers, _ = self.raw("/api/data", {"Host": host, "Origin": f"http://{host}"})
        self.assertEqual(code, 200)
        self.assertFalse(any(k.lower().startswith("access-control-") for k in headers), headers)
        code, headers, _ = self.raw("/api/data", {"Host": host, "Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        self.assertFalse(any(k.lower().startswith("access-control-") for k in headers), headers)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            get(self.port, "/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_second_serve_reuses_running_server(self):
        out = io.StringIO()
        with mock.patch.object(server, "spawn") as spawned, contextlib.redirect_stdout(out):
            code = server.cmd_serve(serve_args(session=ROOT))
        self.assertEqual(code, 0)
        spawned.assert_not_called()
        lines = out.getvalue().strip().splitlines()
        self.assertIn("running", lines[0])
        self.assertEqual(lines[-1], f"http://127.0.0.1:{self.port}/#{ROOT}")

    def test_version_mismatch_restarts(self):
        fresh = {"pid": 1, "port": 1}
        with mock.patch.object(core, "plugin_version", return_value="9.9.9"), \
                mock.patch.object(server, "stop_server") as stopped, \
                mock.patch.object(server, "spawn", return_value=fresh) as spawned, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            server.cmd_serve(serve_args())
        stopped.assert_called_once()
        spawned.assert_called_once()
        self.assertIn("restarted", out.getvalue())

    def test_status(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = server.cmd_serve(serve_args(status=True))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip().splitlines()[-1], f"http://127.0.0.1:{self.port}/")


class IdleTest(unittest.TestCase):
    def test_idle_tracker_with_fake_clock(self):
        now = [1000.0]
        idle = server.IdleTracker(timeout=7200, clock=lambda: now[0])
        now[0] += 7199
        self.assertFalse(idle.expired())
        idle.touch()
        now[0] += 7199
        self.assertFalse(idle.expired())
        now[0] += 1
        self.assertTrue(idle.expired())

    def test_default_timeout_is_two_hours(self):
        self.assertEqual(server.IDLE_TIMEOUT_S, 2 * 3600)


class IdleShutdownTest(ServerBase):
    def test_server_stops_itself_when_idle(self):
        info = {}
        t = threading.Thread(target=lambda: info.update(server.run_foreground(port=0, idle_timeout=0.2,
                                                                              refresh_every=3600)), daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertFalse(server.state_file().exists())


class DaemonTest(ServerBase):
    def test_start_reuse_stop(self):
        self.seed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(server.cmd_serve(serve_args()), 0)
        self.addCleanup(server.stop_server)
        info = server.running()
        self.assertIsNotNone(info)
        self.assertNotEqual(info["pid"], os.getpid())
        lines = out.getvalue().strip().splitlines()
        self.assertIn("started", lines[0])
        self.assertEqual(lines[-1], f"http://127.0.0.1:{info['port']}/")
        with contextlib.redirect_stdout(io.StringIO()) as again:
            server.cmd_serve(serve_args())
        self.assertIn("running", again.getvalue())
        self.assertEqual(server.running()["pid"], info["pid"])
        with contextlib.redirect_stdout(io.StringIO()) as stopped:
            server.cmd_serve(serve_args(stop=True))
        self.assertIn("stopped", stopped.getvalue())
        self.assertIsNone(server.running())
        self.assertFalse(server._pid_alive(info["pid"]) and server.health(info["port"], 0.3))
        self.assertFalse(server.state_file().exists())


if __name__ == "__main__":
    unittest.main()

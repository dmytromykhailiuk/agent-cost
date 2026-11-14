import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import core, hooks, registry, statusline  # noqa: E402

BIN = ROOT / "skills" / "agent-cost" / "bin" / "agent-cost"
MAIN = "bbbbbbbb-0000-4000-8000-000000000001"
CHILD = "bbbbbbbb-0000-4000-8000-000000000002"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text):
    return ANSI.sub("", text)


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.base_env = {"AGENT_COST_STATE_DIR": str(self.state),
                         "HERDR_AGENTS_STATE_DIR": str(Path(self.tmp.name) / "none"),
                         "PATH": os.environ.get("PATH", "")}
        self.child_env = dict(self.base_env, HERDR_AGENT_ID="explore-1", HERDR_AGENT_DEPTH="1",
                              HERDR_PARENT_SESSION_ID=MAIN, HERDR_ROOT_SESSION_ID=MAIN)

    def run_sl(self, payload, env, as_json=True):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        with mock.patch.dict(os.environ, env, clear=True):
            return statusline.run(text, as_json)

    def payload(self, sid, cost=1.0, duration_ms=60000):
        return {"session_id": sid, "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
                "effort": {"level": "medium"}, "cost": {"total_cost_usd": cost, "total_duration_ms": duration_ms}}

    def record(self, sid, root, kind, depth, started, estimate, **extra):
        rec = {"session_id": sid, "root_session_id": root, "parent_session_id": None if sid == root else root,
               "depth": depth, "kind": kind, "started_at": started, "last_activity_at": started + 600,
               "estimate": {"estimated_usd": estimate, "builtin_count": extra.pop("builtins", 0)}}
        rec.update(extra)
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            registry.save_session(rec)

    def test_main_without_record_uses_stdin_cost(self):
        out = json.loads(self.run_sl(self.payload(MAIN, 0.63, 521635), self.base_env))
        self.assertEqual(out["label"], "")
        text = plain(out["line"])
        self.assertTrue(text.startswith("session "), text)
        self.assertIn("Σ $0.63", text)
        self.assertNotIn("agents", text)
        self.assertIn("8m", text)

    def test_main_with_agents(self):
        t0 = time.time() - 4320
        self.record(MAIN, MAIN, "main", 0, t0, 1.0, builtins=2)
        self.record(CHILD, MAIN, "herdr", 1, t0 + 60, 3.1, role="explore", herdr_id="explore-1")
        text = plain(json.loads(self.run_sl(self.payload(MAIN, 1.2), self.base_env))["line"])
        self.assertIn("Σ $4.30", text)
        self.assertIn("agents $3.10 (3)", text)
        self.assertIn("main $1.20", text)
        self.assertIn("1h12m", text)
        self.assertRegex(text, r"\d\d:\d\d")

    def test_child_label_and_self(self):
        t0 = time.time() - 600
        self.record(MAIN, MAIN, "main", 0, t0, 4.0)
        self.record(CHILD, MAIN, "herdr", 1, t0, 0.1, role="explore", herdr_id="explore-1")
        out = json.loads(self.run_sl(self.payload(CHILD, 0.4), self.child_env))
        self.assertEqual(plain(out["label"]), "[sub:explore d1]")
        text = plain(out["line"])
        self.assertIn("Σ $4.40", text)
        self.assertIn("self $0.40", text)
        self.assertNotIn("agents", text)

    def test_child_without_record_uses_env_root(self):
        out = json.loads(self.run_sl(self.payload(CHILD, 0.4), self.child_env))
        self.assertEqual(plain(out["label"]), "[sub:explore-1 d1]")
        self.assertTrue((self.state / "sessions" / MAIN / f"{CHILD}.live.json").is_file())

    def test_qa_fake_env_with_unrelated_meta_dir(self):
        agent_dir = Path(self.tmp.name) / "qa-agent"
        agent_dir.mkdir()
        (agent_dir / "meta.json").write_text(json.dumps({"id": "claude-ffff01", "role": "claude", "depth": 1,
                                                         "session_id": "dddddddd-0000-4000-8000-000000000001"}))
        env = dict(self.child_env, HERDR_AGENT_ID="explore-abc123", HERDR_AGENT_DIR=str(agent_dir))
        out = json.loads(self.run_sl(self.payload(CHILD, 0.4), env))
        self.assertEqual(plain(out["label"]), "[sub:explore d1]")
        self.assertIn("self $0.40", plain(out["line"]))

    def test_real_herdr_child_with_own_meta(self):
        agent_dir = Path(self.tmp.name) / "explore-1"
        agent_dir.mkdir()
        (agent_dir / "meta.json").write_text(json.dumps({"id": "explore-1", "role": "explore", "depth": 1,
                                                         "session_id": CHILD, "parent_session_id": MAIN}))
        out = json.loads(self.run_sl(self.payload(CHILD, 0.4), dict(self.child_env, HERDR_AGENT_DIR=str(agent_dir))))
        self.assertEqual(plain(out["label"]), "[sub:explore d1]")

    def test_attached_non_herdr_sessions_render_as_sub(self):
        t0 = time.time() - 600
        self.record(MAIN, MAIN, "main", 0, t0, 4.0)
        cases = [("bbbbbbbb-0000-4000-8000-000000000003", dict(kind="headless", role="reviewer",
                                                              purpose="verify:review"), "[sub:reviewer d1]"),
                 ("bbbbbbbb-0000-4000-8000-000000000004", dict(kind="headless", purpose="verify:triage"),
                  "[sub:verify:triage d1]"),
                 ("bbbbbbbb-0000-4000-8000-000000000005", dict(kind="main", role="main"), "[sub:agent d1]")]
        for sid, extra, label in cases:
            self.record(sid, MAIN, extra.pop("kind"), 1, t0, 0.2, **extra)
            out = json.loads(self.run_sl(self.payload(sid, 0.3), self.base_env))
            self.assertEqual(plain(out["label"]), label, sid)
            text = plain(out["line"])
            self.assertIn("self $0.30", text)
            self.assertNotIn("agents", text)
            self.assertIn("Σ $4.", text)

    def test_headless_vloop_env_without_record(self):
        self.record(MAIN, MAIN, "main", 0, time.time() - 600, 1.0)
        env = dict(self.base_env, VLOOP_PARENT_SESSION_ID=MAIN, VLOOP_GUARD_ROLE="qa")
        out = json.loads(self.run_sl(self.payload(CHILD, 0.2), env))
        self.assertEqual(plain(out["label"]), "[sub:qa d1]")
        self.assertIn("self $0.20", plain(out["line"]))

    def test_clear_in_herdr_tab_renders_as_root(self):
        cleared = "bbbbbbbb-0000-4000-8000-000000000007"
        env = dict(self.child_env, HERDR_AGENT_ID="child-agent", CLAUDE_CODE_SESSION_ID=cleared)
        with mock.patch.dict(os.environ, env, clear=True):
            hooks.run("session-start", json.dumps({"session_id": cleared, "source": "clear"}), env)
            self.assertEqual(registry.find_root(cleared), cleared)
        out = json.loads(self.run_sl(self.payload(cleared, 0.5), env))
        self.assertEqual(out["label"], "")
        text = plain(out["line"])
        self.assertIn("Σ $0.50", text)
        self.assertNotIn("self", text)
        self.assertTrue((self.state / "sessions" / cleared / f"{cleared}.live.json").is_file())

    def test_plain_output(self):
        out = self.run_sl(self.payload(CHILD, 0.4), self.child_env, as_json=False)
        self.assertTrue(plain(out).startswith("[sub:explore-1 d1] session "))

    def test_garbage_stdin_prints_nothing(self):
        for text in ("", "not json", "[]", "null", "{}", json.dumps({"session_id": "../x"})):
            self.assertEqual(self.run_sl(text, self.base_env), "", text)

    def test_cli_garbage_exit_zero(self):
        res = subprocess.run([str(BIN), "statusline", "--json"], input="garbage", capture_output=True, text=True,
                             env=self.base_env)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_live_written_only_on_change(self):
        self.run_sl(self.payload(MAIN, 1.0), self.base_env)
        path = self.state / "sessions" / MAIN / f"{MAIN}.live.json"
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            live = registry.load_live(MAIN, MAIN)
        self.assertEqual(live["reported_usd"], 1.0)
        self.assertEqual(live["model"], "claude-opus-5-5")
        self.assertEqual(live["effort"], "medium")
        first = path.stat().st_mtime_ns
        self.run_sl(self.payload(MAIN, 1.0), self.base_env)
        self.assertEqual(path.stat().st_mtime_ns, first)
        self.run_sl(self.payload(MAIN, 1.5), self.base_env)
        self.assertGreater(path.stat().st_mtime_ns, first)

    def test_live_cost_counts_before_record_estimate(self):
        t0 = time.time() - 60
        self.record(MAIN, MAIN, "main", 0, t0, 0.5)
        text = plain(json.loads(self.run_sl(self.payload(MAIN, 2.0), self.base_env))["line"])
        self.assertIn("Σ $2.00", text)

    def test_foreign_env_ignored_and_no_live_written(self):
        other = "bbbbbbbb-0000-4000-8000-000000000009"
        env = dict(self.child_env, CLAUDE_CODE_SESSION_ID=other)
        out = json.loads(self.run_sl(self.payload(CHILD, 2.0), env))
        self.assertNotIn("sub:", plain(out["label"]))
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            self.assertIsNone(registry.find_root(CHILD))
        self.assertFalse((self.state / "sessions").exists() and any((self.state / "sessions").rglob("*.live.json")))

    def test_matching_env_still_writes_live(self):
        env = dict(self.child_env, CLAUDE_CODE_SESSION_ID=CHILD)
        self.run_sl(self.payload(CHILD, 2.0), env)
        self.assertTrue((self.state / "sessions" / MAIN / f"{CHILD}.live.json").is_file())

    def test_latency(self):
        self.record(MAIN, MAIN, "main", 0, time.time() - 60, 1.0)
        payload = json.dumps(self.payload(MAIN, 1.0))
        times = []
        for _ in range(5):
            start = time.perf_counter()
            res = subprocess.run([str(BIN), "statusline", "--json"], input=payload, capture_output=True,
                                 text=True, env=self.base_env)
            times.append(time.perf_counter() - start)
            self.assertEqual(res.returncode, 0)
        self.assertLess(min(times), 0.3, times)


if __name__ == "__main__":
    unittest.main()

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import aggregate, auxlink, core, registry, report, scan, server  # noqa: E402
from test_scan import MAIN, ScanBase, run_scan  # noqa: E402

REV = "dddddddd-0000-4000-8000-000000000001"
QA = "dddddddd-0000-4000-8000-000000000002"
RETRO = "dddddddd-0000-4000-8000-000000000003"
NESTED = "dddddddd-0000-4000-8000-000000000004"
WORKER = "dddddddd-0000-4000-8000-000000000005"
STRAY = "dddddddd-0000-4000-8000-000000000006"
R1 = "20261114-100000-demo-aaaa"
R2 = "20261114-110000-demo-bbbb"
R3 = "20261114-120000-demo-cccc"


def iso(ts):
    return core.iso(ts).replace("Z", ".000Z")


class VloopLinkTest(ScanBase):
    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        self.vloop = base / "vloop"
        (self.vloop / "runs").mkdir(parents=True)
        (self.vloop / "sessions").mkdir()
        patcher = mock.patch.dict(os.environ, {"AGENT_COST_VLOOP_DIRS": str(self.vloop),
                                               "AGENT_COST_TMP_DIRS": str(base / "tmpdirs"),
                                               "HERDR_AGENTS_STATE_DIR": str(base / "herdr")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.config / "projects" / "-tmp-scan"
        self.now = core.now()

    def transcript(self, sid, prompt, cwd="/tmp/scan", entrypoint="cli", agent_name=None):
        t = iso(self.now - 120)
        rows = []
        if agent_name:
            rows.append({"type": "agent-name", "agentName": agent_name, "sessionId": sid})
        rows += [
            {"type": "user", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint, "timestamp": t,
             "message": {"role": "user", "content": prompt}},
            {"type": "assistant", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint, "timestamp": iso(self.now - 60),
             "message": {"id": sid[-4:], "model": "claude-sonnet-5-5", "role": "assistant",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 100, "output_tokens": 100}}},
        ]
        (self.project / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def run_dir(self, run_id, attempts=(), retro=None):
        d = self.vloop / "runs" / run_id
        d.mkdir(parents=True)
        ctx = {"schema": 1, "run_id": run_id, "session_id": MAIN, "platform": "claude", "attempts": list(attempts)}
        if retro:
            ctx["retro_agent"] = retro
        (d / "context.json").write_text(json.dumps(ctx))
        return d

    def by_id(self):
        return {s["session_id"]: s for s in self.root(MAIN)["sessions"]}

    def test_reviewer_linked_from_run_context(self):
        self.run_dir(R1, [{"n": 1, "agents": {"reviewer": {"transport": "herdr", "session_id": REV}}}])
        self.transcript(REV, "check the diff", cwd=str(Path.home() / ".claude" / "skills" / "x"))
        run_scan(self.config)
        s = self.by_id()[REV]
        self.assertEqual((s["parent_session_id"], s["kind"], s["role"], s["purpose"]),
                         (MAIN, "herdr", "reviewer", "verify:review"))
        self.assertFalse(aggregate.root_path(REV).exists())

    def test_qa_linked_from_prompt_and_ledger(self):
        (self.vloop / "ledger.jsonl").write_text(json.dumps({"run_id": R2, "session_id": MAIN, "ts": iso(self.now)}) + "\n")
        self.transcript(QA, f"Your task file is /x/state/claude-verify/runs/{R2}/attempt-2/qa.prompt.md. Read it.")
        run_scan(self.config)
        s = self.by_id()[QA]
        self.assertEqual((s["kind"], s["role"], s["purpose"]), ("herdr", "qa", "verify:qa"))

    def test_retro_linked_from_events(self):
        ev = {"ts": iso(self.now), "sid": MAIN, "event": "retro_done", "run": R3,
              "data": {"session_id": RETRO, "transport": "headless"}}
        (self.vloop / "sessions" / f"{MAIN}.events.jsonl").write_text(json.dumps(ev) + "\n")
        self.transcript(RETRO, "summarize the run", entrypoint="sdk-cli")
        run_scan(self.config)
        s = self.by_id()[RETRO]
        self.assertEqual((s["kind"], s["purpose"]), ("headless", "verify:retro"))

    def test_vloop_session_parent_links_any_session(self):
        (self.vloop / "sessions" / f"{NESTED}.json").write_text(json.dumps(
            {"session_id": NESTED, "parent_session_id": MAIN, "depth": 1}))
        self.transcript(NESTED, "Operation: remember Repo root: /tmp/scan Follow ~/.claude/skills/vault/ops/remember.md")
        run_scan(self.config)
        s = self.by_id()[NESTED]
        self.assertEqual((s["kind"], s["purpose"]), ("herdr", "vault"))

    def test_triage_cost_becomes_linked_session(self):
        d = self.run_dir(R1, [{"n": 1, "begin_at": iso(self.now - 300), "agents": {}}])
        (d / "attempt-1").mkdir()
        (d / "attempt-1" / "triage.json").write_text(json.dumps(
            {"source": "triage", "agent": {"cost_usd": 0.02, "model": "claude-haiku-5-5", "duration_s": 12}}))
        d2 = self.run_dir(R2, [{"n": 1, "begin_at": iso(self.now - 200), "agents": {}}])
        (d2 / "attempt-1").mkdir()
        (d2 / "attempt-1" / "triage.json").write_text(json.dumps(
            {"source": "triage", "agent": {"cost_usd": 0.05, "model": "gpt-5.4-mini", "duration_s": 9}}))
        _, out = run_scan(self.config)
        self.assertIn("verify:triage 1", out)
        sessions = self.by_id()
        s = sessions[auxlink.TRIAGE_PREFIX + R1]
        self.assertNotIn(auxlink.TRIAGE_PREFIX + R2, sessions)
        self.assertEqual((s["kind"], s["purpose"], s["parent_session_id"]), ("headless", "verify:triage", MAIN))
        self.assertEqual((s["cost"]["effective_usd"], s["cost"]["reported_source"]), (0.02, "vloop-triage"))
        self.assertEqual(s["duration_s"], 12)
        before = self.root(MAIN)["totals"]["cost_usd"]
        run_scan(self.config)
        self.assertEqual(self.root(MAIN)["totals"]["cost_usd"], before)

    def test_run_owner_found_in_main_transcript_when_ledger_missing(self):
        run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.now - 90)) + "-demo-dddd"
        main_tp = self.project / f"{MAIN}.jsonl"
        main_tp.write_text(main_tp.read_text() + json.dumps(
            {"type": "user", "sessionId": MAIN, "timestamp": iso(self.now - 30),
             "message": {"role": "user", "content": [{"type": "tool_result", "content": f"run {run_id} begun"}]}}) + "\n")
        self.transcript(REV, f"Your task file is /s/runs/{run_id}/attempt-1/reviewer.prompt.md. Read it.")
        run_scan(self.config)
        self.assertEqual(self.by_id()[REV]["purpose"], "verify:review")
        self.assertEqual(core.read_json(core.cache_dir() / "vloop-runs.json"), {run_id: MAIN})

    def test_scratchpad_reference_links_named_agents_only(self):
        prompt = f"Read first: /private/tmp/claude-501/-tmp-scan/{MAIN}/scratchpad/plan.md then implement"
        self.transcript(WORKER, prompt, agent_name="impl-core-1a2b3c")
        self.transcript(STRAY, prompt)
        run_scan(self.config)
        s = self.by_id()[WORKER]
        self.assertEqual((s["kind"], s["herdr_id"], s["purpose"]), ("herdr", "impl-core-1a2b3c", "task"))
        self.assertEqual(registry.find_root(STRAY), STRAY)


class PurposeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_classify(self):
        home = Path.home()
        cases = [
            ({"kind": "main"}, None),
            ({"kind": "herdr", "herdr_id": "verify-review-342faf"}, "verify:review"),
            ({"kind": "herdr", "herdr_id": "verify-qa-1"}, "verify:qa"),
            ({"kind": "herdr", "herdr_id": "vault-retro-9"}, "verify:retro"),
            ({"kind": "herdr", "role": "vault-remember"}, "vault"),
            ({"kind": "headless", "task": {"prompt_excerpt": "Your task file is /s/runs/20261101-101010-r-1/attempt-1/reviewer.prompt.md"}},
             "verify:review"),
            ({"kind": "herdr", "role": "explore", "cwd": str(home / ".claude" / "skills")}, "harness"),
            ({"kind": "herdr", "role": "explore", "cwd": "/tmp/x"}, "task"),
            ({"kind": "herdr", "purpose": "verify:qa", "role": "explore"}, "verify:qa"),
        ]
        for rec, want in cases:
            self.assertEqual(auxlink.classify(rec), want, rec)

    def test_prompt_run(self):
        self.assertEqual(auxlink.prompt_run("x /state/runs/20261101-101010-a-b-1f2e/retro/task.md"),
                         ("20261101-101010-a-b-1f2e", "retro"))
        self.assertIsNone(auxlink.prompt_run("no run here"))

    def test_identity_from_vloop_env(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGENT_COST_STATE_DIR": tmp}):
            ident = registry.identity({"session_id": QA}, {"VLOOP_PARENT_SESSION_ID": MAIN, "VLOOP_GUARD_ROLE": "qa"})
        self.assertEqual((ident["kind"], ident["parent_session_id"], ident["root_session_id"], ident["purpose"]),
                         ("headless", MAIN, MAIN, "verify:qa"))


class VisibilityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "home" / "Desktop" / "proj").mkdir(parents=True)
        env = {"AGENT_COST_STATE_DIR": str(self.base / "state"), "AGENT_COST_CONFIG": str(self.base / "config.json"),
               "HOME": str(self.base / "home"), "CLAUDE_CONFIG_DIR": str(self.base / "claude")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.inside = str(self.base / "home" / "Desktop" / "proj")
        self.outside = str(self.base / "home" / ".claude")

    def seed(self, sid, cwd, ago=60, extra=None):
        now = core.now()
        rec = {"session_id": sid, "root_session_id": sid, "kind": "main", "depth": 0, "cwd": cwd,
               "started_at": now - ago - 300, "last_activity_at": now - ago, "ended_at": now - ago}
        rec.update(extra or {})
        registry.save_session(rec)
        aggregate.write_root(sid)

    def config(self, **cfg):
        Path(os.environ["AGENT_COST_CONFIG"]).write_text(json.dumps(cfg))


class VisibilityTest(VisibilityBase):
    def test_default_is_desktop(self):
        self.assertEqual(report.visible_roots(), [str(self.base / "home" / "Desktop")])
        self.assertFalse(report.is_hidden(self.inside, report.visible_roots()))
        self.assertTrue(report.is_hidden(self.outside, report.visible_roots()))
        self.assertTrue(report.is_hidden(str(self.base / "home" / "Desktopx"), report.visible_roots()))

    def test_config_overrides_and_empty_list_shows_all(self):
        self.config(visible_roots=["~/.claude"])
        self.assertFalse(report.is_hidden(self.outside, report.visible_roots()))
        self.assertTrue(report.is_hidden(self.inside, report.visible_roots()))
        self.config(visible_roots=[])
        self.assertFalse(report.is_hidden("/anywhere", report.visible_roots()))

    def test_list_hides_outside_roots(self):
        self.seed(REV, self.inside)
        self.seed(QA, self.outside)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.cmd_list(argparse.Namespace(limit=20, json=True, all=False))
        self.assertEqual([r["root_session_id"] for r in json.loads(out.getvalue())], [REV])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.cmd_list(argparse.Namespace(limit=20, json=False, all=False))
        self.assertIn("1 hidden roots", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.cmd_list(argparse.Namespace(limit=20, json=True, all=True))
        self.assertEqual({r["root_session_id"] for r in json.loads(out.getvalue())}, {REV, QA})

    def test_payload_flags_hidden_roots(self):
        self.seed(REV, self.inside)
        self.seed(QA, self.outside, ago=10)
        payload = report.collect()
        self.assertEqual(payload["focus_root_session_id"], REV)
        flags = {r["root_session_id"]: r["hidden"] for r in payload["roots"]}
        self.assertEqual(flags, {REV: False, QA: True})


class IncrementalTest(VisibilityBase):
    def setUp(self):
        super().setUp()
        memo = mock.patch.object(server, "VERSION_MEMO_S", 0)
        memo.start()
        self.addCleanup(memo.stop)
        self.seed(REV, self.inside)
        self.seed(QA, self.inside, ago=120)
        self.app = server.App()

    def data(self, since=None):
        return json.loads(self.app.payload_bytes(since))

    @staticmethod
    def merge(known, data):
        known = {} if not data["incremental"] else dict(known)
        for r in data["roots"]:
            known[r["root_session_id"]] = r
        for sid in data.get("removed") or []:
            known.pop(sid, None)
        return known

    def test_since_returns_only_changed_and_removed(self):
        full = self.data()
        self.assertFalse(full["incremental"])
        known = self.merge({}, full)
        self.assertEqual(set(known), {REV, QA})
        same = self.data(full["version"])
        self.assertEqual((same["incremental"], same["roots"], same["removed"]), (True, [], []))

        time.sleep(0.01)
        registry.save_session({"session_id": NESTED, "root_session_id": REV, "parent_session_id": REV,
                               "kind": "herdr", "depth": 1, "cwd": self.inside})
        aggregate.write_root(REV)
        aggregate.root_path(QA).unlink()
        delta = self.data(full["version"])
        self.assertTrue(delta["incremental"])
        self.assertEqual([r["root_session_id"] for r in delta["roots"]], [REV])
        self.assertEqual(delta["removed"], [QA])
        known = self.merge(known, delta)
        self.assertEqual(known, self.merge({}, self.data()))
        self.assertEqual(json.loads(self.app.payload_bytes())["version"], delta["version"])

    def test_unknown_token_or_config_change_gives_full(self):
        full = self.data()
        self.assertFalse(self.data("nope")["incremental"])
        self.config(visible_roots=["~/.claude"])
        again = self.data(full["version"])
        self.assertFalse(again["incremental"])
        self.assertTrue(all(r["hidden"] for r in again["roots"]))

    def test_unchanged_rebuild_keeps_version(self):
        v1 = self.app.token()
        aggregate.write_root(QA)
        self.assertEqual(self.app.token(), v1)


if __name__ == "__main__":
    unittest.main()

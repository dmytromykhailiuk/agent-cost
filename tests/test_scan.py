import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import aggregate, core, registry, scan  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "scan"
MAIN = "bbbbbbbb-0000-4000-8000-000000000001"
CHILD = "bbbbbbbb-0000-4000-8000-000000000002"
GRAND = "bbbbbbbb-0000-4000-8000-000000000003"
LONER = "bbbbbbbb-0000-4000-8000-000000000004"


def run_scan(config, since="30d"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = scan.cmd_scan(argparse.Namespace(since=since, config_dir=[str(config)]))
    return code, out.getvalue()


class ScanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("AGENT_COST_STATE_DIR")
        self.addCleanup(lambda: os.environ.pop("AGENT_COST_STATE_DIR", None) if saved is None
                        else os.environ.__setitem__("AGENT_COST_STATE_DIR", saved))
        os.environ["AGENT_COST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.config = Path(self.tmp.name) / "config"
        shutil.copytree(FIX, self.config)

    def root(self, sid):
        return core.read_json(aggregate.root_path(sid))


class ScanTest(ScanBase):
    def test_links_parent_child_grandchild(self):
        code, out = run_scan(self.config)
        self.assertEqual(code, 0)
        data = self.root(MAIN)
        by_id = {s["session_id"]: s for s in data["sessions"]}
        self.assertEqual(set(by_id), {MAIN, CHILD, GRAND})
        self.assertEqual(by_id[CHILD]["parent_session_id"], MAIN)
        self.assertEqual(by_id[CHILD]["depth"], 1)
        self.assertEqual(by_id[CHILD]["herdr_id"], "explore-186781")
        self.assertEqual(by_id[CHILD]["role"], "explore")
        self.assertEqual(by_id[CHILD]["task"]["description"], "explore")
        self.assertEqual(by_id[CHILD]["task"]["prompt_excerpt"], "explore the repo")
        self.assertEqual(by_id[CHILD]["result_excerpt"], "explored")
        self.assertEqual(by_id[GRAND]["parent_session_id"], CHILD)
        self.assertEqual(by_id[GRAND]["depth"], 2)
        self.assertEqual(by_id[GRAND]["role"], "plan")
        self.assertEqual(by_id[MAIN]["kind"], "main")
        self.assertEqual(by_id[MAIN]["state"], "ended")
        self.assertEqual(by_id[MAIN]["started_at"], "2026-01-01T10:00:00Z")
        self.assertEqual(by_id[GRAND]["last_activity_at"], "2026-01-01T10:03:20Z")
        self.assertIn("2 children linked", out)
        self.assertIn("2 roots", out)

    def test_unlinked_is_own_root(self):
        run_scan(self.config)
        data = self.root(LONER)
        self.assertEqual([s["session_id"] for s in data["sessions"]], [LONER])
        self.assertEqual(data["sessions"][0]["kind"], "main")
        self.assertEqual(data["sessions"][0]["state"], "idle")

    def test_links_by_agent_name_when_session_missing(self):
        path = self.config / "projects" / "-tmp-scan" / f"{MAIN}.jsonl"
        path.write_text(path.read_text().replace(f" session={CHILD}", ""))
        run_scan(self.config)
        by_id = {s["session_id"]: s for s in self.root(MAIN)["sessions"]}
        self.assertEqual(by_id[CHILD]["parent_session_id"], MAIN)

    def test_hook_data_wins(self):
        registry.save_session({"session_id": CHILD, "root_session_id": MAIN, "parent_session_id": MAIN,
                               "depth": 1, "kind": "herdr", "herdr_id": "explore-186781", "role": "researcher",
                               "model": "hook-model", "started_at": 1767261000.0,
                               "task": {"description": "from hook", "prompt_excerpt": None}})
        run_scan(self.config)
        rec = registry.load_session(MAIN, CHILD)
        self.assertEqual(rec["role"], "researcher")
        self.assertEqual(rec["model"], "hook-model")
        self.assertEqual(rec["started_at"], 1767261000.0)
        self.assertEqual(rec["task"]["description"], "from hook")
        self.assertEqual(rec["task"]["prompt_excerpt"], "explore the repo")
        self.assertIn("estimate", rec)

    def test_idempotent(self):
        run_scan(self.config)
        first = {sid: self.root(sid) for sid in (MAIN, LONER)}
        run_scan(self.config)
        for sid, before in first.items():
            after = self.root(sid)
            before.pop("generated_at")
            after.pop("generated_at")
            self.assertEqual(before, after)

    def test_since_filters_by_mtime(self):
        old = time.time() - 10 * 86400
        for p in (self.config / "projects" / "-tmp-scan").glob("*.jsonl"):
            if p.stem == LONER:
                os.utime(p, (old, old))
        code, out = run_scan(self.config, "2d")
        self.assertEqual(code, 0)
        self.assertFalse(aggregate.root_path(LONER).exists())
        self.assertTrue(aggregate.root_path(MAIN).exists())
        run_scan(self.config, "30d")
        self.assertTrue(aggregate.root_path(LONER).exists())

    def test_invalid_since(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = run_scan(self.config, "soon")
        self.assertEqual(code, 2)

    def test_role_of(self):
        self.assertEqual(scan.role_of("explore-186781"), "explore")
        self.assertEqual(scan.role_of("verify-review-d3cfb9"), "verify-review")
        self.assertEqual(scan.role_of("plan"), "plan")


class LinkSourcesTest(ScanBase):
    def setUp(self):
        super().setUp()
        self.tmpdirs = Path(self.tmp.name) / "tmpdirs"
        self.tmpdirs.mkdir()
        self.herdr_state = Path(self.tmp.name) / "herdr"
        patcher = mock.patch.dict(os.environ, {"AGENT_COST_TMP_DIRS": str(self.tmpdirs),
                                               "HERDR_AGENTS_STATE_DIR": str(self.herdr_state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.config / "projects" / "-tmp-scan"

    def strip_link(self):
        path = self.project / f"{MAIN}.jsonl"
        lines = [l for l in path.read_text().splitlines() if "id=explore-186781" not in l]
        path.write_text("\n".join(lines) + "\n")

    def write_transcript(self, sid, cwd, entrypoint):
        rows = [
            {"type": "user", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint, "timestamp": "2026-01-01T10:00:30Z",
             "message": {"role": "user", "content": "triage this"}},
            {"type": "assistant", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint, "timestamp": "2026-01-01T10:00:40Z",
             "message": {"id": sid[-4:], "model": "claude-haiku-5-5", "role": "assistant",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 10, "output_tokens": 10}}},
        ]
        (self.project / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_relinks_session_saved_as_root(self):
        self.strip_link()
        run_scan(self.config)
        self.assertTrue(aggregate.root_path(CHILD).exists())
        self.assertEqual(registry.find_root(GRAND), CHILD)
        shutil.copy(FIX / "projects" / "-tmp-scan" / f"{MAIN}.jsonl", self.project / f"{MAIN}.jsonl")
        code, out = run_scan(self.config)
        self.assertIn("1 relinked", out)
        self.assertFalse(aggregate.root_path(CHILD).exists())
        by_id = {s["session_id"]: s for s in self.root(MAIN)["sessions"]}
        self.assertEqual(set(by_id), {MAIN, CHILD, GRAND})
        self.assertEqual((by_id[CHILD]["depth"], by_id[CHILD]["parent_session_id"]), (1, MAIN))
        self.assertEqual((by_id[GRAND]["depth"], by_id[GRAND]["parent_session_id"]), (2, CHILD))
        self.assertEqual(registry.find_root(GRAND), MAIN)
        self.assertFalse((core.state_dir() / "sessions" / CHILD).exists())

    def test_repairs_half_linked_root(self):
        registry.save_session({"session_id": LONER, "root_session_id": LONER, "parent_session_id": MAIN,
                               "kind": "main", "depth": 0, "herdr_id": "claude-8affbc"})
        run_scan(self.config)
        rec = registry.load_session(MAIN, LONER)
        self.assertEqual((rec["kind"], rec["depth"], rec["root_session_id"]), ("herdr", 1, MAIN))
        self.assertFalse(aggregate.root_path(LONER).exists())

    def test_links_from_background_task_output(self):
        self.strip_link()
        tasks = self.tmpdirs / f"claude-{os.getuid()}" / "-tmp-scan" / MAIN / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "b1.output").write_text("[herdr-agent] spawned explore-186781 in tab wA:t1; waiting\nreport\n"
                                         f"[herdr-agent] id=explore-186781 status=done tab=wA:t1 session={CHILD}\n")
        run_scan(self.config)
        rec = registry.load_session(MAIN, CHILD)
        self.assertEqual((rec["parent_session_id"], rec["herdr_id"], rec["kind"]), (MAIN, "explore-186781", "herdr"))

    def test_launch_record_links_and_persists(self):
        launches = self.herdr_state / "launches"
        launches.mkdir(parents=True)
        (launches / f"{LONER}.json").write_text(json.dumps({"name": "claude-8affbc", "env": {
            "HERDR_AGENT_ID": "claude-8affbc", "HERDR_PARENT_SESSION_ID": MAIN,
            "HERDR_ROOT_SESSION_ID": MAIN, "HERDR_AGENT_DEPTH": "1"}}))
        run_scan(self.config)
        rec = registry.load_session(MAIN, LONER)
        self.assertEqual((rec["parent_session_id"], rec["herdr_id"], rec["depth"]), (MAIN, "claude-8affbc", 1))
        (launches / f"{LONER}.json").unlink()
        run_scan(self.config)
        self.assertEqual(registry.find_root(LONER), MAIN)
        self.assertFalse(aggregate.root_path(LONER).exists())

    def test_headless_linked_to_scratchpad_owner(self):
        sid = "bbbbbbbb-0000-4000-8000-000000000005"
        self.write_transcript(sid, f"/private/tmp/claude-501/-tmp-scan/{MAIN}/scratchpad", "sdk-cli")
        run_scan(self.config)
        rec = registry.load_session(MAIN, sid)
        self.assertEqual((rec["kind"], rec["parent_session_id"], rec["role"], rec["headless"]),
                         ("headless", MAIN, "headless", True))
        self.assertFalse(aggregate.root_path(sid).exists())

    def test_unlinked_headless_root_is_flagged(self):
        sid = "bbbbbbbb-0000-4000-8000-000000000006"
        self.write_transcript(sid, "/tmp/elsewhere", "sdk-cli")
        run_scan(self.config)
        rows = {r["root_session_id"]: r for r in aggregate.list_roots()}
        self.assertTrue(rows[sid]["headless"])
        self.assertFalse(rows[MAIN]["headless"])
        self.assertEqual(self.root(sid)["sessions"][0]["kind"], "main")

    def test_interactive_session_in_scratchpad_not_linked(self):
        sid = "bbbbbbbb-0000-4000-8000-000000000007"
        self.write_transcript(sid, f"/private/tmp/claude-501/-tmp-scan/{MAIN}/scratchpad", "cli")
        run_scan(self.config)
        self.assertEqual(registry.find_root(sid), sid)


if __name__ == "__main__":
    unittest.main()

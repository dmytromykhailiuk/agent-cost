import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import core, hooks, registry  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "registry"
PROJ = FIX / "projects" / "-tmp-reg"
MAIN = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD = "aaaaaaaa-0000-4000-8000-000000000002"
T0 = 1767261600.0


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("AGENT_COST_STATE_DIR")
        self.addCleanup(lambda: os.environ.pop("AGENT_COST_STATE_DIR", None) if saved is None
                        else os.environ.__setitem__("AGENT_COST_STATE_DIR", saved))
        self.state = Path(self.tmp.name) / "state"
        os.environ["AGENT_COST_STATE_DIR"] = str(self.state)
        self.herdr = Path(self.tmp.name) / "herdr"
        shutil.copytree(FIX / "herdr", self.herdr)
        self.base_env = {"HERDR_AGENTS_STATE_DIR": str(Path(self.tmp.name) / "none")}

    def hook(self, event, payload, env=None, at=T0):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        buf = io.StringIO()
        with mock.patch.object(core, "now", return_value=at), redirect_stdout(buf):
            code = hooks.run(event, text, env=env if env is not None else self.base_env)
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue(), "")


class GarbageTest(_Case):
    def test_garbage_stdin_never_raises(self):
        for event in hooks.EVENTS + ("bogus",):
            for text in ("", "not json", "[1, 2]", "null", '{"session_id": "../../etc"}', '{"session_id": 5}',
                         '{"session_id": "' + MAIN + '", "transcript_path": 7, "cwd": ["x"], "model": {"id": 3}}'):
                self.hook(event, text)

    def test_missing_transcript_is_fine(self):
        self.hook("stop", {"session_id": MAIN, "transcript_path": "/nonexistent/x.jsonl"})
        self.assertIsNotNone(registry.load_session(MAIN, MAIN))
        self.assertTrue((self.state / "roots" / f"{MAIN}.json").is_file())

    def test_cli_exit_zero_on_garbage(self):
        env = dict(os.environ, AGENT_COST_STATE_DIR=str(self.state))
        for event in ("session-start", "stop", "nope"):
            p = subprocess.run([sys.executable, str(REPO / "skills" / "agent-cost" / "bin" / "agent-cost"), "hook", event],
                               input="{garbage", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)


class EventsTest(_Case):
    def test_session_start_main(self):
        self.hook("session-start", {"session_id": MAIN, "source": "startup", "model": "claude-opus-5-5",
                                    "cwd": "/tmp/reg", "transcript_path": str(PROJ / f"{MAIN}.jsonl")},
                  env=dict(self.base_env, CLAUDE_EFFORT="high"))
        rec = registry.load_session(MAIN, MAIN)
        self.assertEqual((rec["kind"], rec["depth"], rec["model"], rec["effort"]), ("main", 0, "claude-opus-5-5", "high"))
        self.assertEqual(rec["hook_events"], {"session-start": T0})
        self.assertTrue((self.state / "cli.json").is_file())

    def test_first_prompt_only(self):
        self.hook("prompt", {"session_id": MAIN, "prompt": "first   task\n\nplease"})
        self.hook("prompt", {"session_id": MAIN, "prompt": "second"}, at=T0 + 5)
        rec = registry.load_session(MAIN, MAIN)
        self.assertEqual(rec["task"]["prompt_excerpt"], "first task please")
        self.assertEqual(rec["last_activity_at"], T0 + 5)

    def test_herdr_child_copies_meta_and_prompt(self):
        env = dict(self.base_env, HERDR_AGENT_DIR=str(self.herdr / "child-agent"), HERDR_AGENT_ID="child-agent",
                   HERDR_AGENT_DEPTH="1", HERDR_PARENT_SESSION_ID=MAIN, HERDR_ROOT_SESSION_ID=MAIN)
        self.hook("session-start", {"session_id": CHILD, "source": "startup"}, env=env)
        shutil.rmtree(self.herdr / "child-agent")
        self.hook("prompt", {"session_id": CHILD, "prompt": "something else"}, env=env)
        rec = registry.load_session(MAIN, CHILD)
        self.assertEqual(rec["role"], "explore")
        self.assertEqual((rec["model"], rec["effort"]), ("sonnet", "medium"))
        self.assertEqual(rec["task"], {"description": "explore",
                                       "prompt_excerpt": "Explore the herdr lifecycle and report env vars."})
        self.assertEqual(rec["herdr"]["id"], "child-agent")

    def test_foreign_env_does_not_attach_to_other_tree(self):
        env = dict(self.base_env, CLAUDE_CODE_SESSION_ID=MAIN, HERDR_AGENT_ID="child-agent",
                   HERDR_AGENT_DEPTH="1", HERDR_PARENT_SESSION_ID=MAIN, HERDR_ROOT_SESSION_ID=MAIN)
        self.hook("session-start", {"session_id": CHILD, "source": "startup"}, env=env)
        self.assertEqual(registry.find_root(CHILD), CHILD)
        rec = registry.load_session(CHILD, CHILD)
        self.assertEqual((rec["kind"], rec["depth"]), ("main", 0))

    def test_clear_starts_new_root(self):
        env = dict(self.base_env, HERDR_AGENT_ID="child-agent", HERDR_AGENT_DEPTH="1",
                   HERDR_PARENT_SESSION_ID=MAIN, HERDR_ROOT_SESSION_ID=MAIN)
        new = "cccccccc-0000-4000-8000-000000000001"
        self.hook("session-start", {"session_id": new, "source": "clear"}, env=env)
        self.assertEqual(registry.find_root(new), new)
        self.assertEqual(registry.load_session(new, new)["depth"], 0)

    def test_resume_keeps_root_and_clears_end(self):
        env = dict(self.base_env, HERDR_AGENT_ID="child-agent", HERDR_AGENT_DEPTH="1",
                   HERDR_PARENT_SESSION_ID=MAIN, HERDR_ROOT_SESSION_ID=MAIN)
        self.hook("session-start", {"session_id": CHILD, "source": "startup"}, env=env)
        self.hook("session-end", {"session_id": CHILD, "reason": "other"}, env=env, at=T0 + 10)
        self.assertEqual(registry.load_session(MAIN, CHILD)["ended_at"], T0 + 10)
        self.hook("session-start", {"session_id": CHILD, "source": "resume"}, env=self.base_env, at=T0 + 100)
        rec = registry.load_session(MAIN, CHILD)
        self.assertNotIn("ended_at", rec)
        self.assertEqual(rec["root_session_id"], MAIN)
        self.assertEqual(rec["started_at"], T0)

    def test_stop_result_fallback_to_transcript(self):
        tp = str(PROJ / f"{CHILD}.jsonl")
        self.hook("stop", {"session_id": CHILD, "transcript_path": tp})
        rec = registry.load_session(CHILD, CHILD)
        self.assertEqual(rec["result_excerpt"], "Child result: lifecycle mapped.")
        self.assertEqual(rec["estimate"]["builtin_count"], 1)
        self.assertEqual(rec["cost_state_usd"], 5.0)
        self.hook("stop", {"session_id": CHILD, "transcript_path": tp, "last_assistant_message": "Explicit."})
        self.assertEqual(registry.load_session(CHILD, CHILD)["result_excerpt"], "Explicit.")


if __name__ == "__main__":
    unittest.main()

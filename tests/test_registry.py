import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import core, registry  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "registry"
MAIN = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD = "aaaaaaaa-0000-4000-8000-000000000002"
GRAND = "aaaaaaaa-0000-4000-8000-000000000003"


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("AGENT_COST_STATE_DIR")
        self.addCleanup(lambda: os.environ.pop("AGENT_COST_STATE_DIR", None) if saved is None
                        else os.environ.__setitem__("AGENT_COST_STATE_DIR", saved))
        os.environ["AGENT_COST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.herdr = Path(self.tmp.name) / "herdr"
        shutil.copytree(FIX / "herdr", self.herdr)
        self.empty_herdr_state = str(Path(self.tmp.name) / "no-herdr-state")

    def child_env(self, **extra):
        env = {"HERDR_AGENT_DIR": str(self.herdr / "child-agent"), "HERDR_AGENT_ID": "child-agent",
               "HERDR_AGENT_DEPTH": "1", "HERDR_PARENT_SESSION_ID": MAIN, "HERDR_ROOT_SESSION_ID": MAIN,
               "HERDR_AGENTS_STATE_DIR": self.empty_herdr_state}
        env.update(extra)
        return env


class IdentityTest(_Case):
    def test_main_without_herdr_env(self):
        ident = registry.identity({"session_id": MAIN}, {"HERDR_AGENTS_STATE_DIR": self.empty_herdr_state})
        self.assertEqual(ident, {"session_id": MAIN, "root_session_id": MAIN, "parent_session_id": None,
                                 "depth": 0, "kind": "main", "herdr_id": None, "role": "main"})

    def test_session_id_from_env(self):
        ident = registry.identity({}, {"CLAUDE_CODE_SESSION_ID": MAIN, "HERDR_AGENTS_STATE_DIR": self.empty_herdr_state})
        self.assertEqual(ident["session_id"], MAIN)

    def test_herdr_child_with_meta(self):
        ident = registry.identity({"session_id": CHILD}, self.child_env())
        self.assertEqual(ident["kind"], "herdr")
        self.assertEqual(ident["root_session_id"], MAIN)
        self.assertEqual(ident["parent_session_id"], MAIN)
        self.assertEqual(ident["depth"], 1)
        self.assertEqual(ident["herdr_id"], "child-agent")
        self.assertEqual(ident["role"], "explore")

    def test_herdr_child_meta_dir_gone(self):
        shutil.rmtree(self.herdr / "child-agent")
        ident = registry.identity({"session_id": CHILD}, self.child_env())
        self.assertEqual((ident["kind"], ident["depth"], ident["root_session_id"]), ("herdr", 1, MAIN))
        self.assertIsNone(ident["role"])

    def test_grandchild_depth_two(self):
        env = self.child_env(HERDR_AGENT_DIR=str(self.herdr / "grand-agent"), HERDR_AGENT_ID="grand-agent",
                             HERDR_AGENT_DEPTH="2", HERDR_PARENT_SESSION_ID=CHILD)
        ident = registry.identity({"session_id": GRAND}, env)
        self.assertEqual((ident["root_session_id"], ident["parent_session_id"], ident["depth"]), (MAIN, CHILD, 2))

    def test_launch_record_fallback(self):
        ident = registry.identity({"session_id": CHILD}, {"HERDR_AGENTS_STATE_DIR": str(FIX / "herdr-state")})
        self.assertEqual((ident["kind"], ident["parent_session_id"], ident["herdr_id"], ident["depth"]),
                         ("herdr", MAIN, "child-agent", 1))
        rec = registry.read_launch_record(CHILD, {"HERDR_AGENTS_STATE_DIR": str(FIX / "herdr-state")})
        self.assertEqual((rec["model"], rec["effort"]), ("sonnet", "medium"))

    def test_nested_process_inside_herdr_child(self):
        other = "bbbbbbbb-0000-4000-8000-000000000009"
        ident = registry.identity({"session_id": other}, self.child_env())
        self.assertEqual((ident["parent_session_id"], ident["root_session_id"], ident["depth"], ident["role"]),
                         (CHILD, MAIN, 2, "nested"))

    def test_nested_process_role_from_vloop(self):
        other = "bbbbbbbb-0000-4000-8000-000000000009"
        ident = registry.identity({"session_id": other}, self.child_env(VLOOP_GUARD_ROLE="reviewer"))
        self.assertEqual((ident["parent_session_id"], ident["depth"], ident["role"], ident["purpose"]),
                         (CHILD, 2, "reviewer", "verify:review"))

    def test_explicit_env_wins_over_unrelated_meta_dir(self):
        other = "bbbbbbbb-0000-4000-8000-000000000009"
        env = self.child_env(HERDR_AGENT_ID="explore-abc123")
        ident = registry.identity({"session_id": other}, env)
        self.assertEqual((ident["kind"], ident["parent_session_id"], ident["root_session_id"], ident["depth"],
                          ident["herdr_id"], ident["role"]),
                         ("herdr", MAIN, MAIN, 1, "explore-abc123", "explore"))

    def test_role_from_herdr_id_prefix_without_meta(self):
        env = self.child_env(HERDR_AGENT_ID="verify-qa-1a2b3c")
        del env["HERDR_AGENT_DIR"]
        ident = registry.identity({"session_id": CHILD}, env)
        self.assertEqual((ident["role"], ident["depth"], ident["herdr_id"]), ("verify-qa", 1, "verify-qa-1a2b3c"))

    def test_invalid_sid(self):
        ident = registry.identity({"session_id": "../etc/passwd"}, {})
        self.assertIsNone(ident["session_id"])


class StorageTest(_Case):
    def test_merge_keeps_fields_from_other_events(self):
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN, "model": "opus", "started_at": 100.0,
                               "last_activity_at": 100.0, "hook_events": {"session-start": 100.0},
                               "task": {"description": "d", "prompt_excerpt": "p"}})
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN, "model": None, "started_at": 50.0,
                               "last_activity_at": 90.0, "hook_events": {"stop": 200.0},
                               "task": {"description": None}, "result_excerpt": "r"})
        rec = registry.load_session(MAIN, MAIN)
        self.assertEqual(rec["model"], "opus")
        self.assertEqual(rec["started_at"], 50.0)
        self.assertEqual(rec["last_activity_at"], 100.0)
        self.assertEqual(rec["hook_events"], {"session-start": 100.0, "stop": 200.0})
        self.assertEqual(rec["task"], {"description": "d", "prompt_excerpt": "p"})
        self.assertEqual(rec["result_excerpt"], "r")
        self.assertEqual(rec["schema"], 1)

    def test_unset(self):
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN, "ended_at": 5.0})
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN, "_unset": ["ended_at"]})
        rec = registry.load_session(MAIN, MAIN)
        self.assertNotIn("ended_at", rec)
        self.assertNotIn("_unset", rec)

    def test_find_root_index_and_glob(self):
        self.assertIsNone(registry.find_root(CHILD))
        registry.save_session({"session_id": CHILD, "root_session_id": MAIN})
        self.assertEqual(registry.find_root(CHILD), MAIN)
        shutil.rmtree(core.state_dir() / "index")
        self.assertEqual(registry.find_root(CHILD), MAIN)
        self.assertTrue((core.state_dir() / "index" / CHILD).is_file())

    def test_session_ids_skip_live_files(self):
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN})
        core.write_json_atomic(registry.live_path(MAIN, MAIN), {"reported_usd": 1})
        self.assertEqual(registry.session_ids(MAIN), [MAIN])
        self.assertEqual(registry.load_live(MAIN, MAIN), {"reported_usd": 1})

    def test_rejects_bad_ids(self):
        with self.assertRaises(ValueError):
            registry.save_session({"session_id": "../x", "root_session_id": MAIN})
        self.assertIsNone(registry.load_session("..", MAIN))


if __name__ == "__main__":
    unittest.main()

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import aggregate, core, hooks, registry, transcripts  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "registry"
REPORT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "report_data.json"
MAIN = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD = "aaaaaaaa-0000-4000-8000-000000000002"
GRAND = "aaaaaaaa-0000-4000-8000-000000000003"


def ts(hhmm):
    return core.parse_ts(f"2026-01-01T{hhmm}:00Z")


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("AGENT_COST_STATE_DIR")
        self.addCleanup(lambda: os.environ.pop("AGENT_COST_STATE_DIR", None) if saved is None
                        else os.environ.__setitem__("AGENT_COST_STATE_DIR", saved))
        os.environ["AGENT_COST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.proj = Path(self.tmp.name) / "projects" / "-tmp-reg"
        shutil.copytree(FIX / "projects" / "-tmp-reg", self.proj)
        self.herdr = Path(self.tmp.name) / "herdr"
        shutil.copytree(FIX / "herdr", self.herdr)
        none = str(Path(self.tmp.name) / "none")
        self.main_env = {"HERDR_AGENTS_STATE_DIR": none, "CLAUDE_EFFORT": "high"}
        self.child_env = {"HERDR_AGENTS_STATE_DIR": none, "HERDR_AGENT_DIR": str(self.herdr / "child-agent"),
                          "HERDR_AGENT_ID": "child-agent", "HERDR_AGENT_DEPTH": "1",
                          "HERDR_PARENT_SESSION_ID": MAIN, "HERDR_ROOT_SESSION_ID": MAIN, "CLAUDE_EFFORT": "medium"}
        self.grand_env = dict(self.child_env, HERDR_AGENT_DIR=str(self.herdr / "grand-agent"),
                              HERDR_AGENT_ID="grand-agent", HERDR_AGENT_DEPTH="2", HERDR_PARENT_SESSION_ID=CHILD,
                              CLAUDE_EFFORT="low")
        self._run_flow()

    def tp(self, sid):
        return str(self.proj / f"{sid}.jsonl")

    def hook(self, event, at, env, **payload):
        with mock.patch.object(core, "now", return_value=ts(at)):
            hooks.run(event, json.dumps(payload), env=env)

    def _run_flow(self):
        self.hook("session-start", "10:00", self.main_env, session_id=MAIN, source="startup", cwd="/tmp/reg",
                  transcript_path=self.tp(MAIN), model="claude-opus-5-5")
        self.hook("prompt", "10:00", self.main_env, session_id=MAIN, prompt="Build the cost tracker")
        self.hook("session-start", "10:05", self.child_env, session_id=CHILD, source="startup", cwd="/tmp/reg",
                  transcript_path=self.tp(CHILD))
        self.hook("subagent-stop", "10:07", self.main_env, session_id=MAIN, transcript_path=self.tp(MAIN),
                  agent_id="b2", agent_type="general-purpose")
        self.hook("session-start", "10:10", self.grand_env, session_id=GRAND, source="startup", cwd="/tmp/reg",
                  transcript_path=self.tp(GRAND))
        shutil.rmtree(self.herdr / "child-agent")
        self.hook("stop", "10:15", self.grand_env, session_id=GRAND, transcript_path=self.tp(GRAND),
                  last_assistant_message="Grandchild summary from hook.")
        self.hook("session-end", "10:16", self.grand_env, session_id=GRAND, transcript_path=self.tp(GRAND),
                  reason="other")
        self.hook("stop", "10:20", self.child_env, session_id=CHILD, transcript_path=self.tp(CHILD))
        self.hook("session-end", "10:21", self.child_env, session_id=CHILD, transcript_path=self.tp(CHILD),
                  reason="other")
        self.hook("stop", "10:30", self.main_env, session_id=MAIN, transcript_path=self.tp(MAIN),
                  last_assistant_message="Main done: all built.")
        core.write_json_atomic(registry.live_path(MAIN, MAIN), {"session_id": MAIN, "reported_usd": 0.01,
                                                                "model": "claude-opus-5-5", "ts": ts("10:30")})

    def build(self, at):
        return aggregate.build_root(MAIN, now=ts(at))

    def by_sid(self, data):
        return {s["session_id"]: s for s in data["sessions"]}

    def test_totals_sum_without_double_count(self):
        data = self.build("10:35")
        s = self.by_sid(data)
        self.assertEqual(list(s), [MAIN, CHILD, GRAND])
        main_own = transcripts.summarize(self.tp(MAIN))["estimated_usd"]
        builtins = sum(b["cost_usd"] for b in transcripts.builtin_agents(self.tp(MAIN)))
        grand = transcripts.summarize(self.tp(GRAND))["estimated_usd"]
        self.assertAlmostEqual(s[MAIN]["cost"]["estimated_usd"], main_own + builtins, places=6)
        self.assertAlmostEqual(s[MAIN]["cost"]["effective_usd"], main_own + builtins, places=6)
        self.assertEqual(s[MAIN]["cost"]["reported_source"], "statusline")
        self.assertEqual(s[CHILD]["cost"]["effective_usd"], 5.0)
        self.assertEqual(s[CHILD]["cost"]["reported_source"], "cost-state")
        self.assertLess(s[CHILD]["cost"]["estimated_usd"], 5.0)
        self.assertAlmostEqual(s[GRAND]["cost"]["effective_usd"], grand, places=6)
        total = sum(x["cost"]["effective_usd"] for x in data["sessions"])
        self.assertAlmostEqual(data["totals"]["cost_usd"], total, places=6)
        self.assertAlmostEqual(data["totals"]["cost_usd"], main_own + builtins + 5.0 + grand, places=6)
        self.assertAlmostEqual(data["totals"]["own_cost_usd"], s[MAIN]["cost"]["effective_usd"], places=6)
        self.assertAlmostEqual(data["totals"]["agents_cost_usd"], 5.0 + grand, places=6)
        self.assertEqual((data["totals"]["sessions"], data["totals"]["builtin_agents"]), (3, 3))
        self.assertAlmostEqual(s[MAIN]["subtree_cost_usd"], total, places=6)
        self.assertAlmostEqual(s[CHILD]["subtree_cost_usd"], 5.0 + grand, places=6)
        self.assertTrue(all(b["included_in_parent"] for b in s[MAIN]["builtin_agents"]))
        tok = data["totals"]["by_model"]
        self.assertEqual(set(tok), {"claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"})
        self.assertEqual(tok["claude-haiku-5-5"]["output"], 500 + 300 + 100 + 400)

    def test_identity_and_excerpts(self):
        s = self.by_sid(self.build("10:35"))
        self.assertEqual((s[MAIN]["kind"], s[MAIN]["depth"], s[MAIN]["role"]), ("main", 0, "main"))
        self.assertEqual((s[CHILD]["kind"], s[CHILD]["depth"], s[CHILD]["parent_session_id"], s[CHILD]["herdr_id"]),
                         ("herdr", 1, MAIN, "child-agent"))
        self.assertEqual((s[GRAND]["depth"], s[GRAND]["parent_session_id"]), (2, CHILD))
        self.assertEqual(s[MAIN]["task"]["prompt_excerpt"], "Build the cost tracker")
        self.assertEqual(s[CHILD]["task"], {"description": "explore",
                                            "prompt_excerpt": "Explore the herdr lifecycle and report env vars."})
        self.assertEqual(s[CHILD]["result_excerpt"], "Child result: lifecycle mapped.")
        self.assertEqual(s[GRAND]["result_excerpt"], "Grandchild summary from hook.")
        self.assertEqual(s[MAIN]["model"], "claude-opus-5-5")
        self.assertEqual((s[MAIN]["effort"], s[CHILD]["effort"], s[GRAND]["effort"]), ("high", "medium", "low"))
        self.assertEqual(s[CHILD]["model"], "claude-sonnet-5-5")
        b = s[MAIN]["builtin_agents"][0]
        self.assertEqual((b["agent_type"], b["description"], b["prompt_excerpt"]),
                         ("Explore", "Find config", "Find the config files"))

    def test_times_and_states(self):
        s = self.by_sid(self.build("10:35"))
        self.assertEqual(s[MAIN]["started_at"], "2026-01-01T10:00:00Z")
        self.assertEqual((s[MAIN]["state"], s[MAIN]["duration_s"], s[MAIN]["ended_at"]), ("live", 35 * 60, None))
        self.assertEqual((s[CHILD]["state"], s[CHILD]["ended_at"], s[CHILD]["duration_s"]),
                         ("ended", "2026-01-01T10:21:00Z", 16 * 60))
        self.assertEqual((s[GRAND]["state"], s[GRAND]["duration_s"]), ("ended", 6 * 60))
        self.assertEqual(s[MAIN]["builtin_agents"][0]["duration_s"], 120)
        later = self.by_sid(self.build("11:31"))
        self.assertEqual((later[MAIN]["state"], later[MAIN]["duration_s"]), ("idle", 30 * 60))

    def test_resumed_after_end_is_not_ended(self):
        registry.save_session({"session_id": GRAND, "root_session_id": MAIN, "last_activity_at": ts("10:40")})
        s = self.by_sid(self.build("10:41"))
        self.assertEqual((s[GRAND]["state"], s[GRAND]["ended_at"]), ("live", None))

    def test_live_summary_matches_without_transcripts(self):
        data = self.build("10:35")
        with mock.patch.object(transcripts, "summarize", side_effect=AssertionError("parsed")):
            live = aggregate.root_live_summary(MAIN, now=ts("10:35"))
        self.assertAlmostEqual(live["total_usd"], data["totals"]["cost_usd"], places=6)
        self.assertAlmostEqual(live["agents_usd"], data["totals"]["agents_cost_usd"], places=6)
        self.assertEqual((live["agents_count"], live["builtin_count"], live["state"]), (2, 3, "live"))
        self.assertEqual(live["started_at"], ts("10:00"))
        self.assertEqual(live["own_usd"][CHILD], 5.0)

    def test_reported_wins_when_higher(self):
        core.write_json_atomic(registry.live_path(MAIN, MAIN), {"reported_usd": 99.0, "ts": ts("10:30")})
        s = self.by_sid(self.build("10:35"))
        self.assertEqual((s[MAIN]["cost"]["effective_usd"], s[MAIN]["cost"]["reported_source"]), (99.0, "statusline"))

    def test_write_and_list_roots(self):
        path = aggregate.write_root(MAIN, now=ts("10:35"))
        self.assertEqual(path, core.roots_dir() / f"{MAIN}.json")
        self.assertEqual(core.read_json(path)["schema"], 1)
        rows = aggregate.list_roots()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["root_session_id"], rows[0]["sessions"], rows[0]["title"]),
                         (MAIN, 3, "Build the cost tracker"))

    def test_shape_matches_report_fixture(self):
        if not REPORT_FIXTURE.is_file():
            self.skipTest("report_data.json not present")
        ref = core.read_json(REPORT_FIXTURE)["roots"][0]
        data = self.build("10:35")
        self.assertLessEqual(set(ref), set(data))
        self.assertLessEqual(set(ref["totals"]), set(data["totals"]))
        ref_s, s = ref["sessions"][0], data["sessions"][0]
        self.assertLessEqual(set(ref_s), set(s))
        self.assertLessEqual(set(ref_s["cost"]), set(s["cost"]))
        self.assertLessEqual(set(ref_s["task"]), set(s["task"]))
        ref_b = next(x["builtin_agents"][0] for x in ref["sessions"] if x.get("builtin_agents"))
        self.assertLessEqual(set(ref_b), set(s["builtin_agents"][0]))
        self.assertIsInstance(ref_s["started_at"], str)
        self.assertIsInstance(s["started_at"], str)


if __name__ == "__main__":
    unittest.main()

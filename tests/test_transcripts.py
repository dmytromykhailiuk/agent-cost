import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import transcripts  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts"
SID = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
MAIN = FIXTURES / "projects" / "-tmp-demo" / f"{SID}.jsonl"


class _StateDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("AGENT_COST_STATE_DIR")
        self.addCleanup(lambda: os.environ.pop("AGENT_COST_STATE_DIR", None) if saved is None
                        else os.environ.__setitem__("AGENT_COST_STATE_DIR", saved))
        os.environ["AGENT_COST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()


class UsageNumbersTests(unittest.TestCase):
    def test_split_and_legacy(self):
        n = transcripts.usage_numbers({"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
                                       "cache_creation_input_tokens": 9,
                                       "cache_creation": {"ephemeral_5m_input_tokens": 4,
                                                          "ephemeral_1h_input_tokens": 5}})
        self.assertEqual(n, {"input": 1, "output": 2, "cache_read": 3, "cache_write_5m": 4, "cache_write_1h": 5})
        legacy = transcripts.usage_numbers({"cache_creation_input_tokens": 7})
        self.assertEqual((legacy["cache_write_5m"], legacy["cache_write_1h"]), (7, 0))
        self.assertEqual(transcripts.usage_numbers(None)["input"], 0)


class SummarizeTests(_StateDirCase):
    def test_fixture_summary(self):
        s = transcripts.summarize(MAIN, use_cache=False)
        self.assertEqual(s["session_id"], SID)
        self.assertEqual(s["cwd"], "/tmp/demo")
        opus = s["by_model"]["claude-opus-5-5"]
        self.assertEqual(opus["requests"], 1)
        self.assertEqual((opus["output"], opus["cache_write_5m"]), (50, 2000))
        self.assertAlmostEqual(opus["cost_usd"], (10 * 4 + 50 * 20 + 1000 * 0.2 + 2000 * 5) / 1e6)
        self.assertNotIn("<synthetic>", s["by_model"])
        self.assertEqual(s["unknown_models"], ["claude-mystery-9"])
        self.assertIsNone(s["by_model"]["claude-mystery-9"]["cost_usd"])
        sonnet = (20 * 2 + 30 * 10 + 400 * 4) / 1e6
        self.assertAlmostEqual(s["estimated_usd"], opus["cost_usd"] + sonnet)
        self.assertEqual(s["first_user_prompt"], "Build the demo feature please")
        self.assertEqual(s["last_assistant_text"], "Done. Feature built.")
        self.assertEqual(s["agent_name"], "demo-agent")
        self.assertEqual(s["custom_title"], "Demo title")
        self.assertEqual(s["model"], "claude-sonnet-5-5")
        self.assertEqual(s["effort"], "low")
        self.assertEqual(s["cost_state"]["totalCostUSD"], 1.25)
        self.assertEqual(s["first_ts"], 1767225601.0)
        self.assertEqual(s["last_ts"], 1767225612.0)

    def test_main_only_excludes_sidechain(self):
        main = transcripts.summarize(MAIN, use_cache=False)
        full = transcripts.summarize(MAIN, main_only=False, use_cache=False)
        self.assertEqual(main["by_model"]["claude-opus-5-5"]["input"], 10)
        self.assertEqual(full["by_model"]["claude-opus-5-5"]["input"], 10 + 999)
        self.assertEqual(full["by_model"]["claude-opus-5-5"]["requests"], 2)

    def test_herdr_links(self):
        links = {l["herdr_id"]: l for l in transcripts.summarize(MAIN, use_cache=False)["herdr_links"]}
        self.assertEqual(set(links), {"explore-1a2b", "other-9"})
        self.assertEqual(links["explore-1a2b"], {"herdr_id": "explore-1a2b", "session_id": CHILD,
                                                 "tab": "wA:t5A", "status": "done"})
        self.assertEqual(links["other-9"]["tab"], "wA:t6")
        self.assertIsNone(links["other-9"]["session_id"])

    def test_herdr_regex(self):
        text = ("[herdr-agent] id=explore-186781 status=done tab=wA:t5A session=ad568887-1234-4abc-8def-0123456789ab\n"
                "[herdr-agent] id=x-1 status=blocked tab=wA:t3F session=ad568887-…\n"
                "[herdr-agent] spawned claude-25a0d0 in tab wA:t5G; waiting")
        links = transcripts.parse_herdr_links(text)
        self.assertEqual(links["explore-186781"]["session_id"], "ad568887-1234-4abc-8def-0123456789ab")
        self.assertIsNone(links["x-1"]["session_id"])
        self.assertEqual(links["x-1"]["status"], "blocked")
        self.assertEqual(links["claude-25a0d0"], {"herdr_id": "claude-25a0d0", "session_id": None,
                                                  "tab": "wA:t5G", "status": "spawned"})
        self.assertEqual(transcripts.parse_herdr_links("[herdr-agent] id=<aid> status=x"), {})

    def test_incremental_equals_full(self):
        lines = MAIN.read_bytes().splitlines(keepends=True)
        target = self.work / f"{SID}.jsonl"
        for cut in (3, 6, 9, len(lines)):
            target.write_bytes(b"".join(lines[:cut]))
            inc = transcripts.summarize(target)
            full = transcripts.summarize(target, use_cache=False)
            self.assertEqual(inc, full, f"cut={cut}")
        cache = list((Path(os.environ["AGENT_COST_STATE_DIR"]) / "cache" / "offsets").glob("*.json"))
        self.assertEqual(len(cache), 1)
        self.assertEqual(json.loads(cache[0].read_text())["offset"], target.stat().st_size)

    def test_truncated_last_line(self):
        data = MAIN.read_bytes()
        lines = data.splitlines(keepends=True)
        target = self.work / f"{SID}.jsonl"
        last = lines[-1]
        target.write_bytes(b"".join(lines[:-1]) + last[: len(last) // 2])
        partial = transcripts.summarize(target)
        self.assertNotIn("claude-sonnet-5-5", partial["by_model"])
        cache = next((Path(os.environ["AGENT_COST_STATE_DIR"]) / "cache" / "offsets").glob("*.json"))
        self.assertEqual(json.loads(cache.read_text())["offset"], len(b"".join(lines[:-1])))
        target.write_bytes(data)
        self.assertEqual(transcripts.summarize(target), transcripts.summarize(target, use_cache=False))
        self.assertIn("claude-sonnet-5-5", transcripts.summarize(target)["by_model"])

    def test_last_line_without_newline_counts(self):
        target = self.work / f"{SID}.jsonl"
        target.write_bytes(MAIN.read_bytes().rstrip(b"\n"))
        self.assertEqual(transcripts.summarize(target), transcripts.summarize(MAIN, use_cache=False) | {"path": str(target)})

    def test_shrink_and_rotation_reparse(self):
        lines = MAIN.read_bytes().splitlines(keepends=True)
        target = self.work / f"{SID}.jsonl"
        target.write_bytes(b"".join(lines))
        transcripts.summarize(target)
        target.write_bytes(b"".join(lines[:5]))
        self.assertEqual(transcripts.summarize(target), transcripts.summarize(target, use_cache=False))
        rotated = [lines[0]] + [lines[4].replace(b"Build the demo", b"Rebuild a demo")] + lines[1:4] + lines[5:]
        target.write_bytes(b"".join(rotated))
        self.assertEqual(transcripts.summarize(target), transcripts.summarize(target, use_cache=False))

    def test_dedupe_keeps_last_usage_by_message_id(self):
        target = self.work / "dup.jsonl"
        recs = [
            {"type": "assistant", "requestId": "r1", "uuid": "a", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": 1}}},
            {"type": "assistant", "requestId": "r1", "uuid": "b", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": 7}}},
            {"type": "assistant", "requestId": "r2", "uuid": "c", "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 3}}},
            {"type": "assistant", "requestId": "r2", "uuid": "d", "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 3}}},
            {"type": "assistant", "uuid": "e", "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 2}}},
        ]
        target.write_text("".join(json.dumps(r) + "\n" for r in recs))
        row = transcripts.summarize(target, use_cache=False)["by_model"]["claude-opus-5-5"]
        self.assertEqual((row["requests"], row["output"]), (3, 12))

    def test_user_prompt_filters(self):
        prompt = lambda content, **rec: transcripts._user_prompt(rec, {"content": content})
        self.assertIsNone(prompt("<command-name>/clear</command-name><command-args></command-args>"))
        self.assertEqual(prompt("<command-name>/verify-loop</command-name><command-args>fast</command-args>"),
                         "/verify-loop fast")
        self.assertIsNone(prompt([{"type": "tool_result", "content": "x"}]))
        self.assertIsNone(prompt("<task-notification>x</task-notification>", origin={"kind": "task-notification"}))
        self.assertIsNone(prompt("hi", isMeta=True))
        self.assertEqual(prompt([{"type": "text", "text": "  hello   world "}]), "hello world")

    def test_missing_file(self):
        s = transcripts.summarize(self.work / "nope.jsonl")
        self.assertEqual(s["by_model"], {})
        self.assertEqual(s["estimated_usd"], 0.0)


class BuiltinAgentsTests(_StateDirCase):
    def test_with_and_without_meta(self):
        agents = transcripts.builtin_agents(MAIN)
        self.assertEqual([a["agent_id"] for a in agents], ["aaa111", "bbb222"])
        a, b = agents
        self.assertEqual(a["agent_type"], "Explore")
        self.assertEqual(a["description"], "Find config")
        self.assertEqual((a["spawn_depth"], a["tool_use_id"], a["finished"]), (1, "toolu_9", True))
        self.assertEqual(a["model"], "claude-haiku-5-5")
        self.assertEqual(a["effort"], "medium")
        self.assertEqual(a["prompt_excerpt"], "Find the config files")
        self.assertEqual(a["result_excerpt"], "Found two files.")
        self.assertEqual(a["duration_s"], 30.0)
        self.assertAlmostEqual(a["cost_usd"], (5 * 0.1 + 100 * 0.5 + 1000 * 0.125) / 1e6)
        self.assertEqual(a["by_model"]["claude-haiku-5-5"]["cache_write_5m"], 1000)
        self.assertIsNone(b["agent_type"])
        self.assertIsNone(b["finished"])
        self.assertEqual(b["model"], "claude-sonnet-5-5")
        self.assertEqual(b["prompt_excerpt"], "Second task")

    def test_no_subagents_dir(self):
        self.assertEqual(transcripts.builtin_agents(self.work / "x.jsonl"), [])
        self.assertEqual(transcripts.builtin_agents(None), [])


class FindTranscriptTests(_StateDirCase):
    def test_find(self):
        self.assertEqual(transcripts.find_transcript(SID, [FIXTURES]), MAIN)
        self.assertIsNone(transcripts.find_transcript("missing", [FIXTURES]))
        self.assertIsNone(transcripts.find_transcript(None, [FIXTURES]))

    def test_env_config_dir(self):
        shutil.copytree(FIXTURES, self.work / "cfg")
        saved = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.work / "cfg")
        try:
            found = transcripts.find_transcript(SID)
        finally:
            if saved is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = saved
        self.assertEqual(found, self.work / "cfg" / "projects" / "-tmp-demo" / f"{SID}.jsonl")


if __name__ == "__main__":
    unittest.main()

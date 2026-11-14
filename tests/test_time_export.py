import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import aggregate, cli, core, registry, transcripts  # noqa: E402

MAIN = "bbbbbbbb-0000-4000-8000-000000000001"
CHILD = "bbbbbbbb-0000-4000-8000-000000000002"
RUN = "20261114-100000-abc123"
TRIAGE = "vloop-triage-" + RUN
USAGE = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 50, "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                                               "ephemeral_1h_input_tokens": 30}}


def at(hms):
    return f"2026-01-01T{hms}Z"


def user(hms, text, sid=MAIN, **extra):
    return {"type": "user", "sessionId": sid, "cwd": "/tmp/t", "timestamp": at(hms),
            "message": {"role": "user", "content": text}, **extra}


def tool_result(hms, sid=MAIN):
    return {"type": "user", "sessionId": sid, "cwd": "/tmp/t", "timestamp": at(hms), "toolUseResult": {},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


def assistant(hms, mid, sid=MAIN, text="done"):
    return {"type": "assistant", "sessionId": sid, "cwd": "/tmp/t", "timestamp": at(hms),
            "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": text}], "usage": USAGE}}


MAIN_RECORDS = [
    user("10:00:00", "Build it"),
    assistant("10:00:30", "m1"),
    tool_result("10:01:00"),
    assistant("10:01:20", "m2"),
    user("10:30:00", "Now the second part"),
    assistant("10:30:40", "m3"),
    tool_result("10:40:00"),
    assistant("10:40:10", "m4"),
]


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def snapshot(base):
    out = {}
    for p in Path(base).rglob("*"):
        if p.is_file():
            out[str(p)] = (p.stat().st_mtime_ns, p.stat().st_size)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"AGENT_COST_STATE_DIR": str(Path(self.tmp.name) / "state"), "AGENT_COST_VLOOP_DIRS": "",
               "AGENT_COST_CONFIG": str(Path(self.tmp.name) / "config.json")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_COST_IDLE_GAP_S", None)
        self.proj = Path(self.tmp.name) / "projects" / "-tmp-t"

    def main_transcript(self):
        path = self.proj / f"{MAIN}.jsonl"
        write_jsonl(path, MAIN_RECORDS)
        return path


class TimeSplitTest(Base):
    def test_user_wait_and_long_gap_are_idle(self):
        s = transcripts.summarize(self.main_transcript(), use_cache=False)
        self.assertEqual(s["time"]["wall_s"], 2410)
        self.assertEqual(s["time"]["active_s"], 130)
        self.assertEqual(s["time"]["idle_s"], 2280)

    def test_threshold_is_configurable(self):
        path = self.main_transcript()
        with mock.patch.dict(os.environ, {"AGENT_COST_IDLE_GAP_S": "600"}):
            self.assertEqual(transcripts.summarize(path, use_cache=False)["time"]["active_s"], 690)
        Path(os.environ["AGENT_COST_CONFIG"]).write_text(json.dumps({"idle_gap_s": 600}), encoding="utf-8")
        self.assertEqual(transcripts.summarize(path, use_cache=False)["time"]["active_s"], 690)

    def test_cache_is_rebuilt_when_threshold_changes(self):
        path = self.main_transcript()
        self.assertEqual(transcripts.summarize(path)["time"]["active_s"], 130)
        self.assertEqual(transcripts.summarize(path)["time"]["active_s"], 130)
        with mock.patch.dict(os.environ, {"AGENT_COST_IDLE_GAP_S": "600"}):
            self.assertEqual(transcripts.summarize(path)["time"]["active_s"], 690)

    def test_incremental_matches_full(self):
        path = self.proj / f"{MAIN}.jsonl"
        write_jsonl(path, MAIN_RECORDS[:4])
        transcripts.summarize(path)
        write_jsonl(path, MAIN_RECORDS)
        self.assertEqual(transcripts.summarize(path)["time"]["active_s"], 130)

    def test_out_of_order_keeps_invariant(self):
        path = self.proj / "x.jsonl"
        write_jsonl(path, [user("10:00:00", "go"), assistant("10:02:00", "a"), assistant("10:01:00", "b"),
                           assistant("10:03:00", "c")])
        t = transcripts.summarize(path, use_cache=False)["time"]
        self.assertEqual(t["wall_s"], t["active_s"] + t["idle_s"])
        self.assertEqual(t["active_s"], 180)

    def test_window(self):
        s = transcripts.summarize_window(self.main_transcript(), core.parse_ts(at("10:30:00")), None)
        self.assertEqual(s["time"], {"wall_s": 610, "active_s": 50, "waiting_s": 0, "idle_s": 560})


class TreeFixture(Base):
    def setUp(self):
        super().setUp()
        main_tp = self.main_transcript()
        sub = self.proj / MAIN / "subagents"
        write_jsonl(sub / "agent-retro1.jsonl", [
            user("10:02:00", f"Operation: retro\nTask file: /x/agent-verify-loop/runs/{RUN}/retro/task.md", sid=MAIN),
            assistant("10:03:00", "r1", sid=MAIN)])
        (sub / "agent-retro1.meta.json").write_text(json.dumps({"agentType": "agent-vault:vault-keeper"}))
        write_jsonl(sub / "agent-vs1.jsonl", [
            user("10:31:00", "Operation: search\nvault dir ~/claude-vault/x", sid=MAIN),
            assistant("10:31:30", "v1", sid=MAIN)])
        (sub / "agent-vs1.meta.json").write_text(json.dumps({"agentType": "agent-vault:vault-keeper"}))
        child_tp = self.proj / f"{CHILD}.jsonl"
        write_jsonl(child_tp, [
            user("10:05:00", f"Your task file is /s/agent-verify-loop/runs/{RUN}/attempt-1/reviewer.prompt.md",
                 sid=CHILD),
            assistant("10:06:00", "c1", sid=CHILD)])
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN, "kind": "main", "depth": 0,
                               "transcript_path": str(main_tp), "started_at": core.parse_ts(at("10:00:00")),
                               "ended_at": core.parse_ts(at("10:40:10"))})
        registry.save_session({"session_id": CHILD, "root_session_id": MAIN, "parent_session_id": MAIN,
                               "kind": "herdr", "herdr_id": "verify-review-1a2b", "depth": 1,
                               "transcript_path": str(child_tp), "started_at": core.parse_ts(at("10:05:00")),
                               "ended_at": core.parse_ts(at("10:06:00"))})
        registry.save_session({"session_id": TRIAGE, "root_session_id": MAIN, "parent_session_id": MAIN,
                               "kind": "headless", "depth": 1, "role": "triage", "purpose": "verify:triage",
                               "synthetic": "vloop-triage", "reported_usd": 0.02, "model": "claude-haiku-5-5",
                               "started_at": core.parse_ts(at("10:04:00")), "ended_at": core.parse_ts(at("10:04:30"))})

    def export(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["export", "--session", MAIN, "--json", *extra])
        return code, json.loads(buf.getvalue())


class ExportTest(TreeFixture):
    def test_schema(self):
        code, d = self.export()
        self.assertEqual(code, 0)
        self.assertEqual(set(d), {"contract", "generated_at", "session_id", "window", "main", "agents"})
        self.assertEqual(d["contract"], 1)
        self.assertEqual(d["window"], {"since": None, "until": None})
        main = d["main"]
        self.assertEqual(set(main), {"session_id", "usd", "tokens", "models", "time"})
        self.assertEqual(main["tokens"], {"input": 40, "output": 400, "cache_creation": 200, "cache_read": 4000})
        self.assertEqual(main["time"], {"wall_ms": 2410000, "active_ms": 130000, "waiting_ms": 180000,
                                        "idle_ms": 2100000})
        self.assertEqual(set(main["models"]["claude-opus-5-5"]), {"usd", "tokens"})
        keys = {"session_id", "parent_session_id", "herdr_id", "kind", "purpose", "run_id", "model", "usd",
                "tokens", "time"}
        for a in d["agents"]:
            self.assertEqual(set(a), keys)
            t = a["time"]
            self.assertEqual(set(t), {"wall_ms", "active_ms", "waiting_ms", "idle_ms"})
            self.assertEqual(t["wall_ms"], t["active_ms"] + t["waiting_ms"] + t["idle_ms"])
            self.assertTrue(min(t.values()) >= 0)

    def test_agents_and_purpose(self):
        _, d = self.export()
        by_id = {a["session_id"]: a for a in d["agents"]}
        self.assertEqual(set(by_id), {"agent-retro1", "agent-vs1", CHILD, TRIAGE})
        retro = by_id["agent-retro1"]
        self.assertEqual((retro["kind"], retro["purpose"], retro["run_id"]), ("builtin", "verify:retro", RUN))
        self.assertEqual(retro["parent_session_id"], MAIN)
        self.assertEqual((by_id["agent-vs1"]["purpose"], by_id["agent-vs1"]["run_id"]), ("vault", None))
        child = by_id[CHILD]
        self.assertEqual((child["kind"], child["purpose"], child["run_id"], child["herdr_id"]),
                         ("herdr", "verify:review", RUN, "verify-review-1a2b"))
        tri = by_id[TRIAGE]
        self.assertEqual((tri["kind"], tri["purpose"], tri["run_id"], tri["usd"]),
                         ("synthetic", "verify:triage", RUN, 0.02))
        self.assertEqual(tri["time"], {"wall_ms": 30000, "active_ms": 30000, "waiting_ms": 0, "idle_ms": 0})

    def test_main_excludes_descendants(self):
        _, d = self.export()
        s = transcripts.summarize(self.proj / f"{MAIN}.jsonl", use_cache=False)
        self.assertAlmostEqual(d["main"]["usd"], round(s["estimated_usd"], 6))

    def test_window(self):
        code, d = self.export("--since", at("10:30:00"))
        self.assertEqual(code, 0)
        self.assertEqual(d["window"], {"since": at("10:30:00"), "until": None})
        self.assertEqual(d["main"]["time"], {"wall_ms": 610000, "active_ms": 50000, "waiting_ms": 30000,
                                             "idle_ms": 530000})
        self.assertEqual(d["main"]["tokens"]["output"], 200)
        self.assertEqual([a["session_id"] for a in d["agents"]], ["agent-vs1"])
        _, d = self.export("--since", at("10:00:00"), "--until", at("10:04:45"))
        self.assertEqual({a["session_id"] for a in d["agents"]}, {"agent-retro1", TRIAGE})
        self.assertEqual(d["main"]["time"]["wall_ms"], 80000)

    def test_unknown_session(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["export", "--session", "cccccccc-0000-4000-8000-000000000009", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(buf.getvalue()))

    def test_no_writes(self):
        state = os.environ["AGENT_COST_STATE_DIR"]
        before = snapshot(state)
        self.export()
        self.export("--since", at("10:30:00"))
        self.assertEqual(snapshot(state), before)


class ShowTimeTest(TreeFixture):
    def test_show_has_time_split_and_builtin_purpose(self):
        data = aggregate.build_root(MAIN, now=core.parse_ts(at("11:00:00")))
        for s in data["sessions"]:
            t = s["time"]
            self.assertEqual(t["wall_s"], t["active_s"] + t["waiting_s"] + t["idle_s"])
        main = next(s for s in data["sessions"] if s["session_id"] == MAIN)
        self.assertEqual(main["time"], {"wall_s": 2410, "active_s": 130, "waiting_s": 180, "idle_s": 2100})
        b = {x["agent_id"]: x for x in main["builtin_agents"]}
        self.assertEqual((b["retro1"]["purpose"], b["retro1"]["run_id"]), ("verify:retro", RUN))
        self.assertEqual(b["vs1"]["purpose"], "vault")
        self.assertEqual(b["vs1"]["time"], {"wall_s": 30, "active_s": 30, "waiting_s": 0, "idle_s": 0})
        tt = data["totals"]["time"]
        self.assertEqual((tt["wall_s"], tt["active_s"], tt["waiting_s"], tt["idle_s"]), (2410, 130, 180, 2100))
        self.assertEqual(tt["agents_active_s"], 60 + 60 + 30 + 30)


class VersionTest(unittest.TestCase):
    def test_version_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["version", "--json"])
        self.assertEqual(code, 0)
        d = json.loads(buf.getvalue())
        self.assertEqual(set(d), {"name", "version", "contract"})
        self.assertEqual((d["name"], d["contract"]), ("agent-cost", 1))


if __name__ == "__main__":
    unittest.main()

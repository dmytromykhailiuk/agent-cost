import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: F401,E402
from costlib import aggregate, cli, core, registry, report, statusline, transcripts  # noqa: E402
from test_time_export import MAIN, Base, assistant, at, user, write_jsonl  # noqa: E402

KID1 = "bbbbbbbb-0000-4000-8000-000000000011"
KID2 = "bbbbbbbb-0000-4000-8000-000000000012"
GRAND = "bbbbbbbb-0000-4000-8000-000000000013"
NOTP = "bbbbbbbb-0000-4000-8000-000000000014"
NOW = core.parse_ts(at("11:00:00"))

ORCH = [
    user("10:00:00", "Orchestrate"),
    assistant("10:00:30", "m1"),
    assistant("10:01:00", "m2"),
    user("10:20:00", "Next"),
    assistant("10:20:30", "m3"),
    assistant("10:40:30", "m4"),
]


def ts(hms):
    return core.parse_ts(at(hms))


class WaitingMathTest(unittest.TestCase):
    def test_overlapping_spans_are_merged(self):
        self.assertEqual(transcripts.waiting_s([[0, 100]], [[10, 40], [30, 60], [50, 55]]), 50)

    def test_only_inside_gaps(self):
        self.assertEqual(transcripts.waiting_s([[0, 10], [20, 30]], [[5, 25]]), 10)
        self.assertEqual(transcripts.waiting_s([], [[5, 25]]), 0)
        self.assertEqual(transcripts.waiting_s([[0, 10]], [None, [3, 3], [8, 2]]), 0)


class WaitingFixture(Base):
    def kid(self, sid, start, end, parent=MAIN, depth=1):
        tp = self.proj / f"{sid}.jsonl"
        write_jsonl(tp, [user(start, "Do the part", sid=sid), assistant(end, "k" + sid[-2:], sid=sid)])
        registry.save_session({"session_id": sid, "root_session_id": MAIN, "parent_session_id": parent,
                               "kind": "herdr", "herdr_id": "h-" + sid[-2:], "depth": depth,
                               "transcript_path": str(tp), "started_at": ts(start), "ended_at": ts(end)})

    def setUp(self):
        super().setUp()
        tp = self.proj / f"{MAIN}.jsonl"
        write_jsonl(tp, ORCH)
        registry.save_session({"session_id": MAIN, "root_session_id": MAIN, "kind": "main", "depth": 0,
                               "transcript_path": str(tp), "started_at": ts("10:00:00"),
                               "ended_at": ts("10:40:30")})

    def main_time(self):
        data = aggregate.build_root(MAIN, now=NOW)
        return next(s for s in data["sessions"] if s["session_id"] == MAIN)["time"], data

    def export(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["export", "--session", MAIN, "--json"])
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())


class WaitingTest(WaitingFixture):
    def test_child_during_long_gap_is_waiting(self):
        self.kid(KID1, "10:22:00", "10:30:00")
        t, _ = self.main_time()
        self.assertEqual(t, {"wall_s": 2430, "active_s": 90, "waiting_s": 480, "idle_s": 1860})
        e = self.export()["main"]["time"]
        self.assertEqual(e, {"wall_ms": 2430000, "active_ms": 90000, "waiting_ms": 480000, "idle_ms": 1860000})

    def test_overlapping_children_not_double_counted(self):
        self.kid(KID1, "10:22:00", "10:30:00")
        self.kid(KID2, "10:25:00", "10:35:00")
        self.kid(GRAND, "10:34:00", "10:38:00", parent=KID2, depth=2)
        t, data = self.main_time()
        self.assertEqual(t["waiting_s"], 16 * 60)
        self.assertEqual(self.export()["main"]["time"]["waiting_ms"], 16 * 60 * 1000)
        kid2 = next(s for s in data["sessions"] if s["session_id"] == KID2)["time"]
        self.assertEqual((kid2["active_s"], kid2["waiting_s"]), (0, 60))
        self.assertEqual(data["totals"]["time"]["waiting_s"], 16 * 60)

    def test_user_wait_without_child_is_idle(self):
        self.kid(KID1, "10:22:00", "10:30:00")
        t, _ = self.main_time()
        self.assertEqual(t["active_s"] + t["waiting_s"], 570)
        self.assertGreaterEqual(t["idle_s"], ts("10:20:00") - ts("10:01:00"))

    def test_invariant_everywhere(self):
        self.kid(KID1, "10:22:00", "10:30:00")
        self.kid(KID2, "10:25:00", "10:35:00")
        _, data = self.main_time()
        for s in data["sessions"]:
            t = s["time"]
            self.assertEqual(t["wall_s"], t["active_s"] + t["waiting_s"] + t["idle_s"])
            self.assertTrue(min(t.values()) >= 0)
        d = self.export()
        for t in [d["main"]["time"]] + [a["time"] for a in d["agents"]]:
            self.assertEqual(t["wall_ms"], t["active_ms"] + t["waiting_ms"] + t["idle_ms"])
            self.assertTrue(min(t.values()) >= 0)
        for a in d["agents"]:
            self.assertEqual(a["time"]["waiting_ms"], 0)

    def test_builtin_agent_counts_as_waiting(self):
        sub = self.proj / MAIN / "subagents"
        write_jsonl(sub / "agent-b1.jsonl", [user("10:21:00", "Explore", sid=MAIN),
                                             assistant("10:24:00", "b1", sid=MAIN)])
        t, _ = self.main_time()
        self.assertEqual(t["waiting_s"], 180)
        self.assertEqual(self.export()["main"]["time"]["waiting_ms"], 180000)

    def test_show_text_and_list(self):
        self.kid(KID1, "10:22:00", "10:30:00")
        aggregate.write_root(MAIN, now=NOW)
        lines = report.render_tree(core.read_json(aggregate.root_path(MAIN))).splitlines()
        self.assertIn("waiting 8m 00s", lines[1])
        self.assertIn("waiting 8m 00s", next(line for line in lines if line.startswith("main")))
        with mock.patch.object(report, "visible_roots", lambda: []):
            row = report._list_rows(10)[0]
        t = row["time"]
        self.assertEqual(row["duration_s"], t["wall_s"])
        self.assertEqual(t["wall_s"], t["active_s"] + t["waiting_s"] + t["idle_s"])
        self.assertEqual(t["waiting_s"], 480)


class StatusLineTimeTest(WaitingFixture):
    def test_live_summary_waiting_from_cache(self):
        self.kid(KID1, "10:22:00", "10:30:00")
        for sid in (MAIN, KID1):
            rec = registry.load_session(MAIN, sid)
            rec["estimate"] = aggregate.estimate(rec["transcript_path"])["cache"]
            registry.save_session(rec)
        s = aggregate.root_live_summary(MAIN, now=NOW)
        self.assertEqual((s["active_s"], s["waiting_s"]), (90, 480))

    def test_render_shows_active_waiting_idle(self):
        c = {"is_child": False, "depth": 0, "role": None, "purpose": None, "herdr_id": None, "total": 1.0,
             "self": 1.0, "agents_usd": 0.0, "agents_count": 0, "started": NOW - 3600, "duration": 3600,
             "split": statusline._split(3600, 600, 2400)}
        plain = re.sub(r"\x1b\[[0-9;]*m", "", statusline._render(c)[1])
        self.assertIn("1h00m · act 10m wait 40m idle 10m", plain)
        c["split"] = statusline._split(3600, 600, None)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", statusline._render(c)[1])
        self.assertIn("act 10m idle 50m", plain)
        self.assertNotIn("wait", plain)
        c["split"] = statusline._split(3600, None, None)
        self.assertNotIn("idle", re.sub(r"\x1b\[[0-9;]*m", "", statusline._render(c)[1]))


class UnknownTimeTest(WaitingFixture):
    def test_session_without_transcript_or_cache_is_not_idle(self):
        registry.save_session({"session_id": NOTP, "root_session_id": MAIN, "parent_session_id": MAIN,
                               "kind": "herdr", "depth": 1, "started_at": ts("10:05:00"),
                               "ended_at": ts("10:15:00")})
        _, data = self.main_time()
        s = next(x for x in data["sessions"] if x["session_id"] == NOTP)
        self.assertIsNone(s["time"])
        self.assertEqual(s["duration_s"], 600)
        aggregate.write_root(MAIN, now=NOW)
        lines = report.render_tree(core.read_json(aggregate.root_path(MAIN))).splitlines()
        row = next(line for line in lines if "10m 00s" in line)
        self.assertNotIn("idle", row)

    def test_root_without_transcript_lists_dashes(self):
        rec = registry.load_session(MAIN, MAIN)
        rec.pop("transcript_path")
        registry.save_session(rec)
        Path(self.proj / f"{MAIN}.jsonl").unlink()
        aggregate.write_root(MAIN, now=NOW)
        data = core.read_json(aggregate.root_path(MAIN))
        self.assertIsNone(data["totals"]["time"])
        self.assertIn("time: unknown", report.render_tree(data))
        with mock.patch.object(report, "visible_roots", lambda: []):
            rows = report._list_rows(10)
            self.assertIsNone(rows[0]["time"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                report.cmd_list(mock.Mock(limit=10, json=False, all=True))
        header, row = buf.getvalue().splitlines()[:2]
        self.assertIn("WAITING", header)
        self.assertRegex(row, r"\s-\s+-\s+-\s")


class ExportReadOnlyTest(Base):
    def test_missing_state_dir_is_not_created(self):
        state = Path(self.tmp.name) / "nope" / "state"
        with mock.patch.dict(os.environ, {"AGENT_COST_STATE_DIR": str(state),
                                          "CLAUDE_CONFIG_DIR": str(Path(self.tmp.name))}):
            write_jsonl(self.proj / f"{MAIN}.jsonl", ORCH)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(["export", "--session", MAIN, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["main"]["time"]["active_ms"], 90000)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["export", "--session", KID1, "--json"]), 1)
        self.assertFalse(state.exists())
        self.assertFalse(state.parent.exists())


if __name__ == "__main__":
    unittest.main()

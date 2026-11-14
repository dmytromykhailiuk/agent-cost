import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import core, registry, report  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "report_data.json"
ROOT = "275f9bbd-bd76-44dd-b8a4-94036e0c59b1"
GRANDCHILD = "c344d632"
NOW = core.parse_ts("2026-11-14T13:24:00Z")


def report_args(**kw):
    base = dict(session=None, latest=False, all=False, out=None, no_open=True)
    base.update(kw)
    return argparse.Namespace(**base)


class ReportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = {
            "AGENT_COST_STATE_DIR": str(base / "state"),
            "AGENT_COST_CONFIG": str(base / "config.json"),
            "CLAUDE_CONFIG_DIR": str(base / "claude"),
            "HOME": str(base / "home"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("AGENT_COST_SESSION", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(key, None)
        (base / "home").mkdir()
        now = mock.patch.object(core, "now", return_value=NOW)
        now.start()
        self.addCleanup(now.stop)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.base = base

    def seed_roots(self):
        for data in self.fixture["roots"]:
            core.write_json_atomic(core.roots_dir() / f"{data['root_session_id']}.json", data)

    def child_sid(self, prefix):
        for data in self.fixture["roots"]:
            for s in data["sessions"]:
                if s["session_id"].startswith(prefix):
                    return data["root_session_id"], s
        raise KeyError(prefix)

    def run_cmd(self, func, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()


@mock.patch.object(report, "refresh", lambda *a, **k: None)
class CmdReportTest(ReportTestBase):
    def payload_of(self, path):
        html = Path(path).read_text(encoding="utf-8")
        start = html.index("const DATA = ") + len("const DATA = ")
        end = html.index(";</script>", start)
        return json.loads(html[start:end])

    def test_payload_shape_matches_fixture(self):
        self.seed_roots()
        code, out, _ = self.run_cmd(report.cmd_report, report_args(latest=True))
        self.assertEqual(code, 0)
        payload = self.payload_of(out.strip().splitlines()[-1])
        self.assertEqual(set(payload), set(self.fixture))
        self.assertEqual(len(payload["roots"]), len(self.fixture["roots"]))
        self.assertEqual(set(payload["roots"][0]), set(self.fixture["roots"][0]) | {"hidden"})
        self.assertEqual(payload["generated_at"], "2026-11-14T13:24:00Z")

    def test_path_printed_alone_on_last_line(self):
        self.seed_roots()
        code, out, _ = self.run_cmd(report.cmd_report, report_args(session=ROOT))
        lines = out.rstrip("\n").splitlines()
        self.assertEqual(len(lines), 2)
        path = Path(lines[-1])
        self.assertTrue(path.is_absolute())
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, core.reports_dir().resolve())
        self.assertRegex(path.name, r"^agent-cost-\d{8}-\d{6}(-\d+)?\.html$")
        self.assertIn("focus 275f9bbd", lines[0])
        self.assertIn("5 herdr agents", lines[0])

    def test_out_and_no_open(self):
        self.seed_roots()
        out_path = self.base / "x" / "r.html"
        with mock.patch("webbrowser.open") as opened:
            code, out, _ = self.run_cmd(report.cmd_report, report_args(out=str(out_path), session=ROOT))
        opened.assert_not_called()
        self.assertEqual(out.strip().splitlines()[-1], str(out_path.resolve()))
        self.assertTrue(out_path.is_file())

    def test_open_uses_focus_anchor(self):
        self.seed_roots()
        root, child = self.child_sid(GRANDCHILD)
        registry.save_session({"session_id": child["session_id"], "root_session_id": root})
        with mock.patch("webbrowser.open") as opened:
            self.run_cmd(report.cmd_report, report_args(session=child["session_id"], no_open=False))
        url = opened.call_args[0][0]
        self.assertTrue(url.startswith("file://"))
        self.assertTrue(url.endswith("#" + child["session_id"]))

    def test_focus_from_child_sid_in_env(self):
        self.seed_roots()
        root, child = self.child_sid(GRANDCHILD)
        registry.save_session({"session_id": child["session_id"], "root_session_id": root})
        os.environ["CLAUDE_CODE_SESSION_ID"] = child["session_id"]
        code, out, _ = self.run_cmd(report.cmd_report, report_args())
        payload = self.payload_of(out.strip().splitlines()[-1])
        self.assertEqual(payload["focus_root_session_id"], root)

    def test_agent_cost_session_env_wins(self):
        self.seed_roots()
        other = "ce6ac914-daa3-42a0-9d24-82a182031562"
        registry.save_session({"session_id": other, "root_session_id": other})
        os.environ["CLAUDE_CODE_SESSION_ID"] = ROOT
        os.environ["AGENT_COST_SESSION"] = other
        code, out, _ = self.run_cmd(report.cmd_report, report_args())
        self.assertEqual(self.payload_of(out.strip().splitlines()[-1])["focus_root_session_id"], other)

    def test_default_focus_is_latest_root(self):
        self.seed_roots()
        code, out, _ = self.run_cmd(report.cmd_report, report_args())
        latest = report.aggregate.list_roots()[0]["root_session_id"]
        self.assertEqual(self.payload_of(out.strip().splitlines()[-1])["focus_root_session_id"], latest)

    def test_unknown_session_fails(self):
        self.seed_roots()
        with mock.patch.object(report, "_quiet_scan"):
            code, out, err = self.run_cmd(report.cmd_report, report_args(session="deadbeef-0000"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_old_roots_dropped_unless_all(self):
        self.seed_roots()
        old = dict(self.fixture["roots"][-1])
        old["root_session_id"] = "old0old0-0000-4000-8000-000000000000"
        old["sessions"] = [dict(s, session_id=old["root_session_id"], started_at="2026-01-01T10:00:00Z",
                                last_activity_at="2026-01-01T11:00:00Z", ended_at="2026-01-01T11:00:00Z")
                           for s in old["sessions"][:1]]
        core.write_json_atomic(core.roots_dir() / f"{old['root_session_id']}.json", old)
        _, out, _ = self.run_cmd(report.cmd_report, report_args(latest=True))
        ids = {r["root_session_id"] for r in self.payload_of(out.strip().splitlines()[-1])["roots"]}
        self.assertNotIn(old["root_session_id"], ids)
        _, out, _ = self.run_cmd(report.cmd_report, report_args(all=True))
        ids = {r["root_session_id"] for r in self.payload_of(out.strip().splitlines()[-1])["roots"]}
        self.assertIn(old["root_session_id"], ids)

    def test_prune_keeps_newest_20(self):
        d = core.reports_dir()
        for i in range(25):
            p = d / f"agent-cost-20260101-0000{i:02d}.html"
            p.write_text("x")
            os.utime(p, (1000 + i, 1000 + i))
        (d / "other.html").write_text("keep")
        report.prune_reports()
        left = sorted(p.name for p in d.glob("agent-cost-*.html"))
        self.assertEqual(len(left), 20)
        self.assertEqual(left[0], "agent-cost-20260101-000005.html")
        self.assertTrue((d / "other.html").exists())

    def test_report_prunes_after_write(self):
        self.seed_roots()
        d = core.reports_dir()
        for i in range(22):
            p = d / f"agent-cost-20200101-0000{i:02d}.html"
            p.write_text("x")
            os.utime(p, (1000 + i, 1000 + i))
        _, out, _ = self.run_cmd(report.cmd_report, report_args(latest=True))
        self.assertEqual(len(list(d.glob("agent-cost-*.html"))), 20)
        self.assertTrue(Path(out.strip().splitlines()[-1]).is_file())


class InjectTest(unittest.TestCase):
    def test_escapes_script_and_comment(self):
        data = {"x": "</script><script>alert(1)</script>", "y": "<!-- c -->"}
        html = report.inject("<script>const DATA = /*__DATA__*/null;</script>", data)
        body = html[len("<script>"):-len("</script>")]
        self.assertNotIn("</script", body)
        self.assertNotIn("<!--", body)
        payload = body[len("const DATA = "):-1]
        self.assertEqual(json.loads(payload), data)

    def test_missing_placeholder(self):
        with self.assertRaises(ValueError):
            report.inject("<html></html>", {})

    def test_real_template_has_placeholder(self):
        self.assertIn(report.PLACEHOLDER, report.template_path().read_text(encoding="utf-8"))


class ProjectRootsTest(ReportTestBase):
    def test_defaults_keep_existing_dirs(self):
        home = Path(os.environ["HOME"])
        (home / "projects").mkdir()
        self.assertEqual(report.project_roots(), [{"name": "projects", "path": str(home / "projects")}])
        (home / "Desktop" / "npm-packages").mkdir(parents=True)
        self.assertEqual([r["name"] for r in report.project_roots()], ["projects", "npm-packages"])

    def test_config_overrides_defaults(self):
        (Path(os.environ["HOME"]) / "projects").mkdir()
        Path(os.environ["AGENT_COST_CONFIG"]).write_text(json.dumps(
            {"project_roots": [{"name": "work", "path": "~/work"}, "~/src", {"name": "bad"}]}))
        home = os.environ["HOME"]
        self.assertEqual(report.project_roots(), [{"name": "work", "path": f"{home}/work"},
                                                  {"name": "src", "path": f"{home}/src"}])

    def test_broken_config_falls_back(self):
        Path(os.environ["AGENT_COST_CONFIG"]).write_text("{not json")
        self.assertEqual(report.project_roots(), [])


class RefreshTest(ReportTestBase):
    def test_refresh_writes_recent_roots(self):
        sid = "aaaaaaaa-0000-4000-8000-000000000001"
        child = "aaaaaaaa-0000-4000-8000-000000000002"
        registry.save_session({"session_id": sid, "root_session_id": sid, "kind": "main", "depth": 0,
                               "cwd": "/tmp/p", "started_at": NOW - 600, "last_activity_at": NOW - 60})
        registry.save_session({"session_id": child, "root_session_id": sid, "parent_session_id": sid,
                               "kind": "herdr", "depth": 1, "started_at": NOW - 500, "last_activity_at": NOW - 100})
        def window_of(scanned):
            scanned.assert_called_once()
            return round((core.now() - scanned.call_args.kwargs["cutoff"]) / 86400)

        with mock.patch.object(report.scan, "run_scan", return_value={"roots": set()}) as scanned:
            report.refresh()
        self.assertEqual(window_of(scanned), report.PAYLOAD_DAYS)
        data = core.read_json(core.roots_dir() / f"{sid}.json")
        self.assertEqual({s["session_id"] for s in data["sessions"]}, {sid, child})
        with mock.patch.object(report.scan, "run_scan", return_value={"roots": set()}) as scanned:
            report.refresh()
        self.assertEqual(window_of(scanned), 2)

    def test_resolve_focus_from_child(self):
        root = "aaaaaaaa-0000-4000-8000-000000000001"
        child = "aaaaaaaa-0000-4000-8000-000000000003"
        registry.save_session({"session_id": child, "root_session_id": root, "parent_session_id": root})
        self.assertEqual(report.resolve_focus(child), (root, child))
        self.assertEqual(report.resolve_focus("nope-0000"), (None, None))


class ListShowTest(ReportTestBase):
    def test_list_table(self):
        self.seed_roots()
        code, out, _ = self.run_cmd(report.cmd_list, argparse.Namespace(limit=3, json=False, all=True))
        lines = out.strip().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 4)
        for col in ("START", "PROJECT", "STATE", "DURATION", "TOTAL", "AGENTS"):
            self.assertIn(col, lines[0])
        self.assertIn("$", lines[1])

    def test_list_json(self):
        self.seed_roots()
        code, out, _ = self.run_cmd(report.cmd_list, argparse.Namespace(limit=50, json=True, all=True))
        rows = json.loads(out)
        self.assertEqual(len(rows), len(self.fixture["roots"]))
        row = next(r for r in rows if r["root_session_id"] == ROOT)
        self.assertEqual(row["project"], "npm-packages")
        self.assertEqual(row["herdr_agents"], 5)
        self.assertEqual(row["state"], "live")

    def test_list_empty(self):
        code, out, _ = self.run_cmd(report.cmd_list, argparse.Namespace(limit=20, json=False))
        self.assertEqual(out.strip(), "no sessions recorded")

    def test_show_tree(self):
        self.seed_roots()
        code, out, _ = self.run_cmd(report.cmd_show, argparse.Namespace(sid=ROOT, latest=False, json=False))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith(ROOT))
        self.assertTrue(lines[1].startswith("main  opus-5-5/high"))
        _, grand = self.child_sid(GRANDCHILD)
        self.assertTrue(any(l.startswith("│  ├─ ") and grand["herdr_id"] in l for l in lines))
        self.assertTrue(any("incl." in l and "[Explore]" in l for l in lines))
        self.assertEqual(sum(1 for l in lines if "incl." not in l) - 1, 6)

    def test_show_from_child_and_json(self):
        self.seed_roots()
        root, child = self.child_sid(GRANDCHILD)
        registry.save_session({"session_id": child["session_id"], "root_session_id": root})
        with mock.patch.object(report, "load_root_data", lambda r: core.read_json(core.roots_dir() / f"{r}.json")):
            code, out, _ = self.run_cmd(report.cmd_show,
                                        argparse.Namespace(sid=child["session_id"], latest=False, json=True))
        self.assertEqual(json.loads(out)["root_session_id"], root)

    def test_show_missing(self):
        code, _, err = self.run_cmd(report.cmd_show, argparse.Namespace(sid="zzzz-0000", latest=False, json=False))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()

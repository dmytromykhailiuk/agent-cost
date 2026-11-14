import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import core, registry, setup  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "statusline"
BIN = ROOT / "skills" / "agent-cost" / "bin" / "agent-cost"
HAS_NODE = shutil.which("node") is not None


def call(func, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = func(argparse.Namespace(**kw))
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(HAS_NODE, "node not installed")
class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for name in ("dashboard.js", "details.js"):
            shutil.copy(FIX / name, self.dir / name)
        self.dash = self.dir / "dashboard.js"
        self.original = self.dash.read_text(encoding="utf-8")

    def backups(self):
        return list(self.dir.glob("dashboard.js.bak-*"))

    def test_dry_run_prints_diff_and_keeps_file(self):
        code, out, _ = call(setup.cmd_setup, apply=False, dashboard=str(self.dash))
        self.assertEqual(code, 0)
        self.assertIn("+function agentCost(d)", out)
        self.assertIn("+  if (!(ac && ac.line)) if (d.cost?.total_cost_usd", out)
        self.assertIn("+  if (ac && ac.line) lines.push(ac.line);", out)
        self.assertEqual(self.dash.read_text(encoding="utf-8"), self.original)
        self.assertEqual(self.backups(), [])

    def test_apply_is_valid_js_and_idempotent(self):
        code, out, _ = call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        self.assertEqual(code, 0, out)
        patched = self.dash.read_text(encoding="utf-8")
        self.assertEqual(patched.count("// agent-cost:begin"), 1)
        self.assertIn("const ac = agentCost(d);", patched)
        self.assertIn("ac.label", patched)
        self.assertRegex(patched, r"if \(!\(ac && ac\.line\)\) if \(d\.cost\?\.total_cost_usd != null\) parts\.push")
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(encoding="utf-8"), self.original)
        self.assertEqual(subprocess.run(["node", "--check", str(self.dash)]).returncode, 0)
        code, out, _ = call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        self.assertEqual(code, 0)
        self.assertIn("already applied", out)
        self.assertEqual(self.dash.read_text(encoding="utf-8"), patched)
        self.assertEqual(len(self.backups()), 1)

    def test_old_format_is_upgraded_in_place(self):
        old = self.original
        snippet = setup.SNIPPET.read_text(encoding="utf-8")
        func = setup.FUNC_RE.search(old).start()
        prof = setup.PROFILE_RE.search(old)
        cost = setup.COST_RE.search(old)
        orig_cost = cost.group(0).strip()
        old = old[:cost.start()] + f"  if (ac && ac.segment) parts.push(ac.segment);\n  else {orig_cost}" + old[cost.end():]
        old = old[:prof.end()] + "\n  const ac = agentCost(d);" + old[prof.end():]
        old = old[:func] + snippet.replace("ac.line", "ac.segment") + "\n" + old[func:]
        old = old.replace(setup.LABEL_ANCHOR, setup.LABEL_PATCHED, 1)
        self.dash.write_text(old, encoding="utf-8")
        code, out, _ = call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        self.assertEqual(code, 0, out)
        upgraded = self.dash.read_text(encoding="utf-8")
        self.assertNotIn("ac.segment", upgraded)
        self.assertEqual(upgraded.count("// agent-cost:begin"), 1)
        self.assertEqual(upgraded.count("const ac = agentCost(d);"), 1)
        self.assertEqual(setup.revert_patch(upgraded), self.original)
        self.assertEqual(subprocess.run(["node", "--check", str(self.dash)]).returncode, 0)

    def test_apply_writes_cli_pointer(self):
        with mock.patch.dict(os.environ, {"AGENT_COST_STATE_DIR": str(self.dir / "state")}):
            call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        self.assertTrue((self.dir / "state" / "cli.json").is_file())

    def test_rollback_when_node_check_fails(self):
        with mock.patch.object(setup, "node_check", return_value=(False, "SyntaxError")):
            code, _, err = call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        self.assertEqual(code, 1)
        self.assertIn("restored", err)
        self.assertEqual(self.dash.read_text(encoding="utf-8"), self.original)

    def test_missing_anchor_fails_clearly(self):
        self.dash.write_text(self.original.replace("claudeProfile()", "otherProfile()"), encoding="utf-8")
        code, _, err = call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        self.assertEqual(code, 1)
        self.assertIn("anchor not found", err)
        self.assertEqual(self.backups(), [])

    def test_missing_file(self):
        code, _, err = call(setup.cmd_setup, apply=False, dashboard=str(self.dir / "nope.js"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)

    def test_patched_dashboard_renders_session_line(self):
        state = self.dir / "state"
        env_state = {"AGENT_COST_STATE_DIR": str(state)}
        with mock.patch.dict(os.environ, env_state):
            core.write_cli_pointer()
        call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        env = {"PATH": os.environ["PATH"], "HOME": str(self.dir / "home"), "COLUMNS": "400", **env_state}
        stdin = (FIX / "last-input.json").read_text(encoding="utf-8")
        res = subprocess.run(["node", str(self.dash)], input=stdin, capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = [__import__("re").sub(r"\x1b\[[0-9;]*m", "", ln) for ln in res.stdout.splitlines()]
        self.assertNotIn("$0.63", lines[0])
        self.assertTrue(lines[1].startswith("session "), lines[1])
        self.assertIn("Σ $0.63", lines[1])
        self.assertNotIn("statusline error", res.stdout)

    def test_dashboard_falls_back_without_cli_pointer(self):
        call(setup.cmd_setup, apply=True, dashboard=str(self.dash))
        env = {"PATH": os.environ["PATH"], "HOME": str(self.dir / "home"), "COLUMNS": "400",
               "AGENT_COST_STATE_DIR": str(self.dir / "empty")}
        stdin = (FIX / "last-input.json").read_text(encoding="utf-8")
        res = subprocess.run(["node", str(self.dash)], input=stdin, capture_output=True, text=True, env=env)
        first = __import__("re").sub(r"\x1b\[[0-9;]*m", "", res.stdout.splitlines()[0])
        self.assertIn("$0.63", first)
        self.assertNotIn("Σ", res.stdout)


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"AGENT_COST_STATE_DIR": str(self.dir / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_doctor_reports_each_check(self):
        dash = self.dir / "dashboard.js"
        shutil.copy(FIX / "dashboard.js", dash)
        core.write_cli_pointer()
        registry.save_session({"session_id": "s1", "root_session_id": "s1", "hook_events": {"stop": core.now()}})
        code, out, _ = call(setup.cmd_doctor, dashboard=str(dash))
        self.assertEqual(code, 1)
        self.assertIn("ok   python", out)
        self.assertIn("ok   cli.json pointer", out)
        self.assertIn("ok   hooks", out)
        self.assertIn("FAIL dashboard.js", out)
        call(setup.cmd_setup, apply=True, dashboard=str(dash))
        code, out, _ = call(setup.cmd_doctor, dashboard=str(dash))
        self.assertEqual(code, 0, out)

    def test_doctor_empty_state(self):
        code, out, _ = call(setup.cmd_doctor, dashboard=str(self.dir / "none.js"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL cli.json pointer: missing", out)
        self.assertIn("FAIL hooks", out)


if __name__ == "__main__":
    unittest.main()

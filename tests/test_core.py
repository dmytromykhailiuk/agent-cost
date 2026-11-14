import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import core  # noqa: E402


class StateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {k: os.environ.get(k) for k in ("AGENT_COST_STATE_DIR", "XDG_STATE_HOME")}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_override(self):
        target = Path(self.tmp.name) / "state"
        os.environ["AGENT_COST_STATE_DIR"] = str(target)
        self.assertEqual(core.state_dir(), target)
        self.assertTrue(target.is_dir())
        self.assertEqual(core.sessions_dir("root1"), target / "sessions" / "root1")
        self.assertTrue((target / "sessions" / "root1").is_dir())
        for func, name in ((core.roots_dir, "roots"), (core.cache_dir, "cache"), (core.reports_dir, "reports")):
            self.assertEqual(func(), target / name)
            self.assertTrue((target / name).is_dir())

    def test_xdg_fallback(self):
        os.environ.pop("AGENT_COST_STATE_DIR", None)
        os.environ["XDG_STATE_HOME"] = self.tmp.name
        self.assertEqual(core.state_dir(), Path(self.tmp.name) / "agent-cost")

    def test_cli_pointer_written_once(self):
        os.environ["AGENT_COST_STATE_DIR"] = self.tmp.name
        self.assertTrue(core.write_cli_pointer())
        data = core.read_json(Path(self.tmp.name) / "cli.json")
        self.assertTrue(data["path"].endswith("bin/agent-cost"))
        self.assertIn("updated_at", data)
        self.assertFalse(core.write_cli_pointer())


class JsonTests(unittest.TestCase):
    def test_atomic_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b.json"
            core.write_json_atomic(path, {"x": 1, "t": "é"})
            self.assertEqual(core.read_json(path), {"x": 1, "t": "é"})
            core.write_json_atomic(path, {"x": 2})
            self.assertEqual(core.read_json(path), {"x": 2})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["b.json"])

    def test_atomic_write_failure_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            core.write_json_atomic(path, {"ok": True})
            with self.assertRaises(TypeError):
                core.write_json_atomic(path, {"bad": object()})
            self.assertEqual(core.read_json(path), {"ok": True})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["b.json"])

    def test_read_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(core.read_json(Path(tmp) / "missing.json", {"d": 1}), {"d": 1})
            bad = Path(tmp) / "bad.json"
            bad.write_text("{nope")
            self.assertIsNone(core.read_json(bad))


class TimeTests(unittest.TestCase):
    def test_parse_ts_formats(self):
        self.assertEqual(core.parse_ts("2026-01-02T03:04:05Z"), 1767323045.0)
        self.assertEqual(core.parse_ts("2026-01-02T03:04:05.500+00:00"), 1767323045.5)
        self.assertEqual(core.parse_ts(1767323045), 1767323045.0)
        self.assertEqual(core.parse_ts("1767323045"), 1767323045.0)
        self.assertEqual(core.parse_ts(1767323045000), 1767323045.0)
        self.assertEqual(core.parse_ts("1767323045000"), 1767323045.0)

    def test_parse_ts_invalid(self):
        for value in (None, "", "garbage", True, float("nan")):
            self.assertIsNone(core.parse_ts(value))

    def test_iso(self):
        self.assertEqual(core.iso(1767323045), "2026-01-02T03:04:05Z")
        self.assertIsNone(core.iso(None))
        self.assertIsNone(core.iso("x"))


class ExcerptTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(core.excerpt("a \n\n b\t c "), "a b c")

    def test_strips_pasted_tags_keeps_inner(self):
        text = 'before <pasted_content id="1">inner  text</pasted_content id="1"> after'
        self.assertEqual(core.excerpt(text), "before inner text after")

    def test_truncates(self):
        out = core.excerpt("x" * 100, limit=10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_empty(self):
        self.assertEqual(core.excerpt(None), "")


if __name__ == "__main__":
    unittest.main()

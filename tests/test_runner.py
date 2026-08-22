import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "run_pr_reviews.py"
SPEC = importlib.util.spec_from_file_location("run_pr_reviews", MODULE)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_duration_format(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(runner.duration(start, start + timedelta(minutes=125)), "2h 5m")

    def test_clean_proxy_restores_environment(self):
        original = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://example.invalid"
        with runner.clean_proxy(True):
            self.assertNotIn("HTTP_PROXY", os.environ)
        self.assertEqual(os.environ["HTTP_PROXY"], "http://example.invalid")
        if original is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = original

    @unittest.skipIf(os.name == "nt", "Windows file-lock integration is covered by the manual launcher test")
    def test_second_lock_is_rejected(self):
        temporary_root = Path(__file__).parent / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            first = runner.RunLock(Path(directory) / "runner.lock")
            second = runner.RunLock(Path(directory) / "runner.lock")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
            second.close()

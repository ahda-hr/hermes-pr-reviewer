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

    def test_review_prompt_renders_selected_skills(self):
        template = "PR {{pr_number}} base {{base_ref}}\n{{selected_skills}}"
        prompt = runner.render_review_prompt(template, 42, "refs/pr/42/base", ["review", "security"])
        self.assertIn("PR 42 base refs/pr/42/base", prompt)
        self.assertIn("$review, $security", prompt)

    def test_review_prompt_rejects_unknown_placeholders(self):
        with self.assertRaisesRegex(RuntimeError, "{{unknown}}"):
            runner.render_review_prompt("{{unknown}}", 42, "refs/pr/42/base", [])

    def test_shared_review_template_declares_report_shape(self):
        template = runner.load_review_template()
        self.assertIn("## 🔴 Blocking issues", template)
        self.assertIn("## 🟡 Important issues", template)
        self.assertIn("## 🟢 Ternary refactor suggestions", template)
        self.assertIn("**Copyable suggested reply:**", template)

    def test_configured_skills_are_unique(self):
        self.assertEqual(runner.configured_skill_names({"codexSkills": ["review", "review", "security"]}), ["review", "security"])

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

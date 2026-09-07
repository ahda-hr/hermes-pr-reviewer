import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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

    def test_terminal_notifications_do_not_require_hermes(self):
        with patch.object(runner.shutil, "which", side_effect=lambda name: None if name == "hermes" else f"/{name}"):
            runner.require_tools("terminal")
        with patch.object(runner.shutil, "which", side_effect=lambda name: None if name == "hermes" else f"/{name}"):
            with self.assertRaisesRegex(RuntimeError, "hermes"):
                runner.require_tools("hermes")

    def test_notification_settings_validate_provider_configuration(self):
        self.assertEqual(runner.notification_settings({"notifications": {"provider": "terminal"}}), ("terminal", {}))
        self.assertEqual(
            runner.notification_settings({"notifications": {"provider": "hermes", "hermes": {"profile": "reviewer", "target": "telegram"}}}),
            ("hermes", {"profile": "reviewer", "target": "telegram"}),
        )
        with self.assertRaisesRegex(RuntimeError, "provider"):
            runner.notification_settings({})

    def test_terminal_trigger_overrides_configured_hermes_notifications(self):
        self.assertEqual(runner.active_notification_provider("hermes", "terminal"), "terminal")
        self.assertEqual(runner.active_notification_provider("hermes", "scheduled"), "hermes")

    def test_hermes_notifier_sends_messages_and_attachments(self):
        calls: list[list[str]] = []

        def fake_command(args, **_kwargs):
            calls.append(args)
            return ""

        config = {"notifications": {"provider": "hermes", "hermes": {"profile": "reviewer", "target": "telegram"}}}
        with patch.object(runner, "command", side_effect=fake_command):
            send, provider = runner.notifier(config, "hermes")
            send("finished", [Path("report.md")])
        self.assertEqual(provider, "hermes")
        self.assertEqual(calls[0], ["hermes", "-p", "reviewer", "send", "--to", "telegram", "finished", "--quiet"])
        self.assertEqual(calls[1], ["hermes", "-p", "reviewer", "send", "--to", "telegram", "MEDIA:report.md", "--quiet"])

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

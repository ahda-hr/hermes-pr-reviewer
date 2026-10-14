import importlib.util
import json
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

VALID_FINDING = {
    "category": "blocking",
    "severity": "blocking",
    "path": "src/app.py",
    "startLine": 12,
    "endLine": 13,
    "title": "Validate input",
    "explanation": "Untrusted input can reach the database.",
    "excerpt": "query = request.args['query']",
    "suggestedFix": "Use parameterized queries.",
    "copyableReply": "Please validate this input before it reaches the database.",
}


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
        self.assertIn('"findings"', template)
        self.assertIn('"copyableReply"', template)
        self.assertIn("blocking | important | ternary", template)

    def test_configured_skills_are_unique(self):
        self.assertEqual(runner.configured_skill_names({"codexSkills": ["review", "review", "security"]}), ["review", "security"])

    def test_agent_config_defaults_and_validation(self):
        self.assertEqual(runner.agent_config({}), {"provider": "codex", "model": None})
        self.assertEqual(runner.agent_config({"codexModel": "m"})["model"], "m")
        self.assertEqual(runner.agent_config({"agent": {"provider": "claude", "model": "sonnet"}}), {"provider": "claude", "model": "sonnet"})
        with self.assertRaisesRegex(RuntimeError, "provider"):
            runner.agent_config({"agent": {"provider": "other"}})

    def test_claude_skills_use_their_own_config_key_and_plain_names(self):
        self.assertEqual(runner.configured_skill_names({"claudeSkills": ["review"], "codexSkills": ["x"]}, "claude"), ["review"])
        prompt = runner.render_review_prompt("{{selected_skills}}", 1, "base", ["review"], "claude")
        self.assertIn("Claude skills", prompt)
        self.assertNotIn("$review", prompt)

    def test_claude_agent_writes_unfenced_result(self):
        envelope = json.dumps({"is_error": False, "result": "```json\n{\"findings\": []}\n```"})
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, "command", return_value=envelope) as run:
            raw = Path(tmp) / "raw.json"
            runner.run_review_agent({"provider": "claude", "model": "sonnet"}, Path(tmp), "prompt", raw, False)
            self.assertEqual(json.loads(raw.read_text()), {"findings": []})
            self.assertEqual(run.call_args.args[0][0], "claude")
            self.assertIn("sonnet", run.call_args.args[0])

    def test_terminal_format_adds_stamp_tag_and_report_path(self):
        out = runner.format_terminal("[FINISHED] o/r PR #1 in 2m - 1 important", [Path("/x/report.md")], stamp="12:00:00")
        self.assertEqual(out.splitlines()[0], "12:00:00  FINISHED  o/r PR #1 in 2m - 1 important".replace("FINISHED  ", "FINISHED "))
        self.assertIn("report: /x/report.md", out)
        self.assertIn("\033[", runner.format_terminal("[FAILED] x", color=True))
        self.assertNotIn("\033[", runner.format_terminal("[FAILED] x"))

    def test_terminal_format_lists_summary_items(self):
        out = runner.format_terminal("PR review completed: #1 reviewed; #2 failed: boom")
        self.assertEqual(out.splitlines(), ["Done", "  - #1 reviewed", "  - #2 failed: boom"])

    def test_finding_counts(self):
        self.assertEqual(runner.finding_counts([]), "no findings")
        self.assertEqual(runner.finding_counts([{"severity": "blocking"}, {"severity": "suggestion"}, {"severity": "suggestion"}]), "1 blocking, 2 suggestion(s)")

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

    def test_github_review_mode_defaults_to_off_and_validates_pending(self):
        self.assertEqual(runner.github_review_mode({}), "off")
        self.assertEqual(runner.github_review_mode({"githubReview": {"mode": "pending"}}), "pending")
        with self.assertRaisesRegex(RuntimeError, "mode"):
            runner.github_review_mode({"githubReview": {"mode": "publish"}})

    def test_pull_requests_are_processed_by_ascending_number(self):
        pull_requests = [{"number": 104}, {"number": "8"}, {"number": 42}]
        ordered = runner.order_pull_requests(pull_requests)
        self.assertEqual([pull_request["number"] for pull_request in ordered], ["8", 42, 104])

    def test_open_pull_requests_request_oldest_first(self):
        with patch.object(runner, "command", return_value='[{"number": 12}, {"number": 3}]') as command:
            pull_requests = runner.list_open_pull_requests("owner/repo", 10, False)
        self.assertEqual([pull_request["number"] for pull_request in pull_requests], [3, 12])
        self.assertIn("sort:created-asc", command.call_args.args[0])
        self.assertIn("--limit", command.call_args.args[0])
        self.assertEqual(command.call_args.args[0][command.call_args.args[0].index("--limit") + 1], "10")

    def test_review_findings_validate_and_render_report(self):
        findings = runner.validate_review_findings({"findings": [VALID_FINDING]})
        report = runner.render_review_report(findings)
        self.assertIn("## 🔴 Blocking issues", report)
        self.assertIn("## 🟡 Important issues\n\nNone found.", report)
        self.assertIn("**Copyable suggested reply:**\n> Please validate this input before it reaches the database.", report)
        self.assertIn("## 🟢 Anti-pattern refactor suggestions", report)

    def test_review_findings_reject_invalid_category_and_path(self):
        invalid = {**VALID_FINDING, "category": "unknown"}
        with self.assertRaisesRegex(RuntimeError, "category"):
            runner.validate_review_findings({"findings": [invalid]})
        invalid = {**VALID_FINDING, "path": "../secrets.py"}
        with self.assertRaisesRegex(RuntimeError, "path"):
            runner.validate_review_findings({"findings": [invalid]})

    def test_diff_mapping_and_pending_comments_use_copyable_reply_verbatim(self):
        diff = """diff --git a/src/app.py b/src/app.py
index 123..456 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,2 +10,3 @@ def app():
 context
+query = request.args['query']
 return context
"""
        findings = runner.validate_review_findings({"findings": [VALID_FINDING]})
        comments, skipped = runner.pending_review_comments(findings, runner.parse_diff_lines(diff))
        self.assertEqual(skipped, 0)
        self.assertEqual(comments, [{"path": "src/app.py", "line": 12, "side": "RIGHT", "body": VALID_FINDING["copyableReply"]}])
        unmappable = {**VALID_FINDING, "startLine": 99, "endLine": 99}
        comments, skipped = runner.pending_review_comments(runner.validate_review_findings({"findings": [unmappable]}), runner.parse_diff_lines(diff))
        self.assertEqual(comments, [])
        self.assertEqual(skipped, 1)

    def test_pending_review_payload_has_no_submit_event(self):
        calls: list[tuple[list[str], str | None]] = []

        def fake_command(args, **kwargs):
            calls.append((args, kwargs.get("input_text")))
            return '{"id": 42, "state": "PENDING", "html_url": "https://example.test/review/42"}'

        comment = {"path": "src/app.py", "line": 12, "side": "RIGHT", "body": VALID_FINDING["copyableReply"]}
        with patch.object(runner, "command", side_effect=fake_command):
            review = runner.create_pending_review("owner/repo", 7, "abc123", [comment], False)
        self.assertEqual(review["id"], 42)
        self.assertEqual(calls[0][0], ["gh", "api", "--method", "POST", "repos/owner/repo/pulls/7/reviews", "--input", "-"])
        payload = __import__("json").loads(calls[0][1])
        self.assertNotIn("event", payload)
        self.assertEqual(payload["body"], runner.GITHUB_REVIEW_MARKER)
        self.assertEqual(payload["comments"][0]["body"], VALID_FINDING["copyableReply"])

    def test_tool_reviews_do_not_count_as_manual_activity(self):
        response = {
            "data": {"repository": {"pullRequest": {
                "reviews": {"nodes": [{"author": {"login": "me"}, "body": runner.GITHUB_REVIEW_MARKER, "state": "PENDING", "commit": {"oid": "abc"}}]},
                "comments": {"nodes": []},
                "reviewThreads": {"nodes": [{"comments": {"nodes": [{"author": {"login": "me"}, "pullRequestReview": {"body": runner.GITHUB_REVIEW_MARKER}}]}}]},
            }}}}
        with patch.object(runner, "command", return_value=__import__("json").dumps(response)):
            activity = runner.pull_request_activity("owner/repo", 7, "me", False)
        self.assertFalse(activity["manualActivity"])
        self.assertTrue(runner.has_pending_tool_review(activity))
        self.assertFalse(runner.has_submitted_tool_review_for_commit(activity, "abc"))

    def test_manual_activity_blocks_follow_up_but_submitted_tool_review_does_not(self):
        tool_review = {"author": {"login": "me"}, "body": runner.GITHUB_REVIEW_MARKER, "state": "COMMENTED", "commit": {"oid": "abc"}}
        manual_review = {"author": {"login": "me"}, "body": "Please rename this method.", "state": "COMMENTED", "commit": {"oid": "abc"}}
        base = {"comments": {"nodes": []}, "reviewThreads": {"nodes": []}}
        response = {"data": {"repository": {"pullRequest": {**base, "reviews": {"nodes": [tool_review]}}}}}
        with patch.object(runner, "command", return_value=__import__("json").dumps(response)):
            activity = runner.pull_request_activity("owner/repo", 7, "me", False)
        self.assertFalse(activity["manualActivity"])
        self.assertTrue(runner.has_submitted_tool_review_for_commit(activity, "abc"))
        response["data"]["repository"]["pullRequest"]["reviews"]["nodes"].append(manual_review)
        with patch.object(runner, "command", return_value=__import__("json").dumps(response)):
            activity = runner.pull_request_activity("owner/repo", 7, "me", False)
        self.assertTrue(activity["manualActivity"])

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

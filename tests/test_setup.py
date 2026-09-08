import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
SPEC = importlib.util.spec_from_file_location("review_setup", MODULE)
setup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def test_time_validation_normalizes_values(self):
        self.assertEqual(setup.validate_time("05:00"), "05:00")
        self.assertEqual(setup.validate_time("15:30"), "15:30")

    def test_time_validation_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            setup.validate_time("25:00")
        with self.assertRaises(ValueError):
            setup.validate_time("9am")

    def test_cron_for_weekdays_and_every_day(self):
        self.assertEqual(setup.cron_for("05:00", "weekdays"), "0 5 * * 1-5")
        self.assertEqual(setup.cron_for("15:30", "every day"), "30 15 * * *")

    def test_collect_schedules_accepts_multiple_unique_times(self):
        answers = iter(["every day", "05:00", "yes", "15:30", "no"])
        with patch.object(setup, "ask", side_effect=lambda *_args, **_kwargs: next(answers)):
            self.assertEqual(
                setup.collect_schedules(),
                [
                    ("PR review every day 05:00", "every day", "05:00"),
                    ("PR review every day 15:30", "every day", "15:30"),
                ],
            )

    def test_skill_selection_deduplicates_numbers(self):
        with patch.object(setup, "ask", return_value="2,1,2"):
            self.assertEqual(setup.select_codex_skills(["review", "security"]), ["security", "review"])

    def test_calendar_for_weekdays_and_every_day(self):
        self.assertEqual(setup.calendar_for("05:00", "weekdays"), "Mon..Fri *-*-* 05:00:00")
        self.assertEqual(setup.calendar_for("15:30", "every day"), "*-*-* 15:30:00")

    def test_legacy_cron_cleanup_preserves_unrelated_entries(self):
        existing = "MAILTO=me@example.test\n" + setup.CRON_BEGIN + "\n0 1 * * * /old-review\n" + setup.CRON_END + "\n0 2 * * * /other-job\n"
        remaining = setup.remove_managed_cron_block(existing)
        self.assertEqual(remaining, "MAILTO=me@example.test\n0 2 * * * /other-job")

    def test_systemd_units_include_service_runner_logging_and_schedule(self):
        service = setup.systemd_service_contents()
        timer = setup.systemd_timer_contents([
            ("PR review weekdays 05:00", "weekdays", "05:00"),
            ("PR review every day 15:30", "every day", "15:30"),
        ])
        self.assertIn("WorkingDirectory=", service)
        self.assertIn("--trigger scheduled", service)
        self.assertIn("pipefail", service)
        self.assertIn("scheduled-run.log", service)
        self.assertIn("StandardOutput=journal", service)
        self.assertIn("OnCalendar=Mon..Fri *-*-* 05:00:00", timer)
        self.assertIn("OnCalendar=*-*-* 15:30:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=hermes-pr-reviewer.service", timer)

    def test_install_systemd_schedules_enables_lingering_then_timer(self):
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            service = temporary / setup.SYSTEMD_SERVICE_NAME
            timer = temporary / setup.SYSTEMD_TIMER_NAME
            with patch.object(setup, "systemd_unit_paths", return_value=(service, timer)), \
                 patch.object(setup, "require_commands"), \
                 patch.object(setup, "run", side_effect=lambda arguments, **_kwargs: calls.append(arguments)), \
                 patch.object(setup.getpass, "getuser", return_value="reviewer"):
                setup.install_systemd_schedules([("PR review weekdays 05:00", "weekdays", "05:00")])
            self.assertTrue(service.exists())
            self.assertTrue(timer.exists())
        self.assertEqual(calls, [
            ["loginctl", "enable-linger", "reviewer"],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", setup.SYSTEMD_TIMER_NAME],
        ])

    def test_remove_systemd_schedules_disables_exact_timer_and_removes_units(self):
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            service = temporary / setup.SYSTEMD_SERVICE_NAME
            timer = temporary / setup.SYSTEMD_TIMER_NAME
            service.write_text("service", encoding="utf-8")
            timer.write_text("timer", encoding="utf-8")
            with patch.object(setup, "systemd_unit_paths", return_value=(service, timer)), \
                 patch.object(setup, "require_commands"), \
                 patch.object(setup, "run", side_effect=lambda arguments, **_kwargs: calls.append(arguments)):
                setup.remove_systemd_schedules()
            self.assertFalse(service.exists())
            self.assertFalse(timer.exists())
        self.assertEqual(calls, [
            ["systemctl", "--user", "disable", "--now", setup.SYSTEMD_TIMER_NAME],
            ["systemctl", "--user", "daemon-reload"],
        ])

    def test_legacy_cron_cleanup_skips_when_crontab_is_absent(self):
        with patch.object(setup.shutil, "which", return_value=None), patch.object(setup.subprocess, "run") as run:
            setup.remove_legacy_cron_schedule()
        run.assert_not_called()

    def test_extract_hermes_job_id(self):
        self.assertEqual(setup.extract_hermes_job_id("Created job: job-456"), "job-456")
        self.assertEqual(setup.extract_hermes_job_id("Created cron job ID: job-123"), "job-123")

import importlib.util
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
                    ("PR review every day 05:00", "0 5 * * *", "05:00"),
                    ("PR review every day 15:30", "30 15 * * *", "15:30"),
                ],
            )

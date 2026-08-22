#!/usr/bin/env python3
"""Interactive local setup for the Hermes PR reviewer. Never performs gh login."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ask(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    value = (getpass.getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ")).strip()
    return value or (default or "")


def run(arguments: list[str]) -> None:
    result = subprocess.run(arguments)
    if result.returncode:
        raise RuntimeError(f"Command failed: {' '.join(arguments)}")


def hermes_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "hermes"


def codex_skills_root() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    return Path(configured_home) / "skills" if configured_home else Path.home() / ".codex" / "skills"


def installed_skill_names() -> list[str]:
    root = codex_skills_root()
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if child.is_dir() and child.name != ".system" and (child / "SKILL.md").is_file())


def select_codex_skills(skills: list[str]) -> list[str]:
    if not skills:
        print("No user-installed Codex skills were found; continuing without additional skills.")
        return []
    print("Available user-installed Codex skills:")
    for index, skill in enumerate(skills, start=1):
        print(f"  {index}. {skill}")
    while True:
        selection = ask("Skill numbers to use for every PR review (comma-separated; blank for none)")
        if not selection:
            return []
        try:
            indexes = [int(value.strip()) for value in selection.split(",")]
        except ValueError:
            print("Enter comma-separated numbers from the list, or leave blank.")
            continue
        if not indexes or any(index < 1 or index > len(skills) for index in indexes):
            print("Each skill number must be from the displayed list.")
            continue
        return [skills[index - 1] for index in dict.fromkeys(indexes)]


def update_env(path: Path, updates: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                existing[key] = value
    existing.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in existing.items()), encoding="utf-8")


def update_config(path: Path, codex_skills: list[str]) -> None:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected an object in {path}")
    data["codexSkills"] = codex_skills
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def validate_time(value: str) -> str:
    """Return a zero-padded 24-hour time or raise ValueError."""
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError as error:
        raise ValueError("Enter a time in 24-hour HH:MM format, for example 05:00 or 15:30.") from error


def cron_for(time_of_day: str, cadence: str) -> str:
    hour, minute = validate_time(time_of_day).split(":")
    if cadence == "weekdays":
        return f"{int(minute)} {int(hour)} * * 1-5"
    if cadence == "every day":
        return f"{int(minute)} {int(hour)} * * *"
    raise ValueError(f"Unsupported cadence: {cadence}")


def collect_schedules() -> list[tuple[str, str, str]]:
    while True:
        cadence = ask("Run days (weekdays/every day)", "weekdays").lower()
        if cadence in ("weekdays", "every day"):
            break
        print("Choose either 'weekdays' or 'every day'.")
    schedules: list[tuple[str, str, str]] = []
    while True:
        try:
            time_of_day = validate_time(ask("Review time (HH:MM)"))
        except ValueError as error:
            print(error)
            continue
        name = f"PR review {cadence} {time_of_day}"
        if any(existing[0] == name for existing in schedules):
            print(f"{time_of_day} is already included.")
        else:
            schedules.append((name, cron_for(time_of_day, cadence), time_of_day))
        while True:
            more = ask("Add another review time? (yes/no)", "no").lower()
            if more in ("yes", "y", "no", "n"):
                break
            print("Answer yes or no.")
        if more in ("no", "n"):
            return schedules


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="reviewer")
    parser.add_argument("--skip-schedules", action="store_true")
    args = parser.parse_args()
    missing = [name for name in ("git", "gh", "codex", "hermes") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"Install these commands first: {', '.join(missing)}")
    config_path = ROOT / "config.json"
    if not config_path.exists():
        shutil.copyfile(ROOT / "config.example.json", config_path)
        print(f"Created {config_path}; edit its repository list before your first review.")
    token = ask("Telegram bot token", secret=True)
    user_id = ask("Your Telegram user ID")
    group_id = ask("Telegram review group ID")
    if not token or not user_id or not group_id:
        raise RuntimeError("Bot token, Telegram user ID, and group ID are required.")
    update_config(config_path, select_codex_skills(installed_skill_names()))
    try:
        run(["hermes", "profile", "create", args.profile])
    except RuntimeError:
        # Creating an existing profile is expected on a repeatable setup run.
        run(["hermes", "profile", "show", args.profile])
    profile_dir = hermes_data_dir() / "profiles" / args.profile
    update_env(profile_dir / ".env", {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ALLOWED_USERS": user_id,
        "TELEGRAM_HOME_CHANNEL": group_id,
    })
    quick_command = f'{sys.executable} "{ROOT / "scripts" / "start_pr_review.py"}" --trigger telegram --detach'
    run(["hermes", "-p", args.profile, "config", "set", "telegram.allowed_chats", json.dumps([user_id])])
    run(["hermes", "-p", args.profile, "config", "set", "telegram.require_mention", "true"])
    run(["hermes", "-p", args.profile, "config", "set", "telegram.exclusive_bot_mentions", "true"])
    run(["hermes", "-p", args.profile, "config", "set", "telegram.guest_mode", "false"])
    run(["hermes", "-p", args.profile, "config", "set", "display.background_process_notifications", "off"])
    run(["hermes", "-p", args.profile, "config", "set", "quick_commands.pr-review.type", "exec"])
    run(["hermes", "-p", args.profile, "config", "set", "quick_commands.pr-review.command", quick_command])
    profile_scripts = profile_dir / "scripts"
    profile_scripts.mkdir(parents=True, exist_ok=True)
    wrapper = profile_scripts / "pr-review-launcher.py"
    wrapper.write_text(f'#!/usr/bin/env python3\nimport subprocess\nsubprocess.run([{sys.executable!r}, {str(ROOT / "scripts" / "start_pr_review.py")!r}, "--trigger", "scheduled"], check=True)\n', encoding="utf-8")
    if not args.skip_schedules:
        schedules = collect_schedules()
        existing = subprocess.run(["hermes", "-p", args.profile, "cron", "list"], capture_output=True, text=True).stdout
        for name, schedule, _ in schedules:
            if name not in existing:
                run(["hermes", "-p", args.profile, "cron", "create", "--name", name, "--cron", schedule, "--script", wrapper.name, "--no-agent", "--deliver", "local"])
    run(["hermes", "-p", args.profile, "gateway", "restart"])
    print("Setup complete. Authenticate GitHub yourself with `gh auth login`, edit config.json, then run the terminal command in README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

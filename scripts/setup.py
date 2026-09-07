#!/usr/bin/env python3
"""Interactive local setup for the optional-Hermes PR reviewer. Never performs gh login."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETUP_STATE_PATH = ROOT / ".setup-state.json"
CRON_BEGIN = "# BEGIN hermes-pr-reviewer"
CRON_END = "# END hermes-pr-reviewer"


def ask(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    value = (getpass.getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ")).strip()
    return value or (default or "")


def ask_yes_no(label: str, default: str = "no") -> bool:
    while True:
        value = ask(label, default).lower()
        if value in ("yes", "y"):
            return True
        if value in ("no", "n"):
            return False
        print("Answer yes or no.")


def run(arguments: list[str], *, capture_output: bool = False) -> str:
    result = subprocess.run(arguments, capture_output=capture_output, text=True)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Command failed: {' '.join(arguments)}" + (f"\n{message}" if message else ""))
    return result.stdout if capture_output else ""


def require_commands(names: tuple[str, ...]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"Install these commands first: {', '.join(missing)}")


def hermes_root() -> Path:
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if not configured_home:
        return Path.home() / ".hermes"
    home = Path(configured_home)
    return home.parent.parent if home.parent.name == "profiles" else home


def hermes_profile_dir(profile: str) -> Path:
    return hermes_root() / "profiles" / profile


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


def select_notification_provider() -> str:
    while True:
        provider = ask("Notification mode (terminal/hermes)", "terminal").lower()
        if provider in ("terminal", "hermes"):
            return provider
        print("Choose either 'terminal' or 'hermes'.")


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


def update_config(path: Path, codex_skills: list[str], notifications: dict[str, Any]) -> None:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected an object in {path}")
    data.pop("hermesProfile", None)
    data.pop("telegramTarget", None)
    data["codexSkills"] = codex_skills
    data["notifications"] = notifications
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_setup_state() -> dict[str, Any]:
    if not SETUP_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(SETUP_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid setup state: {error}") from error
    if not isinstance(state, dict):
        raise RuntimeError("Setup state must be an object.")
    return state


def save_setup_state(state: dict[str, Any]) -> None:
    SETUP_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def validate_time(value: str) -> str:
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
        if not ask_yes_no("Add another review time? (yes/no)"):
            return schedules


def managed_cron_block(schedules: list[tuple[str, str, str]]) -> str:
    if not schedules:
        return ""
    command = " ".join((
        shlex.quote(sys.executable),
        shlex.quote(str(ROOT / "scripts" / "start_pr_review.py")),
        "--trigger scheduled",
        f">> {shlex.quote(str(ROOT / 'cache' / 'scheduled-run.log'))} 2>&1",
    ))
    lines = [CRON_BEGIN]
    lines.extend(f"{schedule} {command}" for _, schedule, _ in schedules)
    lines.append(CRON_END)
    return "\n".join(lines) + "\n"


def remove_managed_cron_block(crontab: str) -> str:
    pattern = re.compile(rf"(?ms)^{re.escape(CRON_BEGIN)}\n.*?^{re.escape(CRON_END)}\n?")
    return pattern.sub("", crontab).strip()


def replace_linux_cron(schedules: list[tuple[str, str, str]]) -> None:
    if os.name == "nt":
        raise RuntimeError("Native schedules are supported on Linux only. Use Task Scheduler manually on Windows.")
    require_commands(("crontab",))
    (ROOT / "cache").mkdir(parents=True, exist_ok=True)
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if current.returncode == 0:
        existing = current.stdout
    elif "no crontab" in (current.stderr or "").lower():
        existing = ""
    else:
        raise RuntimeError((current.stderr or current.stdout).strip() or "Could not read crontab.")
    updated = remove_managed_cron_block(existing)
    block = managed_cron_block(schedules)
    if updated and block:
        updated += "\n"
    updated += block
    result = subprocess.run(["crontab", "-"], input=(updated + "\n") if updated else "", text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Could not update crontab.")


def extract_hermes_job_id(output: str) -> str:
    for pattern in (
        r"(?im)\bcreated\s+job\s*:\s*([A-Za-z0-9_-]+)",
        r"(?im)\b(?:job\s+)?id\s*[:=]\s*([A-Za-z0-9_-]+)",
        r"(?im)\bcreated\b.*?\b([0-9a-f]{8}-[0-9a-f-]{27,})\b",
    ):
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    raise RuntimeError("Hermes created a schedule but did not report its job ID; remove it manually before retrying setup.")


def remove_hermes_schedules(profile: str, state: dict[str, Any]) -> None:
    jobs = state.get("hermesCronJobs", [])
    if not isinstance(jobs, list):
        raise RuntimeError("Setup state hermesCronJobs must be an array.")
    previous_profile = state.get("hermesCronProfile", profile)
    if not isinstance(previous_profile, str) or not previous_profile:
        raise RuntimeError("Setup state hermesCronProfile must be a non-empty string.")
    for job_id in jobs:
        if isinstance(job_id, str) and job_id:
            try:
                run(["hermes", "-p", previous_profile, "cron", "remove", job_id])
            except RuntimeError as error:
                if "not found" not in str(error).lower():
                    raise
    state["hermesCronJobs"] = []
    state.pop("hermesCronProfile", None)
    save_setup_state(state)


def create_hermes_schedules(profile: str, wrapper: Path, schedules: list[tuple[str, str, str]], state: dict[str, Any]) -> None:
    created: list[str] = []
    try:
        for name, schedule, _ in schedules:
            output = run([
                "hermes", "-p", profile, "cron", "create", schedule,
                "--name", name, "--script", wrapper.name, "--no-agent", "--deliver", "local",
            ], capture_output=True)
            created.append(extract_hermes_job_id(output))
    except Exception:
        for job_id in created:
            try:
                run(["hermes", "-p", profile, "cron", "remove", job_id])
            except RuntimeError:
                pass
        raise
    state["hermesCronJobs"] = created
    state["hermesCronProfile"] = profile
    save_setup_state(state)


def configure_hermes(profile: str) -> None:
    token = ask("Telegram bot token", secret=True)
    user_id = ask("Your Telegram user ID")
    group_id = ask("Telegram review group ID")
    if not token or not user_id or not group_id:
        raise RuntimeError("Bot token, Telegram user ID, and group ID are required.")
    try:
        run(["hermes", "profile", "create", profile])
    except RuntimeError:
        run(["hermes", "profile", "show", profile])
    profile_dir = hermes_profile_dir(profile)
    update_env(profile_dir / ".env", {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ALLOWED_USERS": user_id,
        "TELEGRAM_HOME_CHANNEL": group_id,
    })
    quick_command = f'{sys.executable} "{ROOT / "scripts" / "start_pr_review.py"}" --trigger hermes --detach'
    run(["hermes", "-p", profile, "config", "set", "telegram.allowed_chats", json.dumps([user_id])])
    run(["hermes", "-p", profile, "config", "set", "telegram.require_mention", "true"])
    run(["hermes", "-p", profile, "config", "set", "telegram.exclusive_bot_mentions", "true"])
    run(["hermes", "-p", profile, "config", "set", "telegram.guest_mode", "false"])
    run(["hermes", "-p", profile, "config", "set", "display.background_process_notifications", "off"])
    run(["hermes", "-p", profile, "config", "set", "quick_commands.pr-review.type", "exec"])
    run(["hermes", "-p", profile, "config", "set", "quick_commands.pr-review.command", quick_command])


def hermes_wrapper(profile: str) -> Path:
    profile_scripts = hermes_profile_dir(profile) / "scripts"
    profile_scripts.mkdir(parents=True, exist_ok=True)
    wrapper = profile_scripts / "pr-review-launcher.py"
    wrapper.write_text(
        f'#!/usr/bin/env python3\nimport subprocess\nsubprocess.run([{sys.executable!r}, {str(ROOT / "scripts" / "start_pr_review.py")!r}, "--trigger", "scheduled"], check=True)\n',
        encoding="utf-8",
    )
    return wrapper


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="reviewer")
    parser.add_argument("--skip-schedules", action="store_true")
    args = parser.parse_args()
    require_commands(("git", "gh", "codex"))
    config_path = ROOT / "config.json"
    if not config_path.exists():
        shutil.copyfile(ROOT / "config.example.json", config_path)
        print(f"Created {config_path}; edit its repository list before your first review.")
    provider = select_notification_provider()
    skills = select_codex_skills(installed_skill_names())
    if provider == "terminal":
        update_config(config_path, skills, {"provider": "terminal"})
        if not args.skip_schedules and ask_yes_no("Configure Linux cron schedules? (yes/no)"):
            replace_linux_cron(collect_schedules())
        elif not args.skip_schedules and shutil.which("crontab"):
            replace_linux_cron([])
        print("Setup complete. Authenticate GitHub with `gh auth login`, edit config.json, then run the terminal command in README.md.")
        return 0

    require_commands(("hermes",))
    configure_hermes(args.profile)
    update_config(config_path, skills, {"provider": "hermes", "hermes": {"profile": args.profile, "target": "telegram"}})
    if not args.skip_schedules:
        state = load_setup_state()
        remove_hermes_schedules(args.profile, state)
        create_hermes_schedules(args.profile, hermes_wrapper(args.profile), collect_schedules(), state)
    run(["hermes", "-p", args.profile, "gateway", "restart"])
    print("Setup complete. Authenticate GitHub with `gh auth login`, edit config.json, then run the terminal command in README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

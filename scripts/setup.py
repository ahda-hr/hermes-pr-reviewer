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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="reviewer")
    parser.add_argument("--morning-cron", default="0 5 * * 1-5", help="Hermes cron expression for the first weekday run")
    parser.add_argument("--afternoon-cron", default="30 15 * * 1-5", help="Hermes cron expression for the second weekday run")
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
        existing = subprocess.run(["hermes", "-p", args.profile, "cron", "list"], capture_output=True, text=True).stdout
        for name, schedule in (("PR review morning", args.morning_cron), ("PR review afternoon", args.afternoon_cron)):
            if name not in existing:
                run(["hermes", "-p", args.profile, "cron", "create", "--name", name, "--cron", schedule, "--script", wrapper.name, "--no-agent", "--deliver", "local"])
    run(["hermes", "-p", args.profile, "gateway", "restart"])
    print("Setup complete. Authenticate GitHub yourself with `gh auth login`, edit config.json, then run the terminal command in README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

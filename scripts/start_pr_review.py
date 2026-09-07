#!/usr/bin/env python3
"""Start the reviewer synchronously or as a detached cross-platform process."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).with_name("run_pr_reviews.py")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", choices=("terminal", "hermes", "scheduled"), default="terminal")
    parser.add_argument("--repo")
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    command = [sys.executable, str(RUNNER), "--trigger", args.trigger]
    if args.repo:
        command.extend(["--repo", args.repo])
    if not args.detach:
        return subprocess.run(command, cwd=ROOT).returncode
    log_path = ROOT / "cache" / "runner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        kwargs: dict[str, object] = {"cwd": ROOT, "stdout": log, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(command, **kwargs)
    print("PR review request accepted. You will receive a start or already-running notification shortly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

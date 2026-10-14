#!/usr/bin/env python3
"""Install the pr-review terminal command for this checkout (Linux/macOS)."""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = "# Managed by hermes-pr-reviewer command installer"


def install_command(bin_dir: Path) -> Path:
    if os.name == "nt":
        raise RuntimeError("This installer supports Linux and macOS. On Windows, use scripts/start_pr_review.py directly.")
    destination = bin_dir.expanduser().resolve() / "pr-review"
    if destination.is_symlink() or (destination.exists() and MARKER not in destination.read_text(encoding="utf-8")):
        raise RuntimeError(f"Refusing to replace an existing unrelated command: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        f"#!/bin/sh\n{MARKER}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'scripts' / 'start_pr_review.py'))} \"$@\"\n",
        encoding="utf-8",
    )
    destination.chmod(0o755)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    args = parser.parse_args()
    try:
        destination = install_command(args.bin_dir)
    except (OSError, RuntimeError, UnicodeError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    print(f"Installed {destination}. Run `pr-review` from any directory.")
    path_dirs = [Path(entry).expanduser().resolve() for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    if destination.parent not in path_dirs:
        print(f'Add this directory to your shell PATH: export PATH={shlex.quote(str(destination.parent))}:"$PATH"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

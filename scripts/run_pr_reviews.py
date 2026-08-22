#!/usr/bin/env python3
"""Cross-platform GitHub PR reviewer. Uses only the Python standard library."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
PROXY_NAMES = ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY")
PROMPT_TEMPLATE_PATH = ROOT / "prompts" / "pr-review.md"
PLACEHOLDER_PATTERN = re.compile(r"{{[^{}]+}}")


class CommandError(RuntimeError):
    pass


class RunLock:
    """An advisory lock released automatically if the runner process exits."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0)
        if not self.handle.read(1):
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


def now() -> datetime:
    return datetime.now().astimezone()


def duration(start: datetime, end: datetime) -> str:
    minutes = max(0, int((end - start).total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60}m"


@contextmanager
def clean_proxy(enabled: bool) -> Iterator[None]:
    saved: dict[str, str | None] = {}
    if enabled:
        for name in PROXY_NAMES:
            saved[name] = os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def command(args: list[str], *, cwd: Path | None = None, clear_proxy_environment: bool = False) -> str:
    with clean_proxy(clear_proxy_environment):
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise CommandError(f"{' '.join(args[:3])} failed: {message}")
    return result.stdout


def require_tools() -> None:
    missing = [tool for tool in ("git", "gh", "codex", "hermes") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(f"Required command(s) not found on PATH: {', '.join(missing)}")


def codex_skills_root() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    return Path(configured_home) / "skills" if configured_home else Path.home() / ".codex" / "skills"


def installed_skill_names() -> list[str]:
    root = codex_skills_root()
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if child.is_dir() and child.name != ".system" and (child / "SKILL.md").is_file())


def configured_skill_names(config: dict[str, Any]) -> list[str]:
    configured = config.get("codexSkills", [])
    if not isinstance(configured, list) or any(not isinstance(name, str) or not name.strip() for name in configured):
        raise RuntimeError("config.codexSkills must be an array of non-empty skill names.")
    return list(dict.fromkeys(name.strip() for name in configured))


def load_review_template() -> str:
    if not PROMPT_TEMPLATE_PATH.is_file():
        raise RuntimeError(f"Missing PR review prompt template: {PROMPT_TEMPLATE_PATH}")
    return PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")


def render_review_prompt(template: str, pr_number: int, base_ref: str, skills: list[str]) -> str:
    skill_instruction = (
        "Use these installed Codex skills as part of this review: " + ", ".join(f"${name}" for name in skills) + "."
        if skills else "No additional Codex skills were selected for this review."
    )
    prompt = (template.replace("{{pr_number}}", str(pr_number))
            .replace("{{base_ref}}", base_ref)
            .replace("{{selected_skills}}", skill_instruction))
    unresolved = PLACEHOLDER_PATTERN.findall(prompt)
    if unresolved:
        raise RuntimeError("Unresolved placeholder(s) in PR review template: " + ", ".join(unresolved))
    return prompt


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        # PowerShell 5.1 writes UTF-8 text with a BOM; accept existing state files
        # created by the retired Windows runner during migration.
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected an object in {path}")
    return value


def save_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def own_activity(repository: str, number: int, login: str, clear_proxy_environment: bool) -> bool:
    owner, name = repository.split("/", 1)
    query = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviews(first:100){nodes{author{login}}} comments(first:100){nodes{author{login}}} reviewThreads(first:100){nodes{comments(first:100){nodes{author{login}}}}}}}}"""
    output = command(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"], clear_proxy_environment=clear_proxy_environment)
    pr = json.loads(output)["data"]["repository"]["pullRequest"]
    authors: set[str] = set()
    for review in pr["reviews"]["nodes"]:
        if review.get("author"):
            authors.add(review["author"]["login"])
    for comment in pr["comments"]["nodes"]:
        if comment.get("author"):
            authors.add(comment["author"]["login"])
    for thread in pr["reviewThreads"]["nodes"]:
        for comment in thread["comments"]["nodes"]:
            if comment.get("author"):
                authors.add(comment["author"]["login"])
    return login in authors


def notifier(config: dict[str, Any], trigger: str):
    clear_proxy_environment = config.get("clearProxyEnvironment") is True
    profile = config.get("hermesProfile")
    target = str(config.get("telegramTarget", "telegram"))

    def send(message: str, attachments: list[Path] | None = None) -> None:
        if trigger == "terminal":
            print(message)
            return
        prefix = ["hermes"] + (["-p", str(profile)] if profile else [])
        try:
            command(prefix + ["send", "--to", target, message, "--quiet"], clear_proxy_environment=clear_proxy_environment)
            for attachment in attachments or []:
                command(prefix + ["send", "--to", target, f"MEDIA:{attachment}", "--quiet"], clear_proxy_environment=clear_proxy_environment)
        except Exception as error:  # Notifications must not discard the review result.
            print(f"Warning: Telegram notification failed: {error}", file=sys.stderr)
    return send


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", choices=("terminal", "telegram", "scheduled"), default="terminal")
    parser.add_argument("--repo")
    args = parser.parse_args()
    config_path = ROOT / "config.json"
    cache = ROOT / "cache"
    worktrees = ROOT / "worktrees"
    reports = ROOT / "reports"
    state_path = cache / "state.json"
    if not config_path.exists():
        raise RuntimeError(f"Missing configuration: {config_path}. Copy config.example.json to config.json first.")
    config = load_json(config_path, {})
    send = notifier(config, args.trigger)
    lock = RunLock(cache / "runner.lock")
    if not lock.acquire():
        send("PR review already running; no second run was started.")
        return 0
    try:
        require_tools()
        clear_proxy_environment = config.get("clearProxyEnvironment") is True
        configured_skills = configured_skill_names(config)
        available_skills = set(installed_skill_names())
        selected_skills = [name for name in configured_skills if name in available_skills]
        missing_skills = [name for name in configured_skills if name not in available_skills]
        if missing_skills:
            send("Warning: configured Codex skill(s) not installed; continuing without them: " + ", ".join(missing_skills))
        prompt_template = load_review_template()
        try:
            login = command(["gh", "api", "user", "--jq", ".login"], clear_proxy_environment=clear_proxy_environment).strip()
        except CommandError as error:
            raise RuntimeError(f"GitHub CLI authentication/API access failed. Authenticate with gh before running the reviewer. Details: {error}") from error
        repositories = [item for item in config.get("repos", []) if item.get("enabled", True)]
        if args.repo:
            repositories = [item for item in repositories if item.get("repo") == args.repo]
        if not repositories:
            send("PR review finished: no enabled repositories are configured.")
            return 0
        cache.mkdir(parents=True, exist_ok=True)
        worktrees.mkdir(parents=True, exist_ok=True)
        state = load_json(state_path, {"prs": {}})
        state.setdefault("prs", {})
        summary: list[str] = []
        for repo_config in repositories:
            repository = str(repo_config.get("repo", ""))
            if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
                raise RuntimeError(f"Invalid repository in config: {repository}")
            slug = str(repo_config.get("slug") or repository.replace("/", "__"))
            bare = cache / f"{slug}.git"
            if not bare.exists():
                command(["git", "clone", "--bare", f"https://github.com/{repository}.git", str(bare)], clear_proxy_environment=clear_proxy_environment)
            raw_prs = command(["gh", "pr", "list", "--repo", repository, "--state", "open", "--limit", str(config.get("maxPrsPerRun", 10)), "--json", "number,author,headRefName,baseRefName,headRefOid,baseRefOid"], clear_proxy_environment=clear_proxy_environment)
            for pr in json.loads(raw_prs):
                author = pr["author"]["login"]
                number = int(pr["number"])
                if author == login or own_activity(repository, number, login, clear_proxy_environment):
                    continue
                signature = "|".join(str(pr[key]) for key in ("headRefOid", "baseRefOid", "headRefName", "baseRefName"))
                key = f"{repository}#{number}"
                if state["prs"].get(key, {}).get("signature") == signature:
                    continue
                run_id = uuid.uuid4().hex
                worktree = worktrees / f"{slug}-pr-{number}-{run_id}"
                report_dir = reports / slug
                safe_author = re.sub(r"[^A-Za-z0-9._-]", "_", author)
                report_path = report_dir / f"{slug}__{number}__{safe_author}.md"
                raw_path = report_dir / f".raw-{run_id}.md"
                started = now()
                try:
                    report_dir.mkdir(parents=True, exist_ok=True)
                    command(["git", f"--git-dir={bare}", "fetch", "--force", "origin", f"+refs/pull/{number}/head:refs/pr/{number}/head", f"+refs/heads/{pr['baseRefName']}:refs/pr/{number}/base"], clear_proxy_environment=clear_proxy_environment)
                    command(["git", f"--git-dir={bare}", "update-ref", f"refs/pr/{number}/base", pr["baseRefOid"]], clear_proxy_environment=clear_proxy_environment)
                    head = command(["git", f"--git-dir={bare}", "rev-parse", f"refs/pr/{number}/head"], clear_proxy_environment=clear_proxy_environment).strip()
                    base = command(["git", f"--git-dir={bare}", "rev-parse", f"refs/pr/{number}/base"], clear_proxy_environment=clear_proxy_environment).strip()
                    if head != pr["headRefOid"] or base != pr["baseRefOid"]:
                        raise RuntimeError("Fetched refs do not match the PR metadata.")
                    fresh = json.loads(command(["gh", "pr", "view", str(number), "--repo", repository, "--json", "headRefOid,baseRefOid,state"], clear_proxy_environment=clear_proxy_environment))
                    if fresh["state"] != "OPEN" or fresh["headRefOid"] != pr["headRefOid"] or fresh["baseRefOid"] != pr["baseRefOid"]:
                        summary.append(f"#{number} changed while preparing; deferred.")
                        continue
                    command(["git", f"--git-dir={bare}", "worktree", "add", "--detach", str(worktree), f"refs/pr/{number}/head"], clear_proxy_environment=clear_proxy_environment)
                    send(f"[STARTED] {repository} PR #{number} by {author} ({pr['headRefName']} -> {pr['baseRefName']})")
                    prompt = render_review_prompt(prompt_template, number, f"refs/pr/{number}/base", selected_skills)
                    codex_args = ["codex", "exec", "-C", str(worktree), "--sandbox", "read-only", "--output-last-message", str(raw_path)]
                    if config.get("codexModel"):
                        codex_args.extend(["--model", str(config["codexModel"])])
                    command(codex_args + [prompt], clear_proxy_environment=clear_proxy_environment)
                    ended = now()
                    header = f"# PR Review: {repository} #{number}\n\n- **Repo name:** {repository}\n- **Author:** {author}\n- **PR number:** {number}\n- **Source branch:** {pr['headRefName']} ({pr['headRefOid']})\n- **Target branch:** {pr['baseRefName']} ({pr['baseRefOid']})\n- **Start time:** {started.strftime('%Y-%m-%d %H:%M:%S %z')}\n- **End time:** {ended.strftime('%Y-%m-%d %H:%M:%S %z')}\n- **Total time:** {duration(started, ended)}\n\n"
                    report_path.write_text(header + raw_path.read_text(encoding="utf-8"), encoding="utf-8")
                    state["prs"][key] = {"signature": signature, "reviewedAt": ended.isoformat(), "report": str(report_path)}
                    save_json(state_path, state)
                    summary.append(f"#{number} reviewed")
                    send(f"[FINISHED] {repository} PR #{number} in {duration(started, ended)}", [report_path])
                except Exception as error:
                    ended = now()
                    reason = " ".join(str(error).splitlines())
                    summary.append(f"#{number} failed: {reason}")
                    send(f"[FAILED] {repository} PR #{number} after {duration(started, ended)} - {reason}")
                finally:
                    if worktree.exists():
                        subprocess.run(["git", f"--git-dir={bare}", "worktree", "remove", "--force", str(worktree)], capture_output=True, text=True)
                        shutil.rmtree(worktree, ignore_errors=True)
                    raw_path.unlink(missing_ok=True)
        send("PR review completed: " + ("; ".join(summary) if summary else "no eligible changed PRs found."))
        return 0
    except Exception as error:
        send(f"PR review failed: {error}")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())

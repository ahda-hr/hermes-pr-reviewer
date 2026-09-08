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
GITHUB_REVIEW_MARKER = "<!-- hermes-pr-reviewer -->"
REPORT_SECTIONS = (
    ("blocking", "blocking", "## 🔴 Blocking issues"),
    ("important", "important", "## 🟡 Important issues"),
    ("ternary", "suggestion", "## 🟢 Ternary refactor suggestions"),
    ("extraction", "suggestion", "## 🟢 Extraction refactor suggestions"),
    ("antiPattern", "suggestion", "## 🟢 Anti-pattern refactor suggestions"),
)


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


def command(args: list[str], *, cwd: Path | None = None, clear_proxy_environment: bool = False, input_text: str | None = None) -> str:
    with clean_proxy(clear_proxy_environment):
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, input=input_text)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise CommandError(f"{' '.join(args[:3])} failed: {message}")
    return result.stdout


def require_tools(notification_provider: str) -> None:
    tools = ["git", "gh", "codex"]
    if notification_provider == "hermes":
        tools.append("hermes")
    missing = [tool for tool in tools if shutil.which(tool) is None]
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


def github_review_mode(config: dict[str, Any]) -> str:
    review = config.get("githubReview", {"mode": "off"})
    if not isinstance(review, dict):
        raise RuntimeError("config.githubReview must be an object when configured.")
    mode = review.get("mode", "off")
    if mode not in ("off", "pending"):
        raise RuntimeError("config.githubReview.mode must be 'off' or 'pending'.")
    return mode


def order_pull_requests(pull_requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Review lower-numbered pull requests before newer ones."""
    return sorted(pull_requests, key=lambda pull_request: int(pull_request["number"]))


def list_open_pull_requests(repository: str, limit: int, clear_proxy_environment: bool) -> list[dict[str, Any]]:
    output = command(
        ["gh", "pr", "list", "--repo", repository, "--state", "open", "--search", "sort:created-asc", "--limit", str(limit), "--json", "number,author,headRefName,baseRefName,headRefOid,baseRefOid"],
        clear_proxy_environment=clear_proxy_environment,
    )
    try:
        pull_requests = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub returned invalid pull-request JSON: {error}") from error
    if not isinstance(pull_requests, list):
        raise RuntimeError("GitHub returned an invalid pull-request list.")
    return order_pull_requests(pull_requests)


def _review_line(value: Any, field: str, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"Review finding {field} must be a positive integer" + ("." if required else " or null."))
    return value


def validate_review_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"findings"} or not isinstance(value["findings"], list):
        raise RuntimeError("Codex review output must be a JSON object containing only a findings array.")
    sections = {category: severity for category, severity, _ in REPORT_SECTIONS}
    required = {"category", "severity", "path", "startLine", "endLine", "title", "explanation", "excerpt", "suggestedFix", "copyableReply"}
    findings: list[dict[str, Any]] = []
    for index, finding in enumerate(value["findings"], start=1):
        if not isinstance(finding, dict) or set(finding) != required:
            raise RuntimeError(f"Review finding {index} does not match the required schema.")
        category = finding["category"]
        severity = finding["severity"]
        if category not in sections or severity != sections[category]:
            raise RuntimeError(f"Review finding {index} has an invalid category or severity.")
        path = finding["path"]
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
            raise RuntimeError(f"Review finding {index} has an invalid repository-relative path.")
        start_line = _review_line(finding["startLine"], "startLine")
        end_line = _review_line(finding["endLine"], "endLine")
        if start_line is not None and end_line is not None and end_line < start_line:
            raise RuntimeError(f"Review finding {index} ends before it starts.")
        for field in ("title", "explanation", "suggestedFix", "copyableReply"):
            if not isinstance(finding[field], str) or not finding[field].strip():
                raise RuntimeError(f"Review finding {index} {field} must be a non-empty string.")
        if finding["excerpt"] is not None and not isinstance(finding["excerpt"], str):
            raise RuntimeError(f"Review finding {index} excerpt must be a string or null.")
        findings.append({
            **finding,
            "path": path,
            "title": finding["title"].strip(),
            "explanation": finding["explanation"].strip(),
            "suggestedFix": finding["suggestedFix"].strip(),
            "copyableReply": finding["copyableReply"],
        })
    return findings


def load_review_findings(path: Path) -> list[dict[str, Any]]:
    try:
        return validate_review_findings(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Codex review output is not valid JSON: {error}") from error


def render_review_report(findings: list[dict[str, Any]]) -> str:
    output: list[str] = []
    for category, _, heading in REPORT_SECTIONS:
        output.extend((heading, ""))
        section_findings = [finding for finding in findings if finding["category"] == category]
        if not section_findings:
            output.extend(("None found.", ""))
            continue
        for finding in section_findings:
            start = finding["startLine"]
            end = finding["endLine"]
            line_label = "Line unavailable" if start is None else str(start) if end in (None, start) else f"{start}–{end}"
            output.extend((
                f"### `{finding['path']}` — [{finding['severity']}] {finding['title']}",
                "",
                f"**Lines:** `{line_label}`",
                "",
                finding["explanation"],
                "",
            ))
            if finding["excerpt"]:
                output.extend((f"```{finding['path']}:{line_label}", finding["excerpt"].rstrip(), "```", ""))
            output.extend(("**Suggested fix:** " + finding["suggestedFix"], "", "**Copyable suggested reply:**"))
            output.extend("> " + line if line else ">" for line in finding["copyableReply"].splitlines())
            output.extend(("", "---", ""))
    return "\n".join(output).rstrip() + "\n"


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


def pull_request_activity(repository: str, number: int, login: str, clear_proxy_environment: bool) -> dict[str, Any]:
    owner, name = repository.split("/", 1)
    query = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviews(first:100){nodes{author{login} body state commit{oid}}} comments(first:100){nodes{author{login}}} reviewThreads(first:100){nodes{comments(first:100){nodes{author{login} pullRequestReview{body}}}}}}}}"""
    output = command(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"], clear_proxy_environment=clear_proxy_environment)
    pr = json.loads(output)["data"]["repository"]["pullRequest"]
    tool_reviews: list[dict[str, Any]] = []
    manual_activity = False
    for review in pr["reviews"]["nodes"]:
        if review.get("author", {}).get("login") != login:
            continue
        if GITHUB_REVIEW_MARKER in str(review.get("body") or ""):
            tool_reviews.append(review)
        else:
            manual_activity = True
    for comment in pr["comments"]["nodes"]:
        if comment.get("author", {}).get("login") == login:
            manual_activity = True
    for thread in pr["reviewThreads"]["nodes"]:
        for comment in thread["comments"]["nodes"]:
            if comment.get("author", {}).get("login") != login:
                continue
            review_body = str((comment.get("pullRequestReview") or {}).get("body") or "")
            if GITHUB_REVIEW_MARKER not in review_body:
                manual_activity = True
    return {"manualActivity": manual_activity, "toolReviews": tool_reviews}


def has_pending_tool_review(activity: dict[str, Any]) -> bool:
    return any(review.get("state") == "PENDING" for review in activity["toolReviews"])


def has_submitted_tool_review_for_commit(activity: dict[str, Any], commit_oid: str) -> bool:
    return any(
        review.get("state") != "PENDING" and (review.get("commit") or {}).get("oid") == commit_oid
        for review in activity["toolReviews"]
    )


def parse_diff_lines(diff: str) -> dict[str, set[int]]:
    paths: dict[str, set[int]] = {}
    path: str | None = None
    new_line: int | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            paths.setdefault(path, set())
            continue
        if line.startswith("+++"):
            path = None
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if path is None or new_line is None or not line:
            continue
        if line.startswith("+"):
            paths[path].add(new_line)
            new_line += 1
        elif line.startswith(" "):
            paths[path].add(new_line)
            new_line += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
    return paths


def pending_review_comments(findings: list[dict[str, Any]], diff_lines: dict[str, set[int]]) -> tuple[list[dict[str, Any]], int]:
    comments: list[dict[str, Any]] = []
    skipped = 0
    for finding in findings:
        if finding["severity"] not in ("blocking", "important"):
            continue
        line = finding["startLine"]
        if line is None or line not in diff_lines.get(finding["path"], set()):
            skipped += 1
            continue
        comments.append({"path": finding["path"], "line": line, "side": "RIGHT", "body": finding["copyableReply"]})
    return comments, skipped


def create_pending_review(repository: str, number: int, commit_oid: str, comments: list[dict[str, Any]], clear_proxy_environment: bool) -> dict[str, Any]:
    payload = {"commit_id": commit_oid, "body": GITHUB_REVIEW_MARKER, "comments": comments}
    output = command(
        ["gh", "api", "--method", "POST", f"repos/{repository}/pulls/{number}/reviews", "--input", "-"],
        clear_proxy_environment=clear_proxy_environment,
        input_text=json.dumps(payload),
    )
    try:
        review = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub returned invalid pending-review JSON: {error}") from error
    if review.get("state") != "PENDING" or not isinstance(review.get("id"), int):
        raise RuntimeError("GitHub did not create a pending review.")
    return review


def notification_settings(config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    notifications = config.get("notifications")
    if not isinstance(notifications, dict):
        raise RuntimeError("config.notifications must be an object with a provider.")
    provider = notifications.get("provider")
    if provider == "terminal":
        return "terminal", {}
    if provider != "hermes":
        raise RuntimeError("config.notifications.provider must be 'terminal' or 'hermes'.")
    hermes = notifications.get("hermes")
    if not isinstance(hermes, dict):
        raise RuntimeError("config.notifications.hermes must be an object when provider is 'hermes'.")
    profile = hermes.get("profile")
    target = hermes.get("target")
    if not isinstance(profile, str) or not profile.strip() or not isinstance(target, str) or not target.strip():
        raise RuntimeError("config.notifications.hermes.profile and target must be non-empty strings.")
    return "hermes", {"profile": profile.strip(), "target": target.strip()}


def active_notification_provider(configured_provider: str, trigger: str) -> str:
    return "terminal" if trigger == "terminal" else configured_provider


def notifier(config: dict[str, Any], trigger: str):
    clear_proxy_environment = config.get("clearProxyEnvironment") is True
    configured_provider, hermes_settings = notification_settings(config)
    provider = active_notification_provider(configured_provider, trigger)

    def send(message: str, attachments: list[Path] | None = None) -> None:
        if provider == "terminal":
            print(message)
            return
        prefix = ["hermes", "-p", hermes_settings["profile"]]
        try:
            command(prefix + ["send", "--to", hermes_settings["target"], message, "--quiet"], clear_proxy_environment=clear_proxy_environment)
            for attachment in attachments or []:
                command(prefix + ["send", "--to", hermes_settings["target"], f"MEDIA:{attachment}", "--quiet"], clear_proxy_environment=clear_proxy_environment)
        except Exception as error:  # Notifications must not discard the review result.
            print(f"Warning: Hermes notification failed: {error}", file=sys.stderr)
    return send, provider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", choices=("terminal", "hermes", "scheduled"), default="terminal")
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
    review_mode = github_review_mode(config)
    send, notification_provider = notifier(config, args.trigger)
    lock = RunLock(cache / "runner.lock")
    if not lock.acquire():
        send("PR review already running; no second run was started.")
        return 0
    try:
        require_tools(notification_provider)
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
            for pr in list_open_pull_requests(repository, int(config.get("maxPrsPerRun", 10)), clear_proxy_environment):
                author = pr["author"]["login"]
                number = int(pr["number"])
                if author == login:
                    continue
                activity = pull_request_activity(repository, number, login, clear_proxy_environment)
                if activity["manualActivity"]:
                    continue
                if has_pending_tool_review(activity):
                    summary.append(f"#{number} has a pending tool review; submit or discard it before a new review.")
                    send(f"[PENDING] {repository} PR #{number} already has an unsubmitted tool review. Submit or discard it before requesting another review.")
                    continue
                signature = "|".join(str(pr[key]) for key in ("headRefOid", "baseRefOid", "headRefName", "baseRefName"))
                key = f"{repository}#{number}"
                if state["prs"].get(key, {}).get("signature") == signature:
                    continue
                if has_submitted_tool_review_for_commit(activity, pr["headRefOid"]):
                    state["prs"][key] = {"signature": signature, "reviewedAt": now().isoformat(), "publication": "recovered"}
                    save_json(state_path, state)
                    summary.append(f"#{number} already has a submitted tool review for this commit.")
                    continue
                run_id = uuid.uuid4().hex
                worktree = worktrees / f"{slug}-pr-{number}-{run_id}"
                report_dir = reports / slug
                safe_author = re.sub(r"[^A-Za-z0-9._-]", "_", author)
                report_path = report_dir / f"{slug}__{number}__{safe_author}.md"
                raw_path = report_dir / f".raw-{run_id}.json"
                findings_path = report_path.with_suffix(".json")
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
                    findings = load_review_findings(raw_path)
                    header = f"# PR Review: {repository} #{number}\n\n- **Repo name:** {repository}\n- **Author:** {author}\n- **PR number:** {number}\n- **Source branch:** {pr['headRefName']} ({pr['headRefOid']})\n- **Target branch:** {pr['baseRefName']} ({pr['baseRefOid']})\n- **Start time:** {started.strftime('%Y-%m-%d %H:%M:%S %z')}\n- **End time:** {ended.strftime('%Y-%m-%d %H:%M:%S %z')}\n- **Total time:** {duration(started, ended)}\n\n"
                    report_path.write_text(header + render_review_report(findings), encoding="utf-8")
                    save_json(findings_path, {"findings": findings})
                    state_entry: dict[str, Any] = {"signature": signature, "reviewedAt": ended.isoformat(), "report": str(report_path), "findings": str(findings_path)}
                    if review_mode == "pending":
                        fresh = json.loads(command(["gh", "pr", "view", str(number), "--repo", repository, "--json", "headRefOid,baseRefOid,state"], clear_proxy_environment=clear_proxy_environment))
                        if fresh["state"] != "OPEN" or fresh["headRefOid"] != pr["headRefOid"] or fresh["baseRefOid"] != pr["baseRefOid"]:
                            summary.append(f"#{number} changed before pending-review publication; report saved, publication deferred.")
                            send(f"[DEFERRED] {repository} PR #{number} changed before pending-review publication; the local report was saved.", [report_path])
                            continue
                        diff = command(["gh", "api", "-H", "Accept: application/vnd.github.v3.diff", f"repos/{repository}/pulls/{number}"], clear_proxy_environment=clear_proxy_environment)
                        comments, skipped = pending_review_comments(findings, parse_diff_lines(diff))
                        state_entry["publication"] = {"mode": "pending", "skipped": skipped}
                        if comments:
                            pending_review = create_pending_review(repository, number, pr["headRefOid"], comments, clear_proxy_environment)
                            state_entry["publication"].update({"status": "pending", "reviewId": pending_review["id"], "url": pending_review.get("html_url"), "comments": len(comments)})
                            summary.append(f"#{number} reviewed; {len(comments)} pending comment(s), {skipped} retained only in the report.")
                            send(f"[PENDING] {repository} PR #{number}: {len(comments)} draft inline comment(s) created; {skipped} eligible finding(s) were retained only in the report.", [report_path])
                        else:
                            state_entry["publication"]["status"] = "no-mappable-findings"
                            summary.append(f"#{number} reviewed; no eligible findings could be mapped to the current diff.")
                            send(f"[FINISHED] {repository} PR #{number} in {duration(started, ended)}; no eligible findings could be mapped to the current diff.", [report_path])
                    else:
                        summary.append(f"#{number} reviewed")
                        send(f"[FINISHED] {repository} PR #{number} in {duration(started, ended)}", [report_path])
                    state["prs"][key] = state_entry
                    save_json(state_path, state)
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

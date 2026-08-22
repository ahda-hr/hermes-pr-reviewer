# Hermes PR Reviewer

Cross-platform, scheduled GitHub pull-request review using Codex, GitHub CLI, Hermes, and Telegram. The reviewer uses a persistent bare clone, creates a temporary worktree per PR, removes it when finished, and writes Markdown reports under `reports/<repo-slug>/`.

The bot accepts commands in its private direct message. It posts review progress and report attachments only to the configured Telegram group.

## Prerequisites

- Python 3.11 or newer
- Git, GitHub CLI (`gh`), Codex CLI, and Hermes on `PATH`
- A GitHub account authenticated by you: `gh auth login`
- A dedicated Telegram bot created through BotFather and added to the review group. Keep BotFather privacy mode enabled.

Install Git and GitHub CLI:

```powershell
# Windows: use winget or your preferred installer
winget install Git.Git GitHub.cli
```

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y git gh

# Arch
sudo pacman -Syu --needed git github-cli
```

## Setup

Clone the repository anywhere you want; all data directories are relative to that clone.

```bash
git clone https://github.com/ahda-hr/hermes-pr-reviewer.git
cd hermes-pr-reviewer
python scripts/setup.py
```

The setup command prompts for the bot token, your Telegram user ID, and the review group ID. It lists user-installed Codex skills and lets you select skills to request for every PR review. It then asks once whether schedules should run on `weekdays` or `every day`, asks for a 24-hour time such as `05:00` or `15:30`, and asks whether to add another time. Each entered time creates a separate Hermes schedule in the machine's local timezone.

The token is saved only in the local Hermes profile `.env`; it is never written to this repository. Authenticate GitHub separately after setup:

```bash
gh auth login
```

Edit the local `config.json` (created from `config.example.json`) to choose repositories:

```json
{
  "repos": [
    { "repo": "OWNER/REPOSITORY", "slug": "repository", "enabled": true }
  ]
}
```

Set `clearProxyEnvironment` to `true` only when inherited proxy variables point to an unavailable local proxy.

## Review prompt and Codex skills

Edit [`prompts/pr-review.md`](prompts/pr-review.md) to adjust the shared review instruction. It supports `{{pr_number}}`, `{{base_ref}}`, and `{{selected_skills}}`; all three are filled for every PR.

The setup selector only lists local folders with a `SKILL.md` file in `$CODEX_HOME/skills` (or `~/.codex/skills`) and excludes internal `.system` skills. The selected names are saved in local `config.json` as `codexSkills`; no skill files are copied into this repository. If a selected skill is missing later, the runner warns and continues with installed selections.

## Use

Run every enabled repository:

```bash
python scripts/run_pr_reviews.py --trigger terminal
```

Run one repository:

```bash
python scripts/run_pr_reviews.py --trigger terminal --repo OWNER/REPOSITORY
```

In the reviewer bot's private chat, send `/pr-review`. It starts a detached process; a second request while a review is active reports that it was not started. The bot may still perform other tasks in direct messages. It does not handle group requests.

Each report is named `<repo-slug>__<pr-number>__<pr-author>.md` and includes source/target details, start/end/total time, plus 🔴 blocking, 🟡 important, and 🟢 refactor-suggestion sections. Every finding includes file/line evidence, a suggested fix, and a plain-language copyable PR reply.

## Security and publication

`config.json`, report output, caches, worktrees, logs, and environment files are ignored by Git. Do not commit bot tokens, Telegram IDs, repository names you consider private, report content, or copied Hermes profile files.

Before publishing changes, run a secret scanner such as [gitleaks](https://github.com/gitleaks/gitleaks) and inspect `git status --ignored` carefully. Enable GitHub secret scanning and push protection for the repository.

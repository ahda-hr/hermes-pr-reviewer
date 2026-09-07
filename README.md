# Hermes PR Reviewer

Scheduled GitHub pull-request review using Codex and GitHub CLI. The reviewer keeps a persistent bare clone, creates a temporary worktree per PR, removes it when finished, and writes Markdown reports under `reports/<repo-slug>/`.

It supports two notification modes:

- **Terminal-only:** no Hermes or Telegram installation. Run reviews manually or schedule them with Linux cron; scheduled output is appended to `cache/scheduled-run.log`.
- **Hermes/Telegram:** Hermes receives `/pr-review` in a private Telegram chat, manages schedules, and posts review progress and report attachments to the configured Telegram group.

## Prerequisites

Every mode requires Python 3.11+, Git, GitHub CLI (`gh`), Codex CLI, and GitHub authentication:

```bash
gh auth login
```

Hermes/Telegram mode additionally requires Hermes on `PATH` and a dedicated Telegram bot created through BotFather and added to the review group. Keep BotFather privacy mode enabled.

Terminal-only scheduled mode additionally requires the Linux `crontab` command. Windows users can run the terminal command manually or configure Task Scheduler themselves.

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

Setup creates `config.json`, lets you choose Codex skills, and prompts for a notification mode:

- Choose `terminal` to run without Hermes or Telegram. Setup can optionally install or replace this repository's managed Linux cron schedules.
- Choose `hermes` to configure the Telegram bot token, private-chat user ID, review group ID, Hermes quick command, and Hermes-managed schedules.

`--skip-schedules` skips schedule changes. `--profile reviewer` selects the Hermes profile when using Hermes mode.

Edit the local `config.json` to choose repositories:

```json
{
  "repos": [
    { "repo": "OWNER/REPOSITORY", "slug": "repository", "enabled": true }
  ],
  "notifications": {
    "provider": "terminal"
  }
}
```

For Hermes delivery, configure instead:

```json
{
  "notifications": {
    "provider": "hermes",
    "hermes": {
      "profile": "reviewer",
      "target": "telegram"
    }
  }
}
```

`config.json` intentionally uses a new notification schema. Existing `hermesProfile` and `telegramTarget` fields are unsupported; run setup again or update the file to the schema above. Existing Hermes schedules created by older versions must be removed manually once.

Set `clearProxyEnvironment` to `true` only when inherited proxy variables point to an unavailable local proxy.

## Review prompt and Codex skills

Edit [`prompts/pr-review.md`](prompts/pr-review.md) to adjust the shared review instruction. It supports `{{pr_number}}`, `{{base_ref}}`, and `{{selected_skills}}`; all three are filled for every PR.

The setup selector only lists local folders with a `SKILL.md` file in `$CODEX_HOME/skills` (or `~/.codex/skills`) and excludes internal `.system` skills. The selected names are saved in local `config.json` as `codexSkills`; no skill files are copied into this repository. If a selected skill is missing later, the runner warns and continues with installed selections.

## Use

Run every enabled repository in the terminal:

```bash
python scripts/run_pr_reviews.py --trigger terminal
```

Run one repository:

```bash
python scripts/run_pr_reviews.py --trigger terminal --repo OWNER/REPOSITORY
```

The runner also accepts `--trigger scheduled` for scheduler launchers and `--trigger hermes` for Hermes quick commands. A terminal trigger always prints locally, even if the configured provider is Hermes. Scheduled runs use the configured provider.

In Hermes/Telegram mode, send `/pr-review` in the reviewer's private chat. It starts a detached process; a second request while a review is active reports that it was not started. The bot may still perform other tasks in direct messages and does not handle group requests.

Each report is named `<repo-slug>__<pr-number>__<pr-author>.md` and includes source/target details, start/end/total time, plus blocking, important, and refactor-suggestion sections. Every finding includes file/line evidence, a suggested fix, and a plain-language copyable PR reply.

## Security and publication

`config.json`, report output, caches, worktrees, logs, setup state, and environment files are ignored by Git. Do not commit bot tokens, Telegram IDs, repository names you consider private, report content, or copied Hermes profile files.

Before publishing changes, run a secret scanner such as [gitleaks](https://github.com/gitleaks/gitleaks) and inspect `git status --ignored` carefully. Enable GitHub secret scanning and push protection for the repository.

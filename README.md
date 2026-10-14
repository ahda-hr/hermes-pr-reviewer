# Hermes PR Reviewer

Scheduled GitHub pull-request review using Codex or Claude Code and GitHub CLI. The reviewer keeps a persistent bare clone, creates a temporary worktree per PR, removes it when finished, and writes Markdown reports under `reports/<repo-slug>/`.

It supports two notification modes:

- **Terminal-only:** no Hermes or Telegram installation. Run reviews manually or schedule them with a systemd user timer; scheduled output is available in both the user journal and `cache/scheduled-run.log`.
- **Hermes/Telegram:** Hermes receives `/pr-review` in a private Telegram chat, manages schedules, and posts review progress and report attachments to the configured Telegram group.

## Prerequisites

Every mode requires Python 3.11+, Git, GitHub CLI (`gh`), the review agent's CLI (Codex or Claude Code), and GitHub authentication:

```bash
gh auth login
```

Hermes/Telegram mode additionally requires Hermes on `PATH` and a dedicated Telegram bot created through BotFather and added to the review group. Keep BotFather privacy mode enabled.

Terminal-only scheduled mode requires Linux with systemd. Setup enables user lingering so schedules continue after logout and reboot; Windows users can run the terminal command manually or configure Task Scheduler themselves.

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

Setup creates `config.json`, lets you choose the review agent and its skills, and prompts for a notification mode:

- Choose `terminal` to run without Hermes or Telegram. Setup can optionally install or replace this repository's managed systemd user timer and removes its legacy managed cron block when present.
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

### Optional pending GitHub reviews

By default, reviews are report-only. To create a pending GitHub review containing inline comments for blocking and important findings, opt in with:

```json
{
  "githubReview": {
    "mode": "pending"
  }
}
```

The reviewer creates one draft review for the exact PR commit it analyzed. It uses each finding's copyable suggested reply verbatim, skips findings that cannot be attached to the current diff, and never submits the review. Open the PR while signed in as the same GitHub account used by `gh` to inspect, edit, and submit or discard the pending review. An unsubmitted tool-created draft blocks new automated reviews of that PR until it is resolved.

## Review agent

Choose the agent that performs each review in `config.json` (setup asks for this too):

```json
{
  "agent": { "provider": "claude", "model": "sonnet" }
}
```

- `provider` is `codex` (default) or `claude`. `model` is optional and passed straight to the CLI. Claude examples: `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`, or the aliases `sonnet`/`opus`/`haiku`. Codex examples: `gpt-6-luna`, `gpt-5.6-luna`, `gpt-5.6-terra` (run `codex` and check `~/.codex/models_cache.json` for what your account offers). JSON has no comments, so `config.example.json` carries these examples in a `_comment` field, which the runner ignores. If `model` is omitted the CLI default is used; for Codex it falls back to the legacy `codexModel` field.
- Codex runs with `--sandbox read-only`. Claude runs `claude -p` in the PR worktree with `--permission-mode dontAsk` and only read tools plus `git diff/log/show/blame` allowed.
- Scheduled runs need the agent to be authenticated non-interactively (Claude: an existing login or `ANTHROPIC_API_KEY` in the timer's environment).
- Skills are stored per agent: `codexSkills` (from `$CODEX_HOME/skills`) or `claudeSkills` (from `~/.claude/skills`, or `$CLAUDE_CONFIG_DIR/skills`).

## Review prompt and skills

Edit [`prompts/pr-review.md`](prompts/pr-review.md) to adjust the shared review instruction. It supports `{{pr_number}}`, `{{base_ref}}`, and `{{selected_skills}}`; all three are filled for every PR. The agent returns validated JSON findings, which the runner renders into the Markdown report and saves as an ignored JSON sidecar for pending-review publishing.

The setup selector only lists local folders with a `SKILL.md` file in the chosen agent's skills directory (`~/.codex/skills` or `~/.claude/skills`) and excludes internal `.system` skills. The selected names are saved in local `config.json` as `codexSkills` or `claudeSkills`; no skill files are copied into this repository. If a selected skill is missing later, the runner warns and continues with installed selections.

## Use

Install a terminal command on Linux/macOS:

```bash
python scripts/install_command.py
```

This installs `pr-review` in `~/.local/bin`, pointing to this checkout and the
Python interpreter used for installation. If that directory is not on `PATH`,
the installer prints the line to add to your shell configuration. Run the
installer again if you move the checkout or change Python installations.

Then run from any directory:

```bash
pr-review
pr-review --repo OWNER/REPOSITORY
pr-review --detach
```

The command uses this checkout's `config.json` and defaults to terminal output.
`--repo` selects an enabled repository already in that configuration.
Detached runs write output to `cache/runner.log` in this checkout.
Use `--bin-dir /path/on/PATH` to choose another installation directory.
The installer refuses to overwrite an unrelated existing command.

Run every enabled repository in the terminal:

```bash
python scripts/run_pr_reviews.py --trigger terminal
```

Run one repository:

```bash
python scripts/run_pr_reviews.py --trigger terminal --repo OWNER/REPOSITORY
```

The runner also accepts `--trigger scheduled` for scheduler launchers and `--trigger hermes` for Hermes quick commands. A terminal trigger always prints locally, even if the configured provider is Hermes. Scheduled runs use the configured provider.

For terminal-mode schedules, inspect the next run with:

```bash
systemctl --user list-timers hermes-pr-reviewer.timer
```

View scheduled-run output with either:

```bash
journalctl --user-unit=hermes-pr-reviewer.service
tail -f cache/scheduled-run.log
```

In Hermes/Telegram mode, send `/pr-review` in the reviewer's private chat. It starts a detached process; a second request while a review is active reports that it was not started. The bot may still perform other tasks in direct messages and does not handle group requests.

Each report is named `<repo-slug>__<pr-number>__<pr-author>.md` and has a matching JSON findings sidecar. Reports include source/target details, start/end/total time, plus blocking, important, and refactor-suggestion sections. Every finding includes file/line evidence, a suggested fix, and a plain-language copyable PR reply.

## Security and publication

`config.json`, report output, caches, worktrees, logs, setup state, and environment files are ignored by Git. Do not commit bot tokens, Telegram IDs, repository names you consider private, report content, or copied Hermes profile files.

Before publishing changes, run a secret scanner such as [gitleaks](https://github.com/gitleaks/gitleaks) and inspect `git status --ignored` carefully. Enable GitHub secret scanning and push protection for the repository.

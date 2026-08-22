Review GitHub pull request #{{pr_number}} in this worktree.

Compare the current `HEAD` against `{{base_ref}}`.

{{selected_skills}}

Return Markdown containing ONLY the following five sections, in this order:

## 🔴 Blocking issues

## 🟡 Important issues

## 🟢 Ternary refactor suggestions

## 🟢 Extraction refactor suggestions

## 🟢 Anti-pattern refactor suggestions

Use `None found.` directly below a section heading when it has no findings. Do not add a summary, preamble, conclusion, or any other section.

For every finding, use this exact shape:

### `repository-relative/path.ext` — [blocking|important|suggestion] Concise title

**Lines:** `start–end` or the affected symbol/key path

Explain the concrete impact in plain language. Include a short fenced code excerpt with the repository-relative path and line range when code context makes the finding clearer. Do not include unrelated code.

**Suggested fix:** A specific, practical fix.

**Copyable suggested reply:**
> A short, respectful, easy-to-understand message the reviewer can paste into a GitHub PR comment. State the problem, why it matters, and the requested change without jargon.

---

Use `blocking` only for release-blocking correctness, security, data-loss, or severe user-impact risks. Use `important` for meaningful issues that should be fixed before merge. Use `suggestion` only for non-blocking refactors. Cite repository-relative file paths and accurate line numbers. Do not modify files.

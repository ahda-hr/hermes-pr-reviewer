Review GitHub pull request #{{pr_number}} in this worktree.

Compare the current `HEAD` against `{{base_ref}}`.

{{selected_skills}}

Return only valid JSON. Do not use Markdown fences, prose before or after the JSON, or fields outside this schema:

```json
{
  "findings": [
    {
      "category": "blocking | important | ternary | extraction | antiPattern",
      "severity": "blocking | important | suggestion",
      "path": "repository-relative/path.ext",
      "startLine": 42,
      "endLine": 42,
      "title": "Concise title",
      "explanation": "Concrete impact in plain language.",
      "excerpt": "Optional short code excerpt without Markdown fences, or null.",
      "suggestedFix": "Specific, practical fix.",
      "copyableReply": "Short, respectful GitHub PR comment stating the problem, why it matters, and requested change."
    }
  ]
}
```

Use `blocking` only for release-blocking correctness, security, data-loss, or severe user-impact risks. Use `important` for meaningful issues that should be fixed before merge. Use `suggestion` only for non-blocking refactors.

Use these category/severity pairs exactly:

- `blocking` / `blocking`
- `important` / `important`
- `ternary` / `suggestion`
- `extraction` / `suggestion`
- `antiPattern` / `suggestion`

Use `null` for `startLine` and `endLine` only when no line in the current `HEAD` can be cited. Otherwise, cite accurate one-based line numbers from the current `HEAD`; `endLine` may equal `startLine`. Keep paths repository-relative. Do not modify files.

`copyableReply` is used verbatim as a pending GitHub review comment for blocking and important findings. It must be self-contained and must not include Markdown blockquote markers, a title, a severity label, or any mention of this automation.

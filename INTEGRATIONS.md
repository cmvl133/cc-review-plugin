# Integrating external tooling with review-deck

review-deck is a **sink**: anything that can write a JSON file can contribute to the review page — a Claude Code workflow, an agent, a git hook, a CI job, a linter wrapper, a five-line script. There is no plugin API, no registration, no SDK. The whole contract is:

1. a **data format** (a fragment of the `notes.ai.json` schema),
2. a **drop location** (`.code-review/<branch-slug>/contrib/`),
3. optional **project knobs** (`.claude/review-deck.json`).

review-deck knows nothing about your project's issue tracker, language, or pipeline. Your side maps *your* data onto the contract; our side merges, anchors, and renders.

## 1. The fragment format

A contrib fragment is a JSON object with **any subset** of these keys (all optional, unknown keys ignored):

```json
{
  "source": "review-task",
  "notes": [
    {
      "file": "src/auth.py",
      "line": 120,
      "severity": "warning",
      "title": "short summary",
      "body": "markdown body — what, why it matters, what to do"
    }
  ],
  "checklist": [
    {"item": "REQ-1: tokens must expire", "status": "missing"},
    {"item": "REQ-2: /me returns profile", "status": "done", "file": "src/routes.py", "line": 12}
  ],
  "triage": [
    {"file": "src/generated/api.ts", "attention": "mechanical", "reason": "generated client"}
  ],
  "tour": [
    {"title": "Entry point", "file": "src/routes.py", "line": 11, "body": "start here"}
  ],
  "overview": {"title": "...", "body": "markdown", "diagrams": [{"title": "...", "mermaid": "..."}]}
}
```

- **Anchoring: use `file` + `line`.** `line` is the line number in the *new* version of the file (old-file numbers resolve for deleted lines). This is the lowest common denominator — every tool has `file:line`. The internal `hunk_index` + `anchor_line_content` anchoring also works and wins when both are present. Lines outside the diff render "unanchored" at the top of the file section — never dropped.
- `severity` ∈ `info` | `suggestion` | `warning`. Map your scale down: e.g. BLOCKER/HIGH/CRITICAL → `warning`, MEDIUM/LOW → `suggestion`, informational → `info`. Unknown values become `info`.
- `checklist.status` ∈ `done` | `partial` | `missing` (e.g. COVERED → `done`, UNVERIFIABLE → `partial`). Unknown values become `partial`.
- `triage.attention` ∈ `risky` | `core` | `skim` | `mechanical` (invalid entries are skipped).
- `source` names your tool; it defaults to the fragment's filename stem and is shown as a badge on every contributed note and checklist item.

## 2. The drop location

```
<repo-root>/.code-review/<branch-slug>/contrib/<your-tool>.notes.json
```

`<branch-slug>` = branch name lowercased, non `[a-z0-9._-]` chars replaced with `-`. Create the directory if it doesn't exist (`.code-review/` is always gitignored). One file per tool; re-running your tool overwrites its own file.

The next `/review` on that branch picks up every `*.json` in the directory (sorted by name, deterministic) via `build_review.py --contrib-dir`. You can also call the generator yourself and pass fragments explicitly with repeated `--contrib FILE`.

Merge semantics:

- `notes` and `checklist` items are **appended** and tagged with the source badge.
- `triage` only fills in files the main review didn't classify — the conductor's judgment wins.
- `overview` and `tour` are taken from a contrib only when the main review has none (first contrib wins).
- Invalid entries are skipped with a warning on stderr; a malformed file never breaks the build.

## 3. Validating your adapter

```
python3 scripts/build_review.py --validate-contrib my-fragment.json
```

Prints a JSON report (`errors` = entries the merge would skip, `warnings` = quality issues such as unanchored notes) and exits non-zero on errors. No patch or output path needed — wire it into your adapter's tests.

## 4. Project knobs — `.claude/review-deck.json`

Committed project config read by the `/review` command (not by the generator):

```json
{
  "base": "devel",
  "exclude": ["composer.lock", "*.min.js", "public/build/**", "vendor/**"],
  "reviewers": ["my-architecture-reviewer", "my-security-reviewer"],
  "bundledReviewer": true
}
```

- `base` — default diff base for a clean working tree (`git diff <base>...HEAD` when `/review` is run without an argument and there is nothing staged or dirty).
- `exclude` — git pathspec excludes applied when generating the diff (lockfiles, generated bundles, vendored code).
- `reviewers` — names of *your project's* agents (`.claude/agents/*.md`) to run for critique notes in addition to the bundled `code-reviewer`. Each gets the same context brief + patch and its findings are merged as notes (the conductor normalizes their output to the schema).
- `bundledReviewer: false` — skip review-deck's own generic reviewer entirely and rely on your specialists.

All keys optional; no file means today's behavior.

## Example: adapting a multi-agent review pipeline

If your setup already produces findings like `{severity: "HIGH", location: "src/Foo.php:120", issue, fix}`, the adapter is a few lines at the end of your workflow:

```js
const SEV = {BLOCKER: 'warning', HIGH: 'warning', MEDIUM: 'suggestion', LOW: 'suggestion', INFO: 'info'}
const notes = findings.map(f => {
  const [file, line] = f.location.split(':')
  return {file, line: line ? +line : undefined, severity: SEV[f.severity] || 'info',
          title: f.issue.slice(0, 80), body: f.issue + (f.fix ? '\n\n**Fix:** ' + f.fix : '')}
})
// write to .code-review/<branch-slug>/contrib/review-task.notes.json
```

Requirement-coverage checkers map even more directly: `COVERED/PARTIAL/MISSING` → `done/partial/missing` in `checklist`.

---
description: Generate an interactive HTML code review of a git diff, with AI notes
argument-hint: "[ref-or-range]"
---

Generate an interactive HTML code review page for a git diff. Follow these steps exactly. Read `${CLAUDE_PLUGIN_ROOT}/skills/review-deck/SKILL.md` first if you have not already this session — it defines the directory layout, the notes JSON schema, and the anchoring rules you must follow.

**Hard rule: never hand-write the review HTML. It is only ever produced by `build_review.py`.** Your job is content (the notes JSON); the script's job is rendering.

## 1. Determine the diff

Argument given: `$ARGUMENTS`

- If an argument was given, treat it as a ref or range and diff it: `git diff <arg>` (works for `main...HEAD`, a single commit hash — for a single commit prefer `git diff <hash>^ <hash>` — or any range).
- If no argument: check `git diff --cached --stat`. If there are staged changes, use `git diff --cached`. Otherwise use `git diff HEAD` (working tree vs HEAD).
- If the resulting diff is empty, tell the user and stop.

## 2. Create the review directory and save the patch

- Repo root: `git rev-parse --show-toplevel`.
- Branch slug: `git rev-parse --abbrev-ref HEAD`, lowercased, with every character outside `[a-z0-9._-]` replaced by `-` (detached HEAD → use `detached`).
- Round dir name: for staged/working-tree reviews use `worktree`; for a ref/range use `git rev-parse --short` of the head of the range.
- Review dir: `<repo-root>/.code-review/<branch-slug>/<round-dir>/`. Create it and write the raw diff to `changes.patch` inside it. If the dir already exists from an earlier run, overwrite its contents — but first, if it contains a `comments.user.md`, note that file's path for step 5.

## 3. Produce the notes (conductor pattern)

You are the conductor: you may hold context the subagent cannot see.

**3a. Assemble a context brief** (under ~500 words) containing, when available: the implementation plan (from this conversation, a plan file, or CLAUDE.md), key decisions and their rationale, the methodology in use, and anything the user said about intent. If nothing is available (foreign/historical diff), assemble what you can from the repo (README, recent commit messages) and say so in the brief.

**3b. Explanatory notes (`info`)** — if the changes were authored in this session or you have a plan/context: write these yourself, as the author explaining intent. One note per meaningful piece of the change: what it does and *why it exists*. Do not delegate these — a context-free agent would only guess at intent. If you have no context at all, skip this step (the subagent covers it in 3c).

**3c. Critique notes (`warning`/`suggestion`)** — launch the `code-reviewer` agent. Pass it: the path to `changes.patch`, the full context brief, and the anchoring rules reminder. It returns a JSON object `{"notes": [...]}`. If you had no context for 3b, tell the agent it is in no-context mode and must also produce `info` explanatory notes.

**3d. Merge** both sets into one `notes.ai.json` in the review dir, following the schema in SKILL.md exactly: `version: 1`, `generated_at` (current UTC ISO-8601), `base`/`head` (the refs you diffed, e.g. `HEAD` and `worktree`, or the range endpoints), and `notes` renumbered sequentially as `n-001`, `n-002`, … in file order. Every note must use hunk-index + exact-line-content anchoring (1-based hunk index, line content copied verbatim from the patch, without the leading `+`/`-`/space).

## 4. Find previous rounds

Look for other round dirs under `<repo-root>/.code-review/<branch-slug>/` that contain a `comments.user.md` (including the one noted in step 2 if you are overwriting the same round dir — copy it aside first, e.g. to `comments.prev.md` in the new dir). Use the most recently modified one as the previous round.

## 5. Build the HTML

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/review-deck/scripts/build_review.py" \
  --patch <dir>/changes.patch \
  --notes <dir>/notes.ai.json \
  --out <dir>/deck-review.html \
  --notes-md <dir>/notes.ai.md \
  --title "<branch-slug>: <base>..<head>" \
  --ensure-gitignore <repo-root> \
  [--prev-comments <path-to-previous-comments.user.md>]
```

The script prints a JSON summary (files, hunks, anchored/unanchored notes, gitignore action). If any notes came out `notes_unanchored`, re-check their `anchor_line_content` against the patch and fix obvious mistakes (wrong hunk index, paraphrased line), then re-run. One retry is enough — a genuinely unanchorable note is rendered at the top of its file section, never dropped.

## 6. Report and open

- Tell the user the path to `review.html` and summarize: N files, N hunks, N AI notes (and how many carried-over comments, if any).
- If the script reported `gitignore: added` or `created`, mention that `.code-review/` was added to `.gitignore`.
- Try to open the page (run quietly; if it fails just tell the user to open the file manually — do not treat this as an error):
  - **WSL** (`grep -qi microsoft /proc/version`): `explorer.exe "$(wslpath -w <path>)"` — explorer.exe may return a non-zero exit code even on success; ignore it.
  - Linux: `xdg-open <path>`; macOS: `open <path>`; Windows: `start <path>`.
- Remind the user: comment in the browser, then "Connect review folder" (Chromium) or "Export comments" into the review dir, then run `/deck-respond`.

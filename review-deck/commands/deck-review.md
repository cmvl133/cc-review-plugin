---
description: Generate an interactive HTML code review of a git diff, with AI notes
argument-hint: "[ref-or-range]"
---

Generate an interactive HTML code review page for a git diff. Follow these steps exactly. Read `${CLAUDE_PLUGIN_ROOT}/skills/review-deck/SKILL.md` first if you have not already this session — it defines the directory layout, the notes JSON schema, and the anchoring rules you must follow.

**Hard rule: never hand-write the review HTML. It is only ever produced by `build_review.py`.** Your job is content (the notes JSON); the script's job is rendering.

## 0. Project config (optional)

Read `<repo-root>/.claude/review-deck.json` if it exists (all keys optional — see `${CLAUDE_PLUGIN_ROOT}/INTEGRATIONS.md`): `base` (default diff base), `exclude` (git pathspec excludes), `reviewers` (project agents for step 3d), `bundledReviewer` (default true). No file → defaults.

## 1. Determine the diff

Argument given: `$ARGUMENTS`

- If an argument was given, treat it as a ref or range and diff it: `git diff <arg>` (works for `main...HEAD`, a single commit hash — for a single commit prefer `git diff <hash>^ <hash>` — or any range).
- If no argument: check `git diff --cached --stat`. If there are staged changes, use `git diff --cached`. Otherwise, if the working tree is dirty, use `git diff HEAD` (working tree vs HEAD). Otherwise, if config `base` is set and the current branch differs from it, use `git diff <base>...HEAD`.
- If config `exclude` is set, append the pathspec to every diff command: `-- . ':(exclude)<pattern>'` for each pattern.
- If the resulting diff is empty, tell the user and stop.

## 2. Create the review directory and save the patch

- Repo root: `git rev-parse --show-toplevel`.
- Branch slug: `git rev-parse --abbrev-ref HEAD`, lowercased, with every character outside `[a-z0-9._-]` replaced by `-` (detached HEAD → use `detached`).
- Round dir name: for staged/working-tree reviews use `worktree`; for a ref/range use `git rev-parse --short` of the head of the range.
- Review dir: `<repo-root>/.code-review/<branch-slug>/<round-dir>/`. Create it and write the raw diff to `changes.patch` inside it. If the dir already exists from an earlier run, overwrite its contents — but first, if it contains a `comments.user.md`, note that file's path for step 5.

## 3. Produce the notes (conductor pattern)

You are the conductor: you may hold context the subagent cannot see.

**3a. Assemble a context brief** (under ~500 words) containing, when available: the implementation plan (from this conversation, a plan file, or CLAUDE.md), key decisions and their rationale, the methodology in use, and anything the user said about intent. If nothing is available (foreign/historical diff), assemble what you can from the repo (README, recent commit messages) and say so in the brief.

**3b. Overview, triage, tour, checklist** — write these `notes.ai.json` objects (schemas in SKILL.md), scaling each to the diff — omit what a small diff doesn't need:

- `overview`: a reader's introduction. `body` (markdown): what this change is, why it exists, and where to start reading — name the entry point and how control flows from it through the changed pieces. Add `diagrams` (mermaid source, e.g. `flowchart` or `sequenceDiagram`) **only when the change has a flow worth drawing** — a request path through a web app, a new pipeline, interacting components. A config tweak or single-function change needs no diagram.
- `triage`: classify **every** file — `risky` (security, money, concurrency, data migrations), `core` (real logic), `skim`, or `mechanical` (renames, lockfiles, generated code, boilerplate) — each with a short `reason`. Be honest about `mechanical`: its whole point is letting the reviewer safely skip it. Set `untested: true` on files whose changed logic no test in the diff exercises.
- `tour`: for diffs where reading order aids comprehension (≥3 interrelated files), 3–10 anchored steps telling the story of the change: entry point → core logic → periphery. Skip for trivial diffs.
- `checklist`: when you have a plan/intent (from the brief), map each plan item to `done`/`partial`/`missing` with an anchor where implemented. Its job is to catch what the change *silently didn't do* — never omit an item because it's missing from the diff; that's exactly the item to include.

**3c. Explanatory notes (`info`)** — if the changes were authored in this session or you have a plan/context: write these yourself, as the author explaining intent. One note per meaningful piece of the change: what it does and *why it exists*. Do not delegate these — a context-free agent would only guess at intent. If you have no context at all, skip this step (the subagent covers it in 3d).

**3d. Critique notes (`warning`/`suggestion`)** — first check the previous round's `comments.user.md` (found as in step 4) for a `## dismissed AI notes` section (findings the user explicitly closed) and a `## note reactions` section (👍/👎 calibration: downvoted notes were noise — tell the reviewers what kinds of notes to avoid; upvoted kinds are worth reinforcing). Then launch the reviewer agents **in parallel**:

- the bundled `code-reviewer` agent (unless config sets `bundledReviewer: false`);
- every agent named in config `reviewers` (project-defined specialists).

Pass each: the path to `changes.patch`, the full context brief, the anchoring rules reminder, and — if any — the dismissed findings list with the instruction not to re-raise them or equivalent notes. The bundled agent returns `{"notes": [...]}`; project agents may return findings in their own shape — normalize them to the notes schema yourself (map their severities onto `warning`/`suggestion`/`info`; anchor by `file` + `line` when that's all they give). If you had no context for 3c, tell the agents they are in no-context mode and the bundled one must also produce `info` explanatory notes.

**3e. Merge** everything into one `notes.ai.json` in the review dir, following the schema in SKILL.md exactly: `version: 1`, `generated_at` (current UTC ISO-8601), `base`/`head` (the refs you diffed, e.g. `HEAD` and `worktree`, or the range endpoints), the `overview`/`triage`/`tour`/`checklist` objects from 3b, and `notes` renumbered sequentially as `n-001`, `n-002`, … in file order. Every note must use hunk-index + exact-line-content anchoring (1-based hunk index, line content copied verbatim from the patch, without the leading `+`/`-`/space).

## 4. Find previous rounds

Look for other round dirs under `<repo-root>/.code-review/<branch-slug>/` that contain a `comments.user.md` (including the one noted in step 2 if you are overwriting the same round dir — copy it aside first, e.g. to `comments.prev.md` in the new dir). Use the most recently modified one as the previous round.

## 5. Build the HTML

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/review-deck/scripts/build_review.py" \
  --patch <dir>/changes.patch \
  --notes <dir>/notes.ai.json \
  --out <dir>/review.html \
  --notes-md <dir>/notes.ai.md \
  --title "<branch-slug>: <base>..<head>" \
  --ensure-gitignore <repo-root> \
  --contrib-dir <repo-root>/.code-review/<branch-slug>/contrib \
  [--prev-comments <path-to-previous-comments.user.md>]
```

`--contrib-dir` merges fragments dropped by external tooling (workflows, hooks, CI — see `INTEGRATIONS.md`); a missing directory is fine. Contributed notes appear with a source badge — mention their count and sources in the final report.

The script prints a JSON summary (files, hunks, anchored/unanchored notes, overview/diagram counts, gitignore action). If any notes came out `notes_unanchored`, re-check their `anchor_line_content` against the patch and fix obvious mistakes (wrong hunk index, paraphrased line), then re-run. One retry is enough — a genuinely unanchorable note is rendered at the top of its file section, never dropped.

## 6. Register in the hub

Add the review to the cross-project hub (best effort — if this fails, mention it and continue, it must not block the review):

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/review-deck/scripts/build_hub.py" register \
  --repo-root <repo-root> --branch <branch-slug> --round <round-dir> \
  --review-html <dir>/review.html --title "<same title as step 5>" \
  --files <N> --hunks <N> --notes <N>
```

(counts come from step 5's JSON summary; the hub page itself is opened with `/deck-hub`.)

## 7. Report and open

- Tell the user the path to `review.html` and summarize: N files, N hunks, N AI notes, whether an overview/diagram was included (and how many carried-over comments, if any).
- If the script reported `gitignore: added` or `created`, mention that `.code-review/` was added to `.gitignore`.
- Try to open the page (run quietly; if it fails just tell the user to open the file manually — do not treat this as an error). **Always run the open command from inside the review directory** (`cd <dir> && …`) — some misconfigured HTML handlers (e.g. Electron apps) drop profile files into their working directory, and this keeps any such droppings inside the gitignored `.code-review/` instead of the repo root:
  - **WSL** (`grep -qi microsoft /proc/version`): `cd <dir> && explorer.exe "$(wslpath -w review.html)"` — explorer.exe may return a non-zero exit code even on success; ignore it.
  - Linux: `cd <dir> && xdg-open review.html`; macOS: `cd <dir> && open review.html`; Windows: `cd <dir> && start review.html`.
- Remind the user: comment in the browser, then "Connect review folder" (Chromium) or "Export comments" into the review dir, then run `/deck-respond`.

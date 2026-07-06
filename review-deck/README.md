# review-deck

A Claude Code plugin that turns any git diff into an **interactive, self-contained HTML code review page** — with AI explanatory notes anchored to the lines they explain, inline user commenting, themes, and keyboard navigation. Comments flow back to Claude for the next iteration, closing a two-way review loop.

AI generates a lot of code; you read more code than ever. Reading raw diffs in a terminal is exhausting — this gives you a comfortable review UI where the AI explains *why* each part of the change exists, and your replies become Claude's next work queue.

## Installation

### Recommended: the install script (no marketplace involved)

Claude Code auto-loads full plugins from `~/.claude/skills/` (global) and `<project>/.claude/skills/` (per project). The repo-root `install.sh` manages that for you:

```bash
./install.sh                 # global (symlink — 'git pull' is your update)
./install.sh --local [DIR]   # only for one project (default: current git repo)
./install.sh --copy          # copy instead of symlink; re-run to update
./install.sh uninstall [--local [DIR]]
```

Symlink installs track this checkout live (restart the session after pulling); `--copy` is for when the checkout may move or you want a frozen version. The script is idempotent, refuses to overwrite anything at the target that isn't review-deck, and warns if a duplicate marketplace install would make the plugin load twice.

### Alternatives

- **One session only:** `claude --plugin-dir /path/to/cc-review-plugin/review-deck`
- **Via a local marketplace** (this repository is one):

  ```
  /plugin marketplace add /path/to/cc-review-plugin
  /plugin install review-deck@cc-review-plugin
  ```

Verify with `claude plugin details review-deck@skills-dir` (script installs) or `claude --debug` / `/plugin`: you should see the `/deck-review` and `/deck-respond` commands, the `code-reviewer` agent, and the `review-deck` skill.

Requirements: `git`, `python3` (stdlib only — no pip installs), any modern browser.

## Usage

### `/deck-review [ref-or-range]`

Generates the review page.

- `/deck-review` — reviews **staged changes** if any, otherwise working tree vs `HEAD`.
- `/deck-review main...HEAD` — reviews a range.
- `/deck-review abc1234` — reviews a single commit.

Claude assembles a context brief (your plan, decisions, stated intent), writes explanatory `info` notes itself when it authored the changes (the author explaining intent), delegates critique (`warning`/`suggestion`) to the fresh-eyes `code-reviewer` subagent, merges everything into `notes.ai.json`, and runs the deterministic generator to produce `review.html`. It then opens the page in your browser.

### In the browser

- Click any diff line (or press `c` on the focused line) to comment. Comments support edit, delete, and resolve.
- **Saving comments**, two ways:
  1. **Connect review folder** (Chromium-only: Chrome, Edge, Brave, …) — uses the File System Access API to write `comments.user.md` live into the review directory as you type. Firefox and Safari do not support this API.
  2. **Export comments** (all browsers) — downloads `comments.user.md`; move it into the review directory. There is also **Copy as Markdown**.
  - Either way, comments are buffered in `localStorage` keyed by review id, so closing the tab loses nothing.
- Themes: light / dark / solarized / high-contrast / custom (color pickers), persisted. Switch freely — your eyes will thank you.
- Keyboard: `j`/`k` hunks, `n`/`p` AI notes, `c` comment, `v` mark file viewed, `?` help.
- The sticky header tracks files viewed, AI note count, and unresolved comments.

### `/deck-respond`

Reads the newest `comments.user.md` for the current branch, replies to each unresolved comment in-chat, proposes/applies code changes where a comment asks for one (with your confirmation), and offers to run `/deck-review` again. The next round's page carries over still-unresolved comments flagged **"from previous round"**.

## The review directory

Everything lives under `.code-review/` at the repo root (auto-added to `.gitignore` on every run):

```
.code-review/
└── <branch-slug>/               # branch name, slugified
    └── <round>/                 # short commit hash, or "worktree" for uncommitted diffs
        ├── changes.patch        # the exact diff that was reviewed
        ├── review.html          # the page (self-contained, zero network requests)
        ├── notes.ai.json        # AI notes — machine-readable source of truth
        ├── notes.ai.md          # the same notes, human-readable
        └── comments.user.md     # your comments, exported from the page
```

`comments.user.md` is plain, pleasant markdown — one `## <file> — hunk N` section per comment with the anchored line quoted, author, timestamp, and resolved status.

## Design choices (where the spec left room)

- **This repo doubles as the marketplace**: `.claude-plugin/marketplace.json` at the repo root points at `./review-deck`, whose own `.claude-plugin/` contains only `plugin.json`, per plugin conventions.
- **AI notes are rendered into the HTML by the script** (server-side, visible without JS); **user comments are rendered client-side** from an embedded JSON blob merged with `localStorage` — the page is the comment editor, so it owns that state. Previous-round unresolved comments ride in via that blob, flagged and still editable/resolvable.
- **Notes anchor to hunk index (1-based) + exact line content**, with whitespace-stripped and unique-substring fallbacks, then a cross-hunk search; failures render "unanchored" at the file top, never dropped.
- **Determinism**: the script generates no timestamps or randomness; the default review id is a hash of the patch, so re-running on the same diff produces identical bytes and preserves your buffered comments.
- **Syntax highlighting** is a ~120-line per-line tokenizer (keywords / strings / comments / numbers) with language families (python, C-like, ruby, shell, sql, css, json, yaml, html) picked by file extension. Multi-line block comments don't carry highlight state across lines — a deliberate simplicity trade-off.
- The raw diff is kept as `changes.patch` in each round dir (not in the spec's file list, but essential for reproducing the page and for `/deck-respond` context).
- A body line consisting solely of `---` inside a comment is escaped on export (`\---`) so it can't terminate the section early.

## Limitations

- File System Access API is Chromium-only; everyone else uses Export/Copy.
- The embedded tokenizer is approximate by design (~200-line budget) — it's a reading aid, not a compiler.
- Comment bodies are treated as plain text in the page (rendered with whitespace preserved), and parsed leniently from `comments.user.md`.

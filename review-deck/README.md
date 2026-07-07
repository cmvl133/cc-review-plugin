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

Verify with `claude plugin details review-deck@skills-dir` (script installs) or `claude --debug` / `/plugin`: you should see the `/deck-review`, `/deck-respond` and `/deck-hub` commands, the `code-reviewer` agent, and the `review-deck` skill.

Requirements: `git`, `python3` (stdlib only — no pip installs), any modern browser.

## Usage

### `/deck-review [ref-or-range]`

Generates the review page.

- `/deck-review` — reviews **staged changes** if any, otherwise working tree vs `HEAD`.
- `/deck-review main...HEAD` — reviews a range.
- `/deck-review abc1234` — reviews a single commit.

Claude assembles a context brief (your plan, decisions, stated intent), writes an **overview** — a reader's introduction at the top of the page: what the change is, why it exists, where the entry point is and how control flows from there, plus **mermaid diagrams** when the change has a flow worth drawing (a request path through a web app, a new pipeline; a config tweak gets no diagram). It writes explanatory `info` notes itself when it authored the changes (the author explaining intent), delegates critique (`warning`/`suggestion`) to the fresh-eyes `code-reviewer` subagent, merges everything into `notes.ai.json`, and runs the deterministic generator to produce `review.html`. It then opens the page in your browser.

Diagrams render fully offline: a vendored `mermaid.min.js` is inlined into the page, and only when diagrams are present (~2.6 MB extra; diagram-free pages stay small). Diagrams follow the light/dark theme switch.

Because AI-assisted work produces *a lot* of diff, Claude also budgets your attention:

- **Triage** — every file is classified `risky` / `core` / `skim` / `mechanical` (with a reason). Files are sorted hardest-first, mechanical ones come collapsed, and a filter bar lets you view one class at a time (plus one click to mark all mechanical files viewed). Files whose changed logic has no test in the diff get an **untested** badge.
- **Guided tour** — a sticky sidebar walking you through the diff in narrative order (entry point → core → periphery), each step deep-linking to the exact line, with prev/next.
- **Plan ↔ implementation checklist** — each plan item marked ✓ done / ≈ partial / ✗ missing with a link to where it lives in the diff. Catches what the change silently didn't do.

### In the browser

- Click any diff line (or press `c` on the focused line) to comment. Comments support edit, delete, and resolve.
- Every AI note has a **Dismiss** button — mark a note you don't want the AI to act on. Dismissed notes are dimmed, ride along in the `comments.user.md` export, and both `/deck-respond` and the next round's reviewer are instructed to leave them (and equivalent findings) alone. **Restore** un-dismisses.
- **Saving comments**:
  1. **Save to review folder / Connect review folder** (Chromium-only: Chrome, Edge, Brave, …) — pick the review round directory once; `comments.user.md` is written straight into it, and live from then on. The folder handle is remembered (IndexedDB), so on the next visit the page reconnects by itself — tick "Allow on every visit" in Chrome's permission prompt and it's fully automatic; otherwise it's one "Reconnect" click.
  2. In browsers without the File System Access API (Firefox, Safari) the button falls back to **Export comments** — a plain download to move into the review directory yourself (a browser can't write to an arbitrary path without a user-granted handle). There is also **Copy as Markdown**.
  - Either way, comments are buffered in `localStorage` keyed by review id, so closing the tab loses nothing.
- Changed lines get **word-level highlighting** — the exact edited span inside a modified line pair lights up, so long lines read at a glance.
- A **minimap** on the right edge shows change density plus note/comment markers across the whole diff — click or drag to jump.
- The side panel has two tabs: the **guided tour** and a **findings digest** — every AI note sorted by severity with jump links and "handled" checkboxes.
- Comments carry a **type** (`fix` / `question` / `nit` / `discuss`, plus one-click canned snippets) so `/deck-respond` knows whether to patch, answer, or discuss. AI notes take **👍/👎** — downvotes teach the next round's reviewers what you consider noise.
- The header shows an **estimated reading time left**, weighted by triage (mechanical files are nearly free) and ticking down as you mark files viewed.
- Marking a file **Viewed** collapses it; `]` marks the current file viewed and jumps to the next unviewed one (`Shift+J`/`K` hop between files); the page also remembers your scroll position, so you resume exactly where you left off.
- Themes: light / dark / solarized / high-contrast / Dracula / Nord / Gruvbox (dark & light) / Monokai / One Dark / Catppuccin (Mocha & Latte) / Tokyo Night / Rosé Pine / Everforest / Ayu Light / custom (color pickers), persisted. Switch freely — your eyes will thank you.
- Keyboard: `j`/`k` hunks, `n`/`p` AI notes, `c` comment, `v` mark file viewed, `?` help.
- The sticky header tracks files viewed, AI note count, and unresolved comments.
- **Arcade mode** (the 🎮 button): earn XP for reviewing — viewing files, commenting, resolving, dismissing — with confetti, levels, an ALL FILES VIEWED celebration, **achievements** (Nitpicker, Speedrunner, Night shift…), and a **rubber duck** 🦆 that waddles along the diff as you review and quacks when you comment. XP and achievements accumulate across all your reviews and show up in the hub's trophy case. Entirely optional, entirely silly, off by default.

### `/deck-hub`

One page with **every review across all your projects**. Each `/deck-review` run registers its page in a global registry (`~/.local/share/review-deck/registry.json`, or `$XDG_DATA_HOME/review-deck/`); `/deck-hub` regenerates a self-contained `index.html` from it — reviews grouped by repo, with branch/round, note counts, unresolved-comment badges, last activity, and a direct link to each page — and opens it. No server, no daemon: a static page rebuilt on demand. Entries whose `review.html` was deleted are pruned automatically on every rebuild.

The hub opens with **Review Wrapped** — your last 7 days at a glance (reviews, projects, unresolved comments, hottest repo) with a "Copy for Slack" button — and, once you've played in arcade mode, a **trophy case** of your achievements and XP level (all `file://` pages share localStorage, so the hub reads your stats with zero backend).

### Plugging in your own tooling

review-deck is a **sink for any pipeline that can write JSON**: your project's review workflows, agents, git hooks, CI jobs, or linter wrappers drop fragments (notes anchored by `file` + `line`, checklist items, triage, tour, overview) into `.code-review/<branch>/contrib/`, and the next `/deck-review` merges them into the page with a per-tool source badge. Project defaults (diff base, pathspec excludes, custom reviewer agents) live in a committed `.claude/review-deck.json`. The full contract — fragment schema, merge semantics, a validator (`--validate-contrib`), and adapter examples — is one page: [INTEGRATIONS.md](INTEGRATIONS.md).

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

`comments.user.md` is plain, pleasant markdown — one `## <file> — hunk N` section per comment with the anchored line quoted, author, timestamp, and resolved status, preceded by a `## dismissed AI notes` list when you dismissed any notes.

## Design choices (where the spec left room)

- **This repo doubles as the marketplace**: `.claude-plugin/marketplace.json` at the repo root points at `./review-deck`, whose own `.claude-plugin/` contains only `plugin.json`, per plugin conventions.
- **AI notes are rendered into the HTML by the script** (server-side, visible without JS); **user comments are rendered client-side** from an embedded JSON blob merged with `localStorage` — the page is the comment editor, so it owns that state. Previous-round unresolved comments ride in via that blob, flagged and still editable/resolvable.
- **Notes anchor to hunk index (1-based) + exact line content**, with whitespace-stripped and unique-substring fallbacks, then a cross-hunk search; failures render "unanchored" at the file top, never dropped.
- **Determinism**: the script generates no timestamps or randomness; the default review id is a hash of the patch, so re-running on the same diff produces identical bytes and preserves your buffered comments.
- **Mermaid is the one vendored dependency**: diagrams need a real renderer, so `assets/mermaid.min.js` (pinned, from jsDelivr) is inlined into pages that contain diagrams — keeping the zero-network-requests guarantee. Everything else stays hand-rolled.
- **The hub is a static page, not a service**: `build_hub.py` keeps `registry.json` + `index.html` under `$XDG_DATA_HOME/review-deck/` and rebuilds on demand; dead entries self-prune. Registration is local metadata only (paths and counts — no code leaves your machine).
- **Syntax highlighting** is a ~120-line per-line tokenizer (keywords / strings / comments / numbers) with language families (python, C-like, ruby, shell, sql, css, json, yaml, html) picked by file extension. Multi-line block comments don't carry highlight state across lines — a deliberate simplicity trade-off.
- The raw diff is kept as `changes.patch` in each round dir (not in the spec's file list, but essential for reproducing the page and for `/deck-respond` context).
- A body line consisting solely of `---` inside a comment is escaped on export (`\---`) so it can't terminate the section early.

## Limitations

- File System Access API is Chromium-only; everyone else uses Export/Copy.
- The embedded tokenizer is approximate by design (~200-line budget) — it's a reading aid, not a compiler.
- Comment bodies are treated as plain text in the page (rendered with whitespace preserved), and parsed leniently from `comments.user.md`.
- A review page with diagrams carries the inlined mermaid renderer (~2.6 MB). If a diagram's mermaid source fails to parse, it falls back to mermaid's inline error rendering — fix the source in `notes.ai.json` and re-run the build.
- The hub's `index.html` links reviews via `file://`, so it lists reviews from this machine only.

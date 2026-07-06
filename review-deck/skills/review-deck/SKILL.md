---
name: review-deck
description: Conventions for generating interactive HTML code reviews from git diffs — the .code-review/ directory layout, the notes.ai.json schema, note anchoring rules, and the build_review.py CLI. Use when running /deck-review or /deck-respond, or when working with .code-review/ directories, notes.ai.json, or comments.user.md files.
---

# review-deck conventions

review-deck turns a git diff into a self-contained interactive HTML review page. The division of labor is strict:

- **The model produces content only**: notes as structured JSON, replies to comments, code fixes.
- **`scripts/build_review.py` produces the HTML** — deterministically (byte-for-byte reproducible for the same inputs). **Never hand-write or edit `review.html`.** If the page needs to change, change the inputs (patch, notes, prev comments) and re-run the script.

## Directory layout

Everything lives under `.code-review/` at the repo root (always gitignored — the script's `--ensure-gitignore` flag maintains the entry):

```
.code-review/
└── <branch-slug>/                 # branch name lowercased, non [a-z0-9._-] chars → '-'
    └── <round>/                   # short head commit hash, or "worktree" for uncommitted diffs
        ├── changes.patch          # the raw unified diff that was reviewed
        ├── review.html            # generated page — script output only
        ├── notes.ai.json          # AI notes, machine-readable (source of truth)
        ├── notes.ai.md            # same notes, human-readable (script output, via --notes-md)
        └── comments.user.md       # user comments exported from the HTML page
```

## notes.ai.json schema

```json
{
  "version": 1,
  "generated_at": "2026-07-06T10:00:00Z",
  "base": "main",
  "head": "feature-branch",
  "overview": {
    "title": "What this change does",
    "body": "markdown intro: what the change is, why, entry point and how control flows through it",
    "diagrams": [
      {"title": "Request flow", "mermaid": "flowchart LR\\n  A[Client] --> B[handler]"}
    ]
  },
  "triage": [
    {"file": "src/auth.py", "attention": "risky", "reason": "short why", "untested": true}
  ],
  "tour": [
    {"title": "Entry point", "file": "src/routes.py", "hunk_index": 1,
     "anchor_line_content": "...", "body": "optional 1-2 sentences"}
  ],
  "checklist": [
    {"item": "plan item", "status": "done", "file": "src/auth.py",
     "hunk_index": 1, "anchor_line_content": "..."},
    {"item": "another plan item", "status": "missing"}
  ],
  "notes": [
    {
      "id": "n-001",
      "file": "src/auth.py",
      "hunk_index": 2,
      "anchor_line_content": "def verify_token(token: str) -> bool:",
      "severity": "info",
      "title": "short summary",
      "body": "markdown explanation of what this part does and why"
    }
  ]
}
```

- `severity` ∈ `info` (author explanation) | `suggestion` (improvement) | `warning` (risk).
- `file` is the post-change path as it appears in the diff (old path for deleted files).
- `id`s are sequential `n-001`, `n-002`, … in the merged file (subagent drafts use `r-###`; the conductor renumbers).
- `overview` is optional and renders at the top of the page: `body` is markdown; each diagram's `mermaid` is raw mermaid source (rendered client-side by the vendored `assets/mermaid.min.js`, which is inlined into the page only when diagrams are present — pages without diagrams stay small). Include diagrams only when the change has a flow worth drawing; scale the overview to the diff.
- `triage` (optional) classifies files so the reviewer can budget attention. `attention` ∈ `risky` (security/money/concurrency/migrations — read hardest) | `core` (real logic — read carefully; also the default for unlisted files) | `skim` (glance is enough) | `mechanical` (renames, generated code, boilerplate — rendered collapsed). `untested: true` flags files whose changed logic no test in this diff exercises. The page sorts files risky → core → skim → mechanical and offers filter buttons; every classified file should have a short `reason` (shown as badge tooltip).
- `tour` (optional) is an ordered reading path through the diff — the story of the change, not file order. Anchored like notes (`file` + `hunk_index` + `anchor_line_content`); renders as a sticky sidebar with prev/next. Include it when reading order genuinely aids comprehension (entry point → core → periphery); skip for small diffs.
- `checklist` (optional) maps plan items to their implementation: `status` ∈ `done` | `partial` | `missing`; anchored items get a "view" link. Include when a plan/intent exists — its job is catching what the change *silently didn't do*, so `missing`/`partial` items matter most. Anchors for `tour`/`checklist` fall back to the file header when unresolvable.

## Anchoring rules

Notes anchor to **hunk index + exact line content** — never absolute line numbers:

- `hunk_index` is **1-based** within that file's hunks in the patch.
- `anchor_line_content` is the line's content copied verbatim from the patch **without** the leading `+`/`-`/space marker.
- Resolution order (implemented in the script): exact match in the given hunk → whitespace-stripped match → unique whitespace-stripped substring match → same three passes across all of the file's hunks (accepted only if unambiguous).
- **Exception for external contributions:** an entry may instead carry `"line": N` — a plain new-file line number (old-file numbers resolve for deleted lines). This exists for external tools (see `INTEGRATIONS.md`), which only know `file:line`. Content anchoring wins when both are present; model-authored notes must keep using content anchors.
- Unresolvable anchors are **never dropped**: the note renders at the top of its file section with an "unanchored" badge. A note whose `file` isn't in the diff renders in a "Notes on files not in this diff" section.

## comments.user.md format

Written by the HTML page (export / File System Access API), parsed by the script for round-trips. An optional `## dismissed AI notes` section lists notes the user marked **Dismiss** in the page — the AI must not act on these and must not re-raise the same finding in later rounds (`build_review.py` ignores this section; it is consumed by `/deck-respond` and `/deck-review`). Then one section per comment, terminated by `---`:

```
# review-deck comments

- review: rd-<id>
- exported: <ISO-8601>

## dismissed AI notes

- n-002 · src/auth.py · auth flag only on /me

---

## src/auth.py — hunk 2

> def verify_token(token: str) -> bool:

- author: user
- time: 2026-07-06T10:00:00Z
- resolved: no

The comment body (may span multiple lines).

---
```

## The cross-project hub

`scripts/build_hub.py` maintains a registry of every generated review at `$XDG_DATA_HOME/review-deck/` (default `~/.local/share/review-deck/`): `registry.json` plus a self-contained `index.html` listing all reviews across all projects, grouped by repo, with `file://` links to each `review.html`.

- `/deck-review` registers each review after building it (`build_hub.py register …` — best effort, never blocks the review).
- `/deck-hub` rebuilds and opens the page (`build_hub.py build`).
- Every rebuild prunes entries whose `review.html` no longer exists — deleting a `.code-review/` dir is how reviews leave the hub. No daemon, no server: it's a static page regenerated on demand.

## External contributions (contrib fragments)

External tooling plugs in by dropping JSON fragments (any subset of `notes` / `triage` / `tour` / `checklist` / `overview`, anchored by `file` + `line`) into `<repo-root>/.code-review/<branch-slug>/contrib/`. `/deck-review` merges them via `--contrib-dir`: notes and checklist items are appended with a source badge, contrib triage only fills unclassified files, contrib overview/tour apply only when the main review has none. Invalid entries are skipped with stderr warnings — a bad fragment never breaks the build. Full contract, merge semantics, and adapter examples: `INTEGRATIONS.md` (plugin root). Project knobs (`base`, `exclude`, `reviewers`, `bundledReviewer`) live in `<repo-root>/.claude/review-deck.json`.

## build_review.py CLI

```
python3 scripts/build_review.py \
  --patch changes.patch          # required: raw unified diff
  --out review.html              # required: output page
  [--notes notes.ai.json]        # AI notes to render inline
  [--notes-md notes.ai.md]       # also emit the human-readable notes mirror
  [--prev-comments FILE]         # previous round's comments.user.md (repeatable);
                                 #   unresolved ones render flagged "from previous round"
  [--contrib FILE]               # external notes fragment to merge (repeatable)
  [--contrib-dir DIR]            # merge every *.json fragment from DIR (missing dir OK)
  [--validate-contrib FILE]      # validate fragment(s) and exit (no patch/out needed)
  [--title TITLE]                # page title
  [--review-id ID]               # stable id for the page's localStorage keying
                                 #   (default: sha256 of the patch, so re-runs on the
                                 #   same diff keep the user's buffered comments)
  [--ensure-gitignore REPO_ROOT] # ensure .code-review/ is in REPO_ROOT/.gitignore
```

It prints a JSON summary: file/hunk counts, `notes_anchored` / `notes_unanchored`, `prev_comments` carried over, and the gitignore action (`created`/`added`/`present`/`skipped`). If notes came out unanchored unexpectedly, fix their anchors (usually a paraphrased line or wrong hunk index) and re-run — same inputs always give the same bytes, so re-running is free.

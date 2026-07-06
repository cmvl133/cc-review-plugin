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

## Anchoring rules

Notes anchor to **hunk index + exact line content** — never absolute line numbers:

- `hunk_index` is **1-based** within that file's hunks in the patch.
- `anchor_line_content` is the line's content copied verbatim from the patch **without** the leading `+`/`-`/space marker.
- Resolution order (implemented in the script): exact match in the given hunk → whitespace-stripped match → unique whitespace-stripped substring match → same three passes across all of the file's hunks (accepted only if unambiguous).
- Unresolvable anchors are **never dropped**: the note renders at the top of its file section with an "unanchored" badge. A note whose `file` isn't in the diff renders in a "Notes on files not in this diff" section.

## comments.user.md format

Written by the HTML page (export / File System Access API), parsed by the script for round-trips. One section per comment, terminated by `---`:

```
# review-deck comments

- review: rd-<id>
- exported: <ISO-8601>

## src/auth.py — hunk 2

> def verify_token(token: str) -> bool:

- author: user
- time: 2026-07-06T10:00:00Z
- resolved: no

The comment body (may span multiple lines).

---
```

## build_review.py CLI

```
python3 scripts/build_review.py \
  --patch changes.patch          # required: raw unified diff
  --out review.html              # required: output page
  [--notes notes.ai.json]        # AI notes to render inline
  [--notes-md notes.ai.md]       # also emit the human-readable notes mirror
  [--prev-comments FILE]         # previous round's comments.user.md (repeatable);
                                 #   unresolved ones render flagged "from previous round"
  [--title TITLE]                # page title
  [--review-id ID]               # stable id for the page's localStorage keying
                                 #   (default: sha256 of the patch, so re-runs on the
                                 #   same diff keep the user's buffered comments)
  [--ensure-gitignore REPO_ROOT] # ensure .code-review/ is in REPO_ROOT/.gitignore
```

It prints a JSON summary: file/hunk counts, `notes_anchored` / `notes_unanchored`, `prev_comments` carried over, and the gitignore action (`created`/`added`/`present`/`skipped`). If notes came out unanchored unexpectedly, fix their anchors (usually a paraphrased line or wrong hunk index) and re-run — same inputs always give the same bytes, so re-running is free.

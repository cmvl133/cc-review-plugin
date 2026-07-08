---
description: Read the user's review comments from the latest review round and act on them
---

Process the user's comments from the most recent review-deck round and close the loop.

## 1. Locate the comments

- Repo root: `git rev-parse --show-toplevel`; branch slug as in `/review` (lowercased branch name, non `[a-z0-9._-]` chars → `-`).
- Find the newest round dir under `<repo-root>/.code-review/<branch-slug>/` (by modification time) that contains `comments.user.md`. If the branch has round dirs but none has `comments.user.md`, ask the user to export their comments from the review page first (the "Export comments" button downloads `comments.user.md` — it must be saved/moved into the round directory; with "Connect review folder" in Chromium it is written there automatically). Then stop and wait.

## 2. Parse and triage

`comments.user.md` contains one section per comment:

```
## <file> — hunk <N>

> <anchored line content>

- author: user
- time: <ISO-8601>
- resolved: yes|no

<body>

---
```

Read every section. Ignore `resolved: yes` comments except to note how many were already resolved.

If a `## dismissed AI notes` section is present, treat those notes as **explicitly closed by the user**: do not address them, do not fix what they describe, do not argue for them — only count them in the final summary. They also must not be re-raised in later review rounds.

## 3. Respond to each unresolved comment

For each unresolved comment, in file order:

1. Quote it briefly (file, the anchored line, the comment body).
2. Read the surrounding code as it exists *now* in the working tree (the diff may be stale).
3. Honor the comment's `type` when present — it removes the guesswork:
   - `fix` — the user wants a code change: propose the concrete edit (confirmation rules below).
   - `question` — answer in-chat; do **not** change code unless the answer reveals a real bug and the user agrees.
   - `nit` — small polish; batch all nits into one grouped proposal, don't deliberate over them individually.
   - `discuss` — respond with your reasoning/trade-offs; no code changes in this pass.
   - no type — infer from the body, as before.
4. If the comment requests a change (or clearly implies one), propose the concrete edit and ask the user to confirm before applying it — unless the user has already told you to apply everything, in which case apply directly. Group trivial confirmed fixes together rather than asking one by one.

## 4. Close the loop

- Summarize: how many comments were addressed with code changes, answered only, or deferred (and how many AI notes the user dismissed, if any).
- Offer to run `/review` again to generate the next review round (the new page will carry over any comments still unresolved, flagged "from previous round").

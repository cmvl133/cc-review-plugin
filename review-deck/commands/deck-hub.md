---
description: Open the review-deck hub — every review across all your projects on one page
---

Rebuild and open the cross-project review hub.

## 1. Rebuild the hub page

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/review-deck/scripts/build_hub.py" build
```

It prunes entries whose `review.html` no longer exists, regenerates the hub `index.html`, and prints a JSON summary (`index` path, `reviews` count, `pruned` count).

## 2. Report and open

- Tell the user how many reviews are listed (and how many stale entries were pruned, if any). If the count is 0, explain that reviews register themselves in the hub whenever `/deck-review` runs, and stop.
- Open the `index` path from the JSON summary (run quietly; if it fails just tell the user to open the file manually — do not treat this as an error). **Always run the open command from inside the hub directory** (`cd <dirname of index> && …`) — some misconfigured HTML handlers (e.g. Electron apps) drop profile files into their working directory, and this keeps any such droppings out of whatever project you happen to be in:
  - **WSL** (`grep -qi microsoft /proc/version`): `cd <dir> && explorer.exe "$(wslpath -w index.html)"` — explorer.exe may return a non-zero exit code even on success; ignore it.
  - Linux: `cd <dir> && xdg-open index.html`; macOS: `cd <dir> && open index.html`; Windows: `cd <dir> && start index.html`.

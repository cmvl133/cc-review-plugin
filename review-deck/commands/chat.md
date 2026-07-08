---
description: Start the deck-chat server — a browser chat with the Claude sessions that authored your reviews (POC)
---

Start (or reuse) the local deck-chat server and open its UI. It lets the user talk to the SPECIFIC Claude Code session that generated each registered review — across all repositories — from one browser page.

## 1. Check whether the server is already running

```
curl -sf http://127.0.0.1:7787/api/ping
```

If it responds, skip to step 3.

## 2. Start it in the background

```
nohup python3 "${CLAUDE_PLUGIN_ROOT}/skills/review-deck/scripts/deck_chat.py" serve \
  > /tmp/chat.log 2>&1 &
```

Wait a moment and re-check the ping. If it still doesn't respond, show the tail of `/tmp/chat.log` and stop.

## 3. Report and open

- Tell the user the UI is at `http://127.0.0.1:7787/` and how many reviews are listed (`/api/reviews` — count entries with `session_id`; reviews made before v0.6 have none and can't be chatted with until re-run).
- Open the URL (run quietly; on failure just tell the user to open it manually):
  - **WSL** (`grep -qi microsoft /proc/version`): `explorer.exe "http://127.0.0.1:7787/"` — non-zero exit code may occur even on success; ignore it. WSL2 forwards localhost, so the Windows browser reaches the Linux server directly.
  - Linux: `xdg-open http://127.0.0.1:7787/`; macOS: `open …`; Windows: `start …`.
- Remind the user: chat sessions get **read-only tools** (Read/Grep/Glob) — they can inspect the repo and discuss, not edit (POC safety). The hub page (`/hub`) shows a Chat button next to each review while this server runs. Stop the server with `pkill -f deck_chat.py`.

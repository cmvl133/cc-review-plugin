#!/usr/bin/env python3
"""deck_chat.py — POC: chat with the Claude Code session behind a review.

Part of the review-deck Claude Code plugin. Python 3 stdlib only.

A tiny local web server (127.0.0.1 only) that lets you talk to the SPECIFIC
Claude Code session that authored a change. For every review registered in
the hub with a --session-id, it can spawn

  claude -p --verbose --resume <session-id> \
         --input-format stream-json --output-format stream-json

as a long-lived subprocess (streaming input keeps it alive across turns),
with cwd set to the review's repo root, and bridges it to a browser chat UI
via Server-Sent Events. Works the same on Ubuntu and WSL (the Windows
browser reaches 127.0.0.1 through WSL2 localhost forwarding).

POC safety: the resumed session gets read-only tools (Read, Grep, Glob) —
everything else is denied by non-interactive default. It can look at the
repo and discuss; it cannot edit.

CLI:
  deck_chat.py serve [--port 7787]
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALLOWED_TOOLS = "Read,Grep,Glob"
DEFAULT_PORT = 7787


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "review-deck"


def find_session_cwd(session_id):
    """Locate the session's transcript under ~/.claude/projects/*/<id>.jsonl
    and return the cwd it was recorded with. `claude --resume` only finds a
    session when spawned from the same cwd, so this beats guessing."""
    for p in Path.home().glob(".claude/projects/*/%s.jsonl" % session_id):
        try:
            with p.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("cwd"):
                        return d["cwd"]
        except OSError:
            continue
    return None


def load_reviews():
    reg = data_dir() / "registry.json"
    try:
        doc = json.loads(reg.read_text(encoding="utf-8"))
        return [e for e in doc.get("reviews", []) if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# One chat = one long-lived `claude --resume` subprocess
# ---------------------------------------------------------------------------

class Chat:
    def __init__(self, session_id, repo_root):
        self.session_id = session_id
        self.repo_root = repo_root
        self.events = []            # [{kind, ...}] — full history for the UI
        self.cond = threading.Condition()
        self.proc = None
        self.err_tail = []

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        cmd = ["claude", "-p", "--verbose",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--include-partial-messages",
               "--resume", self.session_id,
               "--allowedTools", ALLOWED_TOOLS]
        cwd = find_session_cwd(self.session_id) or self.repo_root or None
        if cwd and not Path(cwd).is_dir():
            cwd = self.repo_root or None
        self.proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._push({"kind": "status", "text": "session process started"})

    def _push(self, ev):
        with self.cond:
            ev["i"] = len(self.events)
            self.events.append(ev)
            self.cond.notify_all()

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.err_tail = (self.err_tail + [line.rstrip()])[-10:]

    def _read_stdout(self):
        for line in self.proc.stdout:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "stream_event":
                ev = d.get("event", {})
                if ev.get("type") == "content_block_delta" \
                        and ev.get("delta", {}).get("type") == "text_delta":
                    self._push({"kind": "delta", "text": ev["delta"]["text"]})
            elif t == "assistant":
                texts, tools = [], []
                for b in d.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        texts.append(b["text"])
                    elif b.get("type") == "tool_use":
                        tools.append(b.get("name", "?"))
                if tools:
                    self._push({"kind": "tools", "names": tools})
                if texts:
                    self._push({"kind": "assistant", "text": "\n".join(texts)})
            elif t == "result":
                self._push({"kind": "result",
                            "ok": d.get("subtype") == "success",
                            "cost": d.get("total_cost_usd")})
        code = self.proc.wait()
        self._push({"kind": "status",
                    "text": "session process exited (%s)%s" % (
                        code, (": " + " | ".join(self.err_tail[-3:])) if code else "")})

    def send(self, text):
        if not self.alive():
            self.start()
        self._push({"kind": "user", "text": text})
        msg = {"type": "user",
               "message": {"role": "user",
                           "content": [{"type": "text", "text": text}]}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._push({"kind": "status", "text": "could not reach the session process"})

    def stop(self):
        if self.alive():
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()


CHATS = {}
CHATS_LOCK = threading.Lock()
REGISTRY_LOCK = threading.Lock()


def get_chat(session_id, repo_root):
    with CHATS_LOCK:
        if session_id not in CHATS:
            CHATS[session_id] = Chat(session_id, repo_root)
        return CHATS[session_id]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>review-deck chat</title>
<style>
*{box-sizing:border-box}
:root{--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--panel:#f6f8fa;--border:#d1d9e0;--accent:#0969da;--accent-fg:#fff}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--panel:#161b22;--border:#30363d;--accent:#4493f8;--accent-fg:#0d1117}}
body{margin:0;font:14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg);
  display:grid;grid-template-columns:280px 1fr;grid-template-rows:auto 1fr auto;height:100vh}
header{grid-column:1/3;display:flex;align-items:baseline;gap:12px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--border)}
header .brand{color:var(--accent);font-weight:700}
header .sub{color:var(--muted);font-size:12px}
#side{grid-row:2/4;border-right:1px solid var(--border);overflow-y:auto;background:var(--panel)}
#side h2{margin:0;padding:10px 14px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
#side a{display:block;padding:8px 14px;color:var(--fg);text-decoration:none;border-left:3px solid transparent;font-size:13px}
#side a .r{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
#side a .p{color:var(--muted);font-size:11px;display:block}
#side a:hover{background:var(--bg)}
#side a.cur{border-left-color:var(--accent);background:var(--bg)}
#side .none{padding:10px 14px;color:var(--muted);font-size:12px;font-style:italic}
#log{overflow-y:auto;padding:16px 20px}
.msg{max-width:820px;margin:0 auto 12px;padding:8px 14px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.msg.user{background:var(--accent);color:var(--accent-fg);margin-left:15%}
.msg.assistant{background:var(--panel);border:1px solid var(--border);margin-right:8%}
.msg.meta{color:var(--muted);font-size:12px;text-align:center;background:none;padding:2px}
#composer{display:flex;gap:8px;padding:12px 20px;border-top:1px solid var(--border);background:var(--panel)}
#composer textarea{flex:1;font:inherit;padding:8px 10px;border:1px solid var(--border);border-radius:8px;
  background:var(--bg);color:var(--fg);resize:none;min-height:44px;max-height:160px}
#composer button{font:inherit;padding:8px 18px;border-radius:8px;border:1px solid var(--accent);
  background:var(--accent);color:var(--accent-fg);cursor:pointer}
#composer button:disabled{opacity:.5}
.empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted)}
.typing{opacity:.7;font-style:italic}
</style>
</head>
<body>
<header>
  <span class="brand">review-deck chat</span>
  <span class="sub">talk to the session that wrote the change &middot; read-only tools (POC)</span>
</header>
<nav id="side"><h2>Reviews with a session</h2><div id="side-list"></div></nav>
<main id="log"><div class="empty">Pick a review on the left.</div></main>
<div id="composer">
  <textarea id="inp" placeholder="Ask the authoring session… (Ctrl+Enter to send)" disabled></textarea>
  <button id="send" disabled>Send</button>
</div>
<script>
'use strict';
var cur = null, es = null, streamEl = null;
function $(s){ return document.querySelector(s); }
function el(tag, cls, text){
  var e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text !== undefined) e.textContent = text;
  return e;
}
function addMsg(cls, text){
  var log = $('#log');
  if(log.firstChild && log.firstChild.className === 'empty') log.innerHTML = '';
  var m = el('div', 'msg ' + cls, text);
  log.appendChild(m);
  log.scrollTop = log.scrollHeight;
  return m;
}
function loadReviews(){
  fetch('/api/reviews').then(function(r){ return r.json(); }).then(function(list){
    var box = $('#side-list');
    box.innerHTML = '';
    var withSession = list.filter(function(e){ return e.session_id; });
    if(!withSession.length){
      box.appendChild(el('div', 'none', 'No reviews with a captured session yet. Run /review (v0.6+) in a project.'));
      return;
    }
    withSession.forEach(function(e){
      var a = el('a');
      a.href = '#' + e.session_id;
      a.innerHTML = '<span class="r">' + e.branch + ' @ ' + e.round + '</span>' +
                    '<span class="p">' + e.repo_name + '</span>';
      a.addEventListener('click', function(ev){ ev.preventDefault(); openChat(e, a); });
      box.appendChild(a);
      if(location.hash.slice(1) === e.session_id) openChat(e, a);
    });
  });
}
function openChat(e, aEl){
  if(es){ es.close(); es = null; }
  cur = e;
  streamEl = null;
  location.hash = e.session_id;
  Array.prototype.forEach.call(document.querySelectorAll('#side a'), function(x){ x.classList.remove('cur'); });
  aEl.classList.add('cur');
  $('#log').innerHTML = '';
  addMsg('meta', e.repo_name + ' · ' + e.branch + ' @ ' + e.round + ' · session ' + e.session_id.slice(0, 8) + '…');
  $('#inp').disabled = false;
  $('#send').disabled = false;
  $('#inp').focus();
  es = new EventSource('/api/events?session=' + encodeURIComponent(e.session_id) +
                       '&repo=' + encodeURIComponent(e.repo_root || ''));
  es.onmessage = function(m){
    var d = JSON.parse(m.data);
    if(d.kind === 'user'){ addMsg('user', d.text); streamEl = null; }
    else if(d.kind === 'delta'){
      if(!streamEl) streamEl = addMsg('assistant typing', '');
      streamEl.textContent += d.text;
      $('#log').scrollTop = $('#log').scrollHeight;
    }
    else if(d.kind === 'assistant'){
      if(streamEl){ streamEl.textContent = d.text; streamEl.classList.remove('typing'); streamEl = null; }
      else addMsg('assistant', d.text);
    }
    else if(d.kind === 'tools'){ addMsg('meta', '🔧 ' + d.names.join(', ')); streamEl = null; }
    else if(d.kind === 'result'){ streamEl = null; }
    else if(d.kind === 'status'){ addMsg('meta', d.text); }
  };
}
function send(){
  var t = $('#inp').value.trim();
  if(!t || !cur) return;
  $('#inp').value = '';
  fetch('/api/send', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session: cur.session_id, repo: cur.repo_root, text: t})});
}
$('#send').addEventListener('click', send);
$('#inp').addEventListener('keydown', function(e){
  if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); send(); }
});
loadReviews();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight (the review page POSTs JSON from a file:// origin)
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/ping":
            self._json({"ok": True})
        elif path == "/api/reviews":
            self._json(load_reviews())
        elif path == "/api/events":
            self._sse()
        else:
            self._json({"error": "not found"}, 404)

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _sse(self):
        q = self._query()
        sid = q.get("session", "")
        if not sid:
            self._json({"error": "session required"}, 400)
            return
        chat = get_chat(sid, q.get("repo", ""))
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        i = 0
        try:
            while True:
                with chat.cond:
                    while i >= len(chat.events):
                        chat.cond.wait(timeout=25)
                        if i >= len(chat.events):
                            # keep-alive comment so proxies/browser don't drop us
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    batch = chat.events[i:]
                    i = len(chat.events)
                for ev in batch:
                    self.wfile.write(b"data: " + json.dumps(ev).encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return
        if self.path == "/api/send":
            sid = body.get("session", "")
            text = (body.get("text") or "").strip()
            if not sid or not text:
                self._json({"error": "session and text required"}, 400)
                return
            chat = get_chat(sid, body.get("repo", ""))
            chat.send(text)
            self._json({"ok": True})
        elif self.path == "/api/save-comments":
            # Write comments.user.md next to the review page — the page sends
            # its own file path. Constrained: the path must be an .html inside
            # a .code-review/ directory (never an arbitrary write target).
            page = body.get("page", "")
            content = body.get("content", "")
            if not page or not isinstance(content, str):
                self._json({"error": "page and content required"}, 400)
                return
            if len(content) > 2 * 1024 * 1024:
                self._json({"error": "content too large"}, 400)
                return
            # WSL: a Windows browser sees file://wsl.localhost/<distro>/home/…
            # whose pathname starts with /<distro>; try with and without it.
            candidates = [page]
            parts = page.split("/")
            if len(parts) > 2:
                candidates.append("/" + "/".join(parts[2:]))
            for c in candidates:
                p = Path(c)
                if ".code-review" not in p.parts or p.suffix != ".html":
                    continue
                if not p.parent.is_dir():
                    continue
                target = p.parent / "comments.user.md"
                tmp = p.parent / ".comments.user.md.tmp"
                try:
                    tmp.write_text(content, encoding="utf-8")
                    tmp.replace(target)
                except OSError as e:
                    self._json({"error": str(e)}, 500)
                    return
                self._json({"ok": True, "path": str(target)})
                return
            self._json({"error": "review dir not found for %s" % page}, 404)
        elif self.path == "/api/remove-review":
            html_path = body.get("html", "")
            if not html_path:
                self._json({"error": "html required"}, 400)
                return
            reg_path = data_dir() / "registry.json"
            try:
                with REGISTRY_LOCK:
                    doc = json.loads(reg_path.read_text(encoding="utf-8"))
                    before = len(doc.get("reviews", []))
                    doc["reviews"] = [e for e in doc.get("reviews", [])
                                      if e.get("html") != html_path]
                    reg_path.write_text(
                        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                        encoding="utf-8")
                self._json({"ok": True, "removed": before - len(doc["reviews"])})
            except (OSError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/stop":
            with CHATS_LOCK:
                chat = CHATS.get(body.get("session", ""))
            if chat:
                chat.stop()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chat with the Claude Code sessions behind your reviews.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"url": "http://127.0.0.1:%d/" % args.port,
                      "reviews": len(load_reviews())}))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with CHATS_LOCK:
            for c in CHATS.values():
                c.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

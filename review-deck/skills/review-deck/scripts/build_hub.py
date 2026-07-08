#!/usr/bin/env python3
"""build_hub.py — cross-project registry of review-deck reviews + hub page.

Part of the review-deck Claude Code plugin. Python 3 stdlib only.
Keeps a registry of every generated review in
$XDG_DATA_HOME/review-deck/registry.json (default ~/.local/share/review-deck)
and renders it into a self-contained index.html linking to each review page.
Entries whose review.html no longer exists are dropped on every build —
deleting a .code-review/ dir is how a review leaves the hub.

CLI:
  build_hub.py register --repo-root PATH --branch SLUG --round ROUND \
                        --review-html PATH [--title T] \
                        [--files N] [--hunks N] [--notes N]
  build_hub.py build

Both commands rebuild index.html and print a JSON summary with its path.
"""

import argparse
import html
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "review-deck"


def load_registry(d):
    reg_path = d / "registry.json"
    if reg_path.is_file():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
            if isinstance(reg.get("reviews"), list):
                return reg
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "reviews": []}


def save_registry(d, reg):
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(
        json.dumps(reg, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")


def count_unresolved(html_path):
    """Count 'resolved: no' comment sections in a comments.user.md sitting
    next to the review page, if any."""
    cm = Path(html_path).parent / "comments.user.md"
    if not cm.is_file():
        return None
    try:
        text = cm.read_text(encoding="utf-8")
    except OSError:
        return None
    return len(re.findall(r"(?m)^- resolved:\s*no\s*$", text))


def load_config(d):
    """Optional global hub config: $XDG_DATA_HOME/review-deck/config.json."""
    p = d / "config.json"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_gitlab_mrs(cfg):
    """Fetch open MRs where the token's user is assignee or reviewer.
    cfg: {"url": ..., "token": ... or "token_env": ..., "insecure": bool}.
    Returns (mrs, error): mrs is a list of dicts, error a message or None."""
    url = (cfg.get("url") or "").rstrip("/")
    token = cfg.get("token") or os.environ.get(cfg.get("token_env") or "", "")
    if not url or not token:
        return [], "gitlab config incomplete (need url and token/token_env)"
    ctx = ssl._create_unverified_context() if cfg.get("insecure") else None

    def get(path, params=None):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(url + "/api/v4" + path + q,
                                     headers={"PRIVATE-TOKEN": token})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        me = get("/user")
        base = {"state": "opened", "scope": "all", "per_page": 50,
                "order_by": "updated_at"}
        authored = get("/merge_requests", dict(base, author_id=me["id"]))
        assigned = get("/merge_requests", dict(base, assignee_id=me["id"]))
        reviewing = get("/merge_requests", dict(base, reviewer_id=me["id"]))
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        return [], "gitlab fetch failed: %s" % e

    mrs = {}
    for mr, role in ([(m, "author") for m in authored]
                     + [(m, "assignee") for m in assigned]
                     + [(m, "reviewer") for m in reviewing]):
        e = mrs.setdefault(mr["id"], {
            "title": mr.get("title", "?"),
            "ref": (mr.get("references") or {}).get("full") or "!%s" % mr.get("iid", "?"),
            "url": mr.get("web_url", "#"),
            "notes": mr.get("user_notes_count", 0),
            "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
            "conflicts": bool(mr.get("has_conflicts")),
            "updated": (mr.get("updated_at") or "")[:16].replace("T", " "),
            "branch": mr.get("source_branch", ""),
            "roles": [],
        })
        if role not in e["roles"]:
            e["roles"].append(role)
    return list(mrs.values()), None


def render_gitlab_section(mrs, error):
    parts = ['<section id="mrs"><h2>Merge requests on you</h2>']
    if error:
        parts.append('<p class="mr-err">%s</p>' % esc(error))
    elif not mrs:
        parts.append('<p class="mr-err">No open merge requests assigned to you. 🎉</p>')
    else:
        parts.append('<table>')
        for m in mrs:
            badges = ""
            if m["draft"]:
                badges += '<span class="badge mr-draft">draft</span> '
            if m["conflicts"]:
                badges += '<span class="badge mr-conflict">conflicts</span> '
            if m["notes"]:
                badges += '<span class="badge">&#128172; %d</span> ' % m["notes"]
            roles = ", ".join(m["roles"])
            parts.append(
                '<tr><td class="branch"><a href="%s">%s</a> %s</td>'
                '<td>%s</td><td class="num">%s</td><td class="when">%s</td></tr>'
                % (esc(m["url"]), esc(m["ref"]), esc(m["title"]),
                   badges, esc(roles), esc(m["updated"])))
        parts.append('</table>')
    parts.append('</section>')
    return "".join(parts)


def prune(reg):
    kept, dropped = [], 0
    for e in reg["reviews"]:
        if Path(e.get("html", "")).is_file():
            kept.append(e)
        else:
            dropped += 1
    reg["reviews"] = kept
    return dropped


def esc(s):
    return html.escape(str(s), quote=True)


HUB_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>review-deck hub</title>
<style>
*{box-sizing:border-box}
:root{
  --bg:#ffffff;--fg:#1f2328;--muted:#59636e;--panel:#f6f8fa;--border:#d1d9e0;
  --accent:#0969da;--warn-bg:#fff8c5;--warn-fg:#9a6700;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--panel:#161b22;--border:#30363d;
  --accent:#4493f8;--warn-bg:#3a3000;--warn-fg:#d29922;
}}
body{margin:0;font:14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;display:flex;align-items:baseline;gap:12px;padding:10px 20px;
  background:var(--panel);border-bottom:1px solid var(--border)}
header .brand{color:var(--accent);font-weight:700}
header .sub{color:var(--muted);font-size:12px}
main{max-width:900px;margin:0 auto;padding:16px 20px}
.repo{border:1px solid var(--border);border-radius:8px;margin-bottom:16px;overflow:hidden}
.repo>h2{margin:0;padding:8px 14px;background:var(--panel);font-size:14px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;border-bottom:1px solid var(--border)}
.repo>h2 .path{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:7px 14px;text-align:left;border-top:1px solid var(--border)}
tr:first-child td{border-top:none}
td.branch{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
td.num,td.when{color:var(--muted);white-space:nowrap;font-size:12px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.badge{display:inline-block;padding:0 8px;border-radius:10px;font-size:11px;font-weight:600;
  background:var(--warn-bg);color:var(--warn-fg);white-space:nowrap}
.empty{color:var(--muted);font-style:italic;padding:24px 0;text-align:center}
#wrapped{border:1px solid var(--border);border-radius:8px;margin-bottom:16px;padding:12px 16px;background:var(--panel)}
#wrapped .w-head{display:flex;align-items:center;gap:10px}
#wrapped h2{margin:0;font-size:15px;flex:1}
#wrapped button{font:inherit;font-size:12px;padding:3px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--bg);color:var(--fg);cursor:pointer}
#wrapped button:hover{border-color:var(--accent)}
.w-grid{display:flex;gap:22px;margin:10px 0 4px;flex-wrap:wrap}
.w-stat{font-size:12px;color:var(--muted)}
.w-stat b{display:block;font-size:22px;color:var(--fg)}
.w-hot{margin:4px 0 0;font-size:12.5px;color:var(--muted)}
a.chat-btn{display:inline-block;margin-left:10px;padding:0 10px;border-radius:10px;font-size:11px;font-weight:600;
  background:var(--accent);color:#fff;text-decoration:none}
a.chat-btn:hover{text-decoration:none;opacity:.85}
#mrs{border:1px solid var(--border);border-radius:8px;margin-bottom:16px;overflow:hidden}
#mrs h2{margin:0;padding:8px 14px;background:var(--panel);font-size:14px;border-bottom:1px solid var(--border)}
#mrs .mr-err{margin:0;padding:10px 14px;color:var(--muted);font-size:12.5px;font-style:italic}
.rm-btn{margin-left:8px;padding:0 7px;border:1px solid var(--border);border-radius:6px;background:none;
  color:var(--muted);cursor:pointer;font-size:11px;line-height:1.6}
.rm-btn:hover{color:#ff6b6b;border-color:#ff6b6b}
.badge.mr-draft{background:var(--panel);color:var(--muted);border:1px solid var(--border)}
.badge.mr-conflict{background:#4a0000;color:#ff9f9f}
@media (prefers-color-scheme: light){.badge.mr-conflict{background:#ffebe9;color:#9a1c1c}}
</style>
</head>
<body>
<header>
  <span class="brand">review-deck hub</span>
  <span class="sub">@@COUNT@@ review(s) across @@REPO_COUNT@@ project(s) &middot; built @@BUILT@@</span>
</header>
<main>
<section id="wrapped">
  <div class="w-head"><h2>Review Wrapped &mdash; last 7 days</h2>
  <button id="btn-copy-wrap" title="Copy a summary as markdown">Copy for Slack</button></div>
  <div class="w-grid">
    <div class="w-stat"><b>@@WEEK_REVIEWS@@</b>reviews</div>
    <div class="w-stat"><b>@@WEEK_REPOS@@</b>projects</div>
    <div class="w-stat"><b>@@UNRESOLVED@@</b>unresolved comments</div>
    <div class="w-stat" id="w-xp" hidden><b>0</b><span></span></div>
  </div>
  <p class="w-hot">@@HOT_LINE@@</p>
</section>
@@MRS@@
@@BODY@@
</main>
<script>
/* WSL: when this page is opened from Windows as file://wsl.localhost/<distro>/...
   (or legacy file://wsl$/...), plain file:///home/... links would resolve against
   the Windows filesystem. Rewrite them to the same host+distro prefix the hub
   itself was opened with. Opened natively in Linux, location.host is empty and
   links are left untouched. */
(function(){
  if(location.protocol !== 'file:' || !location.host) return;
  var distro = location.pathname.split('/')[1];
  if(!distro) return;
  var prefix = 'file://' + location.host + '/' + distro;
  Array.prototype.forEach.call(document.querySelectorAll('a[href^="file:///"]'), function(a){
    a.href = prefix + a.href.slice('file://'.length);
  });
})();
</script>
<script>
(function(){
'use strict';
function lsGet(k, d){ try{ var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); }catch(e){ return d; } }
var xp = lsGet('rd:xp', 0), arcade = lsGet('rd:arcade', false);
if(arcade || xp > 0){
  var w = document.getElementById('w-xp');
  w.hidden = false;
  w.querySelector('b').textContent = xp + ' XP';
  w.querySelector('span').textContent = 'level ' + (1 + Math.floor(xp / 100));
}
/* chat buttons appear only when the deck-chat server is running (start it
   with /chat); the fetch works from file:// thanks to CORS * on the server */
fetch('http://127.0.0.1:7787/api/ping').then(function(r){ return r.json(); }).then(function(){
  Array.prototype.forEach.call(document.querySelectorAll('a.chat-btn'), function(a){ a.hidden = false; });
}).catch(function(){});

/* review removal: with the deck-chat server running the entry is removed from
   the registry for good; otherwise it is hidden locally (localStorage) and
   stays hidden across rebuilds. Files in .code-review/ are never touched. */
var hubHidden = lsGet('rd:hubHidden', []);
function lsSetHidden(){ try{ localStorage.setItem('rd:hubHidden', JSON.stringify(hubHidden)); }catch(e){} }
Array.prototype.forEach.call(document.querySelectorAll('tr[data-html]'), function(tr){
  if(hubHidden.indexOf(tr.getAttribute('data-html')) >= 0) tr.style.display = 'none';
});
Array.prototype.forEach.call(document.querySelectorAll('.rm-btn'), function(b){
  b.addEventListener('click', function(){
    var tr = b.closest('tr');
    var html = tr.getAttribute('data-html');
    if(!confirm('Remove this review from the hub?')) return;
    fetch('http://127.0.0.1:7787/api/remove-review', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({html: html})
    }).then(function(r){ if(!r.ok) throw 0; tr.remove(); })
      .catch(function(){
        hubHidden.push(html);
        lsSetHidden();
        tr.style.display = 'none';
      });
  });
});
document.getElementById('btn-copy-wrap').addEventListener('click', function(){
  var md = '**Review Wrapped — last 7 days**\n' +
    '- reviews: @@WEEK_REVIEWS@@ across @@WEEK_REPOS@@ project(s)\n' +
    '- unresolved comments: @@UNRESOLVED@@\n' +
    '@@HOT_MD@@' +
    (xp > 0 ? '- XP: ' + xp + ' (level ' + (1 + Math.floor(xp / 100)) + ')\n' : '');
  (navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(md) : Promise.reject())
    .then(function(){
      var b = document.getElementById('btn-copy-wrap');
      b.textContent = 'Copied!';
      setTimeout(function(){ b.textContent = 'Copy for Slack'; }, 1500);
    }, function(){});
});
})();
</script>
</body>
</html>
"""


def entry_mtime(e):
    """Last activity: the newer of review.html and comments.user.md."""
    p = Path(e["html"])
    t = p.stat().st_mtime
    cm = p.parent / "comments.user.md"
    if cm.is_file():
        t = max(t, cm.stat().st_mtime)
    return t


def build_index(d, reg):
    by_repo = {}
    for e in reg["reviews"]:
        by_repo.setdefault(e.get("repo_root", "?"), []).append(e)

    repo_blocks = []
    for repo_root, entries in by_repo.items():
        entries.sort(key=entry_mtime, reverse=True)
        rows = []
        for e in entries:
            when = datetime.fromtimestamp(entry_mtime(e)).strftime("%Y-%m-%d %H:%M")
            unresolved = count_unresolved(e["html"])
            badge = ('<span class="badge">%d unresolved</span>' % unresolved
                     if unresolved else "")
            counts = "%s files &middot; %s notes" % (e.get("files", "?"), e.get("notes", "?"))
            chat = ('<a class="chat-btn" data-session="%s" href="http://127.0.0.1:7787/#%s" hidden>Chat</a>'
                    % (esc(e["session_id"]), esc(e["session_id"]))
                    if e.get("session_id") else "")
            rows.append(
                '<tr data-html="%s"><td class="branch"><a href="%s">%s</a> @ %s</td>'
                '<td class="num">%s</td><td>%s</td><td class="when">%s%s'
                '<button class="rm-btn" title="Remove this review from the hub '
                '(the .code-review/ files stay on disk)">&#10005;</button></td></tr>'
                % (esc(e["html"]), esc(Path(e["html"]).as_uri()), esc(e.get("branch", "?")),
                   esc(e.get("round", "?")), counts, badge, esc(when), chat))
        name = Path(repo_root).name or repo_root
        repo_blocks.append((max(entry_mtime(e) for e in entries),
                            '<section class="repo"><h2>%s<span class="path">%s</span></h2>'
                            '<table>%s</table></section>'
                            % (esc(name), esc(repo_root), "".join(rows))))
    repo_blocks.sort(key=lambda b: b[0], reverse=True)

    body = "".join(b[1] for b in repo_blocks) if repo_blocks \
        else '<p class="empty">No reviews registered yet. Run /review in any project.</p>'

    cfg = load_config(d)
    mrs_html, gitlab_stat = "", "not configured"
    if isinstance(cfg.get("gitlab"), dict):
        mrs, gl_err = fetch_gitlab_mrs(cfg["gitlab"])
        mrs_html = render_gitlab_section(mrs, gl_err)
        gitlab_stat = gl_err or len(mrs)

    now_ts = datetime.now().timestamp()
    week = [e for e in reg["reviews"] if now_ts - entry_mtime(e) < 7 * 86400]
    week_repos = {e.get("repo_root") for e in week}
    unresolved = sum(count_unresolved(e["html"]) or 0 for e in reg["reviews"])
    hot_line = hot_md = ""
    if week:
        per_repo = {}
        for e in week:
            per_repo[e.get("repo_root", "?")] = per_repo.get(e.get("repo_root", "?"), 0) + 1
        hot_root, hot_n = max(per_repo.items(), key=lambda kv: kv[1])
        hot_name = Path(hot_root).name or hot_root
        hot_line = "Hottest project: <b>%s</b> (%d review%s this week)" % (
            esc(hot_name), hot_n, "s" if hot_n != 1 else "")
        hot_md = "- hottest project: %s (%d)\\n" % (hot_name.replace("'", ""), hot_n)

    out = (HUB_TEMPLATE
           .replace("@@COUNT@@", str(len(reg["reviews"])))
           .replace("@@REPO_COUNT@@", str(len(by_repo)))
           .replace("@@BUILT@@", esc(datetime.now().strftime("%Y-%m-%d %H:%M")))
           .replace("@@WEEK_REVIEWS@@", str(len(week)))
           .replace("@@WEEK_REPOS@@", str(len(week_repos)))
           .replace("@@UNRESOLVED@@", str(unresolved))
           .replace("@@HOT_LINE@@", hot_line)
           .replace("@@HOT_MD@@", hot_md)
           .replace("@@MRS@@", mrs_html)
           .replace("@@BODY@@", body))
    index = d / "index.html"
    d.mkdir(parents=True, exist_ok=True)
    index.write_text(out, encoding="utf-8")
    return index, gitlab_stat


def main(argv=None):
    ap = argparse.ArgumentParser(description="Maintain the cross-project review-deck hub.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    reg_p = sub.add_parser("register", help="add/update one review, then rebuild index.html")
    reg_p.add_argument("--repo-root", required=True)
    reg_p.add_argument("--branch", required=True)
    reg_p.add_argument("--round", required=True)
    reg_p.add_argument("--review-html", required=True)
    reg_p.add_argument("--title", default="")
    reg_p.add_argument("--session-id", default="",
                       help="Claude Code session id that authored the review (enables chat)")
    reg_p.add_argument("--files", type=int, default=None)
    reg_p.add_argument("--hunks", type=int, default=None)
    reg_p.add_argument("--notes", type=int, default=None)

    sub.add_parser("build", help="prune dead entries and rebuild index.html")

    args = ap.parse_args(argv)
    d = data_dir()
    reg = load_registry(d)

    if args.cmd == "register":
        html_path = str(Path(args.review_html).resolve())
        if not Path(html_path).is_file():
            print("error: no such file: %s" % html_path, file=sys.stderr)
            return 1
        key = (str(Path(args.repo_root).resolve()), args.branch, args.round)
        entry = {
            "repo_root": key[0], "repo_name": Path(key[0]).name or key[0],
            "branch": args.branch, "round": args.round,
            "title": args.title, "html": html_path,
        }
        if args.session_id:
            entry["session_id"] = args.session_id
        for k in ("files", "hunks", "notes"):
            v = getattr(args, k)
            if v is not None:
                entry[k] = v
        reg["reviews"] = [e for e in reg["reviews"]
                          if (e.get("repo_root"), e.get("branch"), e.get("round")) != key]
        reg["reviews"].append(entry)

    dropped = prune(reg)
    save_registry(d, reg)
    index, gitlab_stat = build_index(d, reg)

    print(json.dumps({"index": str(index), "reviews": len(reg["reviews"]),
                      "pruned": dropped, "gitlab": gitlab_stat},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

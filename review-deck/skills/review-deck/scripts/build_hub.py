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
import sys
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


HUB_TEMPLATE = """<!DOCTYPE html>
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
#gablota{margin-top:12px;border-top:1px solid var(--border);padding-top:10px}
#gablota h3{margin:0 0 8px;font-size:13px}
.ach-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.ach{display:flex;gap:8px;align-items:center;border:1px solid var(--border);border-radius:8px;
  padding:6px 10px;background:var(--bg);font-size:12px}
.ach .a-icon{font-size:20px}
.ach b{display:block;font-size:12px}
.ach .a-desc{color:var(--muted);font-size:11px}
.ach.locked{opacity:.45;filter:grayscale(1)}
.ach .a-date{color:var(--accent);font-size:10.5px}
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
  <div id="gablota" hidden><h3>Achievements</h3><div class="ach-grid"></div></div>
</section>
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
var CATALOG = [
  {id:'first-words', icon:'💬', name:'First words', desc:'Write your first comment'},
  {id:'nitpicker', icon:'🔬', name:'Nitpicker', desc:'10 comments in a single review'},
  {id:'completionist', icon:'✅', name:'Completionist', desc:'View every file in a review'},
  {id:'speedrunner', icon:'⚡', name:'Speedrunner', desc:'Full review in under 5 minutes'},
  {id:'night-shift', icon:'🌙', name:'Night shift', desc:'Review after 23:00'},
  {id:'marathon', icon:'🏃', name:'Marathon', desc:'Open 10 different reviews'},
  {id:'exterminator', icon:'🧯', name:'Exterminator', desc:'Handle every finding in the digest'},
  {id:'critic', icon:'🗑️', name:'Critic', desc:'Dismiss 5 AI notes (lifetime)'},
  {id:'level-5', icon:'🏆', name:'Level 5', desc:'Reach level 5'},
  {id:'insert-coin', icon:'🕹️', name:'Insert coin', desc:'Turn on arcade mode'}
];
var xp = lsGet('rd:xp', 0), ach = lsGet('rd:ach', {}), arcade = lsGet('rd:arcade', false);
if(arcade || xp > 0){
  var w = document.getElementById('w-xp');
  w.hidden = false;
  w.querySelector('b').textContent = xp + ' XP';
  w.querySelector('span').textContent = 'level ' + (1 + Math.floor(xp / 100));
}
var earned = Object.keys(ach).length;
if(arcade || earned > 0){
  document.getElementById('gablota').hidden = false;
  var grid = document.querySelector('.ach-grid');
  CATALOG.forEach(function(a){
    var d = document.createElement('div');
    d.className = 'ach' + (ach[a.id] ? '' : ' locked');
    d.innerHTML = '<span class="a-icon">' + a.icon + '</span><span><b>' + a.name + '</b>' +
      '<span class="a-desc">' + a.desc + '</span>' +
      (ach[a.id] ? '<span class="a-date">unlocked ' + ach[a.id] + '</span>' : '') + '</span>';
    grid.appendChild(d);
  });
}
document.getElementById('btn-copy-wrap').addEventListener('click', function(){
  var md = '**Review Wrapped — last 7 days**\n' +
    '- reviews: @@WEEK_REVIEWS@@ across @@WEEK_REPOS@@ project(s)\n' +
    '- unresolved comments: @@UNRESOLVED@@\n' +
    '@@HOT_MD@@' +
    (xp > 0 ? '- XP: ' + xp + ' (level ' + (1 + Math.floor(xp / 100)) + '), achievements: ' + earned + '/' + CATALOG.length + '\n' : '');
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
            rows.append(
                '<tr><td class="branch"><a href="%s">%s</a> @ %s</td>'
                '<td class="num">%s</td><td>%s</td><td class="when">%s</td></tr>'
                % (esc(Path(e["html"]).as_uri()), esc(e.get("branch", "?")),
                   esc(e.get("round", "?")), counts, badge, esc(when)))
        name = Path(repo_root).name or repo_root
        repo_blocks.append((max(entry_mtime(e) for e in entries),
                            '<section class="repo"><h2>%s<span class="path">%s</span></h2>'
                            '<table>%s</table></section>'
                            % (esc(name), esc(repo_root), "".join(rows))))
    repo_blocks.sort(key=lambda b: b[0], reverse=True)

    body = "".join(b[1] for b in repo_blocks) if repo_blocks \
        else '<p class="empty">No reviews registered yet. Run /deck-review in any project.</p>'

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
           .replace("@@BODY@@", body))
    index = d / "index.html"
    d.mkdir(parents=True, exist_ok=True)
    index.write_text(out, encoding="utf-8")
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description="Maintain the cross-project review-deck hub.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    reg_p = sub.add_parser("register", help="add/update one review, then rebuild index.html")
    reg_p.add_argument("--repo-root", required=True)
    reg_p.add_argument("--branch", required=True)
    reg_p.add_argument("--round", required=True)
    reg_p.add_argument("--review-html", required=True)
    reg_p.add_argument("--title", default="")
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
            "repo_root": key[0], "branch": args.branch, "round": args.round,
            "title": args.title, "html": html_path,
        }
        for k in ("files", "hunks", "notes"):
            v = getattr(args, k)
            if v is not None:
                entry[k] = v
        reg["reviews"] = [e for e in reg["reviews"]
                          if (e.get("repo_root"), e.get("branch"), e.get("round")) != key]
        reg["reviews"].append(entry)

    dropped = prune(reg)
    save_registry(d, reg)
    index = build_index(d, reg)

    print(json.dumps({"index": str(index), "reviews": len(reg["reviews"]),
                      "pruned": dropped}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

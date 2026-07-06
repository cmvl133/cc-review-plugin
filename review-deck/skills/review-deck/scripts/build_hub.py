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
</style>
</head>
<body>
<header>
  <span class="brand">review-deck hub</span>
  <span class="sub">@@COUNT@@ review(s) across @@REPO_COUNT@@ project(s) &middot; built @@BUILT@@</span>
</header>
<main>
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
    out = (HUB_TEMPLATE
           .replace("@@COUNT@@", str(len(reg["reviews"])))
           .replace("@@REPO_COUNT@@", str(len(by_repo)))
           .replace("@@BUILT@@", esc(datetime.now().strftime("%Y-%m-%d %H:%M")))
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

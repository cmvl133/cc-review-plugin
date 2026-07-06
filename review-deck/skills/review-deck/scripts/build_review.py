#!/usr/bin/env python3
"""build_review.py — deterministic diff+notes -> single-file HTML review page.

Part of the review-deck Claude Code plugin. Python 3 stdlib only.
The output HTML is byte-for-byte reproducible for the same inputs
(no timestamps or randomness are generated here).

CLI:
  build_review.py --patch changes.patch --notes notes.ai.json --out review.html
                  [--prev-comments comments.user.md ...]
                  [--notes-md notes.ai.md] [--title TITLE] [--review-id ID]
                  [--ensure-gitignore REPO_ROOT]
"""

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path

SEVERITIES = ("info", "suggestion", "warning")

# ---------------------------------------------------------------------------
# Unified diff parsing
# ---------------------------------------------------------------------------

class Line:
    __slots__ = ("kind", "old_no", "new_no", "text")

    def __init__(self, kind, old_no, new_no, text):
        self.kind = kind          # 'ctx' | 'add' | 'del' | 'meta'
        self.old_no = old_no
        self.new_no = new_no
        self.text = text


class Hunk:
    def __init__(self, header, old_start, new_start):
        self.header = header      # full "@@ ... @@ trailer" line
        self.old_start = old_start
        self.new_start = new_start
        self.lines = []


class FileDiff:
    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.status = "modified"  # modified | added | deleted | renamed
        self.is_binary = False
        self.hunks = []

    @property
    def path(self):
        if self.status == "deleted":
            return self.old_path or self.new_path or "?"
        return self.new_path or self.old_path or "?"


_GIT_HDR = re.compile(r'^diff --git (?:"?a/(.*?)"?) (?:"?b/(.*?)"?)\s*$')
_HUNK_HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _strip_prefix(p):
    if p == "/dev/null":
        return None
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text):
    files = []
    cur = None
    hunk = None
    old_no = new_no = 0

    for raw in text.splitlines():
        m = _GIT_HDR.match(raw)
        if m:
            cur = FileDiff()
            cur.old_path, cur.new_path = m.group(1), m.group(2)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is not None and (raw == "" or raw[0] in "+- \\"):
            # inside a hunk, content lines win over header lookalikes
            # (e.g. a removed SQL comment line "--- foo")
            if raw.startswith("+"):
                hunk.lines.append(Line("add", None, new_no, raw[1:]))
                new_no += 1
            elif raw.startswith("-"):
                hunk.lines.append(Line("del", old_no, None, raw[1:]))
                old_no += 1
            elif raw.startswith("\\"):
                hunk.lines.append(Line("meta", None, None, raw))
            else:
                hunk.lines.append(Line("ctx", old_no, new_no, raw[1:]))
                old_no += 1
                new_no += 1
            continue
        if raw.startswith("new file mode"):
            cur.status = "added"
            continue
        if raw.startswith("deleted file mode"):
            cur.status = "deleted"
            continue
        if raw.startswith("rename from "):
            cur.status = "renamed"
            cur.old_path = raw[len("rename from "):]
            continue
        if raw.startswith("rename to "):
            cur.status = "renamed"
            cur.new_path = raw[len("rename to "):]
            continue
        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            cur.is_binary = True
            continue
        if raw.startswith("--- "):
            p = _strip_prefix(raw[4:])
            if p is not None:
                cur.old_path = p
            elif cur.status == "modified":
                cur.status = "added"
            continue
        if raw.startswith("+++ "):
            p = _strip_prefix(raw[4:])
            if p is not None:
                cur.new_path = p
            elif cur.status == "modified":
                cur.status = "deleted"
            continue
        m = _HUNK_HDR.match(raw)
        if m:
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            hunk = Hunk(raw, old_no, new_no)
            cur.hunks.append(hunk)
            continue
    return files


# ---------------------------------------------------------------------------
# Anchoring (notes + previous-round comments)
# ---------------------------------------------------------------------------

def _match_in_hunk(hunk, anchor):
    """Return line index in hunk, or None. Exact, then stripped-equal,
    then unique-substring (whitespace-stripped) fallback."""
    stripped = re.sub(r"\s+", "", anchor)
    if not stripped:
        return None
    for i, ln in enumerate(hunk.lines):
        if ln.kind != "meta" and ln.text == anchor:
            return i
    for i, ln in enumerate(hunk.lines):
        if ln.kind != "meta" and re.sub(r"\s+", "", ln.text) == stripped:
            return i
    subs = [i for i, ln in enumerate(hunk.lines)
            if ln.kind != "meta" and stripped in re.sub(r"\s+", "", ln.text)]
    if len(subs) == 1:
        return subs[0]
    return None


def resolve_anchor(fd, hunk_index, anchor):
    """Resolve (1-based hunk_index, anchor line content) against a FileDiff.
    Returns (hunk_idx0, line_idx) or None. Falls back to searching every
    hunk in the file (accepting only an unambiguous match)."""
    if not anchor or not fd.hunks:
        return None
    if isinstance(hunk_index, int) and 1 <= hunk_index <= len(fd.hunks):
        li = _match_in_hunk(fd.hunks[hunk_index - 1], anchor)
        if li is not None:
            return (hunk_index - 1, li)
    hits = []
    for hi, h in enumerate(fd.hunks):
        li = _match_in_hunk(h, anchor)
        if li is not None:
            hits.append((hi, li))
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# comments.user.md parsing (previous rounds)
# ---------------------------------------------------------------------------

_C_HDR = re.compile(r"^(.*?)\s+(?:—|--?)\s+hunk\s+(\d+)\s*$")


def parse_comments_md(text):
    """Parse the comments.user.md format written by the HTML page.
    One '## <file> — hunk N' section per comment, terminated by '---'."""
    comments = []
    blocks = re.split(r"(?m)^## ", text)[1:]
    for block in blocks:
        lines = block.splitlines()
        m = _C_HDR.match(lines[0]) if lines else None
        if not m:
            continue
        c = {
            "file": m.group(1).strip(),
            "hunk": int(m.group(2)),
            "line": "",
            "time": "",
            "resolved": False,
            "body": "",
        }
        body = []
        in_body = False
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            if not in_body:
                if ln.startswith("> ") and not c["line"]:
                    c["line"] = ln[2:]
                    continue
                if ln == ">" and not c["line"]:
                    continue
                kv = re.match(r"^- (author|time|resolved):\s*(.*)$", ln)
                if kv:
                    key, val = kv.group(1), kv.group(2).strip()
                    if key == "time":
                        c["time"] = val
                    elif key == "resolved":
                        c["resolved"] = val.lower() in ("yes", "true", "1")
                    if key == "resolved":
                        in_body = True  # resolved is the last metadata line
                    continue
                if not ln.strip():
                    continue
                in_body = True
            body.append(ln)
        c["body"] = "\n".join(body).strip()
        if c["body"] or c["line"]:
            comments.append(c)
    return comments


# ---------------------------------------------------------------------------
# Minimal markdown -> HTML for note bodies (safe: escapes first)
# ---------------------------------------------------------------------------

def _md_inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def md_render(text):
    out = []
    lines = text.splitlines()
    i = 0
    para = []
    ul = []

    def flush_para():
        if para:
            out.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_ul():
        if ul:
            out.append("<ul>" + "".join("<li>%s</li>" % _md_inline(x) for x in ul) + "</ul>")
            ul.clear()

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            flush_para(); flush_ul()
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code), quote=False))
            i += 1
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", ln)
        if m:
            flush_para()
            ul.append(m.group(1))
            i += 1
            continue
        if not ln.strip():
            flush_para(); flush_ul()
            i += 1
            continue
        flush_ul()
        para.append(ln.strip())
        i += 1
    flush_para(); flush_ul()
    return "".join(out)


# ---------------------------------------------------------------------------
# notes.ai.md rendering (human-readable mirror of notes.ai.json)
# ---------------------------------------------------------------------------

def render_notes_md(notes_doc):
    out = ["# AI review notes", ""]
    out.append("- base: %s" % notes_doc.get("base", "?"))
    out.append("- head: %s" % notes_doc.get("head", "?"))
    out.append("- generated: %s" % notes_doc.get("generated_at", "?"))
    out.append("")
    groups = {}
    order = []
    for n in notes_doc.get("notes", []):
        key = (n.get("file", "?"), n.get("hunk_index"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(n)
    for (fpath, hidx) in order:
        hdr = "## %s — hunk %s" % (fpath, hidx if hidx is not None else "?")
        out.append(hdr)
        out.append("")
        for n in groups[(fpath, hidx)]:
            out.append("### [%s] %s (%s)" % (n.get("severity", "info"),
                                             n.get("title", ""), n.get("id", "")))
            out.append("")
            anchor = n.get("anchor_line_content", "")
            if anchor:
                out.append("> %s" % anchor)
                out.append("")
            out.append(n.get("body", "").strip())
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Language detection for the client-side tokenizer
# ---------------------------------------------------------------------------

EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "clike", ".jsx": "clike", ".ts": "clike", ".tsx": "clike",
    ".mjs": "clike", ".cjs": "clike", ".java": "clike", ".c": "clike",
    ".h": "clike", ".cpp": "clike", ".hpp": "clike", ".cc": "clike",
    ".cs": "clike", ".go": "clike", ".rs": "clike", ".kt": "clike",
    ".swift": "clike", ".php": "clike", ".scala": "clike",
    ".rb": "ruby", ".rake": "ruby",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".css": "css", ".scss": "css", ".less": "css",
    ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "yaml",
    ".html": "html", ".htm": "html", ".xml": "html", ".vue": "html", ".svelte": "html",
}


def detect_lang(path):
    return EXT_LANG.get(Path(path).suffix.lower(), "")


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def esc(s):
    return html.escape(s, quote=True)


def render_note_card(note, unanchored=False):
    sev = note.get("severity", "info")
    if sev not in SEVERITIES:
        sev = "info"
    badge_extra = '<span class="badge unanch">unanchored</span>' if unanchored else ""
    return (
        '<details class="ai-note sev-%s" open data-note="%s">'
        '<summary><span class="badge sev-b">%s</span>%s'
        '<span class="nt">%s</span><span class="nid">%s</span></summary>'
        '<div class="nb">%s</div></details>'
        % (sev, esc(note.get("id", "")), sev, badge_extra,
           esc(note.get("title", "")), esc(note.get("id", "")),
           md_render(note.get("body", "")))
    )


STATUS_BADGE = {
    "added": ("new file", "st-add"),
    "deleted": ("deleted", "st-del"),
    "renamed": ("renamed", "st-ren"),
    "modified": ("modified", "st-mod"),
}


def render_file_section(fd, fidx, anchored, unanchored_notes):
    """anchored: {(hunk_idx0, line_idx): [notes]}"""
    p = fd.path
    label, cls = STATUS_BADGE[fd.status]
    title = esc(p)
    if fd.status == "renamed" and fd.old_path != fd.new_path:
        title = "%s &rarr; %s" % (esc(fd.old_path or "?"), esc(fd.new_path or "?"))
    lang = detect_lang(p)

    parts = []
    parts.append('<details class="file" open data-file="%s" data-lang="%s" id="file-%d">'
                 % (esc(p), lang, fidx))
    parts.append('<summary><span class="fpath">%s</span>'
                 '<span class="badge %s">%s</span>'
                 '<label class="viewed-l"><input type="checkbox" class="viewed-cb" data-file="%s"> Viewed</label>'
                 '</summary>' % (title, cls, label, esc(p)))
    parts.append('<div class="unanchored" data-file="%s">' % esc(p))
    for n in unanchored_notes:
        parts.append(render_note_card(n, unanchored=True))
    parts.append('</div>')

    if fd.is_binary:
        parts.append('<div class="binary-msg">Binary file &mdash; contents not shown.</div>')
    elif not fd.hunks:
        parts.append('<div class="binary-msg">No hunks (mode change or pure rename).</div>')
    else:
        parts.append('<table class="diff"><colgroup><col class="c-no"><col class="c-no">'
                     '<col class="c-gl"><col class="c-code"></colgroup>')
        for hi, hunk in enumerate(fd.hunks):
            parts.append('<tbody class="hunk" data-file="%s" data-h="%d">' % (esc(p), hi + 1))
            parts.append('<tr class="hunkhdr"><td class="no"></td><td class="no"></td>'
                         '<td class="gl">&hellip;</td><td class="code">%s</td></tr>'
                         % esc(hunk.header))
            for li, ln in enumerate(hunk.lines):
                if ln.kind == "meta":
                    parts.append('<tr class="ln meta"><td class="no"></td><td class="no"></td>'
                                 '<td class="gl"></td><td class="code">%s</td></tr>' % esc(ln.text))
                    continue
                glyph = {"add": "+", "del": "-", "ctx": "&nbsp;"}[ln.kind]
                side = "old" if ln.kind == "del" else "new"
                parts.append(
                    '<tr class="ln %s" data-side="%s"><td class="no">%s</td><td class="no">%s</td>'
                    '<td class="gl">%s</td><td class="code">%s</td></tr>'
                    % (ln.kind, side,
                       ln.old_no if ln.old_no is not None else "",
                       ln.new_no if ln.new_no is not None else "",
                       glyph, esc(ln.text)))
                for note in anchored.get((hi, li), []):
                    parts.append('<tr class="ai-note-row"><td colspan="4">%s</td></tr>'
                                 % render_note_card(note))
            parts.append('</tbody>')
        parts.append('</table>')
    parts.append('</details>')
    return "".join(parts)


def build_html(files, notes_doc, prev_comments, title, review_id, template):
    notes = list(notes_doc.get("notes", []))
    by_file = {}
    for fd in files:
        by_file.setdefault(fd.path, fd)
        if fd.old_path and fd.old_path != fd.path:
            by_file.setdefault(fd.old_path, fd)

    anchored = {id(fd): {} for fd in files}
    unanchored = {id(fd): [] for fd in files}
    orphan_notes = []   # notes whose file isn't in the diff at all
    n_anchored = n_unanchored = 0

    for note in notes:
        fd = by_file.get(note.get("file", ""))
        if fd is None:
            orphan_notes.append(note)
            n_unanchored += 1
            continue
        pos = resolve_anchor(fd, note.get("hunk_index"), note.get("anchor_line_content", ""))
        if pos is None:
            unanchored[id(fd)].append(note)
            n_unanchored += 1
        else:
            anchored[id(fd)].setdefault(pos, []).append(note)
            n_anchored += 1

    body = []
    if orphan_notes:
        body.append('<section class="orphans"><h2>Notes on files not in this diff</h2>')
        for n in orphan_notes:
            body.append('<div class="orphan-file">%s</div>' % esc(n.get("file", "?")))
            body.append(render_note_card(n, unanchored=True))
        body.append('</section>')
    if not files:
        body.append('<p class="empty">No changes in this diff.</p>')
    for fidx, fd in enumerate(files):
        body.append(render_file_section(fd, fidx, anchored[id(fd)], unanchored[id(fd)]))

    prev = []
    for i, c in enumerate(c for c in prev_comments if not c["resolved"]):
        prev.append({
            "id": "prev-%03d" % (i + 1),
            "file": c["file"], "hunk": c["hunk"], "line": c["line"],
            "body": c["body"], "time": c["time"],
            "resolved": False, "round": "prev",
        })

    data = {
        "id": review_id,
        "title": title,
        "base": notes_doc.get("base", ""),
        "head": notes_doc.get("head", ""),
        "generated": notes_doc.get("generated_at", ""),
        "files": [{"path": fd.path, "lang": detect_lang(fd.path)} for fd in files],
        "prev": prev,
    }
    data_json = json.dumps(data, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).replace("</", "<\\/")

    refs = ""
    if notes_doc.get("base") or notes_doc.get("head"):
        refs = "%s .. %s" % (notes_doc.get("base", "?"), notes_doc.get("head", "?"))

    out = (template
           .replace("@@TITLE@@", esc(title))
           .replace("@@REFS@@", esc(refs))
           .replace("@@GENERATED@@", esc(notes_doc.get("generated_at", "")))
           .replace("@@FILE_COUNT@@", str(len(files)))
           .replace("@@NOTES_COUNT@@", str(len(notes)))
           .replace("@@BODY@@", "".join(body))
           .replace("@@DATA@@", data_json))
    stats = {"files": len(files),
             "hunks": sum(len(f.hunks) for f in files),
             "notes_anchored": n_anchored,
             "notes_unanchored": n_unanchored,
             "prev_comments": len(prev)}
    return out, stats


# ---------------------------------------------------------------------------
# .gitignore maintenance
# ---------------------------------------------------------------------------

def ensure_gitignore(repo_root):
    gi = Path(repo_root) / ".gitignore"
    entry_forms = {".code-review", ".code-review/", "/.code-review", "/.code-review/"}
    if not gi.exists():
        gi.write_text(".code-review/\n", encoding="utf-8")
        return "created"
    text = gi.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip() in entry_forms:
            return "present"
    if text and not text.endswith("\n"):
        text += "\n"
    gi.write_text(text + ".code-review/\n", encoding="utf-8")
    return "added"


# ---------------------------------------------------------------------------
# Embedded HTML template (placeholders: @@TITLE@@ @@REFS@@ @@GENERATED@@
# @@FILE_COUNT@@ @@NOTES_COUNT@@ @@BODY@@ @@DATA@@)
# ---------------------------------------------------------------------------

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>@@TITLE@@ &middot; review-deck</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
:root{
  --bg:#ffffff;--fg:#1f2328;--muted:#59636e;--panel:#f6f8fa;--border:#d1d9e0;
  --accent:#0969da;--accent-fg:#ffffff;
  --added-bg:#dafbe1;--removed-bg:#ffebe9;--note-bg:#f6f8fa;
  --warn:#9a6700;--sugg:#8250df;--info:#0969da;--res:#1a7f37;
  --kw:#cf222e;--str:#0a3069;--com:#6e7781;--num:#0550ae;
}
:root[data-theme="dark"],:root[data-theme="custom"]{
  --bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--panel:#161b22;--border:#30363d;
  --accent:#4493f8;--accent-fg:#0d1117;
  --added-bg:#182f1f;--removed-bg:#3b1619;--note-bg:#161b22;
  --warn:#d29922;--sugg:#ab7df8;--info:#4493f8;--res:#3fb950;
  --kw:#ff7b72;--str:#a5d6ff;--com:#8b949e;--num:#79c0ff;
}
:root[data-theme="solarized"]{
  --bg:#fdf6e3;--fg:#586e75;--muted:#93a1a1;--panel:#eee8d5;--border:#d9cfb2;
  --accent:#268bd2;--accent-fg:#fdf6e3;
  --added-bg:#e6ecd3;--removed-bg:#f6ded6;--note-bg:#eee8d5;
  --warn:#b58900;--sugg:#6c71c4;--info:#268bd2;--res:#859900;
  --kw:#859900;--str:#2aa198;--com:#93a1a1;--num:#d33682;
}
:root[data-theme="contrast"]{
  --bg:#000000;--fg:#ffffff;--muted:#c8c8c8;--panel:#101010;--border:#8a8a8a;
  --accent:#ffd700;--accent-fg:#000000;
  --added-bg:#003a00;--removed-bg:#4a0000;--note-bg:#0d0d0d;
  --warn:#ffd700;--sugg:#00e5e5;--info:#7fbfff;--res:#00e000;
  --kw:#ff8f8f;--str:#8fff8f;--com:#c8c8c8;--num:#8fc8ff;
}
#topbar{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:8px 16px;background:var(--panel);border-bottom:1px solid var(--border)}
#topbar .brand{color:var(--accent)}
#topbar .refs{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.tb-stats{display:flex;gap:12px;margin-left:auto;font-size:12px;color:var(--muted)}
.tb-stats .stat b{color:var(--fg)}
.tb-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
button,select{font:inherit;font-size:12px;padding:3px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--bg);color:var(--fg);cursor:pointer}
button:hover,select:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
#fsa-status{font-size:11px;color:var(--muted)}
#custom-theme{display:flex;gap:16px;flex-wrap:wrap;padding:8px 16px;background:var(--panel);
  border-bottom:1px solid var(--border);font-size:12px}
#custom-theme label{display:flex;align-items:center;gap:6px}
#custom-theme input[type=color]{width:36px;height:22px;padding:0;border:1px solid var(--border);background:none;cursor:pointer}
main{max-width:1200px;margin:0 auto;padding:16px}
.meta-line{color:var(--muted);font-size:12px;margin:0 0 12px}
details.file{border:1px solid var(--border);border-radius:8px;margin-bottom:16px;background:var(--bg);overflow:hidden}
details.file>summary{display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;
  background:var(--panel);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;list-style-position:outside}
details.file>summary .fpath{font-weight:600;word-break:break-all}
details.file[data-viewed="1"]>summary .fpath{color:var(--muted);text-decoration:line-through}
.viewed-l{margin-left:auto;display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);cursor:pointer}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;line-height:1.5;white-space:nowrap}
.st-add{background:var(--added-bg);color:var(--res)}
.st-del{background:var(--removed-bg);color:var(--kw)}
.st-ren{background:var(--note-bg);color:var(--sugg);border:1px solid var(--border)}
.st-mod{background:var(--note-bg);color:var(--muted);border:1px solid var(--border)}
.binary-msg,.empty{padding:12px;color:var(--muted);font-style:italic}
table.diff{width:100%;border-collapse:collapse;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12.5px;line-height:1.45}
col.c-no{width:44px}
col.c-gl{width:22px}
td.no{text-align:right;padding:0 6px;color:var(--muted);user-select:none;vertical-align:top;
  border-right:1px solid var(--border);font-size:11px}
td.gl{text-align:center;user-select:none;font-weight:700;vertical-align:top}
td.code{padding:0 8px;white-space:pre-wrap;word-break:break-all;vertical-align:top}
tr.hunkhdr td{background:var(--note-bg);color:var(--muted);padding:3px 8px;border-top:1px solid var(--border);
  border-bottom:1px solid var(--border);font-size:11.5px}
tr.ln.add td.code,tr.ln.add td.gl{background:var(--added-bg)}
tr.ln.add td.gl{color:var(--res)}
tr.ln.del td.code,tr.ln.del td.gl{background:var(--removed-bg)}
tr.ln.del td.gl{color:var(--kw)}
tr.ln.meta td{color:var(--muted);font-style:italic}
tr.ln:not(.meta){cursor:pointer}
tr.ln:not(.meta):hover td.code{box-shadow:inset 0 0 0 1px var(--accent)}
tr.ln.cur td.code{box-shadow:inset 0 0 0 2px var(--accent)}
.tok-kw{color:var(--kw)}
.tok-str{color:var(--str)}
.tok-com{color:var(--com);font-style:italic}
.tok-num{color:var(--num)}
.unanchored{padding:0 12px}
.unanchored .ai-note,.unanchored .ucomment{margin:8px 0}
.orphans{border:1px dashed var(--border);border-radius:8px;padding:12px;margin-bottom:16px}
.orphans h2{margin:0 0 8px;font-size:14px}
.orphan-file{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);margin-top:8px}
.ai-note{border:1px solid var(--border);border-left:4px solid var(--info);border-radius:6px;
  background:var(--note-bg);margin:4px 8px;font-family:-apple-system,"Segoe UI",Roboto,sans-serif}
.ai-note.sev-warning{border-left-color:var(--warn)}
.ai-note.sev-suggestion{border-left-color:var(--sugg)}
.ai-note>summary{display:flex;align-items:center;gap:8px;padding:6px 10px;cursor:pointer;font-size:13px}
.ai-note .nt{font-weight:600}
.ai-note .nid{margin-left:auto;color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
.sev-b{color:var(--bg)}
.ai-note.sev-info .sev-b{background:var(--info)}
.ai-note.sev-warning .sev-b{background:var(--warn)}
.ai-note.sev-suggestion .sev-b{background:var(--sugg)}
.badge.unanch{background:var(--removed-bg);color:var(--kw)}
.ai-note .nb{padding:2px 12px 8px;font-size:13px}
.ai-note .nb p{margin:6px 0}
.ai-note .nb code,.uc-body code{background:var(--panel);border:1px solid var(--border);border-radius:4px;
  padding:0 4px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ai-note .nb pre{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:8px;overflow-x:auto}
.ai-note .nb pre code{border:none;background:none;padding:0}
.ai-note.flash{outline:2px solid var(--accent)}
.ucomment{border:1px solid var(--accent);border-radius:6px;background:var(--bg);margin:4px 8px;
  font-family:-apple-system,"Segoe UI",Roboto,sans-serif}
.ucomment.resolved{opacity:.62;border-color:var(--border)}
.uc-head{display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:12px;flex-wrap:wrap}
.uc-b{background:var(--accent);color:var(--accent-fg)}
.prev-b{background:var(--warn);color:var(--bg)}
.res-b{background:var(--res);color:var(--bg)}
.uc-meta{color:var(--muted)}
.uc-actions{margin-left:auto;display:flex;gap:4px}
.uc-actions button{font-size:11px;padding:1px 8px}
.uc-body{padding:2px 12px 8px;font-size:13px;white-space:pre-wrap;word-break:break-word}
.composer{margin:4px 8px;font-family:-apple-system,"Segoe UI",Roboto,sans-serif}
.composer textarea{width:100%;min-height:64px;font:13px/1.5 inherit;font-family:inherit;padding:8px;
  border:1px solid var(--accent);border-radius:6px;background:var(--bg);color:var(--fg);resize:vertical}
.composer .c-btns{display:flex;gap:6px;margin-top:4px;justify-content:flex-end}
#help{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
#help .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:20px 28px;min-width:320px}
#help h2{margin:0 0 12px;font-size:15px}
#help table{border-collapse:collapse;font-size:13px}
#help td{padding:3px 10px 3px 0}
kbd{background:var(--bg);border:1px solid var(--border);border-bottom-width:2px;border-radius:4px;
  padding:0 6px;font-family:ui-monospace,monospace;font-size:12px}
#lost{border:1px dashed var(--warn);border-radius:8px;padding:12px;margin-bottom:16px}
#lost h2{margin:0 0 4px;font-size:14px}
#lost .hint{color:var(--muted);font-size:12px;margin:0 0 8px}
[hidden]{display:none!important}
</style>
</head>
<body>
<header id="topbar">
  <strong class="brand">review-deck</strong>
  <span class="refs">@@REFS@@</span>
  <div class="tb-stats">
    <span class="stat" id="stat-viewed">Files viewed <b>0/@@FILE_COUNT@@</b></span>
    <span class="stat"><b>@@NOTES_COUNT@@</b> AI notes</span>
    <span class="stat" id="stat-comments"><b>0</b> unresolved comments</span>
  </div>
  <div class="tb-actions">
    <select id="theme-select" aria-label="Theme">
      <option value="light">Light</option>
      <option value="dark">Dark</option>
      <option value="solarized">Solarized</option>
      <option value="contrast">High contrast</option>
      <option value="custom">Custom&hellip;</option>
    </select>
    <button id="btn-connect" title="Write comments.user.md live into the review folder (Chromium only)">Connect review folder</button>
    <span id="fsa-status"></span>
    <button id="btn-export" class="primary" title="Download comments.user.md">Export comments</button>
    <button id="btn-copy" title="Copy comments as Markdown">Copy as Markdown</button>
    <button id="btn-help" aria-label="Keyboard help">?</button>
  </div>
</header>
<div id="custom-theme" hidden>
  <label>Page background <input type="color" id="cb-bg"></label>
  <label>Text <input type="color" id="cb-fg"></label>
  <label>Accent <input type="color" id="cb-accent"></label>
  <label>Added background <input type="color" id="cb-add"></label>
  <label>Removed background <input type="color" id="cb-del"></label>
</div>
<main>
<p class="meta-line">Generated @@GENERATED@@ &middot; click any diff line or press <kbd>c</kbd> to comment &middot; <kbd>?</kbd> for keys</p>
@@BODY@@
</main>
<div id="help" hidden>
  <div class="card">
    <h2>Keyboard shortcuts</h2>
    <table>
      <tr><td><kbd>j</kbd> / <kbd>k</kbd></td><td>next / previous hunk</td></tr>
      <tr><td><kbd>n</kbd> / <kbd>p</kbd></td><td>next / previous AI note</td></tr>
      <tr><td><kbd>c</kbd></td><td>comment on focused line</td></tr>
      <tr><td><kbd>v</kbd></td><td>toggle current file viewed</td></tr>
      <tr><td><kbd>?</kbd></td><td>toggle this help</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>close composer / help</td></tr>
    </table>
  </div>
</div>
<script type="application/json" id="rd-data">@@DATA@@</script>
<script>
(function(){
'use strict';
var data = JSON.parse(document.getElementById('rd-data').textContent);
var K = 'rd:' + data.id + ':';
function $(s,el){return (el||document).querySelector(s);}
function $$(s,el){return Array.prototype.slice.call((el||document).querySelectorAll(s));}
function lsGet(k,d){try{var v=localStorage.getItem(k);return v===null?d:JSON.parse(v);}catch(e){return d;}}
function lsSet(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function stripWs(s){return String(s).replace(/\s+/g,'');}

/* ---------------- themes ---------------- */
var CUSTOM_VARS = {'cb-bg':'--bg','cb-fg':'--fg','cb-accent':'--accent','cb-add':'--added-bg','cb-del':'--removed-bg'};
var CUSTOM_DEFAULTS = {'--bg':'#0d1117','--fg':'#e6edf3','--accent':'#4493f8','--added-bg':'#182f1f','--removed-bg':'#3b1619'};
function applyTheme(name){
  var root = document.documentElement;
  root.setAttribute('data-theme', name);
  $('#custom-theme').hidden = name !== 'custom';
  Object.keys(CUSTOM_DEFAULTS).forEach(function(v){ root.style.removeProperty(v); });
  if(name === 'custom'){
    var custom = lsGet('rd:themeCustom', null) || CUSTOM_DEFAULTS;
    Object.keys(CUSTOM_DEFAULTS).forEach(function(v){
      root.style.setProperty(v, custom[v] || CUSTOM_DEFAULTS[v]);
    });
    Object.keys(CUSTOM_VARS).forEach(function(id){
      var inp = document.getElementById(id);
      if(inp) inp.value = custom[CUSTOM_VARS[id]] || CUSTOM_DEFAULTS[CUSTOM_VARS[id]];
    });
  }
  lsSet('rd:theme', name);
}
var themeSel = $('#theme-select');
themeSel.value = lsGet('rd:theme', 'light');
if(!themeSel.value) themeSel.value = 'light';
applyTheme(themeSel.value);
themeSel.addEventListener('change', function(){ applyTheme(themeSel.value); });
Object.keys(CUSTOM_VARS).forEach(function(id){
  var inp = document.getElementById(id);
  inp.addEventListener('input', function(){
    var custom = lsGet('rd:themeCustom', null) || Object.assign({}, CUSTOM_DEFAULTS);
    custom[CUSTOM_VARS[id]] = inp.value;
    lsSet('rd:themeCustom', custom);
    document.documentElement.style.setProperty(CUSTOM_VARS[id], inp.value);
  });
});

/* ---------------- tokenizer (~small, per-line) ---------------- */
var KW = {
  python:'def class return if elif else for while import from as with try except finally raise lambda pass break continue yield global nonlocal assert in is not and or del async await match case None True False self',
  clike:'function return if else for while do switch case default break continue new delete typeof instanceof in of var let const class extends super this import export from try catch finally throw async await yield static void int long float double char bool boolean short byte unsigned signed struct enum union public private protected virtual override final abstract interface implements package namespace using template typename operator sizeof goto null nullptr undefined true false fn mut impl trait pub use crate mod where func go chan defer map range select type string error nil val when object data sealed',
  ruby:'def end class module if elsif else unless case when while until for do return yield begin rescue ensure raise require require_relative include extend attr_accessor attr_reader attr_writer nil true false self and or not then new lambda proc',
  shell:'if then else elif fi for in do done while until case esac function local return export echo read exit set shift source alias unset trap true false',
  sql:'select from where insert into values update set delete create table alter drop index view join left right inner outer full cross on group by order having limit offset as and or not null is in exists between like primary key foreign references unique constraint default union all distinct case when then else end begin commit rollback',
  json:'true false null',
  yaml:'true false null yes no on off',
  css:'',
  html:''
};
var LANG = {
  python:{kw:KW.python, lc:'#', str:['"',"'"]},
  clike:{kw:KW.clike, lc:'//', bc:['/*','*/'], str:['"',"'",'`']},
  ruby:{kw:KW.ruby, lc:'#', str:['"',"'"]},
  shell:{kw:KW.shell, lc:'#', str:['"',"'"]},
  sql:{kw:KW.sql, lc:'--', str:["'"], ci:true},
  json:{kw:KW.json, str:['"']},
  yaml:{kw:KW.yaml, lc:'#', str:['"',"'"]},
  css:{kw:KW.css, bc:['/*','*/'], str:['"',"'"]},
  html:{kw:KW.html, bc:['<!--','-->'], str:['"',"'"]}
};
function span(cls, text){ return '<span class="tok-' + cls + '">' + escHtml(text) + '</span>'; }
function tokenizeLine(text, spec){
  if(!spec._set){
    spec._set = {};
    spec.kw.split(/\s+/).forEach(function(w){ if(w) spec._set[w] = 1; });
  }
  var out = '', i = 0, n = text.length;
  while(i < n){
    var ch = text[i];
    if(spec.lc && text.lastIndexOf(spec.lc, i) === i){
      out += span('com', text.slice(i)); break;
    }
    if(spec.bc && text.lastIndexOf(spec.bc[0], i) === i){
      var e = text.indexOf(spec.bc[1], i + spec.bc[0].length);
      e = e < 0 ? n : e + spec.bc[1].length;
      out += span('com', text.slice(i, e)); i = e; continue;
    }
    if(spec.str && spec.str.indexOf(ch) >= 0){
      var j = i + 1;
      while(j < n){
        if(text[j] === '\\') j += 2;
        else if(text[j] === ch){ j++; break; }
        else j++;
      }
      out += span('str', text.slice(i, Math.min(j, n))); i = Math.min(j, n); continue;
    }
    if(ch >= '0' && ch <= '9' && !/[\w$]/.test(text[i-1] || ' ')){
      var j2 = i + 1;
      while(j2 < n && /[\w.]/.test(text[j2])) j2++;
      out += span('num', text.slice(i, j2)); i = j2; continue;
    }
    if(/[A-Za-z_$]/.test(ch)){
      var j3 = i + 1;
      while(j3 < n && /[\w$]/.test(text[j3])) j3++;
      var w = text.slice(i, j3);
      var key = spec.ci ? w.toLowerCase() : w;
      out += spec._set[key] === 1 ? span('kw', w) : escHtml(w);
      i = j3; continue;
    }
    out += escHtml(ch); i++;
  }
  return out;
}
$$('details.file').forEach(function(f){
  var spec = LANG[f.getAttribute('data-lang')];
  if(!spec) return;
  $$('tr.ln:not(.meta) > td.code', f).forEach(function(td){
    td.innerHTML = tokenizeLine(td.textContent, spec);
  });
});

/* ---------------- viewed tracking ---------------- */
var viewed = lsGet(K + 'viewed', []);
function refreshViewed(){
  var total = data.files.length, done = 0;
  $$('.viewed-cb').forEach(function(cb){
    var on = viewed.indexOf(cb.getAttribute('data-file')) >= 0;
    cb.checked = on;
    cb.closest('details.file').setAttribute('data-viewed', on ? '1' : '0');
    if(on) done++;
  });
  $('#stat-viewed').innerHTML = 'Files viewed <b>' + done + '/' + total + '</b>';
}
function toggleViewed(path){
  var i = viewed.indexOf(path);
  if(i >= 0) viewed.splice(i, 1); else viewed.push(path);
  lsSet(K + 'viewed', viewed);
  refreshViewed();
}
$$('.viewed-cb').forEach(function(cb){
  cb.addEventListener('click', function(e){ e.stopPropagation(); });
  cb.addEventListener('change', function(){ toggleViewed(cb.getAttribute('data-file')); });
});
$$('.viewed-l').forEach(function(l){ l.addEventListener('click', function(e){ e.stopPropagation(); }); });
refreshViewed();

/* ---------------- comment store ---------------- */
var store = lsGet(K + 'comments', null) || {v:1, items:[]};
var deletedIds = lsGet(K + 'deleted', []);
data.prev.forEach(function(p){
  if(deletedIds.indexOf(p.id) >= 0) return;
  var exists = store.items.some(function(c){ return c.id === p.id; });
  if(!exists) store.items.push(p);
});
var seq = lsGet(K + 'seq', 0);
function saveStore(){
  lsSet(K + 'comments', store);
  lsSet(K + 'deleted', deletedIds);
  refreshCommentCount();
  scheduleDisk();
}
function refreshCommentCount(){
  var n = store.items.filter(function(c){ return !c.resolved; }).length;
  $('#stat-comments').innerHTML = '<b>' + n + '</b> unresolved comments';
}

/* ---------------- anchoring ---------------- */
function codeText(row){ var td = $('td.code', row); return td ? td.textContent : ''; }
function matchRows(rows, line){
  var strip = stripWs(line);
  if(!strip) return null;
  var i, r;
  for(i = 0; i < rows.length; i++){ if(codeText(rows[i]) === line) return rows[i]; }
  for(i = 0; i < rows.length; i++){ if(stripWs(codeText(rows[i])) === strip) return rows[i]; }
  r = rows.filter(function(row){ return stripWs(codeText(row)).indexOf(strip) >= 0; });
  if(r.length === 1) return r[0];
  return null;
}
function findAnchorRow(file, hunk, line){
  var tbs = $$('tbody.hunk').filter(function(tb){ return tb.getAttribute('data-file') === file; });
  var scoped = tbs.filter(function(tb){ return +tb.getAttribute('data-h') === hunk; });
  var rows, hit;
  if(scoped.length){
    rows = $$('tr.ln:not(.meta)', scoped[0]);
    hit = matchRows(rows, line);
    if(hit) return hit;
  }
  rows = [];
  tbs.forEach(function(tb){ rows = rows.concat($$('tr.ln:not(.meta)', tb)); });
  return matchRows(rows, line);
}
function fileContainer(file){
  var d = $$('details.file').filter(function(f){ return f.getAttribute('data-file') === file; })[0];
  return d ? $('.unanchored', d) : null;
}
function lostContainer(){
  var s = $('#lost');
  if(!s){
    s = document.createElement('section');
    s.id = 'lost';
    s.innerHTML = '<h2>Comments on lines not in this diff</h2><p class="hint">Carried over from a previous round; the anchor no longer resolves.</p>';
    $('main').insertBefore(s, $('main').firstChild.nextSibling);
  }
  return s;
}

/* ---------------- comment rendering ---------------- */
function buildCard(c){
  var d = document.createElement('div');
  d.className = 'ucomment' + (c.resolved ? ' resolved' : '');
  d.setAttribute('data-cid', c.id);
  d.innerHTML =
    '<div class="uc-head"><span class="badge uc-b">comment</span>' +
    (c.round === 'prev' ? '<span class="badge prev-b">from previous round</span>' : '') +
    (c.resolved ? '<span class="badge res-b">resolved</span>' : '') +
    '<span class="uc-meta">user · ' + escHtml(c.time || '') + '</span>' +
    '<span class="uc-actions">' +
    '<button data-act="resolve">' + (c.resolved ? 'Unresolve' : 'Resolve') + '</button>' +
    '<button data-act="edit">Edit</button>' +
    '<button data-act="del">Delete</button></span></div>' +
    '<div class="uc-body">' + escHtml(c.body) + '</div>';
  return d;
}
function insertAfterAttached(anchorRow, tr){
  var ref = anchorRow;
  while(ref.nextElementSibling &&
        (ref.nextElementSibling.classList.contains('ai-note-row') ||
         ref.nextElementSibling.classList.contains('uc-row') ||
         ref.nextElementSibling.classList.contains('composer-row'))){
    ref = ref.nextElementSibling;
  }
  ref.parentNode.insertBefore(tr, ref.nextElementSibling);
}
function renderComments(){
  $$('.uc-row').forEach(function(r){ r.remove(); });
  $$('.unanchored .ucomment, #lost .ucomment, #lost .orphan-file').forEach(function(x){ x.remove(); });
  store.items.forEach(function(c){
    var card = buildCard(c);
    var row = findAnchorRow(c.file, c.hunk, c.line);
    if(row){
      var tr = document.createElement('tr');
      tr.className = 'uc-row';
      var td = document.createElement('td');
      td.colSpan = 4;
      td.appendChild(card);
      tr.appendChild(td);
      insertAfterAttached(row, tr);
    } else {
      var box = fileContainer(c.file);
      if(!box){
        box = lostContainer();
        var lbl = document.createElement('div');
        lbl.className = 'orphan-file';
        lbl.textContent = c.file + ' — hunk ' + c.hunk + ' — "' + c.line + '"';
        box.appendChild(lbl);
      }
      box.appendChild(card);
    }
  });
  refreshCommentCount();
}
document.addEventListener('click', function(e){
  var btn = e.target.closest('button[data-act]');
  if(!btn) return;
  var card = btn.closest('.ucomment');
  if(!card) return;
  var cid = card.getAttribute('data-cid');
  var c = store.items.filter(function(x){ return x.id === cid; })[0];
  if(!c) return;
  var act = btn.getAttribute('data-act');
  if(act === 'resolve'){ c.resolved = !c.resolved; saveStore(); renderComments(); }
  else if(act === 'del'){
    store.items = store.items.filter(function(x){ return x.id !== cid; });
    if(cid.indexOf('prev-') === 0) deletedIds.push(cid);
    saveStore(); renderComments();
  }
  else if(act === 'edit'){
    var row = findAnchorRow(c.file, c.hunk, c.line);
    openComposer(row, card, c);
  }
});

/* ---------------- composer ---------------- */
var composerEl = null;
function closeComposer(){ if(composerEl){ composerEl.remove(); composerEl = null; } }
function openComposer(anchorRow, hostCard, editing){
  closeComposer();
  var wrap = document.createElement('div');
  wrap.className = 'composer';
  wrap.innerHTML = '<textarea placeholder="Leave a comment… (Ctrl+Enter to save)"></textarea>' +
    '<div class="c-btns"><button class="c-cancel">Cancel</button><button class="primary c-save">Save comment</button></div>';
  var ta = $('textarea', wrap);
  if(editing) ta.value = editing.body;
  $('.c-save', wrap).addEventListener('click', save);
  $('.c-cancel', wrap).addEventListener('click', closeComposer);
  ta.addEventListener('keydown', function(e){
    if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)) save();
    if(e.key === 'Escape'){ closeComposer(); e.stopPropagation(); }
  });
  function save(){
    var body = ta.value.trim();
    if(!body){ closeComposer(); return; }
    if(editing){
      editing.body = body;
      editing.time = new Date().toISOString();
    } else {
      var tb = anchorRow.closest('tbody.hunk');
      seq++; lsSet(K + 'seq', seq);
      store.items.push({
        id: 'c-' + String(seq),
        file: tb.getAttribute('data-file'),
        hunk: +tb.getAttribute('data-h'),
        line: codeText(anchorRow),
        body: body,
        time: new Date().toISOString(),
        resolved: false,
        round: 'current'
      });
    }
    closeComposer();
    saveStore();
    renderComments();
  }
  if(anchorRow){
    var tr = document.createElement('tr');
    tr.className = 'composer-row';
    var td = document.createElement('td');
    td.colSpan = 4;
    td.appendChild(wrap);
    tr.appendChild(td);
    insertAfterAttached(anchorRow, tr);
    composerEl = tr;
  } else if(hostCard){
    hostCard.parentNode.insertBefore(wrap, hostCard.nextSibling);
    composerEl = wrap;
  } else { return; }
  ta.focus();
}

/* ---------------- line focus + click to comment ---------------- */
var curRow = null;
function setCur(row, scroll){
  if(curRow) curRow.classList.remove('cur');
  curRow = row;
  if(row){
    row.classList.add('cur');
    if(scroll) row.scrollIntoView({block:'center'});
  }
}
document.addEventListener('click', function(e){
  if(e.target.closest('button, input, select, textarea, summary, .ucomment, .composer, .ai-note')) return;
  var row = e.target.closest('tr.ln:not(.meta)');
  if(!row) return;
  setCur(row, false);
  openComposer(row, null, null);
});

/* ---------------- markdown export ---------------- */
function toMarkdown(){
  var s = '# review-deck comments\n\n- review: ' + data.id +
          '\n- exported: ' + new Date().toISOString() + '\n';
  store.items.forEach(function(c){
    s += '\n## ' + c.file + ' — hunk ' + c.hunk + '\n\n' +
         '> ' + c.line + '\n\n' +
         '- author: user\n' +
         '- time: ' + c.time + '\n' +
         '- resolved: ' + (c.resolved ? 'yes' : 'no') + '\n\n' +
         c.body.replace(/^---$/gm, '\\---') + '\n\n---\n';
  });
  return s;
}
$('#btn-export').addEventListener('click', function(){
  var blob = new Blob([toMarkdown()], {type:'text/markdown'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'comments.user.md';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function(){ URL.revokeObjectURL(a.href); }, 2000);
});
$('#btn-copy').addEventListener('click', function(){
  var md = toMarkdown();
  var done = function(){
    var b = $('#btn-copy'); b.textContent = 'Copied!';
    setTimeout(function(){ b.textContent = 'Copy as Markdown'; }, 1500);
  };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(md).then(done, function(){ fallbackCopy(md); done(); });
  } else { fallbackCopy(md); done(); }
});
function fallbackCopy(text){
  var ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try{ document.execCommand('copy'); }catch(e){}
  ta.remove();
}

/* ---------------- File System Access API ---------------- */
var dirHandle = null, diskTimer = null;
$('#btn-connect').addEventListener('click', function(){
  if(!window.showDirectoryPicker){
    alert('This browser does not support the File System Access API.\nUse "Export comments" instead — it downloads the same file.');
    return;
  }
  window.showDirectoryPicker({mode:'readwrite'}).then(function(h){
    dirHandle = h;
    $('#fsa-status').textContent = 'connected: ' + h.name;
    writeDisk();
  }, function(){});
});
function scheduleDisk(){
  if(!dirHandle) return;
  clearTimeout(diskTimer);
  diskTimer = setTimeout(writeDisk, 400);
}
function writeDisk(){
  if(!dirHandle) return;
  dirHandle.getFileHandle('comments.user.md', {create:true}).then(function(fh){
    return fh.createWritable();
  }).then(function(w){
    return w.write(toMarkdown()).then(function(){ return w.close(); });
  }).then(function(){
    $('#fsa-status').textContent = 'saved ' + new Date().toLocaleTimeString();
  }, function(){
    $('#fsa-status').textContent = 'write failed';
  });
}

/* ---------------- keyboard navigation ---------------- */
var hunks = $$('tbody.hunk');
var notes = $$('.ai-note');
var hunkIdx = -1, noteIdx = -1;
function focusHunk(){
  var tb = hunks[hunkIdx];
  if(!tb) return;
  var det = tb.closest('details.file');
  if(det) det.open = true;
  setCur($('tr.ln:not(.meta)', tb) || $('tr.hunkhdr', tb), true);
}
function focusNote(){
  var nEl = notes[noteIdx];
  if(!nEl) return;
  var det = nEl.closest('details.file');
  if(det) det.open = true;
  nEl.open = true;
  nEl.scrollIntoView({block:'center'});
  nEl.classList.add('flash');
  setTimeout(function(){ nEl.classList.remove('flash'); }, 900);
}
function toggleHelp(force){
  var h = $('#help');
  h.hidden = force === undefined ? !h.hidden : !force;
}
$('#btn-help').addEventListener('click', function(){ toggleHelp(); });
$('#help').addEventListener('click', function(){ toggleHelp(false); });
document.addEventListener('keydown', function(e){
  if(e.target.closest('textarea, input, select')) return;
  if(e.ctrlKey || e.metaKey || e.altKey) return;
  if(e.key === 'j'){ hunkIdx = Math.min(hunkIdx + 1, hunks.length - 1); focusHunk(); }
  else if(e.key === 'k'){ hunkIdx = Math.max(hunkIdx - 1, 0); focusHunk(); }
  else if(e.key === 'n'){ noteIdx = Math.min(noteIdx + 1, notes.length - 1); focusNote(); }
  else if(e.key === 'p'){ noteIdx = Math.max(noteIdx - 1, 0); focusNote(); }
  else if(e.key === 'c'){ if(curRow) openComposer(curRow, null, null); }
  else if(e.key === 'v'){
    var det = curRow ? curRow.closest('details.file') : (hunks[hunkIdx] ? hunks[hunkIdx].closest('details.file') : null);
    if(det) toggleViewed(det.getAttribute('data-file'));
  }
  else if(e.key === '?'){ toggleHelp(); }
  else if(e.key === 'Escape'){ closeComposer(); toggleHelp(false); }
  else return;
  e.preventDefault();
});

renderComments();
saveStore();
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a git diff + AI notes into a single-file HTML review page.")
    ap.add_argument("--patch", required=True, help="path to the raw unified diff")
    ap.add_argument("--notes", help="path to notes.ai.json")
    ap.add_argument("--out", required=True, help="path for review.html")
    ap.add_argument("--prev-comments", action="append", default=[],
                    help="comments.user.md from a previous round (repeatable)")
    ap.add_argument("--notes-md", help="also write notes.ai.md here")
    ap.add_argument("--title", help="page title (default: base..head or patch name)")
    ap.add_argument("--review-id", help="stable id for localStorage keying (default: sha256 of patch)")
    ap.add_argument("--ensure-gitignore", metavar="REPO_ROOT",
                    help="ensure a .code-review/ entry in REPO_ROOT/.gitignore")
    args = ap.parse_args(argv)

    patch_text = Path(args.patch).read_text(encoding="utf-8", errors="replace")
    files = parse_patch(patch_text)

    notes_doc = {"version": 1, "notes": []}
    if args.notes:
        notes_doc = json.loads(Path(args.notes).read_text(encoding="utf-8"))
        bad = [n.get("id", "?") for n in notes_doc.get("notes", [])
               if n.get("severity") not in SEVERITIES]
        if bad:
            print("warning: unknown severity on notes %s (treated as info)" % ", ".join(bad),
                  file=sys.stderr)

    prev_comments = []
    for pc in args.prev_comments:
        prev_comments.extend(parse_comments_md(Path(pc).read_text(encoding="utf-8")))

    review_id = args.review_id or ("rd-" + hashlib.sha256(patch_text.encode("utf-8")).hexdigest()[:12])
    title = args.title
    if not title:
        if notes_doc.get("base") or notes_doc.get("head"):
            title = "%s..%s" % (notes_doc.get("base", "?"), notes_doc.get("head", "?"))
        else:
            title = "code review"

    out_html, stats = build_html(files, notes_doc, prev_comments, title, review_id, TEMPLATE)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_html, encoding="utf-8")

    if args.notes_md:
        Path(args.notes_md).write_text(render_notes_md(notes_doc), encoding="utf-8")

    gitignore = "skipped"
    if args.ensure_gitignore:
        gitignore = ensure_gitignore(args.ensure_gitignore)

    stats.update({"out": str(out_path), "review_id": review_id,
                  "gitignore": gitignore,
                  "size_kb": round(out_path.stat().st_size / 1024, 1)})
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

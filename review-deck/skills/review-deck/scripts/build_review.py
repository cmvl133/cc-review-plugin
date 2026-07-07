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

If notes.ai.json carries an "overview" object (intro markdown + optional
mermaid diagrams), it renders at the top of the page; when diagrams are
present the vendored assets/mermaid.min.js is inlined so the page stays
fully offline.
"""

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path

SEVERITIES = ("info", "suggestion", "warning")
MERMAID_ASSET = Path(__file__).resolve().parent.parent / "assets" / "mermaid.min.js"

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


def resolve_line(fd, line):
    """Resolve a plain file line number (external-tool style anchoring) to
    (hunk_idx0, line_idx). New-file numbering first, old-file (deleted
    lines) as fallback."""
    if not isinstance(line, int) or isinstance(line, bool):
        return None
    for hi, h in enumerate(fd.hunks):
        for li, ln in enumerate(h.lines):
            if ln.kind != "meta" and ln.new_no == line:
                return (hi, li)
    for hi, h in enumerate(fd.hunks):
        for li, ln in enumerate(h.lines):
            if ln.kind == "del" and ln.old_no == line:
                return (hi, li)
    return None


def resolve_entry(fd, entry):
    """Anchor an entry (note / tour step / checklist item) by content
    anchor first, then by its "line" number."""
    pos = resolve_anchor(fd, entry.get("hunk_index"),
                         entry.get("anchor_line_content", ""))
    if pos is None:
        pos = resolve_line(fd, entry.get("line"))
    return pos


# ---------------------------------------------------------------------------
# Intraline (word-level) diff: for paired del/add lines, find the changed
# char range via common prefix/suffix. Ranges are emitted as data attributes
# and highlighted client-side after tokenization.
# ---------------------------------------------------------------------------

def intraline_ranges(hunk):
    """Return {line_idx: (start, end)} of changed char ranges for lines in
    paired del/add blocks (k-th del paired with k-th add)."""
    res = {}
    lines = hunk.lines
    i = 0
    while i < len(lines):
        if lines[i].kind != "del":
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].kind == "del":
            j += 1
        k = j
        while k < len(lines) and lines[k].kind == "add":
            k += 1
        for m in range(min(j - i, k - j)):
            a, b = lines[i + m].text, lines[j + m].text
            if a == b or not a or not b:
                continue
            p = 0
            while p < min(len(a), len(b)) and a[p] == b[p]:
                p += 1
            s = 0
            while (s < min(len(a), len(b)) - p
                   and a[len(a) - 1 - s] == b[len(b) - 1 - s]):
                s += 1
            if p + s == 0:
                continue  # lines share nothing — not a small edit
            if p < len(a) - s:
                res[i + m] = (p, len(a) - s)
            if p < len(b) - s:
                res[j + m] = (p, len(b) - s)
        i = max(k, i + 1)
    return res


# ---------------------------------------------------------------------------
# Contrib fragments: external setups (workflows, hooks, CI, linters) drop
# JSON files with any subset of {notes, triage, tour, checklist, overview}
# into .code-review/<branch>/contrib/. They are merged into the main notes
# document and tagged with their source name. See INTEGRATIONS.md.
# ---------------------------------------------------------------------------

CHECK_STATUSES = ("done", "partial", "missing")


def validate_fragment(doc):
    """Return (errors, warnings) for a notes-document fragment. Errors mark
    entries the merge would skip; warnings are quality issues (renders
    unanchored, unknown enum values coerced)."""
    errors, warnings = [], []
    if not isinstance(doc, dict):
        return (["fragment is not a JSON object"], [])
    for key in ("notes", "triage", "tour", "checklist"):
        val = doc.get(key, [])
        if not isinstance(val, list):
            errors.append("%s: must be an array" % key)
            continue
        for i, e in enumerate(val):
            where = "%s[%d]" % (key, i)
            if not isinstance(e, dict):
                errors.append("%s: not an object" % where)
                continue
            if key in ("notes", "triage", "tour") and not e.get("file"):
                errors.append('%s: missing "file"' % where)
            if key == "notes":
                if e.get("severity") not in SEVERITIES:
                    warnings.append('%s: unknown severity %r (treated as info)'
                                    % (where, e.get("severity")))
                if not e.get("anchor_line_content") and not isinstance(e.get("line"), int):
                    warnings.append('%s: no "anchor_line_content" or "line" — renders unanchored'
                                    % where)
            if key == "triage" and e.get("attention") not in ATTENTIONS:
                errors.append("%s: attention must be one of %s" % (where, "/".join(ATTENTIONS)))
            if key == "checklist":
                if not e.get("item"):
                    errors.append('%s: missing "item"' % where)
                if e.get("status") not in CHECK_STATUSES:
                    warnings.append("%s: unknown status %r (treated as partial)"
                                    % (where, e.get("status")))
    ov = doc.get("overview")
    if ov is not None and not isinstance(ov, dict):
        errors.append("overview: must be an object")
    return (errors, warnings)


def load_contribs(paths):
    """Load contrib fragment files; returns [(source_name, doc)]."""
    out = []
    for p in paths:
        path = Path(p)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print("warning: skipping contrib %s: %s" % (p, e), file=sys.stderr)
            continue
        errors, _ = validate_fragment(doc)
        if errors:
            print("warning: contrib %s has %d invalid entrie(s) (skipped): %s"
                  % (p, len(errors), "; ".join(errors[:3])), file=sys.stderr)
        name = doc.get("source") if isinstance(doc, dict) else None
        out.append((name or path.name.split(".")[0], doc if isinstance(doc, dict) else {}))
    return out


def merge_contribs(notes_doc, contribs):
    """Merge contrib fragments into the main notes document (in place).
    Notes and checklist items are appended and tagged with their source;
    triage fills only files the main document didn't classify; overview and
    tour are taken from a contrib only when the main document has none."""
    merged_notes = 0
    for name, doc in contribs:
        for i, n in enumerate(x for x in doc.get("notes", [])
                              if isinstance(x, dict) and x.get("file")):
            n = dict(n)
            n["_source"] = name
            if not n.get("id"):
                n["id"] = "%s-%03d" % (name, i + 1)
            if n.get("severity") not in SEVERITIES:
                n["severity"] = "info"
            notes_doc.setdefault("notes", []).append(n)
            merged_notes += 1
        have = {t.get("file") for t in notes_doc.get("triage", [])}
        for t in doc.get("triage", []):
            if (isinstance(t, dict) and t.get("file") and t["file"] not in have
                    and t.get("attention") in ATTENTIONS):
                notes_doc.setdefault("triage", []).append(t)
                have.add(t["file"])
        for c in doc.get("checklist", []):
            if isinstance(c, dict) and c.get("item"):
                c = dict(c)
                c["_source"] = name
                if c.get("status") not in CHECK_STATUSES:
                    c["status"] = "partial"
                notes_doc.setdefault("checklist", []).append(c)
        if isinstance(doc.get("overview"), dict) and not notes_doc.get("overview"):
            notes_doc["overview"] = doc["overview"]
        if doc.get("tour") and not notes_doc.get("tour"):
            notes_doc["tour"] = [t for t in doc["tour"]
                                 if isinstance(t, dict) and t.get("file")]
    return merged_notes


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
            "type": "",
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
                kv = re.match(r"^- (author|time|type|resolved):\s*(.*)$", ln)
                if kv:
                    key, val = kv.group(1), kv.group(2).strip()
                    if key == "time":
                        c["time"] = val
                    elif key == "type":
                        c["type"] = val
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
    overview = notes_doc.get("overview") or {}
    if overview.get("body") or overview.get("diagrams"):
        out.append("## %s" % (overview.get("title") or "Overview"))
        out.append("")
        if overview.get("body"):
            out.append(overview["body"].strip())
            out.append("")
        for d in overview.get("diagrams", []):
            if not d.get("mermaid", "").strip():
                continue
            if d.get("title"):
                out.append("### %s" % d["title"])
                out.append("")
            out.append("```mermaid")
            out.append(d["mermaid"].strip())
            out.append("```")
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
    if note.get("_source"):
        badge_extra += '<span class="badge src-b" title="contributed by an external tool">%s</span>' % esc(note["_source"])
    return (
        '<details class="ai-note sev-%s" open data-note="%s">'
        '<summary><span class="badge sev-b">%s</span>%s'
        '<span class="nt">%s</span><span class="nid">%s</span></summary>'
        '<div class="nb">%s</div></details>'
        % (sev, esc(note.get("id", "")), sev, badge_extra,
           esc(note.get("title", "")), esc(note.get("id", "")),
           md_render(note.get("body", "")))
    )


def render_overview(overview, mermaid_available, checklist_html=""):
    """Render the optional notes.ai.json "overview" object: an intro body
    (markdown) plus mermaid diagrams, plus the plan<->implementation
    checklist when present. Diagrams fall back to plain source blocks when
    the vendored mermaid asset is missing."""
    parts = ['<section id="overview">']
    parts.append('<h2>%s</h2>' % esc(overview.get("title") or "Overview"))
    body = overview.get("body", "")
    if body:
        parts.append('<div class="ov-body">%s</div>' % md_render(body))
    for d in overview.get("diagrams", []):
        src = d.get("mermaid", "").strip()
        if not src:
            continue
        parts.append('<figure class="diagram">')
        if d.get("title"):
            parts.append('<figcaption>%s</figcaption>' % esc(d["title"]))
        if mermaid_available:
            parts.append('<pre class="mermaid">%s</pre>' % html.escape(src, quote=False))
        else:
            parts.append('<pre><code>%s</code></pre>' % html.escape(src, quote=False))
        parts.append('</figure>')
    parts.append(checklist_html)
    parts.append('</section>')
    return "".join(parts)


CHECK_STATUS = {
    "done": ("&#10003;", "chk-done"),
    "partial": ("&#8776;", "chk-partial"),
    "missing": ("&#10007;", "chk-missing"),
}


def render_checklist(checklist):
    """checklist items: {"item", "status", "_target" (resolved element id or None)}"""
    parts = ['<div class="checklist"><h3>Plan &harr; implementation</h3><ul>']
    for c in checklist:
        mark, cls = CHECK_STATUS.get(c.get("status"), CHECK_STATUS["partial"])
        link = ('<a class="chk-link" href="#%s">view</a>' % esc(c["_target"])
                if c.get("_target") else "")
        src = ('<span class="chk-src">%s</span>' % esc(c["_source"])
               if c.get("_source") else "")
        parts.append('<li class="%s"><span class="chk-i">%s</span> %s %s%s</li>'
                     % (cls, mark, _md_inline(c.get("item", "")), link, src))
    parts.append('</ul></div>')
    return "".join(parts)


def render_side_panel(tour, notes):
    """Side panel with a Guided-tour tab (when tour steps exist) and a
    Findings tab (when notes exist): the findings digest lists every note
    sorted by severity with jump links and 'handled' checkboxes."""
    sev_rank = {"warning": 0, "suggestion": 1, "info": 2}
    findings = sorted((n for n in notes if n.get("id")),
                      key=lambda n: sev_rank.get(n.get("severity"), 2))
    parts = ['<nav id="spanel">']
    parts.append('<div class="sp-head">')
    parts.append('<div class="sp-tabs">')
    if tour:
        parts.append('<button class="sp-tab active" data-tab="tour">Tour</button>')
    if findings:
        parts.append('<button class="sp-tab%s" data-tab="findings">Findings'
                     '<span id="fnd-open"></span></button>' % ("" if tour else " active"))
    parts.append('</div>')
    parts.append('<span class="tour-pos" id="tour-pos"></span>')
    parts.append('<button id="sp-toggle" title="Collapse panel">&raquo;</button></div>')
    if tour:
        parts.append('<div class="sp-body" data-tab="tour"><ol id="tour-steps">')
        for i, t in enumerate(tour):
            body = ('<span class="tour-note">%s</span>' % _md_inline(t["body"])
                    if t.get("body") else "")
            target = ' data-target="%s"' % esc(t["_target"]) if t.get("_target") else ""
            parts.append('<li><a href="#" data-step="%d"%s><b>%s</b>%s</a></li>'
                         % (i, target, esc(t.get("title", "step %d" % (i + 1))), body))
        parts.append('</ol>')
        parts.append('<div class="tour-btns"><button id="tour-prev">&uarr; prev</button>'
                     '<button id="tour-next" class="primary">next &darr;</button></div></div>')
    if findings:
        parts.append('<div class="sp-body" data-tab="findings"%s><ol id="fnd-list">'
                     % (' hidden' if tour else ''))
        for n in findings:
            sev = n.get("severity", "info")
            if sev not in SEVERITIES:
                sev = "info"
            fname = Path(n.get("file", "?")).name
            parts.append(
                '<li class="fnd sev-%s"><label><input type="checkbox" class="fnd-cb" data-note="%s"></label>'
                '<a href="#" data-note="%s"><b><span class="fnd-dot"></span>%s</b>'
                '<span class="tour-note">%s</span></a></li>'
                % (sev, esc(n["id"]), esc(n["id"]), esc(n.get("title", n["id"])), esc(fname)))
        parts.append('</ol></div>')
    parts.append('</nav>')
    parts.append('<button id="sp-tab-collapsed" hidden title="Open review panel">Guide</button>')
    return "".join(parts)


def render_filter_bar(att_counts, has_notes):
    parts = ['<div id="filters">']
    parts.append('<span class="f-label">files</span>')
    total = sum(att_counts.values())
    parts.append('<button class="fbtn active" data-f="all">all (%d)</button>' % total)
    for att in ATTENTIONS:
        if att_counts.get(att):
            parts.append('<button class="fbtn" data-f="%s">%s (%d)</button>'
                         % (att, att, att_counts[att]))
    if has_notes:
        parts.append('<span class="f-label">notes</span>')
        for sev in SEVERITIES:
            parts.append('<button class="nbtn active" data-sev="%s">%s</button>' % (sev, sev))
    if att_counts.get("mechanical"):
        parts.append('<button id="btn-mech-viewed" title="Mark all mechanical files as viewed">'
                     'mechanical &rarr; viewed</button>')
    parts.append('</div>')
    return "".join(parts)


MERMAID_INIT = r"""
(function(){
'use strict';
if(typeof mermaid === 'undefined') return;
var blocks = Array.prototype.slice.call(document.querySelectorAll('pre.mermaid'));
if(!blocks.length) return;
blocks.forEach(function(el){ el.setAttribute('data-mmd-src', el.textContent); });
var LIGHT_THEMES = ['light', 'solarized', 'gruvbox-light', 'latte', 'ayu-light'];
function mermaidTheme(){
  var t = document.documentElement.getAttribute('data-theme') || 'light';
  return LIGHT_THEMES.indexOf(t) >= 0 ? 'default' : 'dark';
}
var rendering = false, pending = false;
function render(){
  if(rendering){ pending = true; return; }
  rendering = true;
  mermaid.initialize({startOnLoad:false, securityLevel:'strict', theme: mermaidTheme()});
  blocks.forEach(function(el){
    el.removeAttribute('data-processed');
    el.textContent = el.getAttribute('data-mmd-src');
  });
  mermaid.run({nodes: blocks, suppressErrors: true})
    .catch(function(){})
    .then(function(){
      rendering = false;
      if(pending){ pending = false; render(); }
    });
}
new MutationObserver(function(){ render(); })
  .observe(document.documentElement, {attributes:true, attributeFilter:['data-theme']});
render();
})();
"""


STATUS_BADGE = {
    "added": ("new file", "st-add"),
    "deleted": ("deleted", "st-del"),
    "renamed": ("renamed", "st-ren"),
    "modified": ("modified", "st-mod"),
}

ATTENTIONS = ("risky", "core", "skim", "mechanical")
ATTENTION_ORDER = {"risky": 0, "core": 1, None: 2, "skim": 3, "mechanical": 4}


def render_file_section(fd, fidx, anchored, unanchored_notes, triage=None, row_ids=None):
    """anchored: {(hunk_idx0, line_idx): [notes]};
    triage: optional {"attention":..., "reason":..., "untested":...};
    row_ids: optional {(hunk_idx0, line_idx): element_id} for tour/checklist."""
    p = fd.path
    label, cls = STATUS_BADGE[fd.status]
    title = esc(p)
    if fd.status == "renamed" and fd.old_path != fd.new_path:
        title = "%s &rarr; %s" % (esc(fd.old_path or "?"), esc(fd.new_path or "?"))
    lang = detect_lang(p)
    triage = triage or {}
    row_ids = row_ids or {}
    attention = triage.get("attention")
    if attention not in ATTENTIONS:
        attention = None
    open_attr = "" if attention == "mechanical" else " open"

    att_badges = ""
    if attention:
        att_badges += '<span class="badge att-%s"%s>%s</span>' % (
            attention,
            ' title="%s"' % esc(triage["reason"]) if triage.get("reason") else "",
            attention)
    if triage.get("untested"):
        att_badges += '<span class="badge att-untested" title="changed logic not covered by tests in this diff">untested</span>'

    parts = []
    parts.append('<details class="file"%s data-file="%s" data-lang="%s" data-attention="%s" id="file-%d">'
                 % (open_attr, esc(p), lang, attention or "core", fidx))
    parts.append('<summary><span class="fpath">%s</span>'
                 '<span class="badge %s">%s</span>%s'
                 '<label class="viewed-l"><input type="checkbox" class="viewed-cb" data-file="%s"> Viewed</label>'
                 '</summary>' % (title, cls, label, att_badges, esc(p)))
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
            chg = intraline_ranges(hunk)
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
                rid = row_ids.get((hi, li))
                extra = ' id="%s"' % rid if rid else ""
                if li in chg:
                    extra += ' data-cs="%d" data-ce="%d"' % chg[li]
                parts.append(
                    '<tr class="ln %s" data-side="%s"%s><td class="no">%s</td><td class="no">%s</td>'
                    '<td class="gl">%s</td><td class="code">%s</td></tr>'
                    % (ln.kind, side, extra,
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
    triage_map = {t.get("file"): t for t in notes_doc.get("triage", [])
                  if isinstance(t, dict)}
    files = sorted(files, key=lambda fd: ATTENTION_ORDER.get(
        (triage_map.get(fd.path) or {}).get("attention"), 2))
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
        pos = resolve_entry(fd, note)
        if pos is None:
            unanchored[id(fd)].append(note)
            n_unanchored += 1
        else:
            anchored[id(fd)].setdefault(pos, []).append(note)
            n_anchored += 1

    overview = notes_doc.get("overview") or {}
    diagrams = [d for d in overview.get("diagrams", []) if d.get("mermaid", "").strip()]
    mermaid_js = None
    if diagrams:
        if MERMAID_ASSET.is_file():
            mermaid_js = MERMAID_ASSET.read_text(encoding="utf-8")
        else:
            print("warning: %s missing — diagrams rendered as plain source blocks"
                  % MERMAID_ASSET, file=sys.stderr)

    # Resolve tour steps and checklist items to row element ids
    file_pos = {id(fd): i for i, fd in enumerate(files)}
    row_ids = {id(fd): {} for fd in files}

    def resolve_target(entry, prefix, idx):
        fd = by_file.get(entry.get("file", ""))
        if fd is None:
            return None
        pos = resolve_entry(fd, entry)
        if pos is None:
            return "file-%d" % file_pos[id(fd)]
        existing = row_ids[id(fd)].get(pos)
        if existing:
            return existing
        rid = "%s-%d" % (prefix, idx)
        row_ids[id(fd)][pos] = rid
        return rid

    tour = [t for t in notes_doc.get("tour", []) if isinstance(t, dict)]
    for i, t in enumerate(tour):
        t["_target"] = resolve_target(t, "tour", i)
    checklist = [c for c in notes_doc.get("checklist", []) if isinstance(c, dict)]
    for i, c in enumerate(checklist):
        c["_target"] = resolve_target(c, "chk", i) if c.get("file") else None

    body = []
    if overview.get("body") or diagrams or checklist:
        body.append(render_overview(overview, mermaid_js is not None,
                                    render_checklist(checklist) if checklist else ""))
    if orphan_notes:
        body.append('<section class="orphans"><h2>Notes on files not in this diff</h2>')
        for n in orphan_notes:
            body.append('<div class="orphan-file">%s</div>' % esc(n.get("file", "?")))
            body.append(render_note_card(n, unanchored=True))
        body.append('</section>')
    if not files:
        body.append('<p class="empty">No changes in this diff.</p>')
    else:
        att_counts = {}
        for fd in files:
            att = (triage_map.get(fd.path) or {}).get("attention")
            if att not in ATTENTIONS:
                att = "core"
            att_counts[att] = att_counts.get(att, 0) + 1
        if len(files) > 1 or notes:
            body.append(render_filter_bar(att_counts, bool(notes)))
    for fidx, fd in enumerate(files):
        body.append(render_file_section(fd, fidx, anchored[id(fd)], unanchored[id(fd)],
                                        triage_map.get(fd.path), row_ids[id(fd)]))
    if tour or notes:
        body.append(render_side_panel(tour, notes))

    prev = []
    for i, c in enumerate(c for c in prev_comments if not c["resolved"]):
        prev.append({
            "id": "prev-%03d" % (i + 1),
            "file": c["file"], "hunk": c["hunk"], "line": c["line"],
            "body": c["body"], "time": c["time"], "type": c.get("type", ""),
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

    mermaid_html = ""
    if mermaid_js is not None:
        # '</script' can't appear un-escaped inside an inline script element
        mermaid_html = ("<script>%s</script>\n<script>%s</script>"
                        % (mermaid_js.replace("</script", "<\\/script"),
                           MERMAID_INIT))

    out = (template
           .replace("@@TITLE@@", esc(title))
           .replace("@@REFS@@", esc(refs))
           .replace("@@GENERATED@@", esc(notes_doc.get("generated_at", "")))
           .replace("@@FILE_COUNT@@", str(len(files)))
           .replace("@@NOTES_COUNT@@", str(len(notes)))
           .replace("@@BODY@@", "".join(body))
           .replace("@@DATA@@", data_json)
           .replace("@@MERMAID@@", mermaid_html))
    stats = {"files": len(files),
             "hunks": sum(len(f.hunks) for f in files),
             "notes_anchored": n_anchored,
             "notes_unanchored": n_unanchored,
             "overview": bool(overview.get("body") or diagrams),
             "diagrams": len(diagrams),
             "triage_files": len([f for f in files if triage_map.get(f.path)]),
             "tour_steps": len(tour),
             "checklist_items": len(checklist),
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
# @@FILE_COUNT@@ @@NOTES_COUNT@@ @@BODY@@ @@DATA@@ @@MERMAID@@)
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
:root[data-theme="dracula"]{
  --bg:#282a36;--fg:#f8f8f2;--muted:#6272a4;--panel:#21222c;--border:#44475a;
  --accent:#bd93f9;--accent-fg:#282a36;
  --added-bg:#1f3b2c;--removed-bg:#43242b;--note-bg:#21222c;
  --warn:#ffb86c;--sugg:#ff79c6;--info:#8be9fd;--res:#50fa7b;
  --kw:#ff79c6;--str:#f1fa8c;--com:#6272a4;--num:#bd93f9;
}
:root[data-theme="nord"]{
  --bg:#2e3440;--fg:#d8dee9;--muted:#616e88;--panel:#3b4252;--border:#4c566a;
  --accent:#88c0d0;--accent-fg:#2e3440;
  --added-bg:#37423b;--removed-bg:#4a3439;--note-bg:#3b4252;
  --warn:#ebcb8b;--sugg:#b48ead;--info:#81a1c1;--res:#a3be8c;
  --kw:#81a1c1;--str:#a3be8c;--com:#616e88;--num:#b48ead;
}
:root[data-theme="gruvbox-dark"]{
  --bg:#282828;--fg:#ebdbb2;--muted:#928374;--panel:#32302f;--border:#504945;
  --accent:#83a598;--accent-fg:#282828;
  --added-bg:#34381b;--removed-bg:#442e2d;--note-bg:#32302f;
  --warn:#fabd2f;--sugg:#d3869b;--info:#83a598;--res:#b8bb26;
  --kw:#fb4934;--str:#b8bb26;--com:#928374;--num:#d3869b;
}
:root[data-theme="gruvbox-light"]{
  --bg:#fbf1c7;--fg:#3c3836;--muted:#7c6f64;--panel:#f2e5bc;--border:#d5c4a1;
  --accent:#076678;--accent-fg:#fbf1c7;
  --added-bg:#e0e4b8;--removed-bg:#f6d1c8;--note-bg:#f2e5bc;
  --warn:#b57614;--sugg:#8f3f71;--info:#076678;--res:#79740e;
  --kw:#9d0006;--str:#79740e;--com:#7c6f64;--num:#8f3f71;
}
:root[data-theme="monokai"]{
  --bg:#272822;--fg:#f8f8f2;--muted:#75715e;--panel:#1e1f1c;--border:#49483e;
  --accent:#66d9ef;--accent-fg:#272822;
  --added-bg:#2d3a25;--removed-bg:#4a2b32;--note-bg:#1e1f1c;
  --warn:#e6db74;--sugg:#ae81ff;--info:#66d9ef;--res:#a6e22e;
  --kw:#f92672;--str:#e6db74;--com:#75715e;--num:#ae81ff;
}
:root[data-theme="onedark"]{
  --bg:#282c34;--fg:#abb2bf;--muted:#5c6370;--panel:#21252b;--border:#3e4451;
  --accent:#61afef;--accent-fg:#282c34;
  --added-bg:#2c3b2f;--removed-bg:#43292d;--note-bg:#21252b;
  --warn:#e5c07b;--sugg:#c678dd;--info:#61afef;--res:#98c379;
  --kw:#c678dd;--str:#98c379;--com:#5c6370;--num:#d19a66;
}
:root[data-theme="mocha"]{
  --bg:#1e1e2e;--fg:#cdd6f4;--muted:#6c7086;--panel:#181825;--border:#45475a;
  --accent:#89b4fa;--accent-fg:#1e1e2e;
  --added-bg:#29392f;--removed-bg:#46282f;--note-bg:#181825;
  --warn:#f9e2af;--sugg:#cba6f7;--info:#89b4fa;--res:#a6e3a1;
  --kw:#cba6f7;--str:#a6e3a1;--com:#6c7086;--num:#fab387;
}
:root[data-theme="latte"]{
  --bg:#eff1f5;--fg:#4c4f69;--muted:#8c8fa1;--panel:#e6e9ef;--border:#ccd0da;
  --accent:#1e66f5;--accent-fg:#eff1f5;
  --added-bg:#e0edd7;--removed-bg:#f5dde1;--note-bg:#e6e9ef;
  --warn:#df8e1d;--sugg:#8839ef;--info:#1e66f5;--res:#40a02b;
  --kw:#8839ef;--str:#40a02b;--com:#8c8fa1;--num:#fe640b;
}
:root[data-theme="tokyonight"]{
  --bg:#1a1b26;--fg:#c0caf5;--muted:#565f89;--panel:#16161e;--border:#292e42;
  --accent:#7aa2f7;--accent-fg:#1a1b26;
  --added-bg:#1f3a28;--removed-bg:#3f2831;--note-bg:#16161e;
  --warn:#e0af68;--sugg:#bb9af7;--info:#7aa2f7;--res:#9ece6a;
  --kw:#bb9af7;--str:#9ece6a;--com:#565f89;--num:#ff9e64;
}
:root[data-theme="rosepine"]{
  --bg:#191724;--fg:#e0def4;--muted:#6e6a86;--panel:#1f1d2e;--border:#26233a;
  --accent:#c4a7e7;--accent-fg:#191724;
  --added-bg:#1e3a2f;--removed-bg:#41262e;--note-bg:#1f1d2e;
  --warn:#f6c177;--sugg:#c4a7e7;--info:#9ccfd8;--res:#9ccfd8;
  --kw:#eb6f92;--str:#f6c177;--com:#6e6a86;--num:#c4a7e7;
}
:root[data-theme="everforest"]{
  --bg:#2d353b;--fg:#d3c6aa;--muted:#859289;--panel:#232a2e;--border:#475258;
  --accent:#7fbbb3;--accent-fg:#2d353b;
  --added-bg:#3b4a3a;--removed-bg:#4c3743;--note-bg:#232a2e;
  --warn:#dbbc7f;--sugg:#d699b6;--info:#7fbbb3;--res:#a7c080;
  --kw:#e67e80;--str:#a7c080;--com:#859289;--num:#d699b6;
}
:root[data-theme="ayu-light"]{
  --bg:#fafafa;--fg:#5c6773;--muted:#959da6;--panel:#f0f0f0;--border:#d9d8d7;
  --accent:#399ee6;--accent-fg:#fafafa;
  --added-bg:#e0f0d5;--removed-bg:#fbe2e2;--note-bg:#f0f0f0;
  --warn:#f2ae49;--sugg:#a37acc;--info:#399ee6;--res:#86b300;
  --kw:#fa8d3e;--str:#86b300;--com:#abb0b6;--num:#a37acc;
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
#overview{border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:16px;background:var(--panel)}
#overview h2{margin:0 0 8px;font-size:15px}
#overview .ov-body{font-size:13.5px}
#overview .ov-body p{margin:6px 0}
#overview .ov-body code{background:var(--bg);border:1px solid var(--border);border-radius:4px;
  padding:0 4px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
#overview .ov-body pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;overflow-x:auto}
#overview .ov-body pre code{border:none;background:none;padding:0}
figure.diagram{margin:12px 0 4px;border:1px solid var(--border);border-radius:8px;background:var(--bg);
  padding:10px;overflow-x:auto}
figure.diagram figcaption{font-size:12px;color:var(--muted);margin-bottom:6px}
figure.diagram pre{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
figure.diagram pre.mermaid[data-processed]{text-align:center}
figure.diagram svg{max-width:100%}
.badge.att-risky{background:var(--removed-bg);color:var(--kw)}
.badge.att-core{background:var(--note-bg);color:var(--info);border:1px solid var(--border)}
.badge.att-skim{background:var(--note-bg);color:var(--muted);border:1px solid var(--border)}
.badge.att-mechanical{background:var(--note-bg);color:var(--muted);border:1px dashed var(--border);font-weight:400}
.badge.att-untested{background:var(--note-bg);color:var(--warn);border:1px solid var(--warn)}
#filters{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:8px 10px;margin-bottom:16px;
  border:1px solid var(--border);border-radius:8px;background:var(--panel);font-size:12px}
#filters .f-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;margin:0 2px 0 6px}
#filters .f-label:first-child{margin-left:0}
#filters button.active{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
#filters button.nbtn:not(.active){opacity:.5;text-decoration:line-through}
.f-hidden{display:none!important}
.dchg{background:rgba(250,200,40,.32);border-radius:2px;box-shadow:0 0 0 1px rgba(250,200,40,.18)}
.checklist{margin-top:12px;border-top:1px solid var(--border);padding-top:8px}
.checklist h3{margin:0 0 6px;font-size:13px}
.checklist ul{margin:0;padding:0;list-style:none;font-size:13px}
.checklist li{padding:2px 0}
.checklist .chk-i{display:inline-block;width:18px;font-weight:700;text-align:center}
.checklist .chk-done .chk-i{color:var(--res)}
.checklist .chk-partial .chk-i{color:var(--warn)}
.checklist .chk-missing .chk-i{color:var(--kw)}
.checklist li.chk-missing{color:var(--kw)}
.checklist .chk-link{font-size:11px;margin-left:6px}
#spanel{position:fixed;right:26px;top:96px;z-index:60;width:264px;max-height:calc(100vh - 140px);
  display:flex;flex-direction:column;border:1px solid var(--border);border-radius:10px;
  background:var(--panel);box-shadow:0 4px 16px rgba(0,0,0,.18);font-size:13px}
#spanel .sp-head{display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--border)}
#spanel .sp-tabs{display:flex;gap:4px}
#spanel .sp-tab{border:none;background:none;padding:3px 8px;border-radius:6px;font-weight:600;color:var(--muted)}
#spanel .sp-tab.active{background:var(--bg);color:var(--fg)}
#spanel .sp-tab #fnd-open{color:var(--warn);margin-left:4px;font-size:11px}
#spanel .tour-pos{color:var(--muted);font-size:11px;margin-left:auto}
#spanel .sp-body{display:flex;flex-direction:column;overflow:hidden;flex:1}
#spanel ol{margin:0;padding:4px 0;list-style:none;overflow-y:auto;flex:1}
#spanel ol a{display:block;padding:6px 12px;color:var(--fg);text-decoration:none;border-left:3px solid transparent}
#spanel ol a b{display:block;font-size:12.5px;font-weight:600}
#spanel ol a .tour-note{color:var(--muted);font-size:11.5px}
#spanel ol a:hover{background:var(--bg)}
#spanel ol li.cur a{border-left-color:var(--accent);background:var(--bg)}
#spanel .tour-btns{display:flex;gap:6px;padding:8px 12px;border-top:1px solid var(--border)}
#spanel .tour-btns button{flex:1}
#spanel li.fnd{display:flex;align-items:flex-start}
#spanel li.fnd label{padding:7px 0 0 10px}
#spanel li.fnd a{flex:1;padding-left:8px}
#spanel .fnd-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
#spanel li.fnd.sev-warning .fnd-dot{background:var(--warn)}
#spanel li.fnd.sev-suggestion .fnd-dot{background:var(--sugg)}
#spanel li.fnd.sev-info .fnd-dot{background:var(--info)}
#spanel li.fnd.handled a{opacity:.5}
#spanel li.fnd.handled a b{text-decoration:line-through}
#sp-tab-collapsed{position:fixed;right:14px;top:120px;z-index:60;writing-mode:vertical-rl;padding:10px 4px;
  border-radius:6px 0 0 6px;border-right:none;background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
@media (max-width:1200px){#spanel{width:224px}}
#minimap{position:fixed;right:0;top:0;bottom:0;width:14px;z-index:55;cursor:pointer;background:var(--panel);
  border-left:1px solid var(--border)}
.composer .c-types{display:flex;gap:4px;margin:4px 0;flex-wrap:wrap}
.composer .c-types .t-chip{font-size:11px;padding:1px 8px;border-radius:10px}
.composer .c-types .t-chip.active{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.composer .c-canned{display:flex;gap:4px;flex-wrap:wrap}
.composer .c-canned button{font-size:11px;padding:1px 8px;color:var(--muted)}
.badge.type-b{border:1px solid var(--border);background:var(--panel);color:var(--fg)}
.badge.type-fix{color:var(--kw);border-color:var(--kw)}
.badge.type-question{color:var(--info);border-color:var(--info)}
.badge.type-nit{color:var(--muted)}
.vote-btn{font-size:11px;padding:1px 6px;opacity:.7}
.vote-btn.active{opacity:1;background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
#duck{position:absolute;z-index:70;font-size:22px;pointer-events:none;transition:top .5s cubic-bezier(.5,1.5,.5,1),left .4s ease;
  filter:drop-shadow(0 2px 2px rgba(0,0,0,.3))}
#duck.flip{transform:scaleX(-1)}
#duck .quack{position:absolute;left:20px;top:-16px;font-size:11px;font-weight:700;background:var(--bg);
  border:1px solid var(--border);border-radius:8px 8px 8px 0;padding:1px 6px;white-space:nowrap;
  font-family:-apple-system,"Segoe UI",Roboto,sans-serif;transform:scaleX(1)}
#duck.flip .quack{transform:scaleX(-1)}
@keyframes rd-waddle{25%{transform:rotate(-8deg)}75%{transform:rotate(8deg)}}
#duck.waddle{animation:rd-waddle .5s ease 2}
#duck.flip.waddle{animation:none}
tr.ln.flash td.code{animation:rd-flash 1.2s ease-out}
@keyframes rd-flash{0%,60%{box-shadow:inset 0 0 0 2px var(--accent)}100%{box-shadow:none}}
.cfx{position:fixed;left:0;top:0;width:8px;height:8px;z-index:300;pointer-events:none;border-radius:2px}
#xp-chip{font-size:12px;font-weight:600;color:var(--accent);white-space:nowrap}
#xp-chip .xp-bar{display:inline-block;width:52px;height:6px;border-radius:3px;background:var(--border);
  vertical-align:middle;margin-left:5px;overflow:hidden}
#xp-chip .xp-fill{display:block;height:100%;background:var(--accent)}
#xp-chip.pulse{animation:rd-pulse .8s ease-out}
@keyframes rd-pulse{30%{transform:scale(1.35)}}
#xp-toast{position:fixed;left:50%;top:38%;transform:translate(-50%,-50%);z-index:310;pointer-events:none;
  font-size:42px;font-weight:800;color:var(--accent);text-shadow:0 2px 12px rgba(0,0,0,.35);opacity:0}
#xp-toast.show{animation:rd-toast 1.6s ease-out}
@keyframes rd-toast{10%{opacity:1;transform:translate(-50%,-50%) scale(1.15)}70%{opacity:1}100%{opacity:0;transform:translate(-50%,-80%) scale(1)}}
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
.badge.dis-b{background:var(--panel);color:var(--muted);border:1px solid var(--border)}
.badge.src-b{background:var(--panel);color:var(--muted);border:1px solid var(--border);font-weight:400;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:10.5px}
.chk-src{color:var(--muted);font-size:11px;margin-left:6px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ai-note .dismiss-btn{font-size:11px;padding:1px 8px}
.ai-note.dismissed{opacity:.55}
.ai-note.dismissed .nt{text-decoration:line-through}
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
    <span class="stat" id="stat-eta" title="estimated careful-reading time left (weighted by triage)"></span>
  </div>
  <div class="tb-actions">
    <select id="theme-select" aria-label="Theme">
      <option value="light">Light</option>
      <option value="dark">Dark</option>
      <option value="solarized">Solarized</option>
      <option value="contrast">High contrast</option>
      <option value="dracula">Dracula</option>
      <option value="nord">Nord</option>
      <option value="gruvbox-dark">Gruvbox Dark</option>
      <option value="gruvbox-light">Gruvbox Light</option>
      <option value="monokai">Monokai</option>
      <option value="onedark">One Dark</option>
      <option value="mocha">Catppuccin Mocha</option>
      <option value="latte">Catppuccin Latte</option>
      <option value="tokyonight">Tokyo Night</option>
      <option value="rosepine">Ros&eacute; Pine</option>
      <option value="everforest">Everforest</option>
      <option value="ayu-light">Ayu Light</option>
      <option value="custom">Custom&hellip;</option>
    </select>
    <span id="xp-chip" hidden></span>
    <button id="btn-arcade" title="Arcade mode: XP + confetti while you review">&#127918;</button>
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
      <tr><td><kbd>J</kbd> / <kbd>K</kbd></td><td>next / previous file</td></tr>
      <tr><td><kbd>n</kbd> / <kbd>p</kbd></td><td>next / previous AI note</td></tr>
      <tr><td><kbd>c</kbd></td><td>comment on focused line</td></tr>
      <tr><td><kbd>v</kbd></td><td>toggle current file viewed</td></tr>
      <tr><td><kbd>]</kbd></td><td>mark file viewed &rarr; jump to next unviewed</td></tr>
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

/* ---------------- intraline (word-level) diff highlight ---------------- */
$$('tr.ln[data-cs]').forEach(function(row){
  var td = $('td.code', row);
  if(!td) return;
  var start = +row.getAttribute('data-cs'), end = +row.getAttribute('data-ce');
  var walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
  var node, pos = 0, targets = [];
  while((node = walker.nextNode())){
    var len = node.textContent.length;
    var s = Math.max(start - pos, 0), e = Math.min(end - pos, len);
    if(s < e) targets.push([node, s, e]);
    pos += len;
    if(pos >= end) break;
  }
  targets.forEach(function(t){
    var r = document.createRange();
    r.setStart(t[0], t[1]); r.setEnd(t[0], t[2]);
    var span = document.createElement('span');
    span.className = 'dchg';
    try{ r.surroundContents(span); }catch(err){}
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
  refreshEta();
}
function toggleViewed(path){
  var i = viewed.indexOf(path);
  var marking = i < 0;
  if(marking) viewed.push(path); else viewed.splice(i, 1);
  lsSet(K + 'viewed', viewed);
  refreshViewed();
  if(marking){
    var det = $$('details.file').filter(function(f){ return f.getAttribute('data-file') === path; })[0];
    if(det){ det.open = false; awardXp(10, $('summary', det)); }
    if(data.files.length && viewed.length >= data.files.length) awardXp(50, null, 'ALL FILES VIEWED!');
  }
}
$$('.viewed-cb').forEach(function(cb){
  cb.addEventListener('click', function(e){ e.stopPropagation(); });
  cb.addEventListener('change', function(){ toggleViewed(cb.getAttribute('data-file')); });
});
$$('.viewed-l').forEach(function(l){ l.addEventListener('click', function(e){ e.stopPropagation(); }); });
refreshViewed();

/* ---------------- AI note dismissal + reactions ---------------- */
var dismissed = lsGet(K + 'dismissedNotes', []);
var votes = lsGet(K + 'noteVotes', {});
function noteFile(el){
  var f = el.closest('details.file');
  return f ? f.getAttribute('data-file') : 'not-in-diff';
}
function refreshDismissed(){
  $$('.ai-note').forEach(function(el){
    var on = dismissed.indexOf(el.getAttribute('data-note')) >= 0;
    el.classList.toggle('dismissed', on);
    var b = $('.dismiss-btn', el);
    if(b) b.textContent = on ? 'Restore' : 'Dismiss';
    var badge = $('.dis-b', el);
    if(badge) badge.hidden = !on;
  });
}
function refreshVotes(){
  $$('.ai-note').forEach(function(el){
    var v = votes[el.getAttribute('data-note')] || 0;
    var up = $('.vote-up', el), down = $('.vote-down', el);
    if(up) up.classList.toggle('active', v > 0);
    if(down) down.classList.toggle('active', v < 0);
  });
}
$$('.ai-note').forEach(function(el){
  var s = $('summary', el);
  if(!s || !el.getAttribute('data-note')) return;
  var badge = document.createElement('span');
  badge.className = 'badge dis-b';
  badge.textContent = 'dismissed';
  badge.hidden = true;
  ['up', 'down'].forEach(function(dir){
    var vb = document.createElement('button');
    vb.className = 'vote-btn vote-' + dir;
    vb.type = 'button';
    vb.textContent = dir === 'up' ? '👍' : '👎';
    vb.title = dir === 'up'
      ? 'Useful note — reinforces this kind of feedback in future rounds'
      : 'Noise — future review rounds are told to avoid this kind of note';
    vb.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var id = el.getAttribute('data-note');
      var val = dir === 'up' ? 1 : -1;
      if(votes[id] === val) delete votes[id]; else votes[id] = val;
      lsSet(K + 'noteVotes', votes);
      refreshVotes();
      scheduleDisk();
    });
    s.appendChild(vb);
  });
  var btn = document.createElement('button');
  btn.className = 'dismiss-btn';
  btn.type = 'button';
  btn.title = 'Mark this note so the AI will not address or re-raise it';
  btn.addEventListener('click', function(e){
    e.preventDefault(); e.stopPropagation();
    var id = el.getAttribute('data-note');
    var i = dismissed.indexOf(id);
    if(i >= 0) dismissed.splice(i, 1); else { dismissed.push(id); awardXp(3, btn); bumpStat('dismissed'); }
    lsSet(K + 'dismissedNotes', dismissed);
    refreshDismissed();
    scheduleDisk();
  });
  s.appendChild(badge);
  s.appendChild(btn);
});
refreshDismissed();
refreshVotes();

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
    (c.type ? '<span class="badge type-b type-' + escHtml(c.type) + '">' + escHtml(c.type) + '</span>' : '') +
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
  if(act === 'resolve'){ c.resolved = !c.resolved; if(c.resolved) awardXp(5, card); saveStore(); renderComments(); }
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
var C_TYPES = ['fix', 'question', 'nit', 'discuss'];
var C_CANNED = ['typo', 'why?', 'extract to method', 'naming', 'needs test', 'simplify'];
function openComposer(anchorRow, hostCard, editing){
  closeComposer();
  var wrap = document.createElement('div');
  wrap.className = 'composer';
  wrap.innerHTML =
    '<div class="c-types">' +
    C_TYPES.map(function(t){ return '<button class="t-chip" data-type="' + t + '">' + t + '</button>'; }).join('') +
    '</div>' +
    '<textarea placeholder="Leave a comment… (Ctrl+Enter to save)"></textarea>' +
    '<div class="c-canned">' +
    C_CANNED.map(function(t){ return '<button data-canned="' + t + '">' + t + '</button>'; }).join('') +
    '</div>' +
    '<div class="c-btns"><button class="c-cancel">Cancel</button><button class="primary c-save">Save comment</button></div>';
  var ta = $('textarea', wrap);
  var cType = (editing && editing.type) || '';
  function markType(){
    $$('.t-chip', wrap).forEach(function(ch){
      ch.classList.toggle('active', ch.getAttribute('data-type') === cType);
    });
  }
  $$('.t-chip', wrap).forEach(function(ch){
    ch.addEventListener('click', function(){
      cType = cType === ch.getAttribute('data-type') ? '' : ch.getAttribute('data-type');
      markType();
      ta.focus();
    });
  });
  $$('[data-canned]', wrap).forEach(function(b){
    b.addEventListener('click', function(){
      ta.value = ta.value ? ta.value.replace(/\s*$/, ' ') + b.getAttribute('data-canned') : b.getAttribute('data-canned');
      ta.focus();
    });
  });
  markType();
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
      editing.type = cType;
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
        type: cType,
        time: new Date().toISOString(),
        resolved: false,
        round: 'current'
      });
      awardXp(5, anchorRow);
      duckQuack('Kwak!');
      bumpStat('comments');
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
    duckFollow(row);
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
  if(dismissed.length){
    s += '\n## dismissed AI notes\n\n';
    $$('.ai-note').forEach(function(el){
      var id = el.getAttribute('data-note');
      if(dismissed.indexOf(id) < 0) return;
      var t = $('.nt', el);
      s += '- ' + id + ' · ' + noteFile(el) + ' · ' + (t ? t.textContent : '') + '\n';
    });
    s += '\n---\n';
  }
  var votedIds = Object.keys(votes);
  if(votedIds.length){
    s += '\n## note reactions\n\n';
    $$('.ai-note').forEach(function(el){
      var id = el.getAttribute('data-note');
      if(!votes[id]) return;
      var t = $('.nt', el);
      s += '- ' + id + ' · ' + (votes[id] > 0 ? 'up' : 'down') + ' · ' +
           noteFile(el) + ' · ' + (t ? t.textContent : '') + '\n';
    });
    s += '\n---\n';
  }
  store.items.forEach(function(c){
    s += '\n## ' + c.file + ' — hunk ' + c.hunk + '\n\n' +
         '> ' + c.line + '\n\n' +
         '- author: user\n' +
         '- time: ' + c.time + '\n' +
         (c.type ? '- type: ' + c.type + '\n' : '') +
         '- resolved: ' + (c.resolved ? 'yes' : 'no') + '\n\n' +
         c.body.replace(/^---$/gm, '\\---') + '\n\n---\n';
  });
  return s;
}
function downloadComments(){
  var blob = new Blob([toMarkdown()], {type:'text/markdown'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'comments.user.md';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function(){ URL.revokeObjectURL(a.href); }, 2000);
}
$('#btn-export').addEventListener('click', function(){
  if(dirHandle){ writeDisk(); return; }
  if(window.showDirectoryPicker){
    reconnectDir().then(function(ok){ if(!ok) downloadComments(); });
    return;
  }
  downloadComments();
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
var dirHandle = null, diskTimer = null, storedHandle = null;
function idb(){
  return new Promise(function(res, rej){
    var q = indexedDB.open('review-deck', 1);
    q.onupgradeneeded = function(){ q.result.createObjectStore('kv'); };
    q.onsuccess = function(){ res(q.result); };
    q.onerror = function(){ rej(q.error); };
  });
}
function idbSet(k, v){
  return idb().then(function(db){
    return new Promise(function(res, rej){
      var tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put(v, k);
      tx.oncomplete = res;
      tx.onerror = function(){ rej(tx.error); };
    });
  });
}
function idbGet(k){
  return idb().then(function(db){
    return new Promise(function(res, rej){
      var rq = db.transaction('kv', 'readonly').objectStore('kv').get(k);
      rq.onsuccess = function(){ res(rq.result); };
      rq.onerror = function(){ rej(rq.error); };
    });
  });
}
function updateSaveUi(){
  var ex = $('#btn-export'), cn = $('#btn-connect');
  if(dirHandle){
    ex.hidden = true;
    cn.disabled = true;
    cn.textContent = 'Connected ✓';
  } else {
    ex.hidden = false;
    cn.disabled = false;
    if(window.showDirectoryPicker){
      ex.textContent = 'Save to review folder';
      ex.title = 'Pick the review round directory once — comments save straight into it (and live from then on)';
      cn.textContent = storedHandle ? 'Reconnect review folder' : 'Connect review folder';
    } else {
      ex.textContent = 'Export comments';
      ex.title = 'Download comments.user.md (this browser cannot write files in place)';
    }
  }
}
function adoptDir(h, persist){
  dirHandle = h;
  $('#fsa-status').textContent = 'live-saving to "' + h.name + '"';
  if(persist && window.indexedDB) idbSet('dir:' + data.id, h).catch(function(){});
  writeDisk();
  updateSaveUi();
}
function pickDir(){
  return window.showDirectoryPicker({mode:'readwrite'}).then(function(h){
    adoptDir(h, true);
    return true;
  }, function(){ return false; });
}
function reconnectDir(){
  if(!storedHandle || !storedHandle.requestPermission) return pickDir();
  return storedHandle.requestPermission({mode:'readwrite'}).then(function(p){
    if(p === 'granted'){ adoptDir(storedHandle, false); return true; }
    return pickDir();
  }, function(){ return pickDir(); });
}
$('#btn-connect').addEventListener('click', function(){
  if(!window.showDirectoryPicker){
    alert('This browser does not support the File System Access API.\nUse "Export comments" instead — it downloads the same file.');
    return;
  }
  reconnectDir();
});
if(window.showDirectoryPicker && window.indexedDB){
  idbGet('dir:' + data.id).then(function(h){
    if(!h || !h.queryPermission) return;
    storedHandle = h;
    h.queryPermission({mode:'readwrite'}).then(function(p){
      if(p === 'granted') adoptDir(h, false);
      else updateSaveUi();
    }, function(){});
  }).catch(function(){});
}
updateSaveUi();
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
  else if(e.key === 'J'){ fileJump(1); }
  else if(e.key === 'K'){ fileJump(-1); }
  else if(e.key === ']'){ markAndNext(); }
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

/* ---------------- attention & severity filters ---------------- */
$$('#filters .fbtn').forEach(function(b){
  b.addEventListener('click', function(){
    $$('#filters .fbtn').forEach(function(x){ x.classList.remove('active'); });
    b.classList.add('active');
    var f = b.getAttribute('data-f');
    $$('details.file').forEach(function(d){
      d.classList.toggle('f-hidden', f !== 'all' && d.getAttribute('data-attention') !== f);
    });
  });
});
$$('#filters .nbtn').forEach(function(b){
  b.addEventListener('click', function(){
    b.classList.toggle('active');
    var sev = b.getAttribute('data-sev'), on = b.classList.contains('active');
    $$('.ai-note.sev-' + sev).forEach(function(n){
      var row = n.closest('tr.ai-note-row');
      (row || n).classList.toggle('f-hidden', !on);
    });
  });
});
var mv = $('#btn-mech-viewed');
if(mv) mv.addEventListener('click', function(){
  $$('details.file[data-attention="mechanical"]').forEach(function(d){
    var path = d.getAttribute('data-file');
    if(viewed.indexOf(path) < 0) toggleViewed(path);
  });
});

/* ---------------- side panel: guided tour + findings digest ---------------- */
var fndHandled = lsGet(K + 'fndHandled', []);
var spanel = $('#spanel');
if(spanel){
  var spShow = function(open){
    spanel.hidden = !open;
    $('#sp-tab-collapsed').hidden = open;
    lsSet(K + 'spOpen', open);
  };
  $$('.sp-tab', spanel).forEach(function(tb){
    tb.addEventListener('click', function(){
      $$('.sp-tab', spanel).forEach(function(x){ x.classList.remove('active'); });
      tb.classList.add('active');
      $$('.sp-body', spanel).forEach(function(b){
        b.hidden = b.getAttribute('data-tab') !== tb.getAttribute('data-tab');
      });
    });
  });
  $('#sp-toggle').addEventListener('click', function(){ spShow(false); });
  $('#sp-tab-collapsed').addEventListener('click', function(){ spShow(true); });
  spShow(lsGet(K + 'spOpen', true));

  var refreshFnd = function(){
    var open = 0;
    $$('#fnd-list li.fnd').forEach(function(li){
      var cb = $('.fnd-cb', li);
      var on = fndHandled.indexOf(cb.getAttribute('data-note')) >= 0;
      cb.checked = on;
      li.classList.toggle('handled', on);
      if(!on) open++;
    });
    var b = $('#fnd-open');
    if(b) b.textContent = open ? String(open) : '✓';
  };
  $$('#fnd-list .fnd-cb').forEach(function(cb){
    cb.addEventListener('change', function(){
      var id = cb.getAttribute('data-note');
      var i = fndHandled.indexOf(id);
      if(i >= 0) fndHandled.splice(i, 1); else { fndHandled.push(id); awardXp(2, cb); }
      lsSet(K + 'fndHandled', fndHandled);
      refreshFnd();
    });
  });
  var jumpToNote = function(id){
    var el = $$('.ai-note').filter(function(n){ return n.getAttribute('data-note') === id; })[0];
    if(!el) return;
    var det = el.closest('details.file');
    if(det){ det.open = true; det.classList.remove('f-hidden'); }
    el.open = true;
    el.scrollIntoView({block:'center'});
    el.classList.add('flash');
    setTimeout(function(){ el.classList.remove('flash'); }, 900);
  };
  $$('#fnd-list a[data-note]').forEach(function(a){
    a.addEventListener('click', function(e){ e.preventDefault(); jumpToNote(a.getAttribute('data-note')); });
  });
  refreshFnd();

  var tourLinks = $$('#tour-steps a');
  if(tourLinks.length){
    var tourCur = lsGet(K + 'tourStep', -1);
    var tourPos = $('#tour-pos');
    var tourMark = function(){
      tourLinks.forEach(function(a, j){ a.parentNode.classList.toggle('cur', j === tourCur); });
      tourPos.textContent = tourCur >= 0
        ? (tourCur + 1) + '/' + tourLinks.length
        : tourLinks.length + ' steps';
    };
    var tourGo = function(i){
      if(i < 0 || i >= tourLinks.length) return;
      tourCur = i;
      lsSet(K + 'tourStep', i);
      tourMark();
      var tid = tourLinks[i].getAttribute('data-target');
      var t = tid && document.getElementById(tid);
      if(!t) return;
      var det = t.closest('details.file');
      if(det){ det.open = true; det.classList.remove('f-hidden'); }
      t.scrollIntoView({block:'center'});
      if(t.classList.contains('ln')){
        t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash');
        setCur(t, false);
      }
    };
    tourLinks.forEach(function(a, i){
      a.addEventListener('click', function(e){ e.preventDefault(); tourGo(i); });
    });
    $('#tour-next').addEventListener('click', function(){ tourGo(Math.min(tourCur + 1, tourLinks.length - 1)); });
    $('#tour-prev').addEventListener('click', function(){ tourGo(Math.max(tourCur - 1, 0)); });
    tourMark();
  }
}

/* ---------------- resume scroll position ---------------- */
var scT = null;
window.addEventListener('scroll', function(){
  clearTimeout(scT);
  scT = setTimeout(function(){ lsSet(K + 'scroll', window.scrollY); }, 250);
}, {passive:true});
var savedY = lsGet(K + 'scroll', 0);
if(savedY > 0) setTimeout(function(){ window.scrollTo(0, savedY); }, 0);

/* ---------------- arcade mode (XP + confetti) ---------------- */
var arcade = lsGet('rd:arcade', false);
var xp = lsGet('rd:xp', 0);
var arcadeBtn = $('#btn-arcade');
function xpLevel(v){ return 1 + Math.floor(v / 100); }
function refreshXp(){
  var chip = $('#xp-chip');
  chip.hidden = !arcade;
  arcadeBtn.classList.toggle('primary', arcade);
  if(!arcade) return;
  chip.innerHTML = 'Lv ' + xpLevel(xp) + ' &middot; ' + xp + ' XP' +
    '<span class="xp-bar"><span class="xp-fill" style="width:' + (xp % 100) + '%"></span></span>';
}
function confettiBurst(x, y, n){
  var parts = [];
  for(var i = 0; i < n; i++){
    var d = document.createElement('div');
    d.className = 'cfx';
    d.style.background = 'hsl(' + Math.floor(Math.random() * 360) + ',90%,60%)';
    document.body.appendChild(d);
    parts.push({el:d, x:x, y:y, vx:(Math.random() - .5) * 10, vy:-(Math.random() * 8 + 4), r:Math.random() * 360});
  }
  var t0 = performance.now();
  function tick(now){
    var done = now - t0 > 1100;
    parts.forEach(function(p){
      p.vy += .4; p.x += p.vx; p.y += p.vy; p.r += p.vx * 5;
      p.el.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px) rotate(' + p.r + 'deg)';
      if(done) p.el.style.opacity = '0';
    });
    if(!done) requestAnimationFrame(tick);
    else setTimeout(function(){ parts.forEach(function(p){ p.el.remove(); }); }, 250);
  }
  requestAnimationFrame(tick);
}
function xpToast(text){
  var t = $('#xp-toast');
  if(!t){
    t = document.createElement('div');
    t.id = 'xp-toast';
    document.body.appendChild(t);
  }
  t.textContent = text;
  t.classList.remove('show'); void t.offsetWidth; t.classList.add('show');
}
function awardXp(n, el, label){
  if(!arcade) return;
  var lvBefore = xpLevel(xp);
  xp += n;
  lsSet('rd:xp', xp);
  refreshXp();
  var r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
  var x = r ? r.left + r.width / 2 : window.innerWidth / 2;
  var y = r ? r.top + r.height / 2 : window.innerHeight / 3;
  confettiBurst(x, y, label ? 90 : 28);
  var chip = $('#xp-chip');
  chip.classList.remove('pulse'); void chip.offsetWidth; chip.classList.add('pulse');
  if(label) xpToast(label);
  else if(xpLevel(xp) > lvBefore){
    xpToast('LEVEL ' + xpLevel(xp) + '!');
    confettiBurst(window.innerWidth / 2, window.innerHeight / 3, 120);
  }
  checkAchievements();
}
arcadeBtn.addEventListener('click', function(){
  arcade = !arcade;
  lsSet('rd:arcade', arcade);
  refreshXp();
  duckFollow(curRow);
  if(arcade) awardXp(1, arcadeBtn);
});
refreshXp();

/* ---------------- reading-time estimate ---------------- */
function refreshEta(){
  var el = $('#stat-eta');
  if(!el) return;
  var W = {risky: 1.4, core: 1, skim: .35, mechanical: .08};
  var mins = 0;
  $$('details.file').forEach(function(d){
    if(viewed.indexOf(d.getAttribute('data-file')) >= 0) return;
    var n = $$('tr.ln:not(.meta)', d).length;
    mins += n * (W[d.getAttribute('data-attention')] || 1) / 28;
  });
  el.innerHTML = mins < .75 ? 'done 🎉' : '&#8776;<b>' + Math.max(1, Math.round(mins)) + ' min</b> left';
}

/* ---------------- file-level keyboard flow ---------------- */
function visibleFiles(){
  return $$('details.file').filter(function(d){ return !d.classList.contains('f-hidden'); });
}
function currentFile(){
  return curRow ? curRow.closest('details.file') : null;
}
function focusFile(det){
  if(!det) return;
  det.open = true;
  var row = $('tr.ln:not(.meta)', det);
  if(row) setCur(row, true);
  else det.scrollIntoView({block:'start'});
}
function fileJump(dir){
  var files = visibleFiles();
  if(!files.length) return;
  var i = files.indexOf(currentFile());
  focusFile(files[Math.min(Math.max(i + dir, 0), files.length - 1)]);
}
function markAndNext(){
  var files = visibleFiles();
  if(!files.length) return;
  var cur = currentFile() || files.filter(function(d){
    return viewed.indexOf(d.getAttribute('data-file')) < 0;
  })[0];
  if(!cur) return;
  var path = cur.getAttribute('data-file');
  if(viewed.indexOf(path) < 0) toggleViewed(path);
  var after = files.slice(files.indexOf(cur) + 1).concat(files.slice(0, files.indexOf(cur)));
  var next = after.filter(function(d){
    return viewed.indexOf(d.getAttribute('data-file')) < 0;
  })[0];
  if(next) focusFile(next);
  else if(curRow){ curRow.classList.remove('cur'); curRow = null; }
}

/* ---------------- minimap ---------------- */
var mmap = document.createElement('canvas');
mmap.id = 'minimap';
document.body.appendChild(mmap);
var mmapRows = null, mmapDirty = null;
function cssVar(name){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function mmapCollect(){
  mmapRows = [];
  var sy = window.scrollY;
  $$('tr.ln.add, tr.ln.del, tr.ai-note-row, tr.uc-row').forEach(function(r){
    var rect = r.getBoundingClientRect();
    if(rect.height === 0) return;
    var kind = r.classList.contains('add') ? 'add'
      : r.classList.contains('del') ? 'del'
      : r.classList.contains('ai-note-row') ? 'note' : 'comment';
    var sev = 'info';
    if(kind === 'note'){
      var nEl = $('.ai-note', r);
      if(nEl && nEl.classList.contains('sev-warning')) sev = 'warning';
      else if(nEl && nEl.classList.contains('sev-suggestion')) sev = 'suggestion';
    }
    mmapRows.push({top: rect.top + sy, h: rect.height, kind: kind, sev: sev});
  });
}
function mmapDraw(){
  var H = window.innerHeight, W = 14;
  var dpr = window.devicePixelRatio || 1;
  mmap.width = W * dpr; mmap.height = H * dpr;
  var ctx = mmap.getContext('2d');
  ctx.scale(dpr, dpr);
  var docH = document.documentElement.scrollHeight;
  if(!mmapRows) mmapCollect();
  var colors = {add: cssVar('--res'), del: cssVar('--kw'), warning: cssVar('--warn'),
                suggestion: cssVar('--sugg'), info: cssVar('--info'), comment: cssVar('--accent')};
  mmapRows.forEach(function(r){
    var y = r.top / docH * H, h = Math.max(r.h / docH * H, 1.5);
    if(r.kind === 'add' || r.kind === 'del'){
      ctx.globalAlpha = .8; ctx.fillStyle = colors[r.kind];
      ctx.fillRect(1, y, 7, h);
    } else {
      ctx.globalAlpha = 1;
      ctx.fillStyle = r.kind === 'note' ? colors[r.sev] : colors.comment;
      ctx.beginPath(); ctx.arc(11, y + 1, 2, 0, 7); ctx.fill();
    }
  });
  ctx.globalAlpha = .16;
  ctx.fillStyle = cssVar('--fg');
  ctx.fillRect(0, window.scrollY / docH * H, W, window.innerHeight / docH * H);
  ctx.globalAlpha = 1;
}
function mmapRefresh(){
  clearTimeout(mmapDirty);
  mmapDirty = setTimeout(function(){ mmapCollect(); mmapDraw(); }, 300);
}
window.addEventListener('scroll', function(){ requestAnimationFrame(mmapDraw); }, {passive:true});
window.addEventListener('resize', mmapRefresh);
document.addEventListener('toggle', mmapRefresh, true);
document.addEventListener('click', function(e){
  if(e.target.closest('#filters')) mmapRefresh();
});
new MutationObserver(mmapRefresh)
  .observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
mmap.addEventListener('mousedown', function(e){
  function go(ev){
    var docH = document.documentElement.scrollHeight;
    window.scrollTo(0, ev.clientY / window.innerHeight * docH - window.innerHeight / 2);
  }
  go(e);
  function mv(ev){ go(ev); }
  function up(){ document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); }
  document.addEventListener('mousemove', mv);
  document.addEventListener('mouseup', up);
});
mmapRefresh();

/* ---------------- the duck ---------------- */
var duckEl = null, duckTop = 0;
function duckFollow(row){
  if(!arcade || !row){
    if(duckEl && !arcade){ duckEl.remove(); duckEl = null; }
    return;
  }
  if(!duckEl){
    duckEl = document.createElement('div');
    duckEl.id = 'duck';
    duckEl.textContent = '🦆';
    document.body.appendChild(duckEl);
  }
  var rect = row.getBoundingClientRect();
  var top = rect.top + window.scrollY - 22;
  var left = Math.max(rect.left - 26, 2);
  duckEl.classList.toggle('flip', top > duckTop);
  duckTop = top;
  duckEl.style.top = top + 'px';
  duckEl.style.left = left + 'px';
  duckEl.classList.remove('waddle'); void duckEl.offsetWidth; duckEl.classList.add('waddle');
}
function duckQuack(text){
  if(!duckEl) return;
  var q = document.createElement('span');
  q.className = 'quack';
  q.textContent = text;
  duckEl.appendChild(q);
  setTimeout(function(){ q.remove(); }, 1400);
}

/* ---------------- achievements ---------------- */
var ach = lsGet('rd:ach', {});
var stats = lsGet('rd:stats', {});
var seenReviews = lsGet('rd:seenReviews', []);
if(seenReviews.indexOf(data.id) < 0){
  seenReviews.push(data.id);
  lsSet('rd:seenReviews', seenReviews);
}
var loadT = performance.now();
function bumpStat(k){
  stats[k] = (stats[k] || 0) + 1;
  lsSet('rd:stats', stats);
}
var ACHIEVEMENTS = [
  {id:'first-words', icon:'💬', name:'First words', desc:'Write your first comment',
   test:function(){ return (stats.comments || 0) >= 1; }},
  {id:'nitpicker', icon:'🔬', name:'Nitpicker', desc:'10 comments in a single review',
   test:function(){ return store.items.filter(function(c){ return c.round === 'current'; }).length >= 10; }},
  {id:'completionist', icon:'✅', name:'Completionist', desc:'View every file in a review',
   test:function(){ return data.files.length > 0 && viewed.length >= data.files.length; }},
  {id:'speedrunner', icon:'⚡', name:'Speedrunner', desc:'Full review in under 5 minutes',
   test:function(){ return data.files.length >= 3 && viewed.length >= data.files.length
     && (performance.now() - loadT) < 300000; }},
  {id:'night-shift', icon:'🌙', name:'Night shift', desc:'Review after 23:00',
   test:function(){ var h = new Date().getHours(); return h >= 23 || h < 5; }},
  {id:'marathon', icon:'🏃', name:'Marathon', desc:'Open 10 different reviews',
   test:function(){ return seenReviews.length >= 10; }},
  {id:'exterminator', icon:'🧯', name:'Exterminator', desc:'Handle every finding in the digest',
   test:function(){ var cbs = $$('.fnd-cb'); return cbs.length > 0 && cbs.every(function(c){ return c.checked; }); }},
  {id:'critic', icon:'🗑️', name:'Critic', desc:'Dismiss 5 AI notes (lifetime)',
   test:function(){ return (stats.dismissed || 0) >= 5; }},
  {id:'level-5', icon:'🏆', name:'Level 5', desc:'Reach level 5',
   test:function(){ return xpLevel(xp) >= 5; }},
  {id:'insert-coin', icon:'🕹️', name:'Insert coin', desc:'Turn on arcade mode',
   test:function(){ return arcade; }}
];
function checkAchievements(){
  if(!arcade) return;
  ACHIEVEMENTS.forEach(function(a){
    if(ach[a.id]) return;
    var got = false;
    try{ got = a.test(); }catch(err){}
    if(!got) return;
    ach[a.id] = new Date().toISOString().slice(0, 10);
    lsSet('rd:ach', ach);
    xpToast(a.icon + ' ' + a.name);
    confettiBurst(window.innerWidth / 2, window.innerHeight / 3, 70);
  });
}

renderComments();
saveStore();
})();
</script>
@@MERMAID@@
</body>
</html>
"""

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a git diff + AI notes into a single-file HTML review page.")
    ap.add_argument("--patch", help="path to the raw unified diff")
    ap.add_argument("--notes", help="path to notes.ai.json")
    ap.add_argument("--out", help="path for review.html")
    ap.add_argument("--prev-comments", action="append", default=[],
                    help="comments.user.md from a previous round (repeatable)")
    ap.add_argument("--contrib", action="append", default=[],
                    help="external notes fragment to merge (repeatable; see INTEGRATIONS.md)")
    ap.add_argument("--contrib-dir", metavar="DIR",
                    help="merge every *.json fragment from DIR (sorted; missing dir is fine)")
    ap.add_argument("--validate-contrib", action="append", default=[], metavar="FILE",
                    help="validate fragment file(s) and exit — no patch/out needed")
    ap.add_argument("--notes-md", help="also write notes.ai.md here")
    ap.add_argument("--title", help="page title (default: base..head or patch name)")
    ap.add_argument("--review-id", help="stable id for localStorage keying (default: sha256 of patch)")
    ap.add_argument("--ensure-gitignore", metavar="REPO_ROOT",
                    help="ensure a .code-review/ entry in REPO_ROOT/.gitignore")
    args = ap.parse_args(argv)

    if args.validate_contrib:
        report = {"ok": True, "files": {}}
        for p in args.validate_contrib:
            try:
                doc = json.loads(Path(p).read_text(encoding="utf-8"))
                errors, warnings = validate_fragment(doc)
            except (OSError, json.JSONDecodeError) as e:
                errors, warnings = [str(e)], []
            report["files"][p] = {"errors": errors, "warnings": warnings}
            if errors:
                report["ok"] = False
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1

    if not args.patch or not args.out:
        ap.error("--patch and --out are required (unless using --validate-contrib)")

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

    contrib_paths = list(args.contrib)
    if args.contrib_dir and Path(args.contrib_dir).is_dir():
        contrib_paths.extend(sorted(str(p) for p in Path(args.contrib_dir).glob("*.json")))
    contribs = load_contribs(contrib_paths)
    contrib_notes = merge_contribs(notes_doc, contribs)

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
                  "contrib_files": len(contribs),
                  "contrib_notes": contrib_notes,
                  "size_kb": round(out_path.stat().st_size / 1024, 1)})
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

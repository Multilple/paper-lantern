"""Reads a unified diff and applies it to the pinned files in memory."""
import os
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


class FileChange:
    __slots__ = ("old_path", "new_path", "hunks", "binary", "old_mode", "new_mode")

    def __init__(self):
        self.old_path = self.new_path = None
        self.hunks = []
        self.binary = False
        self.old_mode = self.new_mode = None


def _strip(p):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(text):
    """-> list of FileChange. Raises PatchError on anything it does not understand."""
    out = []
    cur = None
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FileChange()
            out.append(cur)
            m = re.match(r'^diff --git a/(.*) b/(.*)$', ln)
            if m and m.group(1) == m.group(2):
                cur.old_path = cur.new_path = m.group(1)
            i += 1
            continue
        if cur is None:
            if ln.startswith("--- ") or ln.startswith("@@"):
                if ln.startswith("--- "):
                    cur = FileChange()
                    out.append(cur)
                else:
                    raise PatchError("hunk without file")
            else:
                i += 1
                continue
        if ln.startswith("--- "):
            if cur.hunks:      # a plain `diff -u` of several files has no "diff --git" lines
                cur = FileChange()
                out.append(cur)
            p = _strip(ln[4:].split("\t")[0])
            cur.old_path = p
            if p is None and cur.new_path is not None and not cur.hunks:
                pass
            i += 1
            if i < n and lines[i].startswith("+++ "):
                q = _strip(lines[i][4:].split("\t")[0])
                cur.new_path = q
                if p is None:
                    cur.old_path = None
                i += 1
            else:
                raise PatchError("--- without +++")
            continue
        if ln.startswith("rename from "):
            cur.old_path = ln[12:].strip(); i += 1; continue
        if ln.startswith("rename to "):
            cur.new_path = ln[10:].strip(); i += 1; continue
        if ln.startswith("copy from ") or ln.startswith("copy to "):
            raise PatchError("copy")
        if ln.startswith("new file mode"):
            cur.old_path = None; cur.new_mode = ln.split()[-1]; i += 1; continue
        if ln.startswith("deleted file mode"):
            cur.new_path = None; cur.old_mode = ln.split()[-1]; i += 1; continue
        if ln.startswith("old mode"):
            cur.old_mode = ln.split()[-1]; i += 1; continue
        if ln.startswith("new mode"):
            cur.new_mode = ln.split()[-1]; i += 1; continue
        if ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            cur.binary = True
            raise PatchError("binary")
        m = _HUNK.match(ln)
        if m:
            o0 = int(m.group(1)); oc = int(m.group(2) or 1)
            n0 = int(m.group(3)); nc = int(m.group(4) or 1)
            i += 1
            body = []
            so = sn = 0
            while i < n and (so < oc or sn < nc):
                h = lines[i]
                if h.startswith("\\"):
                    body.append(("\\", h)); i += 1; continue
                tag = h[:1]
                if tag == " " or (h == "" and so < oc and sn < nc):
                    body.append((" ", h[1:])); so += 1; sn += 1
                elif tag == "-":
                    body.append(("-", h[1:])); so += 1
                elif tag == "+":
                    body.append(("+", h[1:])); sn += 1
                else:
                    raise PatchError("bad hunk line")
                i += 1
            if i < n and lines[i].startswith("\\"):
                body.append(("\\", lines[i])); i += 1
            if so != oc or sn != nc:
                raise PatchError("short hunk")
            cur.hunks.append((o0, oc, n0, nc, body))
            continue
        i += 1
    for c in out:
        if c.old_path is None and c.new_path is None:
            raise PatchError("no paths")
    return out


def apply(old_text, change):
    """-> (new_text, old_ranges) where old_ranges are 1-based (first, last) old lines removed or,
    for a pure insertion, the two old lines around it. old_text None for a new file."""
    if old_text is None:
        old_lines, old_nl = [], True
    else:
        old_lines = old_text.split("\n") if old_text != "" else []
        old_nl = old_text.endswith("\n") or old_text == ""
        if old_text.endswith("\n"):
            old_lines.pop()
    res = []
    pos = 0
    new_nl = old_nl
    ranges = []
    for o0, oc, n0, nc, body in change.hunks:
        start = o0 - 1 if oc else o0
        if old_text is None:
            start = 0
        if start < pos or start > len(old_lines):
            raise PatchError("hunk order")
        res.extend(old_lines[pos:start])
        pos = start
        prev = None
        rfirst = None
        for tag, s in body:
            if tag == "\\":
                if prev == "-":
                    pass
                elif prev == "+":
                    new_nl = False
                elif prev == " ":
                    new_nl = old_nl
                continue
            if tag == " ":
                if pos >= len(old_lines) or old_lines[pos] != s:
                    raise PatchError("context mismatch")
                res.append(s); pos += 1
            elif tag == "-":
                if pos >= len(old_lines) or old_lines[pos] != s:
                    raise PatchError("removed line mismatch")
                ranges.append((pos + 1, pos + 1)); pos += 1
            else:
                res.append(s)
                ranges.append((max(pos, 1), min(pos + 1, max(len(old_lines), 1))))
                if pos >= len(old_lines):
                    new_nl = True
            prev = tag
    res.extend(old_lines[pos:])
    if not res:
        return "", ranges
    return "\n".join(res) + ("\n" if new_nl else ""), ranges

"""Reads one patch and decides, without importing sqlglot, what kind of change it is."""
import os

import codediff
import patch as patchlib

PKG = ("sqlglot/", "tests/")


class Plan:
    def __init__(self, cid):
        self.cid = cid
        self.kind = "none"      # none | run | all
        self.reason = ""
        self.py = {}            # relpath -> patched source (python files of sqlglot and the tests)
        self.data = {}          # relpath -> patched text, for files that tests read
        self.trees = {}         # relpath -> codediff root, when function code changed
        self.tier3 = False      # code that runs at import changed: needs a fresh import
        self.why = set()        # why tier3: shell | func | syntax | incomparable | dataimp
        self.cands = 0          # bitmap of the tests that can be affected, as far as known here
        self.unrun = 0          # bitmap of tests selected without a run
        self.lazy = False       # the test that starts "python -c 'import sqlglot'" can be affected


def _all(plan, why):
    plan.kind = "all"
    plan.reason = why
    return plan


def _bitmap(idxs):
    b = 0
    for i in idxs:
        b |= 1 << i
    return b


def make_plan(cid, text, maps, root):
    plan = Plan(cid)
    try:
        changes = patchlib.parse(text)
    except patchlib.PatchError as e:
        return _all(plan, "patch: %s" % e)
    opened = maps.opened
    imp = maps.imp
    for c in changes:
        if c.old_path != c.new_path:
            involved = [p for p in (c.old_path, c.new_path) if p]
            if any(p.startswith(PKG) or p in opened or ("DIR:" + os.path.dirname(p)) in opened for p in involved):
                return _all(plan, "file added, removed or renamed: %s" % involved)
            continue
        rel = c.old_path
        if not c.hunks:
            continue
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8", newline="") as f:
                old = f.read()
            new, _ = patchlib.apply(old, c)
        except (OSError, UnicodeError, patchlib.PatchError) as e:
            return _all(plan, "cannot apply to %s: %s" % (rel, e))
        if new == old:
            continue
        readers = opened.get(rel, [])
        if rel in imp["opened"]:
            plan.tier3 = True
            plan.why.add("dataimp")
            plan.data[rel] = new
        if rel.endswith(".py") and rel.startswith(PKG):
            plan.py[rel] = new
            kind, tree = codediff.compare(old, new, path)
            shell = kind in ("syntax", "incomparable")
            if kind == "tree":
                shell = any(n.own_changed and not n.is_function for n in codediff.walk(tree))
            if shell and rel.startswith("tests/"):
                # import-time change in a test file: its own tests, every test that runs a line of it,
                # and the tests of every test file that mentions the module by name
                plan.cands |= maps.tests_in_file(rel)
                stem = os.path.basename(rel)[:-3]
                users = {rel}
                tdir = os.path.join(root, "tests")
                for d, dirs, fs in os.walk(tdir):
                    dirs[:] = [x for x in dirs if x not in ("fixtures", "__pycache__")]
                    for fn in fs:
                        if fn.endswith(".py"):
                            q = os.path.join(d, fn)
                            try:
                                with open(q, encoding="utf-8") as fh:
                                    if stem in fh.read():
                                        users.add(os.path.relpath(q, root))
                            except OSError:
                                pass
                for i, tid in enumerate(maps.tests):
                    if tid.split("::", 1)[0] in users:
                        plan.cands |= 1 << i
            if kind in ("syntax", "incomparable"):
                plan.tier3 = True
                plan.why.add(kind)
                if kind == "incomparable":
                    # functions were added or removed: no pairing of old and new code, so every
                    # test that runs a line of the file can be affected
                    plan.cands |= maps.tests_in_file(rel)
            elif kind == "tree":
                plan.trees[rel] = tree
                ilines = imp["lines"].get(rel, set())
                for node in codediff.walk(tree):
                    if not node.own_changed:
                        continue
                    if not node.is_function:
                        plan.tier3 = True
                        plan.why.add("shell")
                        continue
                    plan.cands |= maps.tests_on_lines(rel, node.lines)
                    x = node
                    while x is not None and x.is_function:
                        if (rel, x.old.co_qualname, x.old.co_firstlineno) in imp["called"] and (node.lines & ilines):
                            plan.tier3 = True  # the changed lines also ran during import
                            plan.why.add("func")
                        x = x.parent
            if readers:
                plan.data[rel] = new
                plan.cands |= _bitmap(readers)
        elif rel.endswith(".py"):
            if readers:
                plan.unrun |= _bitmap(readers)
        else:
            plan.data[rel] = new
            plan.cands |= _bitmap(readers)
    if plan.tier3 and any(p.startswith("sqlglot/") for p in plan.py):
        plan.lazy = True
    if plan.tier3 or plan.cands or plan.unrun:
        plan.kind = "run"
    return plan

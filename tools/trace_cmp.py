"""Operand types at the comparison sites of sqlglot, on the pinned commit.

    trace_cmp.py REPO TREE OUT.json [pytest args...]

Copies REPO to TREE, rewrites every `a is b`, `a is not b`, `a == b`, `a != b` in sqlglot/ so that the
types of both operands are noted when the comparison runs, runs the real pytest session in TREE, and
writes   {relpath: {"line:col": [[kind_a, kind_b], ...]}}   to OUT.json (line and col are those of the
comparison in the pinned source).  A type is recorded as the way its == works:

    N  None                      B  bool
    O  == is identity (the type does not define __eq__ or __ne__)
    V  int, float, complex, str, bytes, tuple, list, dict, set, frozenset (exactly)
    E:<module>:<name>  member of that Enum class, compared as its str or int value
    X  anything else (a type with its own __eq__)
    Z  a type with its own __eq__ whose value, compared with None right there, said "not equal"

The run-time side (app/cmpeq.py) uses this to tell when `is` and `==` cannot differ at a site.
"""
import ast
import glob
import json
import os
import shutil
import subprocess
import sys

OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}

RECORDER = r'''
import atexit, enum, json, os, weakref
_seen = {}
_kinds = weakref.WeakKeyDictionary()
_V = (int, float, complex, str, bytes, tuple, list, dict, set, frozenset)
_none = type(None)
_busy = [False]


def _kind(t):
    if t is _none:
        return "N"
    if t is bool:
        return "B"
    if t in _V:
        return "V"
    eq = getattr(t, "__eq__", None)
    ne = getattr(t, "__ne__", None)
    if eq is object.__eq__ and ne is object.__ne__:
        return "O"
    try:
        if issubclass(t, enum.Enum) and ((eq is str.__eq__ and ne is str.__ne__) or (eq is int.__eq__ and ne is int.__ne__)):
            return "E:%s:%s" % (t.__module__, t.__qualname__)
    except Exception:
        pass
    return "X"


def c(site, a, b, op):
    ta = type(a)
    tb = type(b)
    try:
        ka = _kinds[ta]
    except Exception:
        ka = _kind(ta)
        try:
            _kinds[ta] = ka
        except Exception:
            pass
    try:
        kb = _kinds[tb]
    except Exception:
        kb = _kind(tb)
        try:
            _kinds[tb] = kb
        except Exception:
            pass
    if ((ka == "X" and kb == "N") or (ka == "N" and kb == "X")) and not _busy[0]:
        # a type with its own __eq__, compared with None: asked here what it answers
        # (not while such a question is being answered: __eq__ itself has comparisons in it)
        _busy[0] = True
        try:
            if (a == b) is False and (a != b) is True:
                if ka == "X":
                    ka = "Z"
                else:
                    kb = "Z"
        except BaseException:
            pass
        finally:
            _busy[0] = False
    s = _seen.get(site)
    if s is None:
        s = _seen[site] = set()
    s.add((ka, kb))
    if op == 0:
        return a is b
    if op == 1:
        return a is not b
    if op == 2:
        return a == b
    return a != b


def _dump():
    out = os.environ.get("BR_CMP_OUT")
    if not out or not _seen:
        return
    try:
        with open("%s.%d" % (out, os.getpid()), "w") as f:
            json.dump({k: sorted(v) for k, v in _seen.items()}, f)
    except Exception:
        pass


atexit.register(_dump)
'''


class Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.n = 0

    def visit_Compare(self, node):
        site = "%s|%d:%d" % (self.rel, node.lineno, node.col_offset)
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        self.n += 1
        call = ast.Call(
            func=ast.Name(id="_br_c", ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(OPS[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)

    # annotations are not evaluated the same way everywhere: leave them alone
    def visit_AnnAssign(self, node):
        if node.value is not None:
            node.value = self.visit(node.value)
        node.target = self.visit(node.target)
        return node

    def visit_arg(self, node):
        return node


def rewrite(src, rel):
    tree = ast.parse(src)
    rw = Rewrite(rel)
    tree = rw.visit(tree)
    if not rw.n:
        return None
    # the import goes after the docstring and the __future__ imports
    at = 0
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        at = 1
    while at < len(body) and isinstance(body[at], ast.ImportFrom) and body[at].module == "__future__":
        at += 1
    imp = ast.ImportFrom(module="_br_rec", names=[ast.alias(name="c", asname="_br_c")], level=0)
    body.insert(at, imp)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def main():
    repo, tree, out = sys.argv[1:4]
    extra = sys.argv[4:]
    if os.path.exists(tree):
        shutil.rmtree(tree)
    shutil.copytree(repo, tree, symlinks=True,
                    ignore=lambda d, names: [n for n in names if n in (".git", "__pycache__", ".pytest_cache")])
    n_files = n_sites = 0
    for p in sorted(glob.glob(os.path.join(tree, "sqlglot", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(p, tree)
        src = open(p, encoding="utf-8").read()
        new = rewrite(src, rel)
        if new is not None:
            open(p, "w", encoding="utf-8").write(new)
            n_files += 1
    open(os.path.join(tree, "_br_rec.py"), "w").write(RECORDER)
    print("rewritten files:", n_files, file=sys.stderr)
    for f in glob.glob(out + ".*"):
        os.remove(f)
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("PYTEST_", "BR_")) or k in ("PYTHONHASHSEED", "PYTHONSTARTUP", "PYTHONHOME"):
            env.pop(k)
    env.update(SKIP_INTEGRATION="0", PYTHONPATH=tree, PYTHONDONTWRITEBYTECODE="1", BR_CMP_OUT=out)
    if os.environ.get("BR_SEED"):      # set and dict order can steer the code: several seeds are merged
        env["PYTHONHASHSEED"] = os.environ["BR_SEED"]
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
           "--junitxml", out + ".junit.xml"] + extra
    rc = subprocess.call(cmd, cwd=tree, env=env)
    print("pytest exit code", rc, file=sys.stderr)
    merged = {}
    for f in glob.glob(out + ".*"):
        if f.endswith(".xml"):
            continue
        for site, pairs in json.load(open(f)).items():
            rel, pos = site.split("|")
            merged.setdefault(rel, {}).setdefault(pos, set()).update(tuple(x) for x in pairs)
        n_sites += 1
    json.dump({rel: {pos: sorted(v) for pos, v in sorted(d.items())} for rel, d in sorted(merged.items())},
              open(out, "w"), separators=(",", ":"))
    print("sites:", sum(len(d) for d in merged.values()), "from", n_sites, "processes", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Offline map builder, run on the pinned commit only: which keys of the class-level dicts of
sqlglot every test looks up, and whether it uses a table as a whole. See app/keymap.py.

    trace_keys.py tests.json out.jsonl part nparts

First line of out.jsonl: the tables ({"tags": [[[kind, owner, name], ...] per table], "unsafe":
{index: why}, "imported": {index: [test files whose import read the table]}}).
Then one line per test, run in its own fork: {"id", "bad", "k": {tag index: [key names] | 0}}.
"""
import gc
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import core      # noqa: E402
import keymap    # noqa: E402
import statefp   # noqa: E402

_hits = {}        # tag index -> set of key names, or WHOLE
_armed = [0]      # 0 off, 1 test modules are being imported, 2 a test runs
WHOLE = keymap.WHOLE
_name = keymap.keyname


_quiet = [None]   # the table a derived table is being built from right now (see hook_dispatch)
LEN = "\x00len"    # only the number of entries was asked for
BOOL = "\x00bool"  # only whether the table is empty was asked for
_import_by = {}    # tag -> the test-side modules whose code read the table while being imported


def _reader_module():
    """The test-side module whose code is running, innermost first."""
    f = sys._getframe(2)
    while f is not None:
        n = f.f_globals.get("__name__", "")
        if n == "tests" or n.startswith("tests."):
            return n
        f = f.f_back
    return "?"


def _whole(self):
    if _armed[0] and self._br_tag != _quiet[0]:
        _hits[self._br_tag] = WHOLE
        if _armed[0] == 1:
            _import_by.setdefault(self._br_tag, set()).add(_reader_module())


def _len(self, what):
    a = _armed[0]
    if not a or self._br_tag == _quiet[0]:
        return
    tag = self._br_tag
    if a == 1:
        _import_by.setdefault(tag, set()).add(_reader_module())
        return
    s = _hits.get(tag)
    if s is WHOLE:
        return
    if s is None:
        s = _hits[tag] = set()
    s.add(what)


def _key(self, k):
    a = _armed[0]
    if not a or self._br_tag == _quiet[0]:
        return
    tag = self._br_tag
    if a == 1:
        _import_by.setdefault(tag, set()).add(_reader_module())
        return
    s = _hits.get(tag)
    if s is WHOLE:
        return
    try:
        n = _name(k)
    except Exception:
        n = None
    if n is None:
        _hits[tag] = WHOLE     # a key without a name: nothing is concluded for this test
        return
    if s is None:
        s = _hits[tag] = set()
    s.add(n)


class RecDict(dict):
    """The pinned table, with every way of reading it noted."""
    __slots__ = ("_br_tag",)

    def __getitem__(self, k):
        _key(self, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _key(self, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _key(self, k)
        return dict.__contains__(self, k)

    # everything else that reads or writes counts as using the whole table
    def __iter__(self):
        _whole(self)
        return dict.__iter__(self)

    def __len__(self):
        _len(self, LEN)
        return dict.__len__(self)

    def __bool__(self):
        _len(self, BOOL)
        return dict.__len__(self) != 0

    def keys(self):
        _whole(self)
        return dict.keys(self)

    def values(self):
        _whole(self)
        return dict.values(self)

    def items(self):
        _whole(self)
        return dict.items(self)

    def copy(self):
        _whole(self)
        return dict.copy(self)

    def __eq__(self, o):
        _whole(self)
        return dict.__eq__(self, o)

    def __ne__(self, o):
        _whole(self)
        return dict.__ne__(self, o)

    __hash__ = None

    def __or__(self, o):
        _whole(self)
        return dict.__or__(self, o)

    def __ror__(self, o):
        _whole(self)
        return dict.__ror__(self, o)

    def __ior__(self, o):
        _whole(self)
        return dict.__ior__(self, o)

    def __repr__(self):
        _whole(self)
        return dict.__repr__(self)

    def __reversed__(self):
        _whole(self)
        return dict.__reversed__(self)

    def __reduce_ex__(self, proto):
        _whole(self)
        return (dict, (dict(dict.items(self)),))

    def __reduce__(self):
        _whole(self)
        return (dict, (dict(dict.items(self)),))

    def __copy__(self):
        _whole(self)
        return dict(dict.items(self))

    def __deepcopy__(self, memo):
        _whole(self)
        import copy
        return copy.deepcopy(dict(dict.items(self)), memo)

    def __setitem__(self, k, v):
        _whole(self)
        return dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        _whole(self)
        return dict.__delitem__(self, k)

    def pop(self, *a):
        _whole(self)
        return dict.pop(self, *a)

    def popitem(self):
        _whole(self)
        return dict.popitem(self)

    def setdefault(self, *a):
        _whole(self)
        return dict.setdefault(self, *a)

    def update(self, *a, **kw):
        _whole(self)
        return dict.update(self, *a, **kw)

    def clear(self):
        _whole(self)
        return dict.clear(self)

    def __sizeof__(self):
        _whole(self)
        return dict.__sizeof__(self)


def _base_refs():
    """sys.getrefcount of a dict that one class body holds and nothing else, seen the way install() sees it"""
    c = type("_Probe", (), {"T": {"a": 1}})
    groups = {}
    for name, v in list(vars(c).items()):
        if name == "T":
            groups[id(v)] = (v, [1])
    del name, v
    for d, holders in list(groups.values()):
        return sys.getrefcount(d)


def install():
    """Every plain dict that classes and modules of sqlglot hold under a name, and that nothing else
    refers to, is replaced by a recording copy in all its holders.
    -> (tags: one list of holders [kind, owner, name] per table, unsafe {index: why})"""
    classes = [c for c in statefp.all_classes() if (c.__module__ or "").startswith("sqlglot")]
    count = {}
    for c in classes:
        n = "%s:%s" % (c.__module__, c.__qualname__)
        count[n] = count.get(n, 0) + 1
    base = _base_refs()
    gc.collect()
    groups = {}     # id(dict) -> (dict, [(kind, owner name, attr, holder object)])
    for c in sorted(classes, key=lambda c: "%s:%s" % (c.__module__, c.__qualname__)):
        cn = "%s:%s" % (c.__module__, c.__qualname__)
        if count[cn] != 1:
            continue
        for name, v in list(vars(c).items()):
            if type(v) is dict and not name.startswith("__"):
                groups.setdefault(id(v), (v, []))[1].append(("c", cn, name, c))
    for mn in sorted(m for m in list(sys.modules) if m == "sqlglot" or m.startswith("sqlglot.")):
        mod = sys.modules.get(mn)
        if mod is None:
            continue
        for name, v in list(vars(mod).items()):
            if type(v) is dict and not name.startswith("__"):
                groups.setdefault(id(v), (v, []))[1].append(("g", mn, name, mod))
    del name, v
    tags, unsafe = [], {}
    for d, holders in list(groups.values()):
        i = len(tags)
        tags.append([[k, o, a] for k, o, a, _ in holders])
        refs = sys.getrefcount(d)
        if refs != base + len(holders) - 1:
            unsafe[i] = "referenced %d times elsewhere" % (refs - base - len(holders) + 1)
            continue
        if keymap.names_of(d) is None:
            unsafe[i] = "keys without distinct names"
            continue
        r = RecDict(d)
        r._br_tag = i
        try:
            for k, o, a, h in holders:
                if k == "c":
                    type.__setattr__(h, a, r)
                else:
                    vars(h)[a] = r
        except Exception as e:
            unsafe[i] = "cannot be replaced: %r" % (e,)
    return tags, unsafe


def hook_dispatch():
    """The generator copies TRANSFORMS into a per-class dispatch table the first time a class is
    used (sqlglot/generator.py: _build_dispatch, kept in _DISPATCH_CACHE) and looks expressions up
    there. The copy is not a use of the whole table: the lookups in the dispatch table are noted
    as lookups in the TRANSFORMS table it was copied from."""
    import inspect
    gen = sys.modules.get("sqlglot.generator")
    orig = getattr(gen, "_build_dispatch", None)
    if orig is None or "dict(cls.TRANSFORMS)" not in inspect.getsource(orig) \
            or not isinstance(getattr(gen, "_DISPATCH_CACHE", None), dict):
        return False
    gen._DISPATCH_CACHE.clear()

    def _build_dispatch(cls):
        t = None
        for k in cls.__mro__:
            if "TRANSFORMS" in vars(k):
                t = vars(k)["TRANSFORMS"]
                break
        if not isinstance(t, RecDict):
            return orig(cls)
        _quiet[0] = t._br_tag
        try:
            d = orig(cls)
        finally:
            _quiet[0] = None
        if type(d) is not dict:
            return d
        r = RecDict(d)
        r._br_tag = t._br_tag
        return r
    gen._build_dispatch = _build_dispatch
    return True


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out = open(sys.argv[2], "w")
    part, nparts = int(sys.argv[3]), int(sys.argv[4])
    files = sorted({t["file"] for t in tests})
    core.Warm([])                       # sqlglot only
    tags, unsafe = install()
    print("dispatch hook:", hook_dispatch(), file=sys.stderr)
    # what the test modules read while they are imported sits in their own state afterwards:
    # nothing is concluded for those tables
    _armed[0] = 1
    for f in files:
        try:
            importlib.import_module(core.module_name(f))
        except BaseException as e:
            print("import failed", f, repr(e), file=sys.stderr)
    _armed[0] = 0
    _hits.clear()
    warm = core.Warm(files)
    assert not warm.import_errors, warm.import_errors
    # A table read while a test module is imported has left its mark in that module: every test of
    # a file that is this module, or uses something from it, counts as using the whole table.
    mods = {f: sys.modules[core.module_name(f)] for f in files}
    tmods = {n: m for n, m in sys.modules.items() if (n == "tests" or n.startswith("tests.")) and m is not None}

    def uses(m):
        out_ = set()
        for v in list(vars(m).values()):
            n = v.__name__ if isinstance(v, type(sys)) else getattr(v, "__module__", None)
            if not isinstance(n, str):
                n = getattr(type(v), "__module__", None)
            if isinstance(n, str) and n in tmods and n != m.__name__:
                out_.add(n)
        return out_
    direct = {n: uses(m) for n, m in tmods.items()}

    def closure(n):
        seen, stack = set(), [n]
        while stack:
            x = stack.pop()
            for y in direct.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen
    imported = {}
    for i, readers in _import_by.items():
        if "?" in readers:
            unsafe.setdefault(str(i), "read while the test modules are imported, by code that is not theirs")
            continue
        fs = sorted(f for f, m in mods.items() if m.__name__ in readers or closure(m.__name__) & readers)
        imported[str(i)] = fs
    unsafe = {str(k): v for k, v in unsafe.items()}
    out.write(json.dumps({"tags": tags, "unsafe": unsafe, "imported": imported}) + "\n"); out.flush()
    print("tables", len(tags), "unsafe", len(unsafe), file=sys.stderr)
    gc.collect(); gc.freeze()
    for t in tests:
        if t["number"] % nparts != part:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            _hits.clear()
            _armed[0] = 2
            try:
                core.session_state(t["file"])
                core.prepare_class(t["id"], warm.modules)
            except BaseException:
                pass
            bad = core.run_test(t["id"], warm.modules)
            _armed[0] = 0
            rec = {"id": t["id"], "bad": bad,
                   "k": {str(i): (0 if s is WHOLE else sorted(s)) for i, s in _hits.items()}}
            data = json.dumps(rec).encode()
            while data:
                n = os.write(w, data)
                data = data[n:]
            os._exit(0)
        os.close(w)
        buf = b""
        while True:
            b = os.read(r, 1 << 20)
            if not b:
                break
            buf += b
        os.close(r)
        _, st = os.waitpid(pid, 0)
        if not buf:
            buf = json.dumps({"id": t["id"], "died": st}).encode()
        out.write(buf.decode() + "\n"); out.flush()


if __name__ == "__main__":
    main()

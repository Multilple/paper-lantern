"""Offline map builder, run on the pinned commit only: which keys of the class-level dicts of
sqlglot every test looks up, and whether it uses a table as a whole. See app/keymap.py.

    trace_keys.py tests.json out.jsonl part nparts

First line of out.jsonl: the tables ({"tags": [[class, attr], ...], "unsafe": {index: why}}).
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
_import_reads = set()
WHOLE = keymap.WHOLE
_name = keymap.keyname


def _whole(self):
    if _armed[0]:
        _hits[self._br_tag] = WHOLE
        if _armed[0] == 1:
            _import_reads.add(self._br_tag)


def _key(self, k):
    a = _armed[0]
    if not a:
        return
    tag = self._br_tag
    if a == 1:
        _import_reads.add(tag)
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
        _whole(self)
        return dict.__len__(self)

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
    """sys.getrefcount of a dict that only a class body refers to, measured the way install() looks"""
    c = type("_Probe", (), {"T": {"a": 1}})
    for name, v in list(vars(c).items()):
        if name == "T":
            return sys.getrefcount(v)


def install():
    """-> (tags [(class name, attr)], unsafe {index: why}); the safe ones are replaced in place."""
    classes = [c for c in statefp.all_classes() if (c.__module__ or "").startswith("sqlglot")]
    count = {}
    for c in classes:
        n = "%s:%s" % (c.__module__, c.__qualname__)
        count[n] = count.get(n, 0) + 1
    base = _base_refs()
    tags, unsafe = [], {}
    gc.collect()
    for c in sorted(classes, key=lambda c: "%s:%s" % (c.__module__, c.__qualname__)):
        cn = "%s:%s" % (c.__module__, c.__qualname__)
        if count[cn] != 1:
            continue
        for name, v in list(vars(c).items()):
            if type(v) is not dict or name.startswith("__"):
                continue
            i = len(tags)
            tags.append((cn, name))
            refs = sys.getrefcount(v)
            if refs != base:
                unsafe[i] = "referenced %d times elsewhere" % (refs - base)
                continue
            if keymap.names_of(v) is None:
                unsafe[i] = "keys without distinct names"
                continue
            r = RecDict(v)
            r._br_tag = i
            try:
                type.__setattr__(c, name, r)
            except Exception as e:
                unsafe[i] = "cannot be replaced: %r" % (e,)
    return tags, unsafe


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out = open(sys.argv[2], "w")
    part, nparts = int(sys.argv[3]), int(sys.argv[4])
    files = sorted({t["file"] for t in tests})
    core.Warm([])                       # sqlglot only
    tags, unsafe = install()
    # what the test modules read while they are imported sits in their own state afterwards:
    # nothing is concluded for those tables
    _armed[0] = 1
    for f in files:
        try:
            importlib.import_module(core.module_name(f))
        except BaseException as e:
            print("import failed", f, repr(e), file=sys.stderr)
    _armed[0] = 0
    for i in sorted(_import_reads):
        unsafe.setdefault(i, "read while the test modules are imported")
    _hits.clear()
    warm = core.Warm(files)
    assert not warm.import_errors, warm.import_errors
    out.write(json.dumps({"tags": tags, "unsafe": unsafe}) + "\n"); out.flush()
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

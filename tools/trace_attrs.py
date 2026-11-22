"""Offline map builder, pinned commit only: which class attributes every test READS.

Every entry of every sqlglot class __dict__ (see wrappable()) is replaced by a recording
descriptor that hands out the original value. Each test then runs alone in a fork of the warm
process, exactly as trace.py runs it, and the descriptors that fired are written out.

  plain values (flags, dicts, sets, nested classes ...)  -> Data: stays in place for the whole
      test and records every class the lookup went through (the `objtype` of __get__), so the
      map knows "read as X.attr" for every class X between the lookup class and the defining one
  functions, classmethod, staticmethod, cached_property  -> Once: records (defining class, name)
      on the first read and then puts the original back (so method calls keep their speed)
  property (a data descriptor)                            -> OnceData: same, plus __set__/__delete__

usage: trace_attrs.py tests.json out.jsonl part nparts
       trace_attrs.py build tests.json attrs.bin out.jsonl...
"""
import enum, functools, json, os, pickle, sys, time, types, zlib
sys.path.insert(0, os.environ.get("BR_APP") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

_armed = [0]      # 0 off, 1 test-module import phase (record, never unwrap), 2 test phase
_data_hits = []   # (Data wrapper, lookup class)
_once_hits = []   # Once / OnceData wrappers
_raw_hits = []    # (class name, attr, what): somebody used a wrapper taken straight from a class __dict__


class Data:
    __slots__ = ("c", "n", "v", "seen")

    def __init__(self, c, n, v):
        self.c, self.n, self.v, self.seen = c, n, v, set()

    def __get__(self, obj, objtype=None):
        t = objtype if objtype is not None else type(obj)
        s = self.seen
        if t not in s and _armed[0]:
            s.add(t)
            _data_hits.append((self, t))
        return self.v

    def __getattr__(self, name):
        _raw_hits.append((self.c.__qualname__, self.n, name))
        return getattr(self.v, name)


class Once:
    __slots__ = ("c", "n", "v", "g", "hit")

    def __init__(self, c, n, v):
        self.c, self.n, self.v, self.hit = c, n, v, False
        self.g = type(v).__get__

    def _fire(self):
        a = _armed[0]
        if a:
            if not self.hit:
                self.hit = True
                _once_hits.append(self)
            if a == 2 and self.c.__dict__.get(self.n) is self:
                type.__setattr__(self.c, self.n, self.v)

    def __get__(self, obj, objtype=None):
        self._fire()
        return self.g(self.v, obj, objtype)

    def __getattr__(self, name):
        _raw_hits.append((self.c.__qualname__, self.n, name))
        return getattr(self.v, name)


class OnceData(Once):
    __slots__ = ()

    def __set__(self, obj, value):
        self._fire()
        self.v.__set__(obj, value)

    def __delete__(self, obj):
        self._fire()
        self.v.__delete__(obj)


_SKIP_NAMES = {"_abc_impl"}
_ONCE_TYPES = (types.FunctionType, classmethod, staticmethod, functools.cached_property)


def kind_of(name, v):
    """'d' plain value, 'f' non-data descriptor we know, 'p' property, None: leave alone."""
    if name.startswith("__") and name.endswith("__"):
        return None          # read through type slots and by the class machinery
    if name in _SKIP_NAMES:
        return None
    t = type(v)
    if t is property:
        return "p"
    if hasattr(t, "__set__") or hasattr(t, "__delete__"):
        return None          # slot members, getset descriptors, namedtuple fields
    if hasattr(t, "__get__"):
        return "f" if t in _ONCE_TYPES else None
    return "d"


def class_ok(c, sqlglot_metas):
    """Classes whose dict entries may be replaced."""
    if not c.__module__.startswith("sqlglot"):
        return False                      # test classes keep the broad rule
    if issubclass(c, enum.Enum):
        return False
    m = type(c)
    if m is not type and m not in sqlglot_metas and m.__name__ != "ABCMeta":
        return False                      # TypedDict, Protocol ...: typing reads their dict directly
    if issubclass(c, tuple) and hasattr(c, "_fields"):
        return False                      # namedtuple
    return True


def cname(c):
    return "%s:%s" % (c.__module__, c.__qualname__)


def install():
    """-> (classes sorted by name, {class: {attr: kind}}) ; dup-named classes are not wrapped."""
    import statefp
    classes = statefp.all_classes()
    count = {}
    for c in classes:
        count[cname(c)] = count.get(cname(c), 0) + 1
    metas = {c for c in classes if isinstance(c, type) and issubclass(c, type) and c.__module__.startswith("sqlglot")}
    named = sorted((c for c in classes if count[cname(c)] == 1), key=cname)
    own = {}
    for c in named:
        if not class_ok(c, metas):
            continue
        d = own[c] = {}
        for k, v in list(vars(c).items()):
            kd = kind_of(k, v)
            if kd is None:
                continue
            w = Data(c, k, v) if kd == "d" else OnceData(c, k, v) if kd == "p" else Once(c, k, v)
            type.__setattr__(c, k, w)
            d[k] = kd
    return named, own


def main():
    import core
    tests = json.load(open(sys.argv[1]))["tests"]
    out = open(sys.argv[2], "w")
    part, nparts = int(sys.argv[3]), int(sys.argv[4])
    files = sorted({t["file"] for t in tests})
    core.Warm([])                       # sqlglot only
    named, own = install()
    cid = {c: i for i, c in enumerate(named)}
    attrs = sorted({k for d in own.values() for k in d} | {"<none>"})
    aid = {a: i for i, a in enumerate(attrs)}
    NA = len(attrs)

    def keys_of():
        ks = set()
        for w, t in _data_hits:
            n, D = aid[w.n], w.c
            try:
                mro = t.__mro__
            except Exception:
                mro = (D,)
            if D not in mro:              # explicit __get__ call with an unrelated class
                mro = (D,)
            for X in mro:
                i = cid.get(X)
                if i is not None:
                    ks.add(i * NA + n)
                if X is D:
                    break
        for w in _once_hits:
            ks.add(cid[w.c] * NA + aid[w.n])
        return sorted(ks)

    # ---- phase 1: importing the test modules. What they read while being imported is kept
    # apart: its effect sits in the test modules' own state, not in a test's run.
    import importlib
    _armed[0] = 1
    per_file = {}
    for f in files:
        del _data_hits[:], _once_hits[:]
        importlib.import_module(core.module_name(f))
        per_file[f] = keys_of()      # a shared helper module counts for the first file that imports it
        for wr, t in _data_hits:     # so that the wrapper fires again for the next file
            wr.seen.discard(t)
        for wr in _once_hits:
            wr.hit = False
    _armed[0] = 0
    warm = core.Warm(files)
    assert not warm.import_errors, warm.import_errors
    del _data_hits[:], _once_hits[:], _raw_hits[:]
    # names a class inherits from a wrapped plain value: a root that adds such a name to the
    # class is still covered, because Data records every class a lookup went through
    cand = {}
    inh = {}
    for c in named:
        names = set()
        for b in c.__mro__[1:]:
            d = own.get(b)
            if d:
                names.update(k for k, kd in d.items() if kd == "d")
        mine = vars(c)
        got = []
        for k in names:
            if k in mine:
                continue
            for b in c.__mro__[1:]:
                if k in b.__dict__:
                    if own.get(b, {}).get(k) == "d":
                        got.append(aid[k])
                    break
        if got:
            inh[str(cid[c])] = sorted(got)
    meta = {"meta": 1, "classes": [cname(c) for c in named], "attrs": attrs,
            "own": {str(cid[c]): d for c, d in own.items()},
            "inh": inh, "import_reads": per_file,
            # for every class, the named classes below it (itself included)
            "desc": {str(cid[c]): sorted(cid[x] for x in named if c in x.__mro__) for c in named}}
    out.write(json.dumps(meta, separators=(",", ":")) + "\n"); out.flush()

    real_session_state = core.session_state

    def quiet_session_state(test_file):   # the runner's own read of the dialect registry is not the test's
        a = _armed[0]
        _armed[0] = 0
        try:
            return real_session_state(test_file)
        finally:
            _armed[0] = a
    core.session_state = quiet_session_state

    import gc
    gc.collect(); gc.freeze()
    only = os.environ.get("ATTR_ONLY")
    only = {int(x) for x in only.split(",")} if only else None
    for t in tests:
        if t["number"] % nparts != part or (only is not None and t["number"] not in only):
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            _armed[0] = 2
            t0 = time.process_time()
            bad = core.run_test(t["id"], warm.modules)
            cpu = time.process_time() - t0
            _armed[0] = 0
            rec = {"id": t["id"], "bad": bad, "cpu": round(cpu, 4), "k": keys_of()}
            if _raw_hits:
                rec["raw"] = sorted(set(_raw_hits))[:50]
            os.write(w, json.dumps(rec, separators=(",", ":")).encode())
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
    if sys.argv[1] == "build":
        import build_attrs
        build_attrs.main(sys.argv[2:])
    else:
        main()

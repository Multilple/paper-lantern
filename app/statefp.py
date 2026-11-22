"""Fingerprint of the state that importing sqlglot leaves behind: one digest for every module
global and every class attribute. Two imports of the same tree give the same fingerprint, so a
difference names what a patch changed at import time."""
import enum
import functools
import hashlib
import re
import sys
import types

_SKIP_CLASS_ATTRS = {"__firstlineno__", "__static_attributes__", "__dict__", "__weakref__",
                     "__module__", "__qualname__", "__orig_bases__", "__parameters__", "__abstractmethods__",
                     "_abc_impl", "__annotations__", "__annotate__", "__type_params__"}
_SKIP_GLOBALS = {"__builtins__", "__cached__", "__loader__", "__spec__", "__file__", "__path__",
                 "__doc__", "__name__", "__package__", "__annotations__", "__warningregistry__"}
_PRIM = (int, float, complex, str, bytes, bool, type(None), type(Ellipsis), type(NotImplemented))
_Pattern = type(re.compile(""))


def ours(modname):
    return isinstance(modname, str) and (modname == "sqlglot" or modname.startswith("sqlglot.")
                                         or modname == "tests" or modname.startswith("tests."))


_code_memo = {}


def code_digest(code):
    """Digest of a code object without its line numbers."""
    d = _code_memo.get(code)
    if d is None:
        h = hashlib.blake2b(digest_size=8)
        h.update(code.co_code)
        h.update(repr((code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars,
                       code.co_argcount, code.co_kwonlyargcount, code.co_flags)).encode())
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                h.update(code_digest(c))
            else:
                h.update(repr((type(c).__name__, c)).encode("utf-8", "surrogatepass"))
        d = _code_memo[code] = h.digest()
    return d


class Printer:
    subst = {}  # digest of a function body the patch changed -> digest of its pinned body

    def __init__(self, loose=False):
        self.loose = loose  # order of lists, tuples and dicts does not count
        self.memo = {}
        self.keep = []  # keeps objects alive so that ids stay unique

    def digest(self, v):
        h = hashlib.blake2b(digest_size=12)
        self._w(v, h.update, set(), 0)
        return h.digest()

    def _sub(self, v, path, depth):
        key = id(v)
        d = self.memo.get(key)
        if d is None:
            h = hashlib.blake2b(digest_size=12)
            self._w(v, h.update, path, depth)
            d = h.digest()
            if not path & {key}:
                self.memo[key] = d
                self.keep.append(v)
        return d

    def _w(self, v, put, path, depth):
        t = type(v)
        if t in _PRIM:
            put(b"p"); put(t.__name__.encode()); put(repr(v).encode("utf-8", "surrogatepass")); return
        if isinstance(v, type):
            put(b"T"); put(("%s:%s" % (v.__module__, v.__qualname__)).encode()); return
        if t is types.ModuleType:
            put(b"M"); put(v.__name__.encode()); return
        if isinstance(v, enum.Enum):
            put(b"E"); put(("%s:%s.%s" % (t.__module__, t.__qualname__, v.name)).encode()); return
        vid = id(v)
        if vid in path or depth > 60:
            put(b"<cycle>"); put(t.__name__.encode()); return
        if t is types.FunctionType:
            put(b"F"); put(("%s:%s" % (v.__module__, v.__qualname__)).encode())
            d = code_digest(v.__code__)
            put(self.subst.get(d, d))
            path.add(vid)
            try:
                if v.__defaults__:
                    put(b"d"); put(self._sub(v.__defaults__, path, depth + 1))
                if v.__kwdefaults__:
                    put(b"k"); put(self._sub(v.__kwdefaults__, path, depth + 1))
                if v.__closure__:
                    for name, cell in zip(v.__code__.co_freevars, v.__closure__):
                        put(b"c"); put(name.encode())
                        try:
                            c = cell.cell_contents
                        except ValueError:
                            put(b"<empty>"); continue
                        put(self._sub(c, path, depth + 1))
            finally:
                path.discard(vid)
            return
        if t in (staticmethod, classmethod):
            put(t.__name__.encode()); put(self._sub(v.__func__, path, depth + 1)); return
        if t is property:
            put(b"prop")
            for f in (v.fget, v.fset, v.fdel):
                put(self._sub(f, path, depth + 1))
            return
        if t is types.MethodType:
            put(b"BM"); put(self._sub(v.__func__, path, depth + 1))
            s = v.__self__
            put(self._sub(s if isinstance(s, type) else type(s), path, depth + 1)); return
        if t is functools.partial:
            put(b"partial")
            path.add(vid)
            try:
                put(self._sub(v.func, path, depth + 1)); put(self._sub(v.args, path, depth + 1))
                put(self._sub(v.keywords, path, depth + 1))
            finally:
                path.discard(vid)
            return
        if t is _Pattern:
            put(b"re"); put(repr((v.pattern, v.flags)).encode()); return
        path.add(vid)
        try:
            if t in (tuple, list) or (isinstance(v, (tuple, list))):
                put(b"L" if isinstance(v, list) else b"U"); put(t.__name__.encode())
                ds = [self._sub(x, path, depth + 1) for x in v]
                if self.loose or (len(v) > 1 and all(isinstance(x, type) for x in v)):  # e.g. tuple(set of classes)
                    ds.sort()
                for d in ds:
                    put(d)
                return
            if isinstance(v, dict):
                put(b"D"); put(t.__name__.encode())
                items = list(v.items())
                pairs = [(self._sub(k, path, depth + 1), self._sub(x, path, depth + 1)) for k, x in items]
                if self.loose or (items and all(isinstance(k, type) for k, _ in items)):
                    pairs.sort()  # keyed by classes: the order comes from addresses
                for a, b in pairs:
                    put(a); put(b)
                return
            if isinstance(v, (set, frozenset)):
                put(b"S"); put(t.__name__.encode())
                for d in sorted(self._sub(x, path, depth + 1) for x in list(v)):
                    put(d)
                return
            w = getattr(v, "__wrapped__", None)
            if w is not None and callable(w) and not ours(getattr(t, "__module__", "")):
                put(b"W"); put(t.__name__.encode()); put(self._sub(w, path, depth + 1)); return
            mod = getattr(t, "__module__", "")
            if ours(mod) or mod.startswith("tests"):
                put(b"O"); put(("%s:%s" % (mod, t.__qualname__)).encode())
                d = getattr(v, "__dict__", None)
                if isinstance(d, dict):
                    for k, x in list(d.items()):
                        put(str(k).encode()); put(self._sub(x, path, depth + 1))
                for klass in t.__mro__:
                    for s in klass.__dict__.get("__slots__", ()) if isinstance(klass.__dict__.get("__slots__", ()), (tuple, list)) else ():
                        try:
                            x = getattr(v, s)
                        except AttributeError:
                            continue
                        put(s.encode()); put(self._sub(x, path, depth + 1))
                return
            try:
                r = repr(v)
            except Exception:
                r = "<repr failed>"
            if " at 0x" in r or " object at " in r:
                r = "<%s:%s>" % (mod, t.__qualname__)
            put(b"R"); put(r.encode("utf-8", "surrogatepass"))
        finally:
            path.discard(vid)


def all_classes():
    seen, out, stack = set(), [], [object]
    while stack:
        c = stack.pop()
        try:
            subs = type.__subclasses__(c)
        except TypeError:
            continue
        for s in subs:
            if s not in seen:
                seen.add(s)
                stack.append(s)
                if ours(getattr(s, "__module__", None)):
                    out.append(s)
    return out


def fingerprint(loose_keys=(), subst=None):
    """-> {("g", module, name) | ("c", "module:qualname", attr): digest}
    loose_keys: keys whose value is built in an order that depends on addresses."""
    strict = Printer()
    loose = Printer(loose=True)
    strict.subst = loose.subst = subst or {}
    loose_keys = set(loose_keys)

    class _Pick:
        def digest(self, v, key=None):
            return (loose if key in loose_keys else strict).digest(v)
    pr = _Pick()
    fp = {}
    for name in sorted(m for m in list(sys.modules) if ours(m)):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in list(vars(mod).items()):
            if k in _SKIP_GLOBALS:
                continue
            fp[("g", name, k)] = pr.digest(v, ("g", name, k))
    import gc
    gc.collect()   # classes that are garbage but not yet freed must not be counted
    classes = all_classes()
    count = {}
    for c in classes:
        key = "%s:%s" % (c.__module__, c.__qualname__)
        count[key] = count.get(key, 0) + 1
    for c in classes:
        key = "%s:%s" % (c.__module__, c.__qualname__)
        if count[key] > 1:
            # several classes share this name (made by a metaclass or a factory): one entry for
            # the group, from the sorted digests of their attributes
            h = strict.digest(sorted((k, strict.digest(v)) for k, v in list(vars(c).items())
                                     if k not in _SKIP_CLASS_ATTRS))
            fp.setdefault(("c", key, "<dup>"), []).append(h)
            continue
        fp[("c", key, "<bases>")] = strict.digest(tuple(c.__mro__))
        for k, v in list(vars(c).items()):
            if k in _SKIP_CLASS_ATTRS:
                continue
            fp[("c", key, k)] = pr.digest(v, ("c", key, k))
    for k, v in fp.items():
        if isinstance(v, list):
            fp[k] = b"".join(sorted(v))
    return fp


def changed_roots(base, now):
    out = set()
    for k, d in now.items():
        if base.get(k) != d:
            out.add(k)
    for k in base:
        if k not in now:
            out.add(k)
    return out

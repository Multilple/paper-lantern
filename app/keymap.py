"""Which tests look up WHICH KEY of a class-level table (maps/keys/, built on the pinned commit by
tools/trace_keys.py).

sqlglot is table driven: TRANSFORMS, KEYWORDS, FUNCTIONS, TYPE_MAPPING ... are dicts in class
bodies, and nearly every test reads each of them. When a patch changes one entry, "the tests that
read the attribute" is nearly everything; "the tests that looked up that key, or walked the whole
table" is what can really be affected.

The trace replaces every such dict (exact dict, owned by one class, referenced by nothing else) by a
dict subclass that notes, per test, the keys asked for (found or not) and whether the table was used
as a whole (iterated, copied, merged, compared, measured, written to). A test that never asked for
a changed key and never used the table as a whole gets from the patched table exactly what it got
from the pinned one.
"""
import enum
import json
import os
import pickle
import zlib

WHOLE = 0   # in a trace record: the table was used as a whole


def keyname(k, depth=0):
    """A name for a dict key that is the same in every process, or None when there is none.
    Keys that are equal as dict keys get the same name (1, 1.0 and True; a str Enum and its value)."""
    t = type(k)
    if t is str:
        return "s" + k
    if k is None:
        return "pNone"
    if t is bool or t is int:
        return "p%d" % int(k)
    if t is float:
        if k == k and abs(k) < 1e15 and k == int(k):
            return "p%d" % int(k)
        return "p" + repr(k)
    if t is bytes:
        return "b" + repr(k)
    if isinstance(k, type):
        m = type(k)
        if m.__eq__ is not object.__eq__ or m.__hash__ is not object.__hash__:
            return None        # classes that compare equal to other things (dialects equal their names)
        return "T%s:%s" % (k.__module__, k.__qualname__)
    if isinstance(k, enum.Enum):
        if isinstance(k, str):
            return "s" + str.__str__(k)
        if isinstance(k, int):
            return "p%d" % int(k)
        if t.__eq__ is not object.__eq__ or t.__hash__ is not enum.Enum.__hash__:
            return None
        return "E%s:%s.%s" % (t.__module__, t.__qualname__, k.name)
    if t is tuple and depth < 3:
        parts = []
        for x in k:
            p = keyname(x, depth + 1)
            if p is None:
                return None
            parts.append(p)
        return "U" + "\x1f".join(parts)
    return None


def names_of(d):
    """{name: key} for a dict whose keys all have distinct names, else None."""
    out = {}
    for k in d:
        n = keyname(k)
        if n is None or n in out:
            return None
        out[n] = k
    return out


_index = {}
_tags = {}


def index(maps_dir):
    """-> {(class name, attr): file number} of the tables the trace vouches for"""
    ix = _index.get(maps_dir)
    if ix is None:
        try:
            with open(os.path.join(maps_dir, "keys", "index.json")) as f:
                raw = json.load(f)
            ix = {(c, a): n for c, a, n in raw["tags"]}
        except Exception:
            ix = {}
        _index[maps_dir] = ix
    return ix


def _load(maps_dir, n):
    k = (maps_dir, n)
    d = _tags.get(k)
    if d is None:
        with open(os.path.join(maps_dir, "keys", "%d.bin" % n), "rb") as f:
            d = pickle.loads(zlib.decompress(f.read()))
        _tags[k] = d
    return d


def narrow(roots, base_fp, fp, maps_dir):
    """roots: statefp.changed_roots(base_fp, fp).
    -> (bitmap of the tests that can see the changed entries of the tables this covers,
        the roots that are left for the other maps)."""
    ix = index(maps_dir)
    acc = 0
    rest = set()
    for r in roots:
        if r[0] == "k":
            continue
        if r[0] != "c":
            rest.add(r)
            continue
        n = ix.get((r[1], r[2]))
        kk = ("k", r[1], r[2])
        old, new = base_fp.get(kk), fp.get(kk)
        if n is None or old is None or new is None:
            rest.add(r)
            continue
        try:
            d = _load(maps_dir, n)
        except Exception:
            rest.add(r)
            continue
        acc |= int.from_bytes(d["whole"], "little")
        keys = d["keys"]
        for name in set(old) | set(new):
            if old.get(name) != new.get(name):
                b = keys.get(name)
                if b:
                    acc |= int.from_bytes(b, "little")
    return acc, rest

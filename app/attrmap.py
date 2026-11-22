"""Which tests READ a class attribute (maps/attrs.bin, built on the pinned commit by
tools/trace_attrs.py). Narrows the candidates of a changed class attribute from "every test
that handles an object of the class" to "every test that reads that attribute"."""
import os
import pickle
import zlib

_loaded = {}


def load(path):
    m = _loaded.get(path)
    if m is None:
        m = pickle.loads(zlib.decompress(open(path, "rb").read()))
        m["cid"] = {c: i for i, c in enumerate(m["classes"])}
        m["aid"] = {a: i for i, a in enumerate(m["attrs"])}
        m["unsafe"] = int.from_bytes(m["unsafe"], "little")
        m["_int"] = {}
        m["_by_class"] = None
        _loaded[path] = m
    return m


def _is_data_descriptor(v):
    t = type(v)
    return hasattr(t, "__set__") or hasattr(t, "__delete__")


def narrow(roots, amap, lookup):
    """roots: fingerprint keys (statefp.changed_roots). amap: load(...).
    lookup(class_name) -> the live class of the patched tree, or None.
    -> (bitmap of the tests that read a covered root, set of the roots that are NOT covered).
    The caller maps the uncovered roots the old, broad way.

    A root ("c", C, attr) is covered when the recorders of the pinned trace saw every way a
    test can read C.attr:
      1. C is a class of the pinned tree with a unique name, wrapped by the tracer
         (a sqlglot class; not an Enum, namedtuple, TypedDict, Protocol; not a test class);
      2. attr carried a recorder in C's own dict, or C inherits attr from a recorded PLAIN
         value (then the lookup class of every read was recorded, so a new C.attr is covered).
         Dunders, <bases>, <dup>, slot and getset descriptors, names that no class in C's
         MRO had: not covered;
      3. no test module read the attribute while it was being imported;
      4. the patched value is not a data descriptor unless the pinned one was a property
         (a data descriptor wins over instance attributes: reads the recorder never saw).
    """
    acc = 0
    rest = set()
    cid, aid, own, inh = amap["cid"], amap["aid"], amap["own"], amap["inh"]
    inh_table, key, table, ints = amap["inh_table"], amap["key"], amap["table"], amap["_int"]
    nocover, NA = amap["nocover"], len(amap["attrs"])
    used = False
    for k in roots:
        if k[0] != "c":
            rest.add(k)
            continue
        c, a = cid.get(k[1]), aid.get(k[2])
        if c is None or a is None:
            rest.add(k)
            continue
        kind = own.get(c, {}).get(a)
        if kind is None:
            j = inh.get(c)
            if j is None or a not in inh_table[j]:
                rest.add(k)
                continue
            kind = "d"
        kk = c * NA + a
        if kk in nocover:
            rest.add(k)
            continue
        live = lookup(k[1])
        if live is None:
            rest.add(k)
            continue
        try:
            v = live.__dict__[k[2]]
        except KeyError:
            v = None
        else:
            if kind != "p" and _is_data_descriptor(v):
                rest.add(k)
                continue
        used = True
        j = key.get(kk)
        if j is not None:
            b = ints.get(j)
            if b is None:
                b = ints[j] = int.from_bytes(table[j], "little")
            acc |= b
    if used:
        acc |= amap["unsafe"]
    return acc, rest


def class_readers(amap, cname):
    """Bitmap of the tests that read ANY recorded attribute through the class or one of its
    subclasses. For a root that stays on the broad rule: maps/types.json only knows classes
    whose objects are passed as arguments, not classes reached as `dialect.tokenizer_class.X`."""
    c = amap["cid"].get(cname)
    if c is None:
        return 0
    by = amap["_by_class"]
    if by is None:
        by = amap["_by_class"] = {}
        NA = len(amap["attrs"])
        for kk, j in amap["key"].items():
            by.setdefault(kk // NA, set()).add(j)
    acc = 0
    table, ints = amap["table"], amap["_int"]
    for x in amap.get("desc", {}).get(c, (c,)):
        for j in by.get(x, ()):
            b = ints.get(j)
            if b is None:
                b = ints[j] = int.from_bytes(table[j], "little")
            acc |= b
    return acc | amap["unsafe"]

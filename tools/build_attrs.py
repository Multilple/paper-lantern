"""Packs the traces of trace_attrs.py into maps/attrs.bin.
usage: build_attrs.py tests.json attrs.bin trace.jsonl...

attrs.bin = zlib(pickle(dict)):
  classes   [class name]                      "module:QualName", pinned commit, unique names only
  attrs     [attribute name]
  own       {class id: {attr id: kind}}       entries that carried a recorder: d plain value, f function /
                                              classmethod / staticmethod / cached_property, p property
  inh       {class id: index into inh_table}  names the class inherits from a recorded plain value
  inh_table [sorted attr ids]
  nocover   set of keys read while the test modules were imported (never covered)
  desc      {class id: [ids of the classes that inherit from it, itself included]}  (only when it has subclasses)
  key       {class id * len(attrs) + attr id: index into table}   tests that read the key
  table     [bitmap bytes, little endian, bit = test index of maps/index.json]
  unsafe    bitmap of tests the recorders could not follow (failed or died under tracing): they
            are added to every narrow answer
"""
import json, os, pickle, sys, zlib


def main(args):
    tests_json, out = args[0], args[1]
    ids = [t["id"] for t in json.load(open(tests_json))["tests"]]
    idx = {t: i for i, t in enumerate(ids)}
    n = len(ids)
    nbytes = (n + 7) // 8
    meta = None
    keys = {}
    unsafe = 0
    seen = set()
    raw = {}
    for tf in args[2:]:
        for line in open(tf):
            r = json.loads(line)
            if "meta" in r:
                if meta is None:
                    meta = r
                else:
                    assert meta["classes"] == r["classes"] and meta["attrs"] == r["attrs"]
                    for k, v in r.items():
                        meta.setdefault(k, v)
                continue
            i = idx[r["id"]]
            seen.add(i)
            if "died" in r or r.get("bad"):
                unsafe |= 1 << i
                continue
            if r.get("raw"):
                raw[r["id"]] = r["raw"]
            bit = 1 << i
            for k in r["k"]:
                keys[k] = keys.get(k, 0) | bit
    for i in range(n):
        if i not in seen:
            unsafe |= 1 << i
    NA = len(meta["attrs"])
    uniq = {}
    key = {}
    for k, b in keys.items():
        key[k] = uniq.setdefault(b, len(uniq))
    table = [None] * len(uniq)
    for b, j in uniq.items():
        table[j] = b.to_bytes(nbytes, "little")
    aid = {a: i for i, a in enumerate(meta["attrs"])}
    own = {int(c): {aid[k]: kd for k, kd in d.items()} for c, d in meta["own"].items()}
    it = {}
    inh = {}
    for c, lst in meta["inh"].items():
        inh[int(c)] = it.setdefault(tuple(lst), len(it))
    inh_table = [None] * len(it)
    for lst, j in it.items():
        inh_table[j] = list(lst)
    nocover = set()
    for f, ks in meta["import_reads"].items():
        nocover.update(ks)
    blob = zlib.compress(pickle.dumps({
        "classes": meta["classes"], "attrs": meta["attrs"], "own": own, "inh": inh,
        "inh_table": inh_table, "nocover": nocover, "key": key, "table": table,
        "desc": {int(c): v for c, v in meta.get("desc", {}).items() if v != [int(c)]},
        "unsafe": unsafe.to_bytes(nbytes, "little"), "ntests": n}, protocol=4), 9)
    open(out, "wb").write(blob)
    print("tests", n, "traced", len(seen), "unsafe", [ids[i] for i in range(n) if unsafe >> i & 1],
          "keys read", len(key), "distinct sets", len(table), "raw users", len(raw),
          "bytes", len(blob), file=sys.stderr)
    for t, r in list(raw.items())[:20]:
        print("raw", t, r[:5], file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])

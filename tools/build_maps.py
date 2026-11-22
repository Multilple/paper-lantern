"""Packs the traces of the pinned commit into the map files that the selector reads.
usage: build_maps.py tests.json import.json maps_dir trace.jsonl... [--types types.jsonl...]"""
import json, os, pickle, sys, zlib


def main():
    args = sys.argv[1:]
    opts = {"--types": [], "--timing": [], "--ref": [], "--bare": []}
    cur = None
    plain = []
    for a in args:
        if a in opts:
            cur = a
        elif cur:
            opts[cur].append(a)
        else:
            plain.append(a)
    args = plain
    types_files = opts["--types"]
    tests_json, import_json, out = args[0], args[1], args[2]
    traces = args[3:]
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    idx = {t: i for i, t in enumerate(ids)}
    n = len(ids)
    nbytes = (n + 7) // 8
    cpu = [None] * n
    per_file = {}   # file -> line -> int bitmap
    opened = {}
    bad = []
    for tf in traces:
        for line in open(tf):
            r = json.loads(line)
            i = idx[r["id"]]
            if "died" in r or r.get("bad"):
                bad.append(r["id"])
                continue
            cpu[i] = r["cpu"]
            bit = 1 << i
            for f, ls in r.get("lines", {}).items():
                d = per_file.setdefault(f, {})
                for l in ls:
                    d[l] = d.get(l, 0) | bit
            for p in r.get("opened", []):
                opened.setdefault(p, []).append(i)
    missing = [ids[i] for i in range(n) if cpu[i] is None]
    print("tests", n, "bad", bad, "missing", len(missing), file=sys.stderr)
    os.makedirs(os.path.join(out, "lines"), exist_ok=True)
    files = sorted(per_file)
    total = 0
    for k, f in enumerate(files):
        d = per_file[f]
        uniq = {}
        lines = {}
        anyb = 0
        for l, b in d.items():
            anyb |= b
            lines[l] = uniq.setdefault(b, len(uniq))
        table = [None] * len(uniq)
        for b, j in uniq.items():
            table[j] = b.to_bytes(nbytes, "little")
        blob = zlib.compress(pickle.dumps({"lines": lines, "table": table,
                                           "any": anyb.to_bytes(nbytes, "little")}, protocol=4), 9)
        total += len(blob)
        open(os.path.join(out, "lines", "%d.bin" % k), "wb").write(blob)
    root = os.environ["BR_ROOT"].rstrip("/") + "/"
    op = {}
    for p, ts in opened.items():
        isdir = p.startswith("DIR:")
        q = p[4:] if isdir else p
        q = os.path.abspath(q)
        if not q.startswith(root) and q + "/" != root:
            continue
        rel = q[len(root):]
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        op[("DIR:" if isdir else "") + rel] = sorted(set(ts))
    imp = json.load(open(import_json))
    types = {}
    for tf in types_files:
        for line in open(tf):
            r = json.loads(line)
            for c in r.get("types", []):
                types.setdefault(c, []).append(idx[r["id"]])
    setup = {}
    for tf in opts["--timing"]:
        for line in open(tf):
            r = json.loads(line)
            if "cpu" in r:
                cpu[idx[r["id"]]] = r["cpu"]
                f, c, _ = r["id"].split("::")
                setup.setdefault(f + "::" + c, []).append(r.get("setup", 0.0))
    setup = {k: round(sorted(v)[len(v) // 2], 4) for k, v in setup.items()}
    setup = {k: v for k, v in setup.items() if v >= 0.002}
    ref = float(opts["--ref"][0]) if opts["--ref"] else None
    json.dump({"tests": ids, "cpu": cpu, "setup": setup, "ref_import_cpu": ref,
               "files": {f: k for k, f in enumerate(files)},
               "untraced": sorted(set(missing) | set(bad))}, open(os.path.join(out, "index.json"), "w"))
    json.dump(op, open(os.path.join(out, "opened.json"), "w"))
    json.dump(types, open(os.path.join(out, "types.json"), "w"))
    bare = json.load(open(opts["--bare"][0])) if opts["--bare"] else []
    json.dump({"lines": imp["lines"], "called": imp["called"], "bare": bare,
               "opened": [o for o in imp["opened"] if not o.endswith((".py", ".pyc")) and not o.startswith("DIR:")]},
              open(os.path.join(out, "import.json"), "w"))
    print("line maps", len(files), "files", total, "bytes; opened", len(op), "types", len(types), file=sys.stderr)


if __name__ == "__main__":
    main()

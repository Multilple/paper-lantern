"""Operand kinds at the comparison sites of sqlglot, test by test, on the pinned commit.

    trace_cmp_tests.py prepare REPO TREE
    trace_cmp_tests.py run TREE TESTS.json OUT.jsonl PART NPARTS
    trace_cmp_tests.py build TESTS.json OUT.json TRACE.jsonl [TRACE.jsonl ...]

prepare copies REPO to TREE and rewrites every comparison in sqlglot/ exactly as trace_cmp.py does (the
recorder notes the kinds of both operands, and for plain values what the six comparisons say).
run imports the rewritten tree with our runner (app/core.py), notes what the import itself evaluated,
then runs every test alone in a fork and writes what that test evaluated:
    {"import": {site: [[kind_a, kind_b, bits], ...]}}
    {"id": test, "bad": bool, "s": {site: [[kind_a, kind_b, bits], ...]}}
build merges traces (several hash seeds) into app/maps/cmp_tests.json:
    {"import": {site: [...]}, "sites": {site: [[kind_a, kind_b, bits, hex bitmap of tests], ...]},
     "untraced": hex bitmap}
with tests numbered as in the maps. app/cmpeq.narrow uses it: a test that never evaluated a swapped
comparison on operands that tell the two operators apart does what it did on the pinned commit.
"""
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def prepare(repo, tree):
    import trace_cmp
    if os.path.exists(tree):
        shutil.rmtree(tree)
    shutil.copytree(repo, tree, symlinks=True,
                    ignore=lambda d, names: [n for n in names if n in (".git", "__pycache__", ".pytest_cache")])
    n = 0
    for p in sorted(glob.glob(os.path.join(tree, "sqlglot", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(p, tree)
        src = open(p, encoding="utf-8").read()
        new = trace_cmp.rewrite(src, rel)
        if new is not None:
            open(p, "w", encoding="utf-8").write(new)
            n += 1
    open(os.path.join(tree, "_br_rec.py"), "w").write(trace_cmp.RECORDER)
    print("rewritten files:", n, file=sys.stderr)


def run(tree, tests_json, out_path, part, nparts):
    os.environ["BR_ROOT"] = tree
    os.chdir(tree)
    sys.path.insert(0, os.path.join(HERE, "..", "app"))
    import core
    tests = json.load(open(tests_json))["tests"]
    files = sorted({t["file"] for t in tests})
    warm = core.Warm(files)
    assert not warm.import_errors, warm.import_errors
    rec = sys.modules["_br_rec"]
    out = open(out_path, "w")
    out.write(json.dumps({"import": {k: sorted(v) for k, v in rec._seen.items()}}) + "\n")
    out.flush()
    import gc
    gc.collect()
    gc.freeze()
    for t in tests:
        if t["number"] % nparts != part:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            rec._seen.clear()
            try:
                bad = core.run_test(t["id"], warm.modules)
            except BaseException:
                bad = True
            data = json.dumps({"id": t["id"], "bad": bool(bad),
                               "s": {k: sorted(v) for k, v in rec._seen.items()}}).encode()
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
        out.write(buf.decode() + "\n")
        out.flush()


def build(tests_json, out_path, traces):
    sys.path.insert(0, os.path.join(HERE, "..", "app"))
    import maps as mapslib
    m = mapslib.Maps()
    imp = {}
    sites = {}
    seen_ok = {}          # test index -> number of traces that hold it
    for tr in traces:
        for line in open(tr):
            d = json.loads(line)
            if "import" in d:
                for site, obs in d["import"].items():
                    imp.setdefault(site, set()).update(tuple(o) for o in obs)
                continue
            i = m.index_of(d["id"])
            if i is None or d.get("died") is not None or d.get("bad"):
                continue
            seen_ok[i] = seen_ok.get(i, 0) + 1
            for site, obs in d["s"].items():
                s = sites.setdefault(site, {})
                for o in obs:
                    s[tuple(o)] = s.get(tuple(o), 0) | (1 << i)
    untraced = 0
    for i in range(len(m.tests)):
        if seen_ok.get(i, 0) != len(traces):
            untraced |= 1 << i
    res = {"import": {k: sorted(v) for k, v in sorted(imp.items())},
           "sites": {k: [list(o) + ["%x" % bm] for o, bm in sorted(v.items())] for k, v in sorted(sites.items())},
           "untraced": "%x" % untraced, "n": len(m.tests)}
    json.dump(res, open(out_path, "w"), separators=(",", ":"))
    print("sites", len(sites), "import sites", len(imp), "tests not traced in every run", bin(untraced).count("1"),
          file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "run":
        run(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), int(sys.argv[6]))
    else:
        build(sys.argv[2], sys.argv[3], sys.argv[4:])

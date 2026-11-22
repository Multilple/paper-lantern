"""Builds maps/keys/ from the records of tools/trace_keys.py.

    build_keys.py tests.json out_dir trace0.jsonl [trace1.jsonl ...]

out_dir/index.json  {"tags": [[kind, owner, name, n], ...]}   the holders ("c" class attribute, "g" module
                    global) of the tables the trace vouches for; holders of one dict object share n
out_dir/<n>.bin     zlib(pickle({"whole": bitmap, "len": bitmap, "bool": bitmap, "keys": {key name: bitmap}}))
bitmaps are little-endian bytes over the map's test order (maps/index.json "tests").
"""
import json
import os
import pickle
import sys
import zlib

LEN = "\x00len"
BOOL = "\x00bool"


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out_dir = sys.argv[2]
    order = json.load(open(os.path.join(os.path.dirname(out_dir.rstrip("/")), "index.json")))["tests"]
    pos = {tid: i for i, tid in enumerate(order)}
    tags = unsafe = imported = None
    recs = []
    for p in sys.argv[3:]:
        with open(p) as f:
            head = json.loads(f.readline())
            if tags is None:
                tags, unsafe, imported = head["tags"], head["unsafe"], head.get("imported", {})
            else:
                # the tables are found in the same order whatever the seed; a table that is unsafe
                # or read at import in any trace is so in the map
                assert [sorted(map(tuple, t)) for t in tags] == [sorted(map(tuple, t)) for t in head["tags"]], \
                    "traces of different table sets"
                unsafe.update(head["unsafe"])
                for i, fs in head.get("imported", {}).items():
                    imported[i] = sorted(set(imported.get(i, [])) | set(fs))
            for line in f:
                recs.append(json.loads(line))
    # several traces of one test (other hash seeds: the order a set is walked in can steer which
    # keys are asked for) are merged: a test asked for a key if it did in any of them
    seen = {}
    for r in recs:
        seen[r["id"]] = seen.get(r["id"], 0) + 1
    assert set(seen) == {t["id"] for t in tests} and len(set(seen.values())) == 1, "incomplete traces"
    bad = [r["id"] for r in recs if r.get("bad") or "died" in r]
    assert not bad, bad[:5]
    whole = [0] * len(tags)
    lens = [0] * len(tags)
    bools = [0] * len(tags)
    keys = [dict() for _ in tags]
    # a table read while a test file was imported: all the tests of that file use it as a whole
    for i, fs in imported.items():
        fs = set(fs)
        for tid, n in pos.items():
            if tid.split("::", 1)[0] in fs:
                whole[int(i)] |= 1 << n
    for r in recs:
        bit = 1 << pos[r["id"]]
        for i, v in r["k"].items():
            i = int(i)
            if v == 0:
                whole[i] |= bit
                continue
            for name in v:
                if name == LEN:
                    lens[i] |= bit
                elif name == BOOL:
                    bools[i] |= bit
                else:
                    keys[i][name] = keys[i].get(name, 0) | bit
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, f))
    nbytes = (len(order) + 7) // 8

    def b(x):
        return x.to_bytes(nbytes, "little").rstrip(b"\x00")
    index, total = [], 0
    for i, holders in enumerate(tags):
        if str(i) in unsafe:
            continue
        blob = zlib.compress(pickle.dumps({"whole": b(whole[i]), "len": b(lens[i]), "bool": b(bools[i]),
                                           "keys": {k: b(v) for k, v in keys[i].items()}}, protocol=4), 9)
        with open(os.path.join(out_dir, "%d.bin" % i), "wb") as f:
            f.write(blob)
        total += len(blob)
        for kind, owner, attr in holders:
            index.append([kind, owner, attr, i])
    json.dump({"tags": index}, open(os.path.join(out_dir, "index.json"), "w"), separators=(",", ":"))
    print("tables", len(index), "of", len(tags), "bytes", total)


if __name__ == "__main__":
    main()

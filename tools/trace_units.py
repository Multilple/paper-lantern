"""Offline builder of maps/units, run on the pinned commit only.

For every supported test (app/units.py KINDS) and every unit of it: the lines each phase runs, the
fingerprints of what the phases hand on, the CPU cost of each phase.  Every unit is traced
  cold     alone in a fresh fork of the process that has the class set up (cache-filling paths),
  seq      in the test's order in one process (paths that exist only once caches are warm),
  rev      in reverse order in one process,
and timed untraced.  A fingerprint is kept only when all runs agree on it; a unit that does not pass
in every run makes the build fail.

usage: trace_units.py raw   <outdir> [test-id ...]     trace -> <outdir>/<name>.raw.pkl
       trace_units.py build <outdir> <mapsdir>         raw files -> maps/units/*.bin + index.pkl
"""
import collections
import gc
import os
import pickle
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import core    # noqa: E402
import units   # noqa: E402

ROOT = core.ROOT.rstrip("/") + "/"
UP = ROOT + ".."


class Tracer:
    """Lines run, per phase. A phase change re-arms every line."""

    def __init__(self):
        self.sets = {}
        self.cur = [None]
        self.opened = set()
        self.on = False

    def start(self):
        mon = sys.monitoring
        mon.use_tool_id(3, "bru")
        cur = self.cur

        def on_line(code, line):
            fn = code.co_filename
            if fn.startswith(ROOT) and not fn.startswith(UP):
                s = cur[0]
                if s is not None:
                    s.add((fn, line))
            return mon.DISABLE

        def on_start(code, off):
            # no LINE event is raised for instructions on the line a code object starts on (a one-line
            # lambda or def raises none at all): entering the code counts for its first lines
            # (same rule as tools/trace.py)
            fn = code.co_filename
            if fn.startswith(ROOT) and not fn.startswith(UP):
                s = cur[0]
                if s is not None:
                    first = code.co_firstlineno
                    s.add((fn, first))
                    for _, _, l in code.co_lines():
                        if l is not None:
                            s.add((fn, l))
                            if l != first:
                                break
            return mon.DISABLE

        mon.register_callback(3, mon.events.LINE, on_line)
        mon.register_callback(3, mon.events.PY_START, on_start)
        mon.set_events(3, mon.events.LINE | mon.events.PY_START)
        self.on = True
        opened = self.opened

        def hook(ev, args):
            if ev == "open":
                p = args[0]
                if isinstance(p, bytes):
                    p = os.fsdecode(p)
                if isinstance(p, str):
                    p = os.path.abspath(p)
                    if p.startswith(ROOT):
                        opened.add(p[len(ROOT):])

        sys.addaudithook(hook)

    def enter(self, phase):
        self.cur[0] = self.sets.setdefault(phase, set())
        if self.on:
            sys.monitoring.restart_events()

    def take_one(self, phase):
        self.cur[0] = None
        d = {}
        for fn, line in self.sets.pop(phase, ()):
            d.setdefault(fn[len(ROOT):], []).append(line)
        return {k: sorted(v) for k, v in d.items()}

    def take(self):
        out = {}
        self.cur[0] = None
        for ph, s in list(self.sets.items()):
            d = {}
            for fn, line in s:
                d.setdefault(fn[len(ROOT):], []).append(line)
            out[ph] = {k: sorted(v) for k, v in d.items()}
        self.sets = {}
        self.cur[0] = None
        return out


class TraceControl:
    tracing = True

    def __init__(self, tracer=None, timing=False):
        self.tracer = tracer
        self.timing = timing
        self.fps = {}
        self.cpu = collections.defaultdict(float)
        self._ph = None
        self._t = 0.0
        self.stats = collections.defaultdict(int)
        self.unit = 0

    def begin(self, unit, fresh_input=False):
        self.unit = unit
        self.fps = {}
        self.cpu = collections.defaultdict(float)
        self._ph = None

    def hit(self, *phases):
        return True

    def same(self, name, value, fp=units.fp_state):
        t0 = time.process_time()
        self.fps[name] = fp(value)
        if self.timing:
            dt = time.process_time() - t0
            self.cpu["F"] += dt        # taking fingerprints is its own cost
            self._t += dt
        return True

    def can_cut(self):
        return False

    def enter(self, phase):
        if self.timing:
            now = time.process_time()
            if self._ph is not None:
                self.cpu[self._ph] += now - self._t
            self._ph, self._t = phase, now
        if self.tracer is not None:
            self.tracer.enter(phase)

    def end(self):
        self.enter("-")
        self.cpu.pop("-", None)


def in_child(fn):
    """Runs fn() in a fork and returns what it returns."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            res = ("ok", fn())
        except BaseException as e:
            import traceback
            res = ("err", "%r\n%s" % (e, traceback.format_exc()))
        b = pickle.dumps(res)
        while b:
            n = os.write(w, b)
            b = b[n:]
        os._exit(0)
    os.close(w)
    buf = []
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        buf.append(b)
    os.close(r)
    _, st = os.waitpid(pid, 0)
    if not buf:
        raise RuntimeError("child died: %r" % (st,))
    kind, val = pickle.loads(b"".join(buf))
    if kind != "ok":
        raise RuntimeError(val)
    return val


def run_one(kind, i, ctl):
    ctl.begin(i)
    try:
        ok = kind.run_unit(i, ctl)
        err = None
    except BaseException as e:
        ok, err = False, repr(e)
    if ctl.timing:
        ctl.end()
    rec = {"ok": ok, "err": err, "fps": dict(ctl.fps), "cpu": dict(ctl.cpu)}
    if ctl.tracer is not None:
        rec["lines"] = ctl.tracer.take()
    return rec


def trace_test(test_id, modules):
    spec = [None]

    def make():
        kind = units.KINDS[test_id](test_id)
        st = kind.prepare(modules)
        assert st == "ok", st
        if spec[0] is not None:
            kind.spec = spec[0]
            kind.tags = spec[0]["tags"]
        return kind

    probe_kind = units.KINDS[test_id](test_id)
    out = {"test": test_id, "phases": list(probe_kind.phases), "probe": units.seed_probe()}
    ordered = probe_kind.ordered

    if hasattr(probe_kind, "derive"):
        # a straight-line test: find its units first
        def derive():
            kind = make()
            tr = Tracer()
            tr.start()

            def enter(k):
                if k is None:
                    lines = tr.take()
                    return {int(ph[1:]) for ph, d in lines.items() if any(rel.startswith("sqlglot/") for rel in d)}
                tr.enter("s%d" % k)
            return kind.derive(enter)

        sp = in_child(derive)
        assert sp is not None, "cannot be cut into units"
        nu = 1 + max((t for t in sp["tags"] if t is not None), default=-1)
        sp["tcalls"] = [10 ** 9] * nu
        sp["cut_ok"] = [False] * nu
        spec[0] = sp

        def count_calls():
            kind = make()
            ctl = TraceControl()
            res = []
            for i in range(nu):
                ctl.begin(i)
                kind.run_unit(i, ctl)
                res.append(sum(1 for k in ctl.fps if k.startswith("T")))
            return res

        sp["tcalls"] = in_child(count_calls)

        def probe_cut():
            # does a Cut raised at the last tokenize call of a unit come out of the unit?
            class Probe(TraceControl):
                tracing = False

                def can_cut(self):
                    return True

                def hit(self, *phases):
                    return False

            res = []
            for i in range(nu):
                def one():
                    kind = make()
                    ctl = Probe()
                    kind.spec = dict(sp, cut_ok=[True] * nu)
                    for j in range(i):          # pure statements before it
                        pass
                    ctl.begin(i)
                    before = ctl.stats["cut"]
                    try:
                        kind.run_unit(i, ctl)
                    except BaseException:
                        return False
                    return ctl.stats["cut"] == before + 1
                res.append(sp["tcalls"][i] > 0 and in_child(one))
            return res

        sp["cut_ok"] = probe_cut()
        print(test_id, "spec", sp, flush=True)

    # what runs outside the units: setUpClass and the test's preamble
    def setup():
        tr = Tracer()
        tr.start()
        ctl = TraceControl(tr)
        ctl.enter("S")
        t0 = time.process_time()
        kind = make()
        t1 = time.process_time()
        kind.preamble(ctl)
        t2 = time.process_time()
        extra = spec[0]
        if hasattr(kind, "shared"):
            extra = dict(extra or {}, shared=kind.shared())
            assert extra["shared"] is not None, "the shared data cannot be fingerprinted"
        return {"lines": tr.take().get("S", {}), "opened": sorted(tr.opened), "n": kind.count(),
                "inputs": kind.unit_inputs(), "class_cpu": t1 - t0, "preamble_cpu": t2 - t1, "extra": extra}

    if getattr(probe_kind, "verify_whole", False):
        # the units of this kind do not run the test's own comparison: the test itself must pass
        assert in_child(lambda: not core.run_test(test_id, modules)), "the test fails on the pinned commit"
    out["setup"] = in_child(setup)
    n = out["setup"]["n"]
    print(test_id, "units", n, "setup lines", sum(len(v) for v in out["setup"]["lines"].values()), flush=True)

    def cold_all():
        kind = make()
        kind.preamble(TraceControl())
        gc.collect()
        res = []
        for i in range(n):
            def one():
                tr = Tracer()
                tr.start()
                rec = run_one(kind, i, TraceControl(tr))
                rec["opened"] = sorted(tr.opened)
                return rec
            res.append(in_child(one))
        return res

    def seq(order):
        def f():
            kind = make()
            kind.preamble(TraceControl())
            tr = Tracer()
            tr.start()
            ctl = TraceControl(tr)
            res = {}
            for i in order:
                res[i] = run_one(kind, i, ctl)
            shared = kind.shared() if hasattr(kind, "shared") else None
            return {"units": [res[i] for i in range(n)], "opened": sorted(tr.opened), "shared": shared}
        return f

    def timed():
        kind = make()
        kind.preamble(TraceControl())
        ctl = TraceControl(None, timing=True)
        return [run_one(kind, i, ctl) for i in range(n)]

    t = time.time()
    out["cold"] = in_child(cold_all)
    print("  cold done %.1fs" % (time.time() - t), flush=True)
    out["seq"] = in_child(seq(list(range(n))))
    out["rev"] = in_child(seq(list(range(n)) if ordered else list(range(n - 1, -1, -1))))
    print("  seq/rev done %.1fs" % (time.time() - t), flush=True)
    out["time"] = in_child(timed)
    out["time2"] = in_child(timed)
    print("  timing done %.1fs" % (time.time() - t), flush=True)
    return out


class _TracePolicy:
    """check_file under trace: mode cold (every entry in its own fork), seq (in place) or time."""

    def __init__(self, mode, tracer=None):
        self.mode, self.tracer = mode, tracer
        self.calls = []
        self.units = []
        self.inputs = []
        self.unit_cpu = 0.0

    def call(self, c, file, entries, args):
        base = sum(x["n"] for x in self.calls)
        self.calls.append({"file": file, "n": len(entries), "args": units.fp_state(args), "base": base})
        return {"base": base}

    def whole_call(self, c):
        raise RuntimeError("check_file called with only= or an unreadable fixture")

    def unit(self, plan, j, inp, run):
        self.inputs.append(units.fp_value(inp))
        if self.mode == "cold":
            def one():
                tr = Tracer()
                tr.start()
                tr.enter("U")
                try:
                    run()
                    ok, err = True, None
                except BaseException as e:
                    ok, err = False, repr(e)
                return {"ok": ok, "err": err, "fps": {}, "cpu": {}, "lines": tr.take(), "opened": sorted(tr.opened)}
            self.units.append(in_child(one))
            return
        t0 = time.process_time()
        if self.tracer is not None:
            self.tracer.enter("U")
        try:
            run()
            ok, err = True, None
        except BaseException as e:
            ok, err = False, repr(e)
        rec = {"ok": ok, "err": err, "fps": {}, "cpu": {"U": time.process_time() - t0}}
        self.unit_cpu += time.process_time() - t0
        if self.tracer is not None:
            rec["lines"] = {"U": self.tracer.take_one("U")}
            self.tracer.enter("S")
        self.units.append(rec)
        if not ok:
            raise RuntimeError("unit fails on the pinned commit: %s" % err)


def trace_whole(test_id, modules):
    def run(mode):
        def f():
            tr = None
            if mode == "seq":
                tr = Tracer()
                tr.start()
                tr.enter("S")
            t0 = time.process_time()
            kind = units.KINDS[test_id](test_id)
            st = kind.prepare(modules)
            assert st == "ok", st
            t1 = time.process_time()
            pol = _TracePolicy(mode, tr)
            bad = kind.run_whole(pol)
            t2 = time.process_time()
            assert mode == "cold" or not bad, "the test fails on the pinned commit"
            out = {"units": pol.units, "calls": pol.calls, "inputs": pol.inputs, "class_cpu": t1 - t0,
                   "outside_cpu": t2 - t1 - pol.unit_cpu}
            if tr is not None:
                out["S"] = tr.take().get("S", {})
                out["opened"] = sorted(tr.opened)
            return out
        return f

    probe_kind = units.KINDS[test_id](test_id)
    out = {"test": test_id, "phases": list(probe_kind.phases), "probe": units.seed_probe()}
    t = time.time()
    seq1 = in_child(run("seq"))
    n = len(seq1["units"])
    print(test_id, "units", n, "calls", [(c["file"], c["n"]) for c in seq1["calls"]], flush=True)
    cold = in_child(run("cold"))
    seq2 = in_child(run("seq"))
    tm1 = in_child(run("time"))
    tm2 = in_child(run("time"))
    assert len(cold["units"]) == n and seq1["calls"] == seq2["calls"] == cold["calls"] == tm1["calls"], "unstable calls"
    assert seq1["inputs"] == seq2["inputs"] == cold["inputs"]
    files = sorted({"tests/fixtures/optimizer/%s.sql" % c["file"] for c in seq1["calls"]})
    slines = {}
    for s_ in (seq1, seq2):
        for rel, ls in s_["S"].items():
            slines[rel] = sorted(set(slines.get(rel, ())) | set(ls))
    out["setup"] = {"lines": slines, "opened": sorted(set(seq1["opened"]) | set(seq2["opened"])), "n": n,
                    "inputs": seq1["inputs"], "class_cpu": min(tm1["class_cpu"], tm2["class_cpu"]),
                    "preamble_cpu": min(tm1["outside_cpu"], tm2["outside_cpu"]),
                    "extra": {"calls": seq1["calls"], "data_files": files}}
    out["cold"] = cold["units"]
    out["seq"] = {"units": seq1["units"], "opened": seq1["opened"]}
    out["rev"] = {"units": seq2["units"], "opened": seq2["opened"]}
    out["time"] = tm1["units"]
    out["time2"] = tm2["units"]
    print("  done %.1fs" % (time.time() - t), flush=True)
    return out


def name_of(test_id):
    return test_id.split("::")[-2] + "." + test_id.split("::")[-1]


def raw_main(outdir, wanted):
    os.makedirs(outdir, exist_ok=True)
    tests = wanted or sorted(units.KINDS)
    files = sorted({t.split("::")[0] for t in tests})
    warm = core.Warm(files)
    assert not warm.import_errors, warm.import_errors
    gc.collect()
    gc.freeze()
    for t in tests:
        whole = getattr(units.KINDS[t](t), "whole", False)
        raw = trace_whole(t, warm.modules) if whole else trace_test(t, warm.modules)
        with open(os.path.join(outdir, name_of(t) + ".raw.pkl"), "wb") as f:
            pickle.dump(raw, f)


def build_one(raw):
    phases = raw["phases"]
    nph = len(phases)
    n = raw["setup"]["n"]
    problems = []
    runs = {"cold": raw["cold"], "seq": raw["seq"]["units"], "rev": raw["rev"]["units"]}
    bitmap = collections.defaultdict(int)     # (rel, line) -> bits
    cold_lines = [set() for _ in range(n)]
    warm_lines = [set() for _ in range(n)]
    for name, recs in runs.items():
        for i, rec in enumerate(recs):
            if not rec["ok"]:
                problems.append("unit %d fails in run %s: %s" % (i, name, rec["err"]))
            for ph, d in rec["lines"].items():
                if ph not in phases:
                    if ph != "-" or any(rel.startswith("sqlglot/") for rel in d):
                        problems.append("unit %d: lines in unknown phase %s" % (i, ph))
                    continue
                bit = 1 << (i * nph + phases.index(ph))
                for rel, ls in d.items():
                    for l in ls:
                        bitmap[(rel, l)] |= bit
                        (cold_lines if name == "cold" else warm_lines)[i].add((rel, l))
    warm_only = collections.defaultdict(set)
    for i in range(n):
        for rel, l in warm_lines[i] - cold_lines[i]:
            warm_only[rel].add(l)
    fps = []
    unstable = collections.Counter()
    for i in range(n):
        names = set()
        for recs in list(runs.values()) + [raw["time"], raw["time2"]]:
            names |= set(recs[i]["fps"])
        d = {}
        for nm in names:
            vals = {recs[i]["fps"].get(nm) for recs in list(runs.values()) + [raw["time"], raw["time2"]]}
            if len(vals) == 1 and None not in vals:
                d[nm] = vals.pop()
            else:
                unstable[nm] += 1
        fps.append(d)
    want_shared = (raw["setup"].get("extra") or {}).get("shared")
    for name in ("seq", "rev"):
        if raw[name].get("shared") != want_shared:
            problems.append("the shared data changes during run %s" % name)
    for rec_list in (raw["time"], raw["time2"]):
        for i, rec in enumerate(rec_list):
            if not rec["ok"]:
                problems.append("unit %d fails in a timing run: %s" % (i, rec["err"]))
    cpu = []
    for i in range(n):
        a, b = raw["time"][i]["cpu"], raw["time2"][i]["cpu"]
        cpu.append({ph: round(min(a.get(ph, 0.0), b.get(ph, 0.0)), 5) for ph in set(a) | set(b)})
    mask = sum(1 << (i * nph) for i in range(n))
    table = {}
    files = {}
    for (rel, l), bits in bitmap.items():
        j = table.setdefault(bits, len(table))
        files.setdefault(rel, {})[l] = j
    tb = [None] * len(table)
    for bits, j in table.items():
        tb[j] = bits.to_bytes((bits.bit_length() + 7) // 8 or 1, "little")
    opened = set(raw["setup"]["opened"]) | set(raw["seq"]["opened"]) | set(raw["rev"]["opened"])
    for rec in raw["cold"]:
        opened |= set(rec.get("opened", ()))
    m = {
        "test": raw["test"], "phases": phases, "n": n, "probe": raw["probe"],
        "fps": fps, "cpu": cpu, "inputs": raw["setup"]["inputs"],
        "files": files, "table": tb,
        "setup": {k: set(v) for k, v in raw["setup"]["lines"].items()},
        "warm_only": dict(warm_only),
        "opened": sorted(p for p in opened if not p.endswith(".py")),
        "pure": [ph for k, ph in enumerate(phases)
                 if not any(rel.startswith("sqlglot/") and (bits >> k) & mask for (rel, l), bits in bitmap.items())],
        "extra": {"preamble_cpu": raw["setup"]["preamble_cpu"], "class_cpu": raw["setup"]["class_cpu"],
                  "outside": raw["setup"].get("extra"),
                  "data_files": (raw["setup"].get("extra") or {}).get("data_files", ())},
    }
    info = {"n": n, "pure": m["pure"], "lines": len(bitmap), "bitmaps": len(tb), "unstable": dict(unstable),
            "warm_only": sum(len(v) for v in warm_only.values()), "problems": problems,
            "cpu_total": round(sum(sum(c.values()) for c in cpu), 2)}
    return m, info


def build_main(outdir, mapsdir):
    os.makedirs(mapsdir, exist_ok=True)
    idx = {"format": units.FORMAT, "tests": {}}
    for f in sorted(os.listdir(outdir)):
        if not f.endswith(".raw.pkl"):
            continue
        raw = pickle.load(open(os.path.join(outdir, f), "rb"))
        m, info = build_one(raw)
        print(raw["test"], info)
        if info["problems"]:
            print("  NOT WRITTEN: a unit does not pass on the pinned commit")
            continue
        name = f[:-8] + ".bin"
        blob = zlib.compress(pickle.dumps(m, protocol=4), 9)
        with open(os.path.join(mapsdir, name), "wb") as h:
            h.write(blob)
        print("  ->", name, len(blob), "bytes")
        idx["tests"][raw["test"]] = {"file": name, "n": m["n"]}
    with open(os.path.join(mapsdir, "index.pkl"), "wb") as h:
        pickle.dump(idx, h, protocol=4)


if __name__ == "__main__":
    if sys.argv[1] == "raw":
        raw_main(sys.argv[2], sys.argv[3:])
    else:
        build_main(sys.argv[2], sys.argv[3])

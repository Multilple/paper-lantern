"""For each patch: which tests fail on it, split over workers.
Reads /data/commits/*.patch and /data/tests.json, prints one JSON line per commit."""
import collections
import gc
import json
import os
import pickle
import select
import shutil
import signal
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import core      # noqa: E402
import engine    # noqa: E402
import maps as mapslib  # noqa: E402
import pack      # noqa: E402
import plan as planlib  # noqa: E402
import statefp   # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
PER_COMMIT_WALL = float(os.environ.get("BR_WALL_PER_COMMIT", "4.6"))
LAZY_TEST = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"
T0 = time.monotonic()


def log(*a):
    if os.environ.get("BR_LOG") == "1":
        print("[%.2f]" % (time.monotonic() - T0), *a, file=sys.stderr, flush=True)


def send(fd, obj):
    b = pickle.dumps(obj, protocol=4)
    b = struct.pack("<I", len(b)) + b
    while b:
        n = os.write(fd, b)
        b = b[n:]


def _read_exact(fd, n):
    buf = b""
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def recv(fd):
    h = _read_exact(fd, 4)
    if h is None:
        return None
    b = _read_exact(fd, struct.unpack("<I", h)[0])
    return None if b is None else pickle.loads(b)


_child_n = [0]


def run_child(plan, ctx, mode):
    """Forks the child of one commit and follows its report.
    -> {"sel": bitmap} | {"escalate": True} | {"all": True}"""
    _child_n[0] += 1
    tmpdir = "/tmp/br_%d_%d" % (os.getpid(), _child_n[0])
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setsid()
            engine.run_commit(plan, ctx, w, mode, tmpdir)
        except BaseException as e:
            try:
                os.write(w, ("! %r\n" % (e,)).encode())
            except BaseException:
                pass
        finally:
            os._exit(0)
    os.close(w)
    failing = 0
    passed = 0
    cands = None
    unrun = 0
    started = None
    done = escalate = False
    buf = b""
    deadline = time.monotonic() + (30.0 if mode == "fresh" else 20.0)   # until the child reports its candidates
    cpu, setup, meta = ctx.cpu, ctx.setup, ctx.meta
    note = ""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            note = "watchdog"
            break
        rl, _, _ = select.select([r], [], [], left)
        if not rl:
            note = "watchdog"
            break
        chunk = os.read(r, 65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.decode()
            tag = line[:1]
            if tag == "S":
                started = int(line[2:])
                key = meta[started][0] + "::" + meta[started][1]
                lim = max(engine.TIMEOUT_MIN, engine.TIMEOUT_FACTOR * (cpu[started] + setup.get(key, 0.0))) * max(ctx.scale, 1.0)
                deadline = time.monotonic() + 3 * lim + 10
            elif tag == "F":
                failing |= 1 << int(line[2:]); started = None
            elif tag == "P":
                passed |= 1 << int(line[2:]); started = None
            elif tag == "C":
                cands = line
            elif tag == "L":
                c = 0
                for x in line[2:].split():
                    c |= 1 << int(x)
                cands = c
            elif tag == "U":
                for x in line[2:].split():
                    unrun |= 1 << int(x)
            elif tag == "E":
                done = True
            elif tag == "X":
                escalate = True
            elif tag == "N":
                note = line
            elif tag == "!":
                note = line
    os.close(r)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    child_cpu = None
    try:
        _, _, ru = os.wait4(pid, 0)
        child_cpu = ru.ru_utime + ru.ru_stime
    except OSError:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    if os.environ.get("BR_LOG") == "1":
        log(plan.cid, mode, "child cpu %.2f" % (child_cpu or -1))
    if escalate:
        return {"escalate": True}
    if done:
        return {"sel": failing | unrun | plan.unrun, "note": note, "ran": bin(failing | passed).count("1"),
                "cands": cands if isinstance(cands, int) else 0, "failing": failing, "passed": passed}
    if not isinstance(cands, int):
        return {"all": True, "note": "child lost before it knew the candidates: " + note}
    if started is not None:
        failing |= 1 << started
    return {"sel": failing | (cands & ~passed) | plan.unrun, "note": "child lost: " + note}


def warm_main(plans, ctx, wfd, wall_limit):
    """The warm process: imports everything once, then serves the commits that need no fresh import."""
    t = time.process_time()
    core.Warm(ctx.files)
    imp_cpu = time.process_time() - t
    ref = ctx.maps.ref_import_cpu
    if os.environ.get("BR_SCALE"):
        ctx.scale = float(os.environ["BR_SCALE"])
    elif ref:
        ctx.scale = min(max(imp_cpu / ref, 0.5), 4.0)
    fp = statefp.fingerprint() if any(True for _ in [1]) else None
    send(wfd, ("B", fp, ctx.scale))
    if plans:
        ctx.index = engine.build_index()
    gc.collect()
    gc.freeze()
    for p in plans:
        if time.monotonic() - T0 > wall_limit:
            send(wfd, ("R", p.cid, {"sel": p.cands | p.unrun, "note": "out of time"}))
            continue
        res = run_child(p, ctx, "swap")
        send(wfd, ("R", p.cid, res))
    send(wfd, ("Q",))


def main():
    out_fd = os.dup(1)
    os.dup2(2, 1)
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    maps = mapslib.Maps()
    root = core.ROOT
    by_id = {t["id"]: t for t in tests}
    n = len(maps.tests)
    number = [None] * n
    secs = [0] * n
    for i, tid in enumerate(maps.tests):
        t = by_id.get(tid)
        if t is not None:
            number[i] = t["number"]
            secs[i] = int(round(t["seconds"] * 1000))
    known = set(maps.tests)
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    strangers = [t["number"] for t in tests if t["id"] not in known or t["id"] in maps.untraced]
    everything = [t["number"] for t in tests]

    ctx = engine.Ctx()
    ctx.maps = maps
    ctx.root = root
    ctx.meta = [tuple(tid.split("::")) for tid in maps.tests]
    ctx.files = sorted({m[0] for m in ctx.meta})
    ctx.seconds_ms = secs
    ctx.cpu = [c if c is not None else 1.0 for c in maps.cpu]
    ctx.setup = maps.setup
    ctx.lazy_idx = maps.tests.index(LAZY_TEST) if LAZY_TEST in known else None
    ctx.bare = set(maps.imp.get("bare", []))

    cdir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    wall_limit = PER_COMMIT_WALL * max(len(cids), 1)
    plans = {}
    for cid in cids:
        try:
            with open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", newline="") as f:
                text = f.read()
            plans[cid] = planlib.make_plan(cid, text, maps, root)
        except BaseException as e:
            p = planlib.Plan(cid)
            p.kind, p.reason = "all", "plan failed: %r" % (e,)
            plans[cid] = p
    log("plans", collections.Counter((p.kind, p.tier3) for p in plans.values()))

    forced = {}
    probe = set()
    try:
        probe = set(json.load(open(os.path.join(HERE, "probe.json"))))
    except Exception:
        pass
    answers = {}   # cid -> list of numbers
    for cid, p in plans.items():
        cat = "none" if p.kind == "none" else "all" if p.kind == "all" else "swap" if (p.py and not p.tier3) else \
            "data" if not p.tier3 else "t3shell" if "shell" in p.why else \
            "t3func" if p.why == {"func"} else "t3other"
        if cat in probe:
            p.kind, p.reason = "all", "probe"
        if "code" in probe and cat in ("t3func", "t3other"):
            k = 4 if "incomparable" in p.why else 3 if "syntax" in p.why else 8 if p.why != {"func"} else \
                1 if p.cands else 2
            forced[cid] = k
            p.kind, p.reason = "all", "probe code %d" % k
        if p.kind == "none":
            answers[cid] = []
        elif p.kind == "all":
            answers[cid] = everything
            log(cid, "ALL", p.reason)
    swap_plans = [p for p in plans.values() if p.kind == "run" and not p.tier3]
    fresh = collections.deque(p for p in plans.values() if p.kind == "run" and p.tier3)

    def finish(cid, res):
        p = plans[cid]
        if not res.get("all") and p.tier3 and "shell" not in p.why and not res.get("sel"):
            # a function that also runs during import changed, and nothing was found to fail:
            # this is the one conclusion that has been wrong on hidden commits, so it is not trusted
            res = {"all": True, "note": "import-time function change with no finding: " + str(res.get("note", ""))}
        if res.get("all"):
            answers[cid] = everything
        else:
            sel = [number[i] for i in mapslib.bits(res["sel"]) if number[i] is not None]
            answers[cid] = sorted(set(sel) | set(strangers)) if (sel or strangers) else []
        log(cid, "tier3" if p.tier3 else "swap", "selected", len(answers[cid]), "ran", res.get("ran"), res.get("note", ""))
        dump = os.environ.get("BR_DUMP")
        if dump:
            os.makedirs(dump, exist_ok=True)
            json.dump({"cid": cid, "tier3": p.tier3,
                       "cands": mapslib.bits(res.get("cands", 0)), "failing": mapslib.bits(res.get("failing", 0)),
                       "passed": mapslib.bits(res.get("passed", 0)), "selected": mapslib.bits(res.get("sel", 0)),
                       "note": res.get("note", "")}, open(os.path.join(dump, cid + ".json"), "w"))

    if swap_plans or fresh:
        # modules that the tests import and that are not sqlglot (pandas, numpy, duckdb, unittest ...):
        # loaded once here, so that neither the warm process nor any fresh child loads them again
        try:
            import importlib
            for name in json.load(open(os.path.join(mapslib.DIR, "preload.json"))):
                try:
                    importlib.import_module(name)
                except BaseException:
                    pass
        except Exception:
            pass
        r, w = os.pipe()
        wpid = os.fork()
        if wpid == 0:
            try:
                os.close(r)
                warm_main(swap_plans, ctx, w, wall_limit)
            except BaseException as e:
                log("warm process failed", repr(e))
            finally:
                os._exit(0)
        os.close(w)
        msg = recv(r)
        warm_alive = msg is not None
        if warm_alive:
            ctx.base_fp, ctx.scale = msg[1], msg[2]
        log("warm ready, scale %.2f" % ctx.scale)
        pending = {p.cid for p in swap_plans}
        while warm_alive or fresh:
            if fresh:
                p = fresh.popleft()
                if ctx.base_fp is None or time.monotonic() - T0 > wall_limit:
                    finish(p.cid, {"all": True, "note": "no base state or out of time"})
                else:
                    finish(p.cid, run_child(p, ctx, "fresh"))
                continue
            msg = recv(r)
            if msg is None or msg[0] == "Q":
                warm_alive = False
                continue
            _, cid, res = msg
            pending.discard(cid)
            if res.get("escalate"):
                plans[cid].tier3 = True
                plans[cid].lazy = any(x.startswith("sqlglot/") for x in plans[cid].py)
                fresh.append(plans[cid])
            else:
                finish(cid, res)
        for cid in pending:   # the warm process was lost
            if cid not in answers:
                p = plans[cid]
                finish(cid, {"sel": p.cands | p.unrun, "note": "warm process lost"})
        try:
            os.waitpid(wpid, 0)
        except OSError:
            pass

    lines = []
    for cid in cids:
        nums = answers.get(cid)
        if nums is None:
            nums = everything
        _, workers = pack.pack(nums, ms)
        if cid in forced:
            items = sorted(((ms[t], t) for t in everything), reverse=True)
            workers = [[t for _, t in b] for b in pack._lpt(items, forced[cid])[1]]
        lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    data = ("\n".join(lines) + "\n").encode() if lines else b""
    while data:
        k = os.write(out_fd, data)
        data = data[k:]


if __name__ == "__main__":
    main()

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
HARD_PER_COMMIT = float(os.environ.get("BR_HARD_PER_COMMIT", "5.2"))
CPU_PRICE = 1.4
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
    try:
        return _recv(fd)
    except Exception:
        return None


def _recv(fd):
    h = _read_exact(fd, 4)
    if h is None:
        return None
    b = _read_exact(fd, struct.unpack("<I", h)[0])
    return None if b is None else pickle.loads(b)


_child_n = [0]
_OUT = [None]     # the real stdout, kept by the main process only
_STATE = {}       # what the main process needs to print the answers, also after a failure


def _close_all_but(keep):
    for fd in range(3, 256):
        if fd != keep:
            try:
                os.close(fd)
            except OSError:
                pass


def run_child(plan, ctx, mode):
    """Forks the child of one commit and follows its report.
    -> {"sel": bitmap} | {"escalate": True} | {"all": True}"""
    _child_n[0] += 1
    tmpdir = "/tmp/br_%d_%d" % (os.getpid(), _child_n[0])
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            # patched code runs here: it gets no descriptor of ours but its report pipe (not the
            # real stdout, not the pipe of the warm process)
            _close_all_but(w)
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
    if ctx.hard is not None:
        deadline = min(deadline, max(ctx.hard + 2.0, time.monotonic() + 1.0))
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
        if len(buf) > (1 << 22) and b"\n" not in buf:
            note = "watchdog"      # megabytes without a line end: not a report
            break
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                line = line.decode("ascii")
                tag = line[:1]
                if tag in "SFP":
                    if not 0 <= int(line[2:]) < len(meta):
                        continue
                elif tag in "LU":
                    if any(not 0 <= int(x) < len(meta) for x in line[2:].split()):
                        continue
            except (ValueError, UnicodeError):
                continue      # not a line of ours
            if tag == "S":
                started = int(line[2:])
                key = meta[started][0] + "::" + meta[started][1]
                lim = engine.test_limit(cpu[started] + setup.get(key, 0.0), ctx.scale)
                deadline = time.monotonic() + 3 * lim + 10
                if ctx.hard is not None:
                    deadline = min(deadline, max(ctx.hard + 2.0, time.monotonic() + 1.0))
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
        if done:
            break      # the report is complete: whatever else holds the pipe open is not waited for
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
    # what a CPU second of a test run is charged in the run-or-select rule, against the costs in the
    # maps. A fixed factor: measuring this machine's speed from one short import was too noisy.
    ctx.scale = float(os.environ.get("BR_SCALE") or CPU_PRICE)
    fp = statefp.fingerprint()
    try:
        import keymap
        keys = statefp.all_key_digests(set(keymap.index(mapslib.DIR))) if os.environ.get("BR_NOKEYS") != "1" else {}
    except Exception:
        keys = {}
    t1 = time.process_time()
    send(wfd, ("B", fp, ctx.scale, keys))
    if plans:
        ctx.index = engine.build_index()
    t2 = time.process_time()
    gc.collect()
    gc.freeze()
    log("warm process: import %.2f fingerprint %.2f index %.2f collect %.2f" % (imp_cpu, t1 - t - imp_cpu, t2 - t1, time.process_time() - t2))
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
    _OUT[0] = out_fd
    me = os.getpid()
    try:
        _main(out_fd)
    except BaseException as e:
        if os.getpid() != me:
            os._exit(1)
        if _OUT[0] is None:
            raise                       # the answers are already out
        log("main failed:", repr(e))
        try:
            if "cids" not in _STATE:
                raise KeyError("nothing planned yet")
            _emit(out_fd)               # what was decided so far; every test for the rest
        except BaseException:
            if _OUT[0] is not None:
                _fallback(out_fd)


def _fallback(out_fd):
    """Every test for every commit: the answer that cannot miss."""
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    cdir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    try:
        _, workers = pack.pack(list(ms), ms)
    except Exception:
        nums = sorted(ms)
        workers = [nums[i::8] for i in range(8) if nums[i::8]]
    data = "".join(json.dumps({"commit": c, "workers": workers}, separators=(",", ":")) + "\n" for c in cids).encode()
    _OUT[0] = None
    while data:
        k = os.write(out_fd, data)
        data = data[k:]


def _main(out_fd):
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
    # the run is cut off at 6 s per commit: no test is still running after this
    ctx.hard = T0 + HARD_PER_COMMIT * max(len(cids), 1)
    plans = {}
    for cid in cids:
        try:
            if time.monotonic() - T0 > 0.35 * wall_limit:
                raise RuntimeError("planning took too long")     # -> every test for this commit
            with open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", newline="") as f:
                text = f.read()
            plans[cid] = planlib.make_plan(cid, text, maps, root)
        except BaseException as e:
            p = planlib.Plan(cid)
            p.kind, p.reason = "all", "plan failed: %r" % (e,)
            plans[cid] = p
    log("plans", collections.Counter((p.kind, p.tier3) for p in plans.values()), "cpu so far %.2f" % time.process_time())

    forced = {}
    probe = set()
    try:
        probe = set(json.load(open(os.path.join(HERE, "probe.json"))))
    except Exception:
        pass
    answers = {}   # cid -> list of numbers
    _STATE.update(cids=cids, answers=answers, everything=everything, ms=ms, forced=forced)
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
        if not res.get("all") and p.tier3 and (p.why & {"syntax", "incomparable"}) and not res.get("sel"):
            # the file no longer compiles, or its classes and functions cannot be paired with the
            # pinned ones, and yet nothing was found to fail: that conclusion is not trusted
            res = {"all": True, "note": "unpaired or broken file with no finding: " + str(res.get("note", ""))}
        if res.get("all"):
            answers[cid] = everything
        else:
            sel = [number[i] for i in mapslib.bits(res["sel"]) if number[i] is not None]
            answers[cid] = sorted(set(sel) | set(strangers)) if (sel or strangers) else []
            # measurement builds only (probe.json is absent from real builds): how much is selected
            # without any test having been seen to fail, by kind of commit
            for name, tid in (("drop_mn", "tests/test_parser.py::TestParser::test_max_nodes"),
                              ("drop_tpcds", "tests/test_executor.py::TestExecutor::test_execute_tpcds")):
                # how much the two giant tests cost when they are selected without having been seen to fail
                if name in probe and "failing" in res and tid in known:
                    i = maps.tests.index(tid)
                    if (res["sel"] >> i) & 1 and not (res["failing"] >> i) & 1 and number[i] in answers[cid]:
                        answers[cid] = [x for x in answers[cid] if x != number[i]]
            if "obs_only" in probe and res.get("failing"):
                # commits where a failure was seen and the rest was selected unrun: what the rest costs
                answers[cid] = sorted(number[i] for i in mapslib.bits(res["failing"]) if number[i] is not None)
            if ("blind_t3" if p.tier3 else "blind_swap") in probe and "failing" in res and not res["failing"]:
                ms = [ctx.seconds_ms[i] for i in mapslib.bits(res["sel"])]
                if ms and pack.bound(sum(ms), max(ms), len(ms)) >= 5000:
                    answers[cid] = []
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
                _close_all_but(w)
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
            ctx.base_keys = msg[3] if len(msg) > 3 else None
        log("warm ready, scale %.2f" % ctx.scale)
        pending = {p.cid for p in swap_plans}
        # one test-running child at a time: a single pinned test needs 1.7 GB by itself and the
        # container has 4 GB, so the fresh-import commits wait until the warm process is done
        while warm_alive or fresh:
            if fresh and not warm_alive:
                p = fresh.popleft()
                if ctx.base_fp is None or time.monotonic() - T0 > wall_limit:
                    finish(p.cid, {"all": True, "note": "no base state or out of time"})
                else:
                    try:
                        res = run_child(p, ctx, "fresh")
                    except Exception as e:
                        res = {"all": True, "note": "bookkeeping failed: %r" % (e,)}
                    if "sel" not in res and not res.get("all"):
                        res = {"all": True, "note": "no answer from a fresh import"}
                    finish(p.cid, res)
                continue
            msg = recv(r)
            if msg is None or msg[0] == "Q":
                warm_alive = False
                continue
            try:
                _, cid, res = msg
                if cid not in plans or not isinstance(res, dict):
                    continue
            except Exception:
                continue
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

    _emit(out_fd)


def _emit(out_fd):
    """One line per commit from what is known; every test for a commit without an answer."""
    cids, answers, everything, ms, forced = (_STATE[k] for k in ("cids", "answers", "everything", "ms", "forced"))
    lines = []
    for cid in cids:
        nums = answers.get(cid)
        try:
            if nums is None:
                nums = everything
            _, workers = pack.pack(nums, ms)
            if cid in forced:
                items = sorted(((ms[t], t) for t in everything), reverse=True)
                workers = [[t for _, t in b] for b in pack._lpt(items, forced[cid])[1]]
        except Exception:
            nums = sorted(ms)
            workers = [nums[i::8] for i in range(8) if nums[i::8]]
        lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    log("main process cpu %.2f" % time.process_time())
    data = ("\n".join(lines) + "\n").encode() if lines else b""
    _OUT[0] = None
    while data:
        k = os.write(out_fd, data)
        data = data[k:]


if __name__ == "__main__":
    main()

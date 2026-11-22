"""Offline map builder, run on the pinned commit only. For every test, in its own fork of the
warm process: the lines it runs, the files it opens, its verdict and its CPU cost.
usage: trace.py tests.json out.jsonl part nparts [notrace]"""
import json, os, sys, time, resource
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import core


def main():

    tests = json.load(open(sys.argv[1]))["tests"]
    out = open(sys.argv[2], "w")
    part, nparts = int(sys.argv[3]), int(sys.argv[4])
    notrace = len(sys.argv) > 5
    ROOT = core.ROOT.rstrip("/") + "/"
    files = sorted({t["file"] for t in tests})
    warm = core.Warm(files)
    assert not warm.import_errors, warm.import_errors
    import gc
    gc.collect(); gc.freeze()

    for t in tests:
        if t["number"] % nparts != part:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            lines = {}
            opened = set()
            if not notrace:
                mon = sys.monitoring
                mon.use_tool_id(3, "br")
                def on_line(code, line):
                    fn = code.co_filename
                    if fn.startswith(ROOT):
                        s = lines.get(fn)
                        if s is None:
                            s = lines[fn] = set()
                        s.add(line)
                    return mon.DISABLE
                def on_start(code, off):
                    # no LINE event is raised for instructions on the line a code object starts on
                    # (a one-line lambda or def raises none at all), so entering the code counts
                    # for its first lines
                    fn = code.co_filename
                    if fn.startswith(ROOT):
                        s = lines.get(fn)
                        if s is None:
                            s = lines[fn] = set()
                        s.add(code.co_firstlineno)
                        for _, _, l in code.co_lines():
                            if l is not None:
                                s.add(l)
                                if l != code.co_firstlineno:
                                    break
                    return mon.DISABLE
                mon.register_callback(3, mon.events.LINE, on_line)
                mon.register_callback(3, mon.events.PY_START, on_start)
                mon.set_events(3, mon.events.LINE | mon.events.PY_START)
                def hook(ev, args):
                    if ev == "open":
                        p = args[0]
                        if isinstance(p, bytes):
                            p = os.fsdecode(p)
                        if isinstance(p, str):
                            opened.add(p)
                    elif ev in ("os.listdir", "os.scandir", "glob.glob"):
                        p = args[0]
                        if isinstance(p, str):
                            opened.add("DIR:" + p)
                sys.addaudithook(hook)
            t0 = time.process_time()
            try:
                core.session_state(t["file"])
                core.prepare_class(t["id"], warm.modules)
            except BaseException:
                pass
            t1 = time.process_time()
            bad = core.run_test(t["id"], warm.modules)
            cpu = time.process_time() - t1
            rec = {"id": t["id"], "bad": bad, "cpu": round(cpu, 4), "setup": round(t1 - t0, 4)}
            if not notrace:
                rec["lines"] = {k[len(ROOT):]: sorted(v) for k, v in lines.items()}
                rec["opened"] = sorted(os.path.abspath(p) if not p.startswith("DIR:") else p for p in opened)
            os.write(w, json.dumps(rec).encode())
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
        out.write(buf.decode() + "\n"); out.flush()


if __name__ == "__main__":
    main()

"""Offline map builder, pinned commit only. For every test, in its own fork of the warm process:
the sqlglot classes whose objects (or the classes themselves) are passed as one of the first
three arguments of any Python call. usage: trace_types.py tests.json out.jsonl part nparts"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import core


def main():

    tests = json.load(open(sys.argv[1]))["tests"]
    out = open(sys.argv[2], "w")
    part, nparts = int(sys.argv[3]), int(sys.argv[4])
    files = sorted({t["file"] for t in tests})
    warm = core.Warm(files)
    import gc
    gc.collect(); gc.freeze()

    for t in tests:
        if t["number"] % nparts != part:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            seen = set()
            add = seen.add
            getframe = sys._getframe
            mon = sys.monitoring
            mon.use_tool_id(3, "br")
            nargs = {}
            def on_start(code, offset):
                n = nargs.get(code)
                if n is None:
                    n = code.co_argcount + code.co_kwonlyargcount
                    n = nargs[code] = code.co_varnames[:min(n, 3)]
                if n:
                    loc = getframe(1).f_locals
                    for name in n:
                        try:
                            v = loc[name]
                        except KeyError:
                            continue
                        tv = type(v)
                        if tv not in seen:
                            add(tv)
                        if tv is type or isinstance(v, type):
                            if v not in seen:
                                add(v)
            mon.register_callback(3, mon.events.PY_START, on_start)
            mon.set_events(3, mon.events.PY_START)
            core.run_test(t["id"], warm.modules)
            mon.set_events(3, 0)
            # the class and every class it inherits from: classes made by a metaclass share one
            # name (type("Tokenizer", (base,), {})), so only the base names tell them apart
            names = set()
            for c in seen:
                try:
                    mro = c.__mro__
                except Exception:
                    mro = (c,)
                for b in mro:
                    m = getattr(b, "__module__", None)
                    if isinstance(m, str) and m.split(".")[0] in ("sqlglot", "tests"):
                        names.add(m + ":" + b.__qualname__)
            names = sorted(names)
            os.write(w, json.dumps({"id": t["id"], "types": names}).encode())
            os._exit(0)
        os.close(w)
        buf = b""
        while True:
            b = os.read(r, 1 << 20)
            if not b:
                break
            buf += b
        os.close(r)
        os.waitpid(pid, 0)
        out.write((buf.decode() if buf else json.dumps({"id": t["id"], "died": 1})) + "\n"); out.flush()


if __name__ == "__main__":
    main()

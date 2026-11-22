"""Offline map builder, pinned commit only: the lines that run, the functions that are called and
the files that are opened while sqlglot and the test files are imported.
usage: trace_import.py tests.json out.json"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import core


def main():
    ROOT = core.ROOT.rstrip("/") + "/"
    tests = json.load(open(sys.argv[1]))["tests"]
    lines, called, opened = {}, set(), set()
    mon = sys.monitoring
    mon.use_tool_id(3, "br")
    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(ROOT):
            lines.setdefault(fn, set()).add(line)
        return mon.DISABLE
    def on_start(code, off):
        fn = code.co_filename
        if fn.startswith(ROOT) and code.co_flags & 1:
            called.add((fn[len(ROOT):], code.co_qualname, code.co_firstlineno))
        return mon.DISABLE
    mon.register_callback(3, mon.events.LINE, on_line)
    mon.register_callback(3, mon.events.PY_START, on_start)
    mon.set_events(3, mon.events.LINE | mon.events.PY_START)
    def hook(ev, args):
        if ev == "open" and isinstance(args[0], str):
            opened.add(os.path.abspath(args[0]))
        elif ev in ("os.listdir", "os.scandir") and isinstance(args[0], str):
            opened.add("DIR:" + os.path.abspath(args[0]))
    sys.addaudithook(hook)
    warm = core.Warm(sorted({t["file"] for t in tests}))
    mon.set_events(3, 0)
    assert not warm.import_errors
    json.dump({"lines": {k[len(ROOT):]: sorted(v) for k, v in lines.items()},
               "called": sorted(called),
               "opened": sorted(p[len(ROOT):] if p.startswith(ROOT) else p for p in opened if ROOT in p),
               "modules": sorted(m for m in sys.modules if m.split(".")[0] in ("sqlglot", "tests"))},
              open(sys.argv[2], "w"))


if __name__ == "__main__":
    main()

"""What runs inside the forked child of one commit: put the patch in place, find the tests that
can be affected, run them, and report which ones fail."""
import builtins
import gc
import io
import importlib
import importlib.machinery
import os
import re
import signal
import sys
import types

import codediff
import core
import pack
import statefp
from maps import bits

# ---- how much a test run may cost before the test is selected without a verdict
THETA_CLEAN = float(os.environ.get("BR_THETA_CLEAN", "0.75"))   # nothing has failed yet
THETA_DIRTY = float(os.environ.get("BR_THETA_DIRTY", "0.9"))    # something already failed
TIMEOUT_MIN = 4.0
TIMEOUT_FACTOR = 15.0


class Ctx:
    """Everything a child needs to know. Filled by main.py."""
    maps = None
    root = None
    files = None       # test files, for Warm
    meta = None        # idx -> (file, class, method)
    seconds_ms = None  # idx -> recorded duration in ms
    cpu = None         # idx -> our CPU seconds for the test alone
    setup = None       # "file::class" -> CPU seconds of setUpClass
    scale = 1.0        # this machine against the one the costs were measured on
    index = None       # filename -> {(firstlineno, qualname): [live functions]}  (warm process only)
    base_fp = None     # fingerprint of the pinned import state (fresh children only)
    lazy_idx = None    # idx of the test that starts a python subprocess
    bare = None        # relpaths that "import sqlglot" loads


def build_index():
    idx = {}
    FT = types.FunctionType
    for o in gc.get_objects():
        if type(o) is FT:
            c = o.__code__
            idx.setdefault(c.co_filename, {}).setdefault((c.co_firstlineno, c.co_qualname), []).append(o)
    return idx


# ---------------------------------------------------------------- patch in place

def install_sources(root, py):
    """Any import from now on compiles the patched text of a changed file."""
    patched = {os.path.join(root, rel): src for rel, src in py.items()}
    loader = importlib.machinery.SourceFileLoader
    orig = loader.get_code

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        src = patched.get(path)
        if src is not None:
            return compile(src, path, "exec", dont_inherit=True)
        return orig(self, fullname)

    loader.get_code = get_code


def install_overlay(root, files, tmpdir):
    """open() of a changed file returns its patched content."""
    if not files:
        return
    os.makedirs(tmpdir, exist_ok=True)
    mapping = {}
    for n, (rel, text) in enumerate(files.items()):
        p = os.path.join(tmpdir, "f%d_%s" % (n, os.path.basename(rel)))
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        mapping[os.path.join(root, rel)] = p
    real = builtins.open

    def opener(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            try:
                p = os.path.abspath(os.fsdecode(file))
            except Exception:
                p = None
            t = mapping.get(p)
            if t is not None:
                file = t
        return real(file, *a, **k)

    builtins.open = opener
    io.open = opener


def make_farm(root, py, tmpdir):
    """A copy of the sqlglot package (links, and the patched files as real files) for a test that
    starts its own python process from the working directory."""
    farm = os.path.join(tmpdir, "farm")
    src = os.path.join(root, "sqlglot")
    for d, dirs, fs in os.walk(src):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        rel = os.path.relpath(d, root)
        os.makedirs(os.path.join(farm, rel), exist_ok=True)
        for f in fs:
            r = os.path.join(rel, f)
            dst = os.path.join(farm, r)
            if r in py:
                with open(dst, "w", encoding="utf-8", newline="") as h:
                    h.write(py[r])
            else:
                os.symlink(os.path.join(d, f), dst)
    return farm


def do_swaps(plan, ctx):
    """Function bodies changed and nothing else: give the live functions their new code.
    False when that cannot be done exactly."""
    for rel, tree in plan.trees.items():
        fname = os.path.join(ctx.root, rel)
        live = ctx.index.get(fname, {})
        by_key = {}
        for node in codediff.walk(tree):
            if node.is_function:
                by_key.setdefault((node.old.co_firstlineno, node.old.co_qualname), []).append(node)
        for key, nodes in by_key.items():
            if not any(n.changed for n in nodes):
                continue
            for f in live.get(key, ()):
                code = f.__code__
                match = [n for n in nodes if n.old.co_code == code.co_code and n.old.co_names == code.co_names
                         and n.old.co_varnames == code.co_varnames]
                if len(match) != 1:
                    if len(match) > 1 and not any(n.changed for n in match):
                        continue
                    return False
                n = match[0]
                if not n.changed:
                    continue
                if n.new.co_freevars != code.co_freevars:
                    return False
                od, nd = codediff._doc(n.old), codediff._doc(n.new)
                f.__code__ = n.new
                if od != nd and f.__doc__ == od:
                    try:
                        f.__doc__ = nd
                    except Exception:
                        pass
    return True


# ---------------------------------------------------------------- import-time changes

_src_cache = {}


def _sources(root):
    if not _src_cache:
        for top in ("sqlglot", "tests"):
            for d, dirs, fs in os.walk(os.path.join(root, top)):
                dirs[:] = [x for x in dirs if x != "__pycache__" and x != "fixtures"]
                for f in fs:
                    if f.endswith(".py"):
                        p = os.path.join(d, f)
                        try:
                            _src_cache[os.path.relpath(p, root)] = open(p, encoding="utf-8").read()
                        except Exception:
                            pass
    return _src_cache


def _resolve(name):
    mod, _, qual = name.partition(":")
    o = sys.modules.get(mod)
    if o is None:
        return None
    for part in qual.split("."):
        o = getattr(o, part, None)
        if o is None:
            return None
    return o if isinstance(o, type) else None


def roots_to_tests(roots, ctx, plan):
    maps = ctx.maps
    acc = 0
    cnames = {k[1] for k in roots if k[0] == "c"}
    if cnames:
        cobjs = {o for o in (_resolve(n) for n in cnames) if o is not None}
        touched = set()   # changed classes that some test is known to touch
        for kname, tests in maps.types.items():
            hit = kname in cnames
            k = None
            if not hit:
                k = _resolve(kname)
                hit = k is None or any(c in cobjs for c in k.__mro__)
            if hit:
                for i in tests:
                    acc |= 1 << i
                if kname in cnames:
                    touched.add(kname)
                elif k is not None:
                    for c in k.__mro__:
                        if c in cobjs:
                            touched.add("%s:%s" % (c.__module__, c.__qualname__))
        for n in cnames - touched:
            # no test is recorded as touching this class: fall back to every test that runs a
            # line of the file that defines it
            rel = n.split(":")[0].replace(".", "/")
            for cand in (rel + ".py", rel + "/__init__.py"):
                acc |= maps.tests_in_file(cand)
        for n in cnames:
            if n.startswith("tests."):
                f = n.split(":")[0].replace(".", "/") + ".py"
                cls = n.split(":")[1].split(".")[0]
                for i, (tf, tc, _) in enumerate(ctx.meta):
                    if tf == f and tc == cls:
                        acc |= 1 << i
    gnames = {}
    for k in roots:
        if k[0] == "g":
            gnames.setdefault(k[2], set()).add(k[1])
    if gnames:
        srcs = _sources(ctx.root)
        patched = plan.py
        for name, mods in gnames.items():
            for m in mods:
                rel = m.replace(".", "/")
                for cand in (rel + ".py", rel + "/__init__.py"):
                    acc |= maps.tests_in_file(cand)
            if not name.isidentifier():
                continue
            pat = re.compile(r"\b%s\b" % re.escape(name))
            for rel, text in srcs.items():
                if pat.search(text) or (rel in patched and pat.search(patched[rel])):
                    acc |= maps.tests_in_file(rel)
    return acc


# ---------------------------------------------------------------- running

def _on_alarm(signum, frame):
    raise core.TestTimeout()


RSS_LIMIT = int(float(os.environ.get("BR_RSS_MB", "1300")) * 1024 * 1024)
DATA_LIMIT = int(float(os.environ.get("BR_DATA_MB", "2600")) * 1024 * 1024)
_PAGE = os.sysconf("SC_PAGE_SIZE")


def _on_prof(signum, frame):
    """A patch can make a test allocate without bound (a join that explodes). The container has
    4 GB and going over it rejects the whole run, so a test that grows far past anything the
    pinned tests need is stopped and counts as failing."""
    try:
        with open("/proc/self/statm") as f:
            rss = int(f.read().split()[1]) * _PAGE
    except Exception:
        return
    if rss > RSS_LIMIT:
        raise core.TestTimeout()


def run_commit(plan, ctx, wfd, mode, tmpdir):
    """mode "swap": forked from the warm process. mode "fresh": forked before sqlglot was imported."""
    def out(s):
        os.write(wfd, (s + "\n").encode())

    dn = os.open(os.devnull, os.O_RDWR)
    os.dup2(dn, 0); os.dup2(dn, 1)
    if os.environ.get("BR_DEBUG") != "1":
        os.dup2(dn, 2)
    install_overlay(ctx.root, plan.data, tmpdir)
    install_sources(ctx.root, plan.py)
    failed_now = 0
    if mode == "swap":
        if not do_swaps(plan, ctx):
            out("X")
            return
        cands = plan.cands
        modules = None
    else:
        # a patched import can loop for ever or eat memory: the same guards as for a test
        signal.signal(signal.SIGVTALRM, _on_alarm)
        signal.signal(signal.SIGPROF, _on_prof)
        signal.setitimer(signal.ITIMER_VIRTUAL, 12.0 * max(ctx.scale, 1.0))
        signal.setitimer(signal.ITIMER_PROF, 0.2, 0.2)
        try:
            warm = core.Warm(ctx.files)
        finally:
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
            signal.setitimer(signal.ITIMER_PROF, 0)
        for i, (tf, _, _) in enumerate(ctx.meta):
            if tf in warm.import_errors:
                failed_now |= 1 << i
        # function bodies that the patch changed are followed through line coverage; in the
        # fingerprint they stand for their pinned bodies, so that only other differences show
        subst = {}
        for tree in plan.trees.values():
            for node in codediff.walk(tree):
                if node.changed and node.is_function:
                    subst[statefp.code_digest(node.new)] = statefp.code_digest(node.old)
        fp = statefp.fingerprint(subst=subst)
        roots = statefp.changed_roots(ctx.base_fp, fp)
        if not roots and not plan.cands and plan.why - {"func"}:
            # code that runs at import changed, yet nothing that can be seen differs and no test
            # runs a changed function: do not conclude "harmless" from that
            for rel in plan.py:
                cands_extra = ctx.maps.tests_in_file(rel)
                plan.cands |= cands_extra
        out("N %d %s" % (len(roots), ";".join(sorted("%s|%s|%s" % k for k in roots))[:300]))
        cands = plan.cands | roots_to_tests(roots, ctx, plan)
        if plan.lazy and ctx.lazy_idx is not None and any(r in ctx.bare for r in plan.py):
            cands |= 1 << ctx.lazy_idx
        cands &= ~failed_now
        modules = warm.modules
    for i in bits(failed_now):
        out("F %d" % i)
    cands &= ~plan.unrun
    cpu, secs, setup, meta, scale = ctx.cpu, ctx.seconds_ms, ctx.setup, ctx.meta, ctx.scale
    order = sorted(bits(cands), key=lambda i: (cpu[i], i))
    out("L " + " ".join(str(i) for i in order))
    F_total = F_long = F_n = 0
    for i in bits(failed_now | plan.unrun):
        F_total += secs[i]; F_long = max(F_long, secs[i]); F_n += 1
    U_total = sum(secs[i] for i in order)
    suffix_long = [0] * (len(order) + 1)   # tests are run cheapest first; the longest left is in the suffix
    for j in range(len(order) - 1, -1, -1):
        suffix_long[j] = max(suffix_long[j + 1], secs[order[j]])
    R = sum(cpu[i] for i in order) * scale
    classes = {}
    for i in order:
        key = meta[i][0] + "::" + meta[i][1]
        classes[key] = classes.get(key, 0) + 1
    R += sum(setup.get(k, 0.0) for k in classes) * scale
    hung = set()
    signal.signal(signal.SIGVTALRM, _on_alarm)
    signal.signal(signal.SIGPROF, _on_prof)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_DATA, (DATA_LIMIT, DATA_LIMIT))
    except Exception:
        pass
    farm = None
    pos = 0
    n = len(order)
    while pos < n:
        i = order[pos]
        key = meta[i][0] + "::" + meta[i][1]
        stop_cost = pack.bound(F_total + U_total, max(F_long, suffix_long[pos]), F_n + n - pos)
        if F_n:
            gain = stop_cost - pack.bound(F_total, F_long, F_n)
            stop = R * 1000.0 > THETA_DIRTY * gain
        else:
            stop = R * 1000.0 > THETA_CLEAN * stop_cost
        if stop:
            break
        pos += 1
        U_total -= secs[i]
        c = cpu[i] * scale
        R -= c
        if key in hung:
            out("F %d" % i)
            F_total += secs[i]; F_long = max(F_long, secs[i]); F_n += 1
            continue
        if core.class_state(meta[i]) is None:
            R -= setup.get(key, 0.0) * scale
        out("S %d" % i)
        limit = max(TIMEOUT_MIN, TIMEOUT_FACTOR * (cpu[i] + setup.get(key, 0.0))) * max(scale, 1.0)
        cwd = None
        if i == ctx.lazy_idx and plan.py:
            if farm is None:
                farm = make_farm(ctx.root, plan.py, tmpdir)
            cwd = os.getcwd()
            os.chdir(farm)
        signal.setitimer(signal.ITIMER_VIRTUAL, limit, limit / 2)
        signal.setitimer(signal.ITIMER_PROF, 0.2, 0.2)
        timed_out = False
        try:
            bad = core.run_test("%s::%s::%s" % meta[i], modules)
        except core.TestTimeout:
            bad = True
            timed_out = True
        finally:
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
            signal.setitimer(signal.ITIMER_PROF, 0)
            if cwd is not None:
                os.chdir(cwd)
        if core.last_timed_out():
            timed_out = True
        if timed_out:
            bad = True
            hung.add(key)
        if bad:
            out("F %d" % i)
            F_total += secs[i]; F_long = max(F_long, secs[i]); F_n += 1
        else:
            out("P %d" % i)
    if pos < n:
        out("U " + " ".join(str(i) for i in order[pos:]))
    out("E")

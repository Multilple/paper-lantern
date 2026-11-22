"""What runs inside the forked child of one commit: put the patch in place, find the tests that
can be affected, run them, and report which ones fail."""
import builtins
import gc
import io
import importlib
import importlib.machinery
import os
import time
import re
import signal
import sys
import types

import attrmap
import keymap
import codediff
import core
import pack
import policy
import statefp
import units
from maps import bits

# ---- how much a test run may cost before the test is selected without a verdict
THETA_CLEAN = float(os.environ.get("BR_THETA_CLEAN", "0.75"))   # nothing has failed yet
THETA_DIRTY = float(os.environ.get("BR_THETA_DIRTY", "0.9"))    # something already failed
UNITS_ALWAYS = os.environ.get("BR_UNITS_ALWAYS", "0") == "1"
UNITS_LIMIT = 80.0
TRUTH = os.environ.get("BR_TRUTH") in ("1", "2")
TRUTH_ALL = os.environ.get("BR_TRUTH") == "2"     # do not stop at the first failure
GIANT_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
GIANT_DEAR = 4.0      # CPU seconds: a unit verdict dearer than this is not taken when the rule below applies
SENTINEL_CPU = 3.0    # map CPU seconds of the other candidates, at most
SENTINEL_MIN = int(os.environ.get("BR_SENTINEL_MIN", "10"))   # 0 turns the rule off
UNITS_GAIN = 0.9      # decide a giant unit by unit when that is expected to cost less than this share of a run
POLICY_MODEL = policy.Model(0.15, 0.2, 0.1, small=(0.0, 5.0, 0.5), small_n=2)
TIMEOUT_MIN = 4.0
TIMEOUT_FACTOR = 15.0
# Waiting for a slow test costs CPU seconds one for one, and selecting it without a verdict costs
# at most its own time, so a test is given up on once it has used about twice its usual time.
TIMEOUT_SLOPE = 2.0
TIMEOUT_SLACK = 4.0
# all the tests of one commit together may run this much longer than they usually do
COMMIT_OVERRUN = 12.0
# more changed roots than this after a fresh import: the import broke, every test is selected
ROOTS_CAP = int(os.environ.get("BR_ROOTS_CAP", "3000"))


def test_limit(c, scale):
    """CPU seconds a test that usually takes c seconds (with its class set-up) may use."""
    return max(TIMEOUT_MIN, min(TIMEOUT_FACTOR * c, TIMEOUT_SLOPE * c + TIMEOUT_SLACK)) * max(scale, 1.0)


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
    base_keys = None   # {(class, attr): {key name: digest}} of the pinned class-level dicts
    lazy_idx = None    # idx of the test that starts a python subprocess
    bare = None        # relpaths that "import sqlglot" loads
    hard = None        # time.monotonic() value after which no test is run any more


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

    compiled = {}

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        src = patched.get(path)
        if src is not None:
            # a module that fails to import is imported again by every later importer: compile
            # the patched text once (a file that does not compile can cost seconds each time)
            if path not in compiled:
                try:
                    compiled[path] = compile(src, path, "exec", dont_inherit=True)
                except Exception as e:
                    compiled[path] = e
            c = compiled[path]
            if isinstance(c, Exception):
                raise c
            return c
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


def mapslib_dir():
    import maps as mapslib
    return mapslib.DIR


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


def _new_methods(roots, ctx, amap):
    """A method that a generator or parser class did not have before (added, or the new name of a
    renamed one). Who can call it? Code the patch changed (candidates through its own lines), the
    generator's dispatch table (_dispatch_users), and every call by that name that went to the
    method of the same name further up the class's bases before: the tests that read THAT
    method, whatever class they read it through.
    -> (bitmap, the roots answered here)"""
    gen = sys.modules.get("sqlglot.generator")
    par = sys.modules.get("sqlglot.parser")
    bases = tuple(x for x in (getattr(gen, "Generator", None), getattr(par, "Parser", None)) if isinstance(x, type))
    acc, done = 0, set()
    if not bases or not ctx.base_fp:
        return acc, done
    cid, aid, own, key, table = amap["cid"], amap["aid"], amap["own"], amap["key"], amap["table"]
    NA, nocover = len(amap["attrs"]), amap["nocover"]
    for k in roots:
        if k[0] != "c" or k in ctx.base_fp or k[2].startswith("__") or k[2].startswith("<"):
            continue
        g = _resolve(k[1])
        if g is None or not issubclass(g, bases) or cid.get(k[1]) is None:
            continue
        v = vars(g).get(k[2])
        if isinstance(v, (staticmethod, classmethod)):
            v = v.__func__
        if not isinstance(v, types.FunctionType):
            continue
        a = aid.get(k[2])
        ok, found = True, 0
        for p in g.__mro__[1:]:
            if k[2] not in vars(p):
                continue
            pc = cid.get("%s:%s" % (p.__module__, p.__qualname__))
            kind = own.get(pc, {}).get(a) if pc is not None and a is not None else None
            if kind != "f" or (pc * NA + a) in nocover:
                ok = False          # the name went to something the map does not follow as a method
            else:
                j = key.get(pc * NA + a)
                if j is not None:
                    found = int.from_bytes(table[j], "little")
            break
        if ok:
            acc |= found | amap["unsafe"]
            done.add(k)
    return acc, done


def _dispatch_users(roots, maps):
    """Tests that reach a changed `<expression>_sql` method of a generator class through the
    generator's dispatch table: those that asked that table for the expression class, in the
    class itself or in a class that inherits from it (keymap: the lookups in a dispatch table
    are noted under the TRANSFORMS table it is built from)."""
    gen = sys.modules.get("sqlglot.generator")
    base = getattr(gen, "Generator", None)
    if base is None:
        return 0
    acc = 0
    ix = keymap.index(mapslib_dir())
    for k in roots:
        if k[0] != "c" or not k[2].endswith("_sql") or k[2].startswith("_"):
            continue
        g = _resolve(k[1])
        if g is None or not issubclass(g, base):
            continue
        ecls = gen.exp.EXPR_CLASSES.get(k[2][:-4])
        if ecls is None:
            continue                      # no expression of that name: the table never holds it
        name = keymap.keyname(ecls)
        todo, seen = [g], set()
        while todo:
            d = todo.pop()
            if d in seen:
                continue
            seen.add(d)
            todo.extend(d.__subclasses__())
            owner = next(c for c in d.__mro__ if "TRANSFORMS" in vars(c))
            n = ix[("c", "%s:%s" % (owner.__module__, owner.__qualname__), "TRANSFORMS")]
            t = keymap._load(mapslib_dir(), n)
            acc |= int.from_bytes(t["whole"], "little")
            b = t["keys"].get(name) if name is not None else None
            if name is None:
                raise KeyError(k)
            if b:
                acc |= int.from_bytes(b, "little")
    return acc


def roots_to_tests(roots, ctx, plan, subst=None):
    maps = ctx.maps
    acc = 0
    acc0 = 0
    # tables first: maps/keys knows which tests asked for which KEY of a class-level dict
    # (keymap.py). A table it vouches for gives the tests that asked for a changed key or used
    # the table as a whole; everything else goes on to the maps below.
    if ctx.base_keys:
        try:
            def new_keys(root):
                kind, owner, attr = root
                o = _resolve(owner) if kind == "c" else sys.modules.get(owner)
                if o is None or attr not in vars(o):
                    return None
                return statefp.key_digests(vars(o)[attr], subst)
            acc0, roots = keymap.narrow(roots, ctx.base_keys, new_keys, mapslib_dir())
        except Exception:
            acc0 = 0
    # methods of generator classes named <expression>_sql are also reached through the dispatch
    # table the generator fills once per class (the attribute map leaves that filling out)
    try:
        acc0 |= _dispatch_users(roots, maps)
    except Exception:
        for k in roots:
            if k[0] == "c" and k[2].endswith("_sql"):
                for i in maps.types.get(k[1]) or ():
                    acc0 |= 1 << i
    # class attributes first: maps/attrs.bin knows which tests READ an attribute of a class
    # (attrmap.py). Roots it cannot vouch for go the broad way below.
    amap = None
    try:
        amap = attrmap.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", "attrs.bin"))
        try:
            more, done = _new_methods(roots, ctx, amap)
            acc0 |= more
            roots = set(roots) - done
        except Exception:
            pass
        acc, roots = attrmap.narrow(roots, amap, _resolve)
    except Exception:
        amap = None
    cnames = {k[1] for k in roots if k[0] == "c"}
    if cnames:
        # maps/types.json lists, for every class, the tests that handle an object of that class
        # or of a class that inherits from it (names of the pinned commit)
        types = maps.types
        for n in cnames:
            tests = types.get(n)
            if amap is not None:
                try:
                    acc |= attrmap.class_readers(amap, n)   # classes reached as dialect.tokenizer_class.X
                except Exception:
                    pass
            if tests:
                for i in tests:
                    acc |= 1 << i
            else:
                # no test is recorded as touching this class: fall back to every test that runs
                # a line of the file that defines it
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
    return acc | acc0


# ---------------------------------------------------------------- running

def _on_alarm(signum, frame):
    # a test can swallow the exception (inside subTest, or `except BaseException`) and go on: from
    # now on it is interrupted again every few hundredths of a CPU second until it ends
    signal.setitimer(signal.ITIMER_VIRTUAL, 0.03, 0.03)
    raise core.TestTimeout()


RSS_LIMIT = int(float(os.environ.get("BR_RSS_MB", "1300")) * 1024 * 1024)
RSS_LIMIT_BIG = int(2900 * 1024 * 1024)
# the one pinned test that needs a lot of memory by itself (about 1.7 GB: two million tokens)
BIG_MEMORY_TESTS = {"tests/test_parser.py::TestParser::test_max_nodes"}
DATA_LIMIT = int(float(os.environ.get("BR_DATA_MB", "3200")) * 1024 * 1024)
_rss_limit = [RSS_LIMIT]
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
    if rss > _rss_limit[0]:
        raise core.TestTimeout()


def run_commit(plan, ctx, wfd, mode, tmpdir):
    """mode "swap": forked from the warm process. mode "fresh": forked before sqlglot was imported."""
    def out(s):
        os.write(wfd, (s + "\n").encode())

    dn = os.open(os.devnull, os.O_RDWR)
    os.dup2(dn, 0); os.dup2(dn, 1)
    if os.environ.get("BR_DEBUG") != "1":
        os.dup2(dn, 2)
    try:
        # before any patched code runs (a fresh import included): one huge allocation cannot be
        # stopped by a timer, a limit on the data segment turns it into a MemoryError
        import resource
        resource.setrlimit(resource.RLIMIT_DATA, (DATA_LIMIT, DATA_LIMIT))
    except Exception:
        pass
    install_overlay(ctx.root, plan.data, tmpdir)
    install_sources(ctx.root, plan.py)
    failed_now = 0
    roots = None
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
        if roots and subst and not (plan.why - {"func"}):
            # Only function bodies changed (the fresh import is here because one of them could not
            # be given to the live function, or also runs at import). A changed function can still
            # show up as a root when the edit changed which outer variables it closes over
            # (`super()` gone: no __class__ cell any more). That is the same edit, and the tests
            # that run the function are candidates through its lines already.
            for r in list(roots):
                try:
                    o = _resolve(r[1]) if r[0] == "c" else sys.modules.get(r[1])
                    v = vars(o).get(r[2]) if o is not None else None
                    if isinstance(v, (staticmethod, classmethod)):
                        v = v.__func__
                    if isinstance(v, types.FunctionType) and statefp.code_digest(v.__code__) in subst:
                        roots.discard(r)
                except Exception:
                    pass
        if "sqlglot" not in sys.modules or len(roots) > ROOTS_CAP:
            # the package does not import, or the import state is unrecognisable (thousands of
            # roots, and mapping them to tests alone would take most of a minute): everything
            for i in bits(failed_now):
                out("F %d" % i)
            out("L")
            out("U " + " ".join(str(i) for i in range(len(ctx.meta)) if not (failed_now >> i) & 1))
            out("E")
            return
        if not roots and not plan.cands and plan.why - {"func"}:
            # code that runs at import changed, yet nothing that can be seen differs and no test
            # runs a changed function: do not conclude "harmless" from that
            for rel in plan.py:
                cands_extra = ctx.maps.tests_in_file(rel)
                plan.cands |= cands_extra
        out("N %d %s" % (len(roots), ";".join(sorted("%s|%s|%s" % k for k in roots))[:int(os.environ.get("BR_NLEN", "300"))]))
        cands = plan.cands | roots_to_tests(roots, ctx, plan, subst)
        if plan.lazy and ctx.lazy_idx is not None and any(r in ctx.bare for r in plan.py):
            cands |= 1 << ctx.lazy_idx
        cands &= ~failed_now
        modules = warm.modules
    for i in bits(failed_now):
        out("F %d" % i)
    cands &= ~plan.unrun
    cpu, secs, setup, meta, scale = ctx.cpu, ctx.seconds_ms, ctx.setup, ctx.meta, ctx.scale
    # the few giant tests can be decided unit by unit (units.py): what that is expected to cost
    uchanged, ucost = None, {}
    try:
        uchanged = units.changed_of(plan, mode, same_import_state=(mode != "swap" and not roots))
        for i in bits(cands):
            tid = "%s::%s::%s" % meta[i]
            if units.supported(tid):
                est = units.estimate(tid, uchanged)
                if est is not None and est < UNITS_GAIN * cpu[i]:
                    ucost[i] = est
    except Exception:
        ucost = {}
    if ucost:
        cpu = list(cpu)
        for i, est in ucost.items():
            cpu[i] = est
    order = sorted(bits(cands), key=lambda i: (cpu[i], i))
    out("L " + " ".join(str(i) for i in order))
    pol_cpu = cpu
    if ucost and UNITS_ALWAYS:
        pol_cpu = list(cpu)
        for i in ucost:
            pol_cpu[i] = 0.01
    # The TPC-DS run is the one test whose verdict costs more CPU than selecting it does when the
    # changed lines are in the executor: every query has to be executed again. When it is one
    # candidate among many small ones, the small ones are run, and it is left out if all of them
    # pass (see README, "the one calculated risk"). Any failure, any test not run: it is selected.
    giant = None
    if SENTINEL_MIN and mode == "swap" and not plan.data and not plan.unrun and not failed_now \
            and all(r.startswith("sqlglot/") for r in plan.py) and len(order) > SENTINEL_MIN and not TRUTH:
        for i in order:
            if i in ucost and "%s::%s::%s" % meta[i] == GIANT_TEST and ucost[i] * scale > GIANT_DEAR:
                giant = i
        if giant is not None and sum(ctx.cpu[i] for i in order if i != giant) > SENTINEL_CPU:
            giant = None      # the others are too dear to run them all for this: the usual rule
    st = _PolicyState([i for i in order if i != giant], failed_now | plan.unrun, bin(failed_now).count("1"),
                      ctx, pol_cpu)
    hung = set()
    truth_skip = TRUTH and sum(ctx.cpu[i] for i in order) > float(os.environ.get("BR_TRUTH_CAP", "1e9"))
    if truth_skip:
        out("N toobig")
    signal.signal(signal.SIGVTALRM, _on_alarm)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.signal(signal.SIGPROF, _on_prof)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_DATA, (DATA_LIMIT, DATA_LIMIT))
    except Exception:
        pass
    farm = None
    hard = getattr(ctx, "hard", None)        # time.monotonic() value after which nothing more is run
    allowance = COMMIT_OVERRUN * max(scale, 1.0)
    usual = 0.0
    t_begin = time.process_time()
    while st.rem:
        over = max(0.0, (time.process_time() - t_begin) - usual)
        if not TRUTH and (over >= allowance or (hard is not None and time.monotonic() > hard - 1.0)):
            out("N stopped: %.1fs over the usual time" % over)
            break
        try:
            act = policy.decide_next(st, POLICY_MODEL)
        except Exception:
            act = ("stop",)
        if truth_skip:
            act = ("stop",)
        elif TRUTH or giant is not None:
            # (offline labelling, or the rule above) every candidate, cheapest first, until one fails
            act = ("stop",) if (st.failed and not TRUTH_ALL) else ("run", min(st.rem, key=lambda j: (cpu[j], j)))
        if act[0] != "run":
            break
        i = act[1]
        st.rem.discard(i)
        key = meta[i][0] + "::" + meta[i][1]
        if key in hung:
            out("F %d" % i)
            st.failed.append(i)
            continue
        out("S %d" % i)
        c_usual = (cpu[i] if i in ucost else ctx.cpu[i]) + setup.get(key, 0.0)
        limit = test_limit(ctx.cpu[i] + setup.get(key, 0.0), scale)
        if i in ucost:   # deciding unit by unit must not cost more than selecting the test does
            limit = min(limit, (ctx.cpu[i] + TIMEOUT_SLACK) * max(scale, 1.0))
        limit = max(1.0, min(limit, c_usual * max(scale, 1.0) + (allowance - over)))
        if i in ucost and (UNITS_ALWAYS or TRUTH):
            limit = max(limit, UNITS_LIMIT)
        if TRUTH:
            limit = max(limit, 4 * test_limit(ctx.cpu[i] + setup.get(key, 0.0), scale))
        usual += c_usual * max(scale, 1.0)
        real = 4 * limit + 2
        if hard is not None:
            real = max(0.5, min(real, hard - time.monotonic()))
        cwd = None
        if i == ctx.lazy_idx and plan.py:
            if farm is None:
                farm = make_farm(ctx.root, plan.py, tmpdir)
            cwd = os.getcwd()
            os.chdir(farm)
        _rss_limit[0] = RSS_LIMIT_BIG if ("%s::%s::%s" % meta[i]) in BIG_MEMORY_TESTS else RSS_LIMIT
        signal.setitimer(signal.ITIMER_VIRTUAL, limit, limit / 2)
        signal.setitimer(signal.ITIMER_REAL, real)   # a test that waits instead of computing
        signal.setitimer(signal.ITIMER_PROF, 0.2, 0.2)
        timed_out = False
        try:
            verdict = "unknown"
            if i in ucost:
                verdict = units.decide("%s::%s::%s" % meta[i], uchanged, modules)
                if units.last.get("timeout"):
                    timed_out = True
                if verdict == "unknown" and ctx.cpu[i] > 3.0:
                    verdict = "fail"   # no cheap verdict: selected without one, a full run costs more
            if verdict == "unknown":
                bad = core.run_test("%s::%s::%s" % meta[i], modules)
            else:
                bad = verdict == "fail"
        except core.TestTimeout:
            bad = True
            timed_out = True
        finally:
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.setitimer(signal.ITIMER_PROF, 0)
            if cwd is not None:
                os.chdir(cwd)
        if core.last_timed_out():
            timed_out = True
        if timed_out:
            bad = True
            hung.add(key)
        if core.pool_trouble:
            # the tests of a class can share one worker pool (TestOptimizer.pool). In a real run a
            # task that never ends keeps its worker, and a worker that dies breaks the pool: every
            # later test of the class that uses the pool then fails too. They are selected unrun.
            try:
                tm = meta[i]
                mod = sys.modules.get(core.module_name(tm[0]))
                shared = getattr(getattr(mod, tm[1], None), "pool", None)
                if any(p is shared for p in core.pool_trouble):
                    hung.add(key)
                    for j, m in enumerate(meta):
                        if j != i and m[0] == tm[0] and m[1] == tm[1] and m[2] > tm[2] and j not in st.rem:
                            out("F %d" % j)
            except Exception:
                pass
            del core.pool_trouble[:]
        st.done_cls.add(key)
        if bad:
            out("F %d" % i)
            st.failed.append(i)
        else:
            out("P %d" % i)
            st.passed.append(i)
    if hung:
        # something never ended on this commit. A candidate that was not run can hang the same way,
        # and in a class whose tests share one worker pool that takes the later tests with it.
        try:
            import json
            shared = set(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", "pools.json"))))
        except Exception:
            shared = set()
        for key in shared:
            f, c = key.split("::")
            mine = [meta[i][2] for i in list(st.rem) + st.failed if meta[i][0] == f and meta[i][1] == c]
            if mine:
                first = min(mine)
                for j, m in enumerate(meta):
                    if m[0] == f and m[1] == c and m[2] > first and j not in st.rem:
                        out("F %d" % j)
    if os.environ.get("BR_MARK") == "1":     # measurement builds only
        try:
            g = [i for i in order if "%s::%s::%s" % meta[i] == GIANT_TEST]
            mk = None
            if g and giant is not None:
                mk = 1 | (2 if st.failed else 0) | (4 if st.rem else 0) | (8 if hung else 0) | (min(len(st.passed), 31) << 4)
            elif g and g[0] in st.rem and not st.failed:
                oc = sum(ctx.cpu[i] for i in order if i != g[0])
                mk = (2 if mode == "swap" else 0) | (4 if plan.data else 0) | (8 if plan.unrun else 0) \
                    | (16 if g[0] in ucost else 0) | (32 if g[0] in ucost and ucost[g[0]] * scale > GIANT_DEAR else 0) \
                    | (64 if len(order) > SENTINEL_MIN else 0) | (128 if oc <= SENTINEL_CPU else 0) \
                    | (256 if all(r.startswith("sqlglot/") for r in plan.py) and not failed_now else 0)
            if mk is not None:
                out("M %d" % mk)
        except Exception:
            pass
    if giant is not None:
        if st.rem or st.failed or hung or len(st.passed) < SENTINEL_MIN:
            st.rem.add(giant)
        else:
            out("N giant left out: the %d other tests on the changed lines pass" % len(st.passed))
    if st.rem:
        out("U " + " ".join(str(i) for i in sorted(st.rem)))
    out("E")


class _PolicyState:
    """What policy.decide_next sees: verdicts so far and the cost tables (CPU scaled to this machine)."""

    def __init__(self, order, forced_bits, n_forced_fail, ctx, cpu):
        self.rem = set(order)
        self.passed, self.failed = [], []
        self.forced = bits(forced_bits)
        self.n_forced_fail = n_forced_fail
        self.secs = ctx.seconds_ms
        self.meta = ctx.meta
        self.cls = _Cls(ctx.meta)
        self.cpu = _Scaled(cpu, ctx.scale)
        self.setup, self.scale = ctx.setup, ctx.scale
        self.done_cls = set()
        self.memo = {}

    def cost(self, i):
        k = self.cls[i]
        c = self.cpu[i]
        if k not in self.done_cls and core.class_state(self.meta[i]) is None:
            c += self.setup.get(k, 0.0) * self.scale
        return c


class _Cls:
    def __init__(self, meta):
        self.meta = meta

    def __getitem__(self, i):
        m = self.meta[i]
        return m[0] + "::" + m[1]


class _Scaled:
    def __init__(self, cpu, scale):
        self.cpu, self.scale = cpu, scale

    def __getitem__(self, i):
        return self.cpu[i] * self.scale

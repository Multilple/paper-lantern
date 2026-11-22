"""Exact verdicts for the few very expensive tests, without running them whole.

Such a test is a loop over independent units (one TPC query, one fixture entry, one statement).
From the pinned commit we keep, for every unit and every part ("phase") of it, the lines it runs
and a fingerprint of what the part hands on to the next part (maps/units/*.bin, built by
tools/trace_units.py).  On a patch:

  * a unit none of whose lines changed behaves as on the pinned commit: it passes, unrun;
  * the other units run their sqlglot parts for real on the patched code.  When what they hand on
    has the pinned fingerprint, the parts behind it that run no changed line are left out, because
    they would do what they did on the pinned commit (for the TPC tests: the Python executor, and
    the DuckDB query the result is compared with);
  * anything that differs is run for real with the test's own code, cheapest unit first, and the
    first failing unit ends the work: one failing unit fails the test.

decide() answers "pass" or "fail" only when that reasoning is complete and "unknown" otherwise.
"""
import enum
import hashlib
import os
import pickle
import sys
import time
import unittest
import zlib

import core

DIR = os.path.join(os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")),
                   "units")
FORMAT = 1


class Unknown(Exception):
    """A unit met something this module does not follow up: the verdict is "unknown"."""


class Cut(BaseException):
    """Leaves a unit at a point behind which everything is known to go as on the pinned commit."""


class _Pinned:
    """Stands for a value that was not computed because it is the pinned one."""

    def __repr__(self):
        return "<pinned>"


PINNED = _Pinned()


# ---------------------------------------------------------------- fingerprints

def seed_probe():
    """Hash values enter the fingerprints of syntax trees (a tree caches its hash). They can only
    be compared between processes that hash alike."""
    return [sys.version_info[:2], hash("blast radius"), hash(("a", 1, 2.5, None, True)), hash(1 << 70)]


class _Poison(Exception):
    pass


_PRIM = {str, int, float, bool, type(None), bytes}
_slot_cache = {}


def _slots(cls):
    s = _slot_cache.get(cls)
    if s is None:
        names = []
        for k in cls.__mro__:
            sl = k.__dict__.get("__slots__", ())
            if isinstance(sl, str):
                sl = (sl,)
            for n in sl:
                if n not in ("__dict__", "__weakref__") and n not in names:
                    names.append(n)
        s = _slot_cache[cls] = tuple(names)
    return s


def _dump(x, ap, memo, depth):
    """Writes every bit of state reachable from x in a canonical order: types, values, order of
    lists and dicts, shared and cyclic references, cached hashes."""
    tx = type(x)
    if tx is str:
        ap("s%d:%s" % (len(x), x))
    elif x is None:
        ap("N")
    elif x is True:
        ap("T")
    elif x is False:
        ap("F")
    elif tx is int:
        ap("i%d" % x)
    elif tx is float:
        ap("f" + repr(x))
    elif tx is bytes:
        ap("b" + x.hex())
    elif tx is list or tx is tuple:
        ap("[%d" % len(x) if tx is list else "(%d" % len(x))
        for y in x:
            _dump(y, ap, memo, depth + 1)
    elif tx is dict:
        ap("{%d" % len(x))
        for k, v in x.items():
            _dump(k, ap, memo, depth + 1)
            _dump(v, ap, memo, depth + 1)
    elif tx is set or tx is frozenset:
        parts = []
        for y in x:
            sub = []
            _dump(y, sub.append, {}, depth + 1)
            parts.append("\x01".join(sub))
        parts.sort()
        ap("<%d" % len(parts) if tx is set else "<f%d" % len(parts))
        for p in parts:
            ap(p)
    elif isinstance(x, enum.Enum):
        ap("e%s.%s.%s" % (tx.__module__, tx.__qualname__, x.name))
    elif isinstance(x, type):
        ap("t%s.%s" % (x.__module__, x.__qualname__))
    else:
        if depth > 400:
            raise _Poison()
        mod = tx.__module__
        if not (mod == "sqlglot" or mod.startswith("sqlglot.")) and mod not in ("datetime", "decimal"):
            raise _Poison()          # an object we do not know how to look into
        if mod in ("datetime", "decimal"):
            ap("r%s.%s:%r" % (mod, tx.__qualname__, x))
            return
        n = memo.get(id(x))
        if n is not None:
            ap("#%d" % n)
            return
        memo[id(x)] = len(memo)
        ap("o%s.%s" % (mod, tx.__qualname__))
        for name in _slots(tx):
            try:
                v = getattr(x, name)
            except AttributeError:
                ap("U")
                continue
            if name == "parent" and v is not None:
                # the parent was met on the way down, or the tree is not a tree
                pn = memo.get(id(v))
                if pn is None:
                    raise _Poison()
                ap("^%d" % pn)
            else:
                _dump(v, ap, memo, depth + 1)
        d = getattr(x, "__dict__", None)
        if d is not None:
            ap("{%d" % len(d))
            for k, v in d.items():
                _dump(k, ap, memo, depth + 1)
                _dump(v, ap, memo, depth + 1)


def _digest(parts):
    return hashlib.blake2b("\x00".join(parts).encode("utf-8", "surrogatepass"), digest_size=10).digest()


def fp_state(x):
    """Fingerprint of an object graph (syntax trees). None when it cannot be taken exactly."""
    out = []
    try:
        _dump(x, out.append, {}, 0)
    except (_Poison, RecursionError):
        return None
    return _digest(out)


_ROW_TYPES = None


def fp_table(t):
    """Fingerprint of what the test reads from an executor result: its columns and rows."""
    global _ROW_TYPES
    if _ROW_TYPES is None:
        import datetime
        import decimal
        _ROW_TYPES = {str, int, float, bool, type(None), datetime.date, datetime.datetime, datetime.time,
                      decimal.Decimal}
    try:
        cols, rows = t.columns, t.rows
        if type(cols) is not tuple or type(rows) is not list:
            return None
        if any(type(c) is not str for c in cols):
            return None
        seen = set()
        for r in rows:
            if type(r) is not tuple:
                return None
            seen.update(map(type, r))
        if not seen <= _ROW_TYPES:
            return None
        body = repr((type(t).__module__, type(t).__qualname__, cols, rows))
    except Exception:
        return None
    return hashlib.blake2b(body.encode("utf-8", "surrogatepass"), digest_size=10).digest()


def fp_objects(xs):
    """Fingerprint of a list of flat objects of one slotted class (tokens), column by column."""
    try:
        if type(xs) is not list:
            return None
        if not xs:
            return _digest(["[0"])
        kinds = set(map(type, xs))
        if len(kinds) != 1:
            return None
        cls = kinds.pop()
        names = _slots(cls)
        if not names or hasattr(xs[0], "__dict__"):
            return None
        if len(set(map(id, xs))) != len(xs):
            return None                  # the same object twice: not a plain list of tokens
        h = hashlib.blake2b(digest_size=10)
        h.update(("%s.%s|%d|%s" % (cls.__module__, cls.__qualname__, len(xs), ",".join(names))).encode())
        import operator
        for name in names:
            col = list(map(operator.attrgetter(name), xs))
            types = set(map(type, col))
            if types == {str}:
                h.update(b"S" + "\x00".join(col).encode("utf-8", "surrogatepass"))
                h.update(repr(list(map(len, col))).encode() if any("\x00" in c for c in col) else b"")
            elif types == {int}:
                h.update(b"I" + repr(col).encode())
            elif len(types) == 1 and issubclass(next(iter(types)), enum.Enum):
                seen = {}
                ids = list(map(id, col))
                for k, v in zip(ids, col):
                    if k not in seen:
                        seen[k] = "%s.%s.%s" % (type(v).__module__, type(v).__qualname__, v.name)
                order = {k: n for n, k in enumerate(seen)}
                h.update(b"E" + "\x00".join(seen.values()).encode() + repr([order[k] for k in ids]).encode())
            elif types == {list} and not any(col):
                h.update(b"L0")          # all empty lists
            else:
                out = []
                for v in col:
                    _dump(v, out.append, {}, 0)
                h.update(b"G" + "\x00".join(out).encode("utf-8", "surrogatepass"))
        return h.digest()
    except (_Poison, RecursionError, AttributeError):
        return None


def fp_text(s):
    if type(s) is not str:
        return None
    return hashlib.blake2b(b"s" + s.encode("utf-8", "surrogatepass"), digest_size=10).digest()


def fp_value(x):
    """Fingerprint of plain data (strings, numbers, lists, dicts of them)."""
    return fp_state(x)


# ---------------------------------------------------------------- the map of one test

class UnitMap:
    """maps/units/<name>.bin: for every line of the pinned files the (unit, phase) pairs that run it."""

    def __init__(self, raw):
        self.raw = raw
        self.test = raw["test"]
        self.phases = raw["phases"]
        self.nph = len(self.phases)
        self.n = raw["n"]
        self.fps = raw["fps"]            # unit -> {name: digest}
        self.cpu = raw["cpu"]            # unit -> {phase: seconds}
        self.inputs = raw["inputs"]      # unit -> digest of its input, or None
        self.files = raw["files"]        # relpath -> {line: index into table}
        self.table = [int.from_bytes(b, "little") for b in raw["table"]]
        self.setup = raw["setup"]        # relpath -> set of lines run outside the units
        self.warm_only = raw.get("warm_only", {})   # relpath -> lines some unit runs only after others ran
        self.opened = set(raw.get("opened", ()))    # data files the test reads
        self.pure = set(raw.get("pure", ()))        # phases that run no sqlglot line at all
        self.extra = raw.get("extra", {})
        self.hash_ok = raw.get("probe") == seed_probe()

    def hits(self, lines):
        """bitmap over unit * nph + phase of the parts that run any of the changed lines"""
        acc = 0
        for rel, ls in lines.items():
            m = self.files.get(rel)
            if not m:
                continue
            for l in ls:
                j = m.get(l)
                if j is not None:
                    acc |= self.table[j]
        return acc

    def touches(self, lines, which):
        for rel, ls in lines.items():
            s = which.get(rel)
            if s and not s.isdisjoint(ls):
                return True
        return False


_maps = {}
_index = None


def index():
    global _index
    if _index is None:
        try:
            with open(os.path.join(DIR, "index.pkl"), "rb") as f:
                _index = pickle.load(f)
            if _index.get("format") != FORMAT:
                _index = {"tests": {}}
        except Exception:
            _index = {"tests": {}}
    return _index


def supported(test_id):
    return test_id in index()["tests"]


def load(test_id):
    m = _maps.get(test_id)
    if m is None:
        name = index()["tests"][test_id]["file"]
        with open(os.path.join(DIR, name), "rb") as f:
            m = UnitMap(pickle.loads(zlib.decompress(f.read())))
        _maps[test_id] = m
    return m


# ---------------------------------------------------------------- what a patch changed

class Changed:
    """lines: relpath -> pinned line numbers of the changed instructions (function bodies only).
    full:  the import state may differ (class bodies, module level): line sets say nothing.
    data:  relpaths of changed files that are read as data.
    tests: a python file under tests/ changed."""

    def __init__(self, lines=None, full=False, data=(), tests=False):
        self.lines = {k: set(v) for k, v in (lines or {}).items()}
        self.full = full
        self.data = set(data)
        self.tests = tests


def changed_of(plan, mode="swap", same_import_state=False):
    """The engine's plan of a commit -> Changed.
    mode "swap": the child was forked from the warm process and only function code was replaced.
    Otherwise the patched tree was imported afresh; the changed lines of function bodies then only
    mean something when the import state has the pinned fingerprint (same_import_state)."""
    import codediff
    lines = {}
    full = mode != "swap" and not same_import_state
    tests = False
    for rel in plan.py:
        if not rel.startswith("sqlglot/"):
            tests = True
        if rel not in plan.trees:
            full = True          # could not be compared function by function
    for rel, tree in plan.trees.items():
        for node in codediff.walk(tree):
            if not node.own_changed:
                continue
            if not node.is_function:
                full = True
            else:
                lines.setdefault(rel, set()).update(node.lines)
    return Changed(lines, full, set(plan.data), tests)


# ---------------------------------------------------------------- control of one run

class Control:
    """Tells a unit which of its parts a patch can reach, and compares what it hands on."""

    tracing = False

    def __init__(self, umap, changed):
        self.m = umap
        self.all = changed.full
        self.cuts = not changed.full and umap.hash_ok
        self.bits = 0 if changed.full else umap.hits(changed.lines)
        if not changed.full and umap.touches(changed.lines, umap.setup):
            self.all = True       # state made outside the units may differ: every unit, no short cut
            self.cuts = False
        self.order_dependent = (not changed.full) and umap.touches(changed.lines, umap.warm_only)
        self.unit = 0
        self.fresh_input = False
        self.stats = {"skipped": 0, "ran": 0, "cut": 0, "real": 0}

    def begin(self, unit, fresh_input=False):
        self.unit = unit
        self.fresh_input = fresh_input

    def hit(self, *phases):
        if self.fresh_input:
            return True
        if self.all:
            # a phase that runs no sqlglot line cannot notice a change of sqlglot
            pure = self.m.pure
            for p in phases:
                if p not in pure:
                    return True
            return False
        b = self.bits >> (self.unit * self.m.nph)
        ph = self.m.phases
        for p in phases:
            if (b >> ph.index(p)) & 1:
                return True
        return False

    def unit_hit(self, unit):
        if self.all:
            return True
        return bool((self.bits >> (unit * self.m.nph)) & ((1 << self.m.nph) - 1))

    def same(self, name, value, fp=fp_state):
        """Does this value have the pinned fingerprint?"""
        if self.fresh_input:
            return False
        want = self.m.fps[self.unit].get(name)
        if want is None:
            return False
        return fp(value) == want

    def can_cut(self):
        return self.cuts and not self.fresh_input

    def enter(self, phase):
        pass


# ---------------------------------------------------------------- kinds of tests

def _roundtrip(x):
    return pickle.loads(pickle.dumps(x))


_patched = []      # (object, attribute, original): what is replaced while a unit runs


def _patch(obj, name, new):
    _patched.append((obj, name, obj.__dict__[name] if isinstance(obj, type) else getattr(obj, name)))
    setattr(obj, name, new)


def _unpatch(upto=0):
    """Puts back everything replaced since the list had `upto` entries (also after an exception that
    arrived in the middle of a unit, such as a timeout)."""
    while len(_patched) > upto:
        obj, name, old = _patched.pop()
        try:
            setattr(obj, name, old)
        except Exception:
            pass


def _session(test_file):
    """The state core.run_test gives a test before it starts (the dialect registry of a real session)."""
    f = getattr(core, "session_state", None)
    if f is not None:
        f(test_file)


class Kind:
    """One supported test. Subclasses say what its units are and how one of them runs."""

    phases = ()
    data_files = ()       # data files whose change we can follow unit by unit
    ordered = False       # units must keep the test's order

    def use_map(self, umap):
        """Checks the map against what this process sees. False: the map does not apply."""
        return True

    def __init__(self, test_id):
        self.test_id = test_id
        self.file, self.cls_name, self.method = core.split_id(test_id)
        self.cls = None
        self.mod = None
        self.case = None

    # -- preparation
    def prepare(self, modules=None):
        """The class with its setUpClass done, as core.run_test would have it. -> "ok" | "fail" | "unknown" """
        _session(self.file)
        mod = modules[self.file] if modules is not None and self.file in modules else \
            sys.modules.get(core.module_name(self.file))
        if mod is None:
            return "unknown"
        cls = getattr(mod, self.cls_name, None)
        if cls is None or getattr(cls, "__unittest_skip__", False):
            return "unknown"
        fn = getattr(cls, self.method, None)
        if fn is None or getattr(fn, "__unittest_skip__", False) or getattr(fn, "__unittest_expecting_failure__", False):
            return "unknown"
        for name in ("setUp", "tearDown", "run", "doCleanups", "subTest"):
            if getattr(cls, name) is not getattr(unittest.TestCase, name):
                return "unknown"
        if core.prepare_class(self.test_id, modules) is None:
            return "fail"
        self.cls, self.mod = cls, mod
        self.case = cls(self.method)
        return "ok"

    def unit_inputs(self):
        """digest of the input of every unit as the patched data files give it (None: no data input)"""
        return None

    def preamble(self, ctl):
        """What the test does before its first unit. Raises when the test would fail there."""

    def run_unit(self, i, ctl):
        """True when unit i passes."""
        raise NotImplementedError

    def cost(self, i, ctl, umap):
        """CPU seconds unit i is expected to take when it passes."""
        return sum(umap.cpu[i].values())


class TpcExecute(Kind):
    """TestExecutor.test_execute_tpch / test_execute_tpcds: one unit per TPC query.

    The test parses the query, sends it through pickle to mp_execute, which runs
    sqlglot.executor.execute on it, and compares the result with DuckDB's for the query transpiled
    to duckdb.  Phases of a unit:
      A    parse_one(sql) and the trip through pickle
      PRE  mp_execute and execute() up to the call of optimize
      O    optimize(...)
      G    from the return of optimize to the call of Plan
      P    Plan(expression)
      E    the Python executor, to the end of mp_execute, and the trip of the result through pickle
      D    transpile(sql, write="duckdb")
      C    the rest of subtestHelper (the DuckDB query and the comparison)
    Fingerprints: A the parsed tree after pickle, O the tree handed to Plan, T the result table
    (columns and rows), D the duckdb text.
    """

    phases = ("A", "PRE", "O", "G", "P", "E", "D", "C")

    def __init__(self, test_id, tpch):
        super().__init__(test_id)
        self.tpch = tpch
        # setUpClass reads both fixtures and really runs here; the units read the list of their own suite
        self.data_files = ("tests/fixtures/optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql")

    def sqls(self):
        return self.cls.tpch_sqls if self.tpch else self.cls.tpcds_sqls

    def count(self):
        return len(self.sqls())

    def unit_inputs(self):
        return [fp_value([meta, sql]) for meta, sql, _ in self.sqls()]

    def preamble(self, ctl):
        ctl.enter("S")
        mod, cls = self.mod, self.cls
        schema = mod.TPCH_SCHEMA if self.tpch else mod.TPCDS_SCHEMA
        tables = cls.tpch_tables if self.tpch else cls.tpcds_tables
        mod.initializer(*_roundtrip((schema, tables)))

    def shared(self):
        """Fingerprint of the data all units read: the tables and the schema the initializer set.
        A unit that changed them would be felt by the units that are left out."""
        mod = self.mod
        try:
            tables, schema = mod._tables, mod._schema
            h = hashlib.blake2b(digest_size=10)
            fs = fp_value(schema)
            if fs is None:
                return None
            h.update(fs)
            for name in sorted(tables):
                t = tables[name]
                ft = fp_table(t)
                if ft is None:
                    return None
                h.update(name.encode() + ft + repr(t.column_range).encode())
            return h.digest()
        except Exception:
            return None

    def _execute(self, fn, args, kw, ctl, cut):
        """mp_execute(expression, meta) and the trip of its result through pickle; with `cut`, leaves at
        the call of Plan when the tree handed to it is the pinned one."""
        ex = sys.modules.get("sqlglot.executor")
        real_opt = getattr(ex, "optimize", None)
        real_plan = getattr(ex, "Plan", None)
        wrap = ex is not None and real_opt is not None and real_plan is not None and (cut or ctl.tracing)
        if wrap:
            def w_opt(*a, **k):
                ctl.enter("O")
                try:
                    return real_opt(*a, **k)
                finally:
                    ctl.enter("G")

            def w_plan(*a, **k):
                tree = a[0] if a else k.get("expression")
                if ctl.same("O", tree) and cut:
                    raise Cut()
                ctl.enter("P")
                try:
                    return real_plan(*a, **k)
                finally:
                    ctl.enter("E")

        mark = len(_patched)
        if wrap:
            _patch(ex, "optimize", w_opt)
            _patch(ex, "Plan", w_plan)
        try:
            ctl.enter("PRE")
            r = fn(*args, **kw)
            ctl.enter("E")
            return _roundtrip(r)
        except Cut:
            return PINNED
        finally:
            _unpatch(mark)

    def run_unit(self, i, ctl):
        mod = self.mod
        meta, sql, _ = self.sqls()[i]
        executes = bool(meta.get("execute"))
        hit = ctl.hit
        r = PINNED
        fn = None
        back = hit("PRE", "O", "G", "P", "E", "C")
        if hit("A") or back:
            ctl.enter("A")
            e = mod.parse_one(sql)
            fn, args, kw = _roundtrip((mod.mp_execute, (e, meta), {}))
            same_a = True
            if executes and hit("A"):
                same_a = ctl.same("A", args[0])
            if back or not same_a:
                cut = same_a and ctl.can_cut() and not hit("PRE", "P", "E", "C")
                r = self._execute(fn, args, kw, ctl, cut)
                ctl.stats["cut" if r is PINNED else "real"] += 1
        if r is None or (r is PINNED and not executes):
            return True               # a query the test does not execute: it only has to parse
        same_t = r is PINNED or ctl.same("T", r, fp_table)
        same_d = True
        if hit("D"):
            ctl.enter("D")
            same_d = ctl.same("D", mod.transpile(sql, write="duckdb")[0], fp_text)
        if same_t and same_d and not hit("C") and not ctl.tracing:
            return True               # DuckDB would get the pinned text and be compared with the pinned rows
        if r is PINNED:
            if fn is None:
                ctl.enter("A")
                e = mod.parse_one(sql)
                fn, args, kw = _roundtrip((mod.mp_execute, (e, meta), {}))
            r = self._execute(fn, args, kw, ctl, False)
            if r is None:
                return True
        ctl.enter("C")
        real_tr = mod.transpile
        mark = len(_patched)
        if ctl.tracing:
            def w_tr(*a, **k):       # cached_execute transpiles the query: those lines belong to D
                ctl.enter("D")
                try:
                    return real_tr(*a, **k)
                finally:
                    ctl.enter("C")
            _patch(mod, "transpile", w_tr)
        try:
            self.case.subtestHelper(i, r, tpch=self.tpch)
        finally:
            _unpatch(mark)
        return True

    def cost(self, i, ctl, umap):
        c = umap.cpu[i]
        ctl.begin(i)
        hit = ctl.hit
        t = 0.0
        back = hit("PRE", "O", "G", "P", "E", "C")
        if hit("A") or back:
            t += c.get("A", 0.0) + c.get("F", 0.0)
            if back:
                t += c.get("PRE", 0.0) + c.get("O", 0.0) + c.get("G", 0.0)
                if not ctl.can_cut() or hit("PRE", "P", "E", "C"):
                    t += c.get("P", 0.0) + c.get("E", 0.0)
        if hit("D"):
            t += c.get("D", 0.0)
        if hit("C"):
            t += c.get("C", 0.0)
        return t


class TpchOptimized(Kind):
    """TestExecutor.test_optimized_tpch: for every TPC-H query the test runs the query and its optimized
    form (both from the fixture) through DuckDB, each transpiled to duckdb, and compares the two results.
    sqlglot only produces the two duckdb texts.  Phases: D1 transpile(sql), D2 transpile(optimized);
    fingerprints: the two texts.  With both texts pinned, DuckDB gets what it got on the pinned commit.
    A text that differs is not followed up here: the answer is "unknown" and the test runs as a whole.
    """

    phases = ("D1", "D2")
    fixture = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
    verify_whole = True

    def __init__(self, test_id):
        super().__init__(test_id)
        self.data_files = (self.fixture, "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql")

    def count(self):
        return len(self.cls.tpch_sqls)

    def unit_inputs(self):
        return [fp_value([sql, optimized]) for _, sql, optimized in self.cls.tpch_sqls]

    def run_unit(self, i, ctl):
        mod = self.mod
        _, sql, optimized = self.cls.tpch_sqls[i]
        same = True
        if ctl.hit("D1"):
            ctl.enter("D1")
            same = ctl.same("D1", mod.transpile(sql, write="duckdb")[0], fp_text) and same
        if ctl.hit("D2"):
            ctl.enter("D2")
            same = ctl.same("D2", mod.transpile(optimized, write="duckdb")[0], fp_text) and same
        if not same:
            raise Unknown("a duckdb text differs in unit %d" % i)
        return True

    def cost(self, i, ctl, umap):
        c = umap.cpu[i]
        ctl.begin(i)
        return sum(c.get(p, 0.0) for p in self.phases if ctl.hit(p)) + c.get("F", 0.0)


class Statements(Kind):
    """A straight-line test (TestParser.test_max_nodes): the top-level statements of its body.

    A statement that runs no sqlglot line is "pure" (it builds strings, lists): it always runs.
    The other statements are grouped into units by the names they hand to each other; a unit runs
    for real unless no changed line is in it.  Every unit keeps its place in the test's order.
    Phases of a unit:
      T   the calls of Dialect.tokenize (sql -> tokens)
      P   everything else
    Fingerprints: T<k> the tokens the k-th tokenize call of the unit returns.  When only T lines
    changed and the last tokenize call of the unit returns the pinned tokens (as did the ones before),
    the rest of the unit, parser and assertions, goes as on the pinned commit.
    """

    phases = ("T", "P")
    ordered = True

    def __init__(self, test_id):
        super().__init__(test_id)
        self.cursor = 0
        self.ns = None
        self.stmts = None
        self.tags = None       # statement -> None (pure) | unit number
        self.spec = None

    # -- the statements of the test function, from its source
    def source(self):
        import ast
        path = os.path.join(core.ROOT, self.file)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        tree = ast.parse(text, path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == self.cls_name:
                for fn in node.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name == self.method:
                        if fn.decorator_list or [a.arg for a in fn.args.args] != ["self"] or fn.args.vararg \
                                or fn.args.kwarg or fn.args.kwonlyargs:
                            return None, None
                        for st in fn.body:
                            for sub in ast.walk(st):
                                if isinstance(sub, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await, ast.Global,
                                                    ast.Nonlocal)):
                                    return None, None
                        return fn.body, fp_text(ast.dump(fn))
        return None, None

    def prepare(self, modules=None):
        st = super().prepare(modules)
        if st != "ok":
            return st
        body, digest = self.source()
        if body is None:
            return "unknown"
        self.stmts = body
        self.src_digest = digest
        self.ns = dict(self.mod.__dict__)
        self.ns["self"] = self.case
        self.cursor = 0
        return "ok"

    def use_map(self, umap):
        spec = umap.extra.get("outside") or {}
        if spec.get("src") != self.src_digest or len(spec.get("tags", ())) != len(self.stmts):
            return False
        self.spec = spec
        self.tags = spec["tags"]
        return True

    def count(self):
        return 1 + max((t for t in self.tags if t is not None), default=-1)

    # -- offline: which statements form which unit
    def derive(self, enter):
        """Runs every statement once, in order. enter(k) tells the tracer that statement k starts;
        it returns, called with None, the set of statements that ran sqlglot lines.
        -> {"src", "tags"} or None when the test cannot be cut into units."""
        import ast
        n = len(self.stmts)
        stores, loads = [], []
        for st in self.stmts:
            s_, l_ = set(), set()
            for sub in ast.walk(st):
                if isinstance(sub, ast.Name):
                    (s_ if isinstance(sub.ctx, (ast.Store, ast.Del)) else l_).add(sub.id)
                elif isinstance(sub, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    s_.add(sub.name)
                elif isinstance(sub, ast.alias):
                    s_.add((sub.asname or sub.name).split(".")[0])
                elif isinstance(sub, ast.ExceptHandler) and sub.name:
                    s_.add(sub.name)
                elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                    (s_ if isinstance(sub.ctx, (ast.Store, ast.Del)) else l_).add("self." + sub.attr)
            stores.append(s_)
            loads.append(l_)
        local = set().union(*stores) if stores else set()
        frozen = (str, int, float, bool, bytes, type(None), frozenset)
        touched = []
        for k in range(n):
            enter(k)
            self._exec(k)
            # a statement that runs sqlglot code may change the objects it was given
            touched.append({x for x in loads[k] if x in local and not isinstance(self.ns.get(x), frozen)})
        impure = enter(None)
        parent = {}

        def find(u):
            while parent[u] != u:
                parent[u] = parent[parent[u]]
                u = parent[u]
            return u

        owner = {}
        raw = [None] * n
        for k in range(n):
            deps = {owner[x] for x in loads[k] if owner.get(x) is not None}
            if k in impure or deps:
                parent[k] = k
                for d in deps:
                    parent[find(d)] = k
                raw[k] = k
                for x in stores[k] | (touched[k] if k in impure else set()):
                    owner[x] = k
            else:
                for x in stores[k]:
                    owner[x] = None
        roots = {}
        tags = []
        for k in range(n):
            if raw[k] is None:
                tags.append(None)
            else:
                r = find(k)
                tags.append(r)
        order = []
        for t in tags:
            if t is not None and t not in order:
                order.append(t)
        tags = [None if t is None else order.index(t) for t in tags]
        # units must not interleave
        seen_last = -1
        for u in range(len(order)):
            ks = [k for k, t in enumerate(tags) if t == u]
            if ks[0] < seen_last:
                return None
            seen_last = ks[-1]
        return {"src": self.src_digest, "tags": tags}

    def _exec(self, k):
        import ast
        st = self.stmts[k]
        code = compile(ast.Module(body=[st], type_ignores=[]), os.path.join(core.ROOT, self.file), "exec")
        exec(code, self.ns)

    def run_unit(self, i, ctl):
        tags = self.tags
        last_stmt = max(k for k, t in enumerate(tags) if t == i)
        tok_calls = self.spec["tcalls"][i]
        cut = ctl.can_cut() and not ctl.hit("P") and tok_calls > 0 and self.spec["cut_ok"][i]
        dialect_mod = sys.modules.get("sqlglot.dialects.dialect")
        D = getattr(dialect_mod, "Dialect", None)
        real = D.__dict__.get("tokenize") if D is not None else None
        wrap = real is not None and (cut or ctl.tracing)
        state = {"n": 0, "same": True}
        mark = len(_patched)
        if wrap:
            def tokenize(*a, **k):
                ctl.enter("T")
                try:
                    r = real(*a, **k)
                finally:
                    ctl.enter("P")
                n = state["n"]
                state["n"] = n + 1
                if not ctl.same("T%d" % n, r, fp_objects):
                    state["same"] = False
                if cut and state["same"] and n == tok_calls - 1:
                    raise Cut()
                return r
            tokenize.__wrapped__ = real
            _patch(D, "tokenize", tokenize)
        try:
            while self.cursor <= last_stmt:
                k = self.cursor
                self.cursor += 1
                t = tags[k]
                if t is None:
                    ctl.enter("-")
                    self._exec(k)             # pure: always runs
                elif t == i:
                    ctl.enter("P")
                    self._exec(k)
                # statements of units that are left out are passed over
        except Cut:
            self.cursor = last_stmt + 1
            ctl.stats["cut"] += 1
            return True
        finally:
            _unpatch(mark)
        ctl.stats["real"] += 1
        return True

    def cost(self, i, ctl, umap):
        c = umap.cpu[i]
        ctl.begin(i)
        if ctl.hit("P") or not ctl.can_cut() or not self.spec or not self.spec["cut_ok"][i]:
            return sum(c.values()) - c.get("F", 0.0)
        return c.get("T", 0.0) + c.get("F", 0.0)


def _describe(f):
    """A function by where it is defined (a partial: with its arguments)."""
    import functools
    if isinstance(f, functools.partial):
        return ["partial", _describe(f.func), list(f.args), dict(f.keywords or {})]
    return ["%s.%s" % (getattr(f, "__module__", "?"), getattr(f, "__qualname__", repr(type(f))))]


class CheckFile(Kind):
    """A TestOptimizer test that calls self.check_file(file, func, ...): one unit per fixture entry.

    The real test runs (core.run_test), with check_file replaced for the time being: for every entry
    of the fixture that a changed line can reach, or whose text changed, it calls the test's own
    check_file with only=<title of the entry>, which runs exactly that entry; the other entries are
    left out.  Everything else in the test body runs as it is.  One phase: U, the whole entry.
    Entries are left out only when the arguments of the check_file call have the pinned fingerprint.
    """

    phases = ("U",)
    whole = True

    def __init__(self, test_id):
        super().__init__(test_id)
        self.data_files = ()

    def prepare(self, modules=None):
        # setUp/tearDown of the class run inside the real test run; only the class itself is needed here
        _session(self.file)
        mod = modules[self.file] if modules is not None and self.file in modules else \
            sys.modules.get(core.module_name(self.file))
        if mod is None:
            return "unknown"
        cls = getattr(mod, self.cls_name, None)
        if cls is None or getattr(cls, "__unittest_skip__", False) or "check_file" not in cls.__dict__:
            return "unknown"
        fn = getattr(cls, self.method, None)
        if fn is None or getattr(fn, "__unittest_skip__", False) or getattr(fn, "__unittest_expecting_failure__", False):
            return "unknown"
        if core.prepare_class(self.test_id, modules) is None:
            return "fail"
        self.cls, self.mod, self.modules = cls, mod, modules
        return "ok"

    def run_whole(self, policy):
        """Runs the test with check_file under `policy`. -> True when the test fails (as core.run_test)."""
        cls, mod = self.cls, self.mod
        orig = cls.__dict__["check_file"]
        real_load = mod.load_sql_fixture_pairs
        memo = {}

        def load(filename):
            if filename not in memo:
                memo[filename] = list(real_load(filename))
            return iter(memo[filename])

        state = {"call": 0}

        def check_file(case, file, func, pretty=False, execute=None, only=None, **kwargs):
            c = state["call"]
            state["call"] += 1
            entries = None
            if only is None:
                try:
                    entries = list(load("optimizer/%s.sql" % file))
                except Exception:
                    entries = None
            args = [file, _describe(func), pretty, execute, kwargs]
            plan = policy.call(c, file, entries, args) if entries is not None else None
            if plan is None:
                policy.whole_call(c)
                return orig(case, file, func, pretty=pretty, execute=execute, only=only, **kwargs)
            for j, (meta, sql, expected) in enumerate(entries, start=1):
                title = meta.get("title") or "%d, %s" % (j, sql)
                policy.unit(plan, j - 1, [meta, sql, expected],
                            lambda title=title: orig(case, file, func, pretty=pretty, execute=execute, only=title,
                                                     **kwargs))

        mark = len(_patched)
        _patch(cls, "check_file", check_file)
        _patch(mod, "load_sql_fixture_pairs", load)
        try:
            return core.run_test(self.test_id, self.modules)
        finally:
            _unpatch(mark)


class _FilePolicy:
    """decide(): which entries of a check_file call run."""

    def __init__(self, umap, ctl):
        self.umap, self.ctl = umap, ctl
        self.calls = (umap.extra.get("outside") or {}).get("calls", [])
        self.ran = self.left = 0
        self.unknown = None
        self.seen = 0

    def call(self, c, file, entries, args):
        self.seen += 1
        if c >= len(self.calls) or self.ctl.all:
            return None
        spec = self.calls[c]
        if spec["file"] != file or spec["n"] != len(entries):
            return None
        fp = fp_state(args)
        return {"base": spec["base"], "same_args": fp is not None and fp == spec["args"]}

    def whole_call(self, c):
        self.ran += 1

    def unit(self, plan, j, inp, run):
        u = plan["base"] + j
        ctl = self.ctl
        if plan["same_args"] and not ctl.unit_hit(u) and fp_value(inp) == self.umap.inputs[u]:
            self.left += 1
            return
        self.ran += 1
        run()


KINDS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": lambda t: TpcExecute(t, False),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": lambda t: TpcExecute(t, True),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": TpchOptimized,
    "tests/test_parser.py::TestParser::test_max_nodes": Statements,
}


# ---------------------------------------------------------------- memory guard

MEM_EXTRA_KB = int(os.environ.get("BR_MEM_EXTRA_KB", str(1536 * 1024)))


class MemGuard:
    """A patch can make a query produce a huge intermediate result (seen: > 5 GB). While the guard is
    on, a profiling timer looks at the memory of the process a few times per CPU second and ends the
    work with core.TestTimeout once it grew by more than MEM_EXTRA_KB: the test counts as hanging."""

    def __init__(self, extra_kb=None, every=0.2):
        self.extra = MEM_EXTRA_KB if extra_kb is None else extra_kb
        self.every = every
        self.old = None
        self.tripped = False

    @staticmethod
    def rss_kb():
        try:
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
        except Exception:
            return 0

    def __enter__(self):
        import signal
        try:
            cur = signal.getsignal(signal.SIGPROF)
            if callable(cur) and signal.getitimer(signal.ITIMER_PROF)[0] > 0:
                return self          # the caller already watches the memory (engine._on_prof)
        except Exception:
            pass
        limit = self.rss_kb() + self.extra

        def check(signum, frame):
            if self.rss_kb() > limit:
                self.tripped = True
                signal.setitimer(signal.ITIMER_PROF, 0)
                raise core.TestTimeout()

        try:
            self.old = signal.signal(signal.SIGPROF, check)
            signal.setitimer(signal.ITIMER_PROF, self.every, self.every)
        except Exception:
            self.old = None
        return self

    def __exit__(self, *a):
        import signal
        try:
            signal.setitimer(signal.ITIMER_PROF, 0)
            if self.old is not None:
                signal.signal(signal.SIGPROF, self.old)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- the verdict

last = {}      # what the last decide() did, for logs


def _gate(test_id, changed):
    """-> (kind, map, None) or (None, None, reason)"""
    if test_id not in KINDS or not supported(test_id):
        return None, None, "not a supported test"
    if changed.tests:
        return None, None, "a test file changed"
    try:
        umap = load(test_id)
    except Exception as e:
        return None, None, "no map: %r" % (e,)
    kind = KINDS[test_id](test_id)
    for rel in changed.data:
        if rel in umap.opened and rel not in kind.data_files and rel not in umap.extra.get("data_files", ()):
            return None, None, "data file %s changed" % rel
    return kind, umap, None


def estimate(test_id, changed):
    """Expected CPU seconds of decide() when every unit passes, or None when decide() would not answer.
    Uses the maps only."""
    kind, umap, why = _gate(test_id, changed)
    if kind is None:
        return None
    if any(rel in kind.data_files or rel in umap.extra.get("data_files", ()) for rel in changed.data) \
            and not changed.full and not changed.lines:
        # a fixture edit: the entries whose text changed run whole; guess two of the dearest
        return umap.extra.get("preamble_cpu", 0.0) + 2 * max(sum(c.values()) for c in umap.cpu)
    ctl = Control(umap, changed)
    if ctl.order_dependent:
        ctl.all = True
        ctl.cuts = False
    kind.spec = umap.extra.get("outside")
    total = umap.extra.get("preamble_cpu", 0.0)
    for i in range(umap.n):
        if ctl.unit_hit(i):
            total += kind.cost(i, ctl, umap)
    return total


def decide(test_id, changed, modules=None, budget=None):
    """-> "pass" | "fail" | "unknown".  Runs inside the child of a commit, the patch already in place.
    budget: CPU seconds after which the answer is "unknown" (checked between units).
    A core.TestTimeout raised by the caller's timer, or by the memory guard, gives "fail" with
    last["timeout"] set: the test hangs (or eats the machine) on this patch."""
    global last
    last = {"test": test_id}
    mark = len(_patched)
    try:
        with MemGuard() as guard:
            try:
                return _decide(test_id, changed, modules, budget)
            finally:
                if guard.tripped:
                    last["memory"] = True
    except core.TestTimeout:
        last["timeout"] = True
        return "fail"
    finally:
        _unpatch(mark)


def _decide(test_id, changed, modules, budget):
    kind, umap, why = _gate(test_id, changed)
    if kind is None:
        last["why"] = why
        return "unknown"
    t0 = time.process_time()
    state = kind.prepare(modules)
    if state != "ok":
        last["why"] = "class: " + state
        return state
    if not kind.use_map(umap):
        last["why"] = "the map does not fit the test"
        return "unknown"
    ctl = Control(umap, changed)
    if ctl.order_dependent:
        # a changed line that a unit only runs after other units ran: keep the test's own order and
        # leave no unit out
        ctl.all = True
        ctl.cuts = False
    if getattr(kind, "whole", False):
        pol = _FilePolicy(umap, ctl)
        if not ctl.all and not ctl.bits and not any(rel in umap.opened for rel in changed.data):
            last["why"] = "no unit runs the changed lines"
            return "unknown"
        bad = kind.run_whole(pol)
        if core.last_timed_out():
            raise core.TestTimeout()
        last.update(units=pol.ran, left=pol.left, all=ctl.all, cpu=time.process_time() - t0)
        if pol.seen != len(pol.calls) and not bad:
            last["why"] = "check_file was called %d times, not %d" % (pol.seen, len(pol.calls))
            return "unknown"
        return "fail" if bad else "pass"
    try:
        inputs = kind.unit_inputs()
    except core.TestTimeout:
        raise
    except BaseException as e:
        last["why"] = "inputs: %r" % (e,)
        return "unknown"
    fresh = set()
    if inputs is not None:
        if len(inputs) != umap.n:
            last["why"] = "number of units changed"
            return "unknown"
        fresh = {i for i in range(umap.n) if inputs[i] is None or inputs[i] != umap.inputs[i]}
    elif any(rel in kind.data_files for rel in changed.data):
        last["why"] = "data changed"
        return "unknown"
    todo = [i for i in range(umap.n) if i in fresh or ctl.unit_hit(i)]
    if not todo and not any(rel in umap.opened for rel in changed.data):
        # the engine sees a changed line in this test that no unit of ours runs: a path we do not know
        last["why"] = "no unit runs the changed lines"
        return "unknown"
    last.update(units=len(todo), fresh=len(fresh), all=ctl.all, cuts=ctl.cuts, stats=ctl.stats)
    if todo:
        try:
            kind.preamble(ctl)
        except core.TestTimeout:
            raise
        except BaseException as e:
            last["why"] = "preamble failed: %r" % (e,)
            return "fail"
    if not ctl.order_dependent and not kind.ordered:
        costs = {}
        for i in todo:
            costs[i] = sum(umap.cpu[i].values()) if i in fresh else kind.cost(i, ctl, umap)
        todo.sort(key=lambda i: (costs[i], i))
    guard = getattr(kind, "shared", None)
    before = None
    if guard is not None and todo:
        # the pinned value holds as long as nothing outside the units changed
        before = guard() if (ctl.all or fresh) else (umap.extra.get("outside") or {}).get("shared")
    for i in todo:
        if budget is not None and time.process_time() - t0 > budget:
            last["why"] = "budget"
            return "unknown"
        ctl.begin(i, i in fresh)
        try:
            ok = kind.run_unit(i, ctl)
        except core.TestTimeout:
            raise
        except unittest.SkipTest:
            last["why"] = "the test skips itself"
            return "unknown"
        except Unknown as e:
            if ctl.tracing:
                raise
            last["why"] = str(e)
            return "unknown"
        except BaseException as e:
            last["why"] = "unit %d: %r" % (i, e)
            ok = False
        if not ok:
            last["failed_unit"] = i
            last["cpu"] = time.process_time() - t0
            return "fail"
    if guard is not None and ctl.stats["real"]:
        # units ran for real; did they leave the shared data as they found it?
        if before is None or guard() != before:
            last["why"] = "the units changed the data they share"
            last["cpu"] = time.process_time() - t0
            return "unknown"
    last["cpu"] = time.process_time() - t0
    return "pass"

"""Shared pieces: loading the pinned tests into one warm process and running a single test."""
import importlib
import os
import sys
import unittest

ROOT = os.environ.get("BR_ROOT", "/repo")


def module_name(test_file):
    return test_file[:-3].replace("/", ".")


def split_id(test_id):
    f, c, m = test_id.split("::")
    return f, c, m


class TestTimeout(BaseException):
    """Raised inside a test that runs far longer than it should."""


class InlinePool:
    """Stands in for ProcessPoolExecutor in the tests: a task runs at once in this process.
    Arguments and results still go through pickle, as they do on the way to a worker."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._initializer, self._initargs = initializer, initargs
        self._ready = False
        self._closed = False
        self._broken = None

    def submit(self, fn, /, *args, **kwargs):
        import pickle
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool
        if self._closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        if self._broken is not None:
            raise BrokenProcessPool(self._broken)
        f = Future()
        f.set_running_or_notify_cancel()
        if not self._ready:
            self._ready = True
            if self._initializer is not None:
                try:
                    self._initializer(*pickle.loads(pickle.dumps(self._initargs)))
                except TestTimeout:
                    raise
                except BaseException:
                    self._broken = "A process in the process pool was terminated abruptly"
                    f.set_exception(BrokenProcessPool(self._broken))
                    return f
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
        except TestTimeout:
            raise
        except BaseException as e:
            f.set_exception(e)
            return f
        try:
            r = fn2(*a2, **k2)
            r = pickle.loads(pickle.dumps(r))
        except TestTimeout:
            raise
        except SystemExit:
            self._broken = "A process in the process pool was terminated abruptly"
            f.set_exception(BrokenProcessPool(self._broken))
        except BaseException as e:
            try:
                e = pickle.loads(pickle.dumps(e))
            except BaseException as e2:
                e = e2
            f.set_exception(e)
        else:
            f.set_result(r)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        fs = [self.submit(fn, *a) for a in zip(*iterables)]
        def gen():
            for f in fs:
                yield f.result()
        return gen()

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()
        return False


def install_inline_pool():
    import concurrent.futures
    import concurrent.futures.process
    concurrent.futures.ProcessPoolExecutor = InlinePool
    concurrent.futures.process.ProcessPoolExecutor = InlinePool


class Warm:
    """Imports sqlglot and the test modules once. Children are forked from this state."""

    def __init__(self, test_files):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        install_inline_pool()
        self.modules = {}
        self.import_errors = {}
        # every sqlglot module, so that no test has to import a dialect on its own
        pkg = os.path.join(ROOT, "sqlglot")
        names = []
        for d, dirs, fs in os.walk(pkg):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            for f in sorted(fs):
                if f.endswith(".py") and f != "__main__.py":
                    rel = os.path.relpath(os.path.join(d, f), ROOT)[:-3].replace(os.sep, ".")
                    names.append(rel[:-9] if rel.endswith(".__init__") else rel)
        for n in names:
            try:
                importlib.import_module(n)
            except BaseException:
                pass
        for f in test_files:
            try:
                self.modules[f] = importlib.import_module(module_name(f))
            except BaseException as e:  # a test file that cannot be imported fails all its tests
                self.import_errors[f] = repr(e)


class _Enough(BaseException):
    """Ends a test whose verdict is already known."""


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = False

    def _note(self, err):
        self.bad = True
        if err is not None and err[0] is not None and issubclass(err[0], TestTimeout):
            _timed_out[0] = True

    def addFailure(self, test, err):
        self._note(err)

    def addError(self, test, err):
        self._note(err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._note(err)
            raise _Enough()  # one failing subtest decides the test: no need to run the rest

    def addUnexpectedSuccess(self, test):
        self.bad = True


_class_ready = {}
_timed_out = [False]


def last_timed_out():
    """True when the last run_test met a TestTimeout somewhere inside."""
    return _timed_out[0]


def class_state(meta):
    mod = sys.modules.get(module_name(meta[0]))
    cls = getattr(mod, meta[1], None) if mod is not None else None
    return _class_ready.get(cls)


def prepare_class(test_id, modules=None):
    """Runs setUpClass once per class per process. -> the class, or None when it cannot be used."""
    f, c, m = split_id(test_id)
    mod = modules[f] if modules is not None and f in modules else sys.modules.get(module_name(f))
    if mod is None:
        mod = importlib.import_module(module_name(f))
    cls = getattr(mod, c)
    state = _class_ready.get(cls)
    if state is None:
        try:
            if not getattr(cls, "__unittest_skip__", False):
                cls.setUpClass()
            state = True
        except unittest.SkipTest:
            state = True
        except TestTimeout:
            _timed_out[0] = True
            state = False
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            state = False
        _class_ready[cls] = state
    return cls if state else None


def run_test(test_id, modules=None):
    """True when the test fails."""
    _timed_out[0] = False
    try:
        cls = prepare_class(test_id, modules)
        if cls is None:
            return True
        res = _Result()
        cls(test_id.rsplit("::", 1)[1]).run(res)
        return res.bad
    except (KeyboardInterrupt, SystemExit):
        raise
    except TestTimeout:
        _timed_out[0] = True
        return True
    except BaseException:
        return True

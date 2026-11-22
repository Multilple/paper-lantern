"""Reads the map files (all built from the pinned commit)."""
import json
import os
import pickle
import zlib

DIR = os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))


def bits(b):
    """indices of the set bits of a little-endian bitmap (bytes or int)"""
    x = b if isinstance(b, int) else int.from_bytes(b, "little")
    out = []
    while x:
        low = x & -x
        out.append(low.bit_length() - 1)
        x ^= low
    return out


class Maps:
    def __init__(self):
        idx = json.load(open(os.path.join(DIR, "index.json")))
        self.tests = idx["tests"]
        self.cpu = idx["cpu"]
        self.setup = idx.get("setup", {})
        self.files = idx["files"]
        self.untraced = set(idx.get("untraced", []))
        self.ref_import_cpu = idx.get("ref_import_cpu")
        self._lines = {}
        self._opened = None
        self._types = None
        self._import = None
        self._cmp = None

    def line_map(self, relpath):
        m = self._lines.get(relpath)
        if m is None:
            k = self.files.get(relpath)
            if k is None:
                m = {"lines": {}, "table": [], "any": 0}
            else:
                m = pickle.loads(zlib.decompress(open(os.path.join(DIR, "lines", "%d.bin" % k), "rb").read()))
                m["table"] = [int.from_bytes(b, "little") for b in m["table"]]
                m["any"] = int.from_bytes(m["any"], "little")
            self._lines[relpath] = m
        return m

    def tests_on_lines(self, relpath, lines):
        """bitmap of the tests that run any of these lines of the pinned file"""
        m = self.line_map(relpath)
        acc = 0
        tl, tb = m["lines"], m["table"]
        for l in lines:
            j = tl.get(l)
            if j is not None:
                acc |= tb[j]
        return acc

    def tests_in_file(self, relpath):
        return self.line_map(relpath)["any"]

    @property
    def pytest_only(self):
        if getattr(self, "_po", None) is None:
            try:
                self._po = json.load(open(os.path.join(DIR, "pytest_only.json")))
            except Exception:
                self._po = {}
        return self._po

    def index_of(self, test_id):
        if getattr(self, "_idx", None) is None:
            self._idx = {t: i for i, t in enumerate(self.tests)}
        return self._idx.get(test_id)

    @property
    def opened(self):
        if self._opened is None:
            self._opened = json.load(open(os.path.join(DIR, "opened.json")))
        return self._opened

    @property
    def types(self):
        if self._types is None:
            self._types = json.load(open(os.path.join(DIR, "types.json")))
        return self._types

    @property
    def cmp_tests(self):
        """maps/cmp_tests.json (tools/trace_cmp_tests.py): what every test evaluated at every comparison"""
        if getattr(self, "_cmp_tests", None) is None:
            try:
                with open(os.path.join(DIR, "cmp_tests.json")) as f:
                    self._cmp_tests = json.load(f)
            except Exception:
                self._cmp_tests = {}
        return self._cmp_tests

    @property
    def cmp(self):
        """maps/cmp.json (tools/trace_cmp.py): relpath -> {"line:col": [[kind, kind], ...]}"""
        if self._cmp is None:
            try:
                with open(os.path.join(DIR, "cmp.json")) as f:
                    self._cmp = json.load(f)
            except Exception:
                self._cmp = {}
        return self._cmp

    @property
    def imp(self):
        if self._import is None:
            d = json.load(open(os.path.join(DIR, "import.json")))
            d["lines"] = {k: set(v) for k, v in d["lines"].items()}
            d["called"] = {tuple(x) for x in d["called"]}
            d["opened"] = set(d["opened"])
            self._import = d
        return self._import

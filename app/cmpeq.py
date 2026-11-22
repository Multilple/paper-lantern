"""When a patch only swaps `is` and `==` (or `is not` and `!=`), and cannot change anything.

maps/cmp.json (tools/trace_cmp.py) holds, for every comparison in sqlglot that the test suite runs on
the pinned commit, how == works for the types of the two operands that were seen there. For some
pairs of types `a is b` and `a == b` are the same thing by the rules of the language:

    N  None            B  bool           O  a type that does not define __eq__ (== is identity)
    V  int, str, tuple, ... (exactly)    E:<class>  member of an Enum compared by str or int value

    O with anything listed, N with anything listed, two bools, two members of one Enum class.
    Z  a value of a type with its own __eq__ that, compared with None on the pinned commit, answered
       "not equal" every time: Z with N only.

The same map holds, for two plain values of one sort (numbers, strings, tuples of them), what each of
== != < <= > >= said about them; an ordering comparison swapped for another one that said the same
every time (`<` for `<=` where the two sides were never equal) is covered the same way.

If every pair seen at every swapped comparison is of that sort, each run of the comparison gives the
value it gave before, so every test does exactly what it did on the pinned commit: the file counts
as unchanged. Anything else (other types, a comparison no test was seen to run, any other edit in
the file) proves nothing here and the normal path decides.
"""
import ast

_EQ_METHODS = ("__eq__", "__ne__", "__hash__", "__bool__", "__lt__", "__le__", "__gt__", "__ge__")
_SWAPS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}
# what each ordering comparison said about two plain values is in the map, one bit per operator
_BITS = {ast.Eq: 1, ast.NotEq: 2, ast.Lt: 4, ast.LtE: 8, ast.Gt: 16, ast.GtE: 32}


def same_pair(ka, kb):
    """True when `is` and `==` agree for every two values whose types are of these kinds."""
    if ka == "X" or kb == "X":
        return False
    if ka == "Z" or kb == "Z":
        return ka == "N" or kb == "N"
    if ka == kb:
        return ka in ("N", "O", "B") or ka.startswith("E:")
    if ka == "O" or kb == "O":
        return True
    if ka == "N" or kb == "N":
        return True
    return False


def _diff(a, b, out):
    """Compares two syntax trees. -> False when they differ in more than swapped comparisons."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            oa, ob = type(a.ops[0]), type(b.ops[0])
            if (oa, ob) not in _SWAPS and not (oa in _BITS and ob in _BITS):
                return False
            out.append((a.lineno, a.col_offset, oa, ob))
            return _diff(a.left, b.left, out) and _diff(a.comparators, b.comparators, out)
        for f in a._fields:
            if not _diff(getattr(a, f, None), getattr(b, f, None), out):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not _diff(x, y, out):
                return False
        return True
    return a == b


def swapped_sites(old_src, new_src):
    """-> [(line, col, old operator, new operator)] of the swapped comparisons, by their place in
    the old source, or None when the two sources differ in anything else (or in nothing)."""
    try:
        a = ast.parse(old_src)
        b = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    out = []
    try:
        if not _diff(a, b, out):
            return None
    except RecursionError:
        return None
    # inside the methods that define equality itself nothing is concluded
    for node in ast.walk(a):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _EQ_METHODS:
            for site in out:
                if node.lineno <= site[0] <= (node.end_lineno or node.lineno):
                    return None
    return out or None


def unchanged(old_src, new_src, rel, table):
    """True when the new source provably behaves as the old one in every test."""
    if not table:
        return False
    sites = swapped_sites(old_src, new_src)
    if not sites:
        return False
    seen = table.get(rel)
    if not seen:
        return False
    for line, col, oa, ob in sites:
        pairs = seen.get("%d:%d" % (line, col))
        if not pairs:
            return False
        for p in pairs:
            if (oa, ob) in _SWAPS:
                if not same_pair(p[0], p[1]):
                    return False
            else:
                m = p[2] if len(p) > 2 else -1
                if m < 0 or bool(m & _BITS[oa]) != bool(m & _BITS[ob]):
                    return False
    return True

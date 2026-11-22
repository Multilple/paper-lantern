"""Compares the compiled code of a pinned Python file with its patched version."""
import dis
import types

CO_OPTIMIZED = 0x1
_JUMPS = set(dis.hasjrel) | set(getattr(dis, "hasjabs", []))


class Node:
    """A pair of code objects at the same place in the two code trees."""
    __slots__ = ("old", "new", "parent", "kids", "own_changed", "changed", "lines", "whole")

    def __init__(self, old, new, parent):
        self.old, self.new, self.parent = old, new, parent
        self.kids = []
        self.own_changed = False   # this code object itself differs (not counting nested ones)
        self.changed = False       # this or any nested one differs
        self.lines = set()         # old lines of the instructions that differ
        self.whole = False         # cannot localize: every line of the old code counts

    @property
    def is_function(self):
        return bool(self.old.co_flags & CO_OPTIMIZED)


class Incomparable(Exception):
    pass


def _key(v):
    if isinstance(v, types.CodeType):
        return ("code", v.co_qualname)
    if isinstance(v, (tuple, frozenset)):
        return (type(v).__name__, tuple(sorted((_key(x) for x in v), key=repr)) if isinstance(v, frozenset)
                else tuple(_key(x) for x in v))
    return (type(v).__name__, repr(v))


def _instrs(code):
    out = []
    for ins in dis.get_instructions(code):
        if ins.opcode in _JUMPS:
            arg = ("j", ins.arg)
        elif ins.opname in ("LOAD_CONST", "RETURN_CONST", "KW_NAMES"):
            arg = _key(ins.argval)
        else:
            arg = ("a", ins.arg, ins.argrepr)
        line = ins.positions.lineno if ins.positions is not None else None
        if ins.opname == "STORE_NAME" and ins.argval == "__firstlineno__" and out and out[-1][0] == "LOAD_CONST":
            out[-1] = ("LOAD_CONST", ("lineno",), out[-1][2])  # a class body stores its own first line
        out.append((ins.opname, arg, line))
    return out


def _doc(code):
    return code.co_consts[0] if code.co_flags & 0x4000000 and code.co_consts else None


def _all_lines(code):
    return {l for _, _, l in code.co_lines() if l is not None}


def _deep_lines(code):
    out = _all_lines(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            out |= _deep_lines(c)
    return out


def _shape(code):
    """What a code object does, without its line numbers (equal for a function that only moved)."""
    consts = tuple(_shape(c) if isinstance(c, types.CodeType) else _key(c) for c in code.co_consts)
    return (code.co_qualname, code.co_flags, code.co_code, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount, consts,
            len(code.co_exceptiontable))


def _align(ok, nk):
    """Pairs the nested code objects of an old and a new class body or module that are identical.
    -> ([(old, new)], [(old without partner, lines that count for it)]) or (None, None)."""
    import difflib
    try:
        sa = [_shape(c) for c in ok]
        sb = [_shape(c) for c in nk]
        ha = [hash(x) for x in sa]
        hb = [hash(x) for x in sb]
    except (TypeError, RecursionError):
        return None, None
    pairs, used = [], set()
    for i, j, n in difflib.SequenceMatcher(None, ha, hb, autojunk=False).get_matching_blocks():
        for k in range(n):
            if sa[i + k] == sb[j + k]:
                pairs.append((ok[i + k], nk[j + k]))
                used.add(i + k)
    gone = []
    for i, c in enumerate(ok):
        if i in used:
            continue
        lines = _deep_lines(c)
        for j, d in enumerate(ok):          # identical twins: which one went cannot be told
            if j != i and sa[j] == sa[i]:
                lines |= _deep_lines(d)
        gone.append((c, lines))
    return pairs, gone


def build(old, new, parent=None):
    node = Node(old, new, parent)
    ok = [c for c in old.co_consts if isinstance(c, types.CodeType)]
    nk = [c for c in new.co_consts if isinstance(c, types.CodeType)]
    if len(ok) != len(nk) or [c.co_qualname for c in ok] != [c.co_qualname for c in nk] \
            or [c.co_flags & CO_OPTIMIZED for c in ok] != [c.co_flags & CO_OPTIMIZED for c in nk]:
        # the nested functions do not pair up (for instance `while True:` turned into
        # `while False:` drops the lambdas of the loop body). Inside a function that is simply a
        # changed function: every line of it and of what it nests counts. At class or module
        # level there is no such answer.
        if old.co_flags & CO_OPTIMIZED and new.co_flags & CO_OPTIMIZED:
            node.own_changed = node.changed = node.whole = True
            node.lines = _deep_lines(old)
            return node
        # A class body or a module that gained or lost functions (an entry with a lambda added to
        # or removed from a table, a method added or deleted). The functions that are the same in
        # both, body for body, pair up; an old one without a partner is a function that is gone
        # (or was rewritten): every line of it counts, as for any changed function. A new one
        # without a partner has no old lines; what it changes shows in the import state.
        pairs, gone = _align(ok, nk)
        if pairs is None:
            raise Incomparable("nested code objects do not pair up in %s" % old.co_qualname)
        for a, b in pairs:
            node.kids.append(build(a, b, node))
        for a, lines in gone:
            k = Node(a, a, node)
            k.own_changed = k.changed = k.whole = True
            k.lines = lines
            node.kids.append(k)
        ok = nk = ()
    for a, b in zip(ok, nk):
        node.kids.append(build(a, b, node))
    meta_same = (
        old.co_names == new.co_names and old.co_varnames == new.co_varnames
        and old.co_freevars == new.co_freevars and old.co_cellvars == new.co_cellvars
        and old.co_argcount == new.co_argcount and old.co_posonlyargcount == new.co_posonlyargcount
        and old.co_kwonlyargcount == new.co_kwonlyargcount and old.co_flags == new.co_flags
        and old.co_name == new.co_name and old.co_qualname == new.co_qualname
    )
    if meta_same and old.co_code == new.co_code and old.co_exceptiontable == new.co_exceptiontable \
            and len(old.co_consts) == len(new.co_consts):
        # the same bytes, names and constants: the same instructions, without taking them apart
        try:
            if all(x is y or _key(x) == _key(y) for x, y in zip(old.co_consts, new.co_consts)):
                node.changed = any(k.changed for k in node.kids)
                return node
        except RecursionError:
            pass
    ia, ib = _instrs(old), _instrs(new)
    sa = [(o, a) for o, a, _ in ia]
    sb = [(o, a) for o, a, _ in ib]
    consts_same = _key(_doc(old)) == _key(_doc(new))  # other constants show up in the instructions
    if sa != sb or not meta_same or not consts_same or old.co_exceptiontable != new.co_exceptiontable and len(sa) != len(sb):
        node.own_changed = True
        if not meta_same and (old.co_varnames != new.co_varnames or old.co_freevars != new.co_freevars
                              or old.co_cellvars != new.co_cellvars or old.co_argcount != new.co_argcount
                              or old.co_kwonlyargcount != new.co_kwonlyargcount
                              or old.co_posonlyargcount != new.co_posonlyargcount or old.co_flags != new.co_flags):
            node.whole = True
        if sa == sb:
            node.whole = True  # same instructions, something else differs
        else:
            i = 0
            m = min(len(sa), len(sb))
            while i < m and sa[i] == sb[i]:
                i += 1
            j = 0
            while j < m - i and sa[len(sa) - 1 - j] == sb[len(sb) - 1 - j]:
                j += 1
            lo, hi = max(i - 1, 0), min(len(sa) - j, len(sa) - 1)
            for k in range(lo, hi + 1):
                if ia[k][2] is None:
                    node.whole = True
                else:
                    node.lines.add(ia[k][2])
            if not node.lines:
                node.whole = True
        if node.whole:
            node.lines = _all_lines(old)
    elif old.co_exceptiontable != new.co_exceptiontable:
        # same instructions: the table differs only when offsets moved, which cannot happen here
        node.own_changed = True
        node.whole = True
        node.lines = _all_lines(old)
    node.changed = node.own_changed or any(k.changed for k in node.kids)
    return node


def walk(node):
    yield node
    for k in node.kids:
        yield from walk(k)


def compare(old_src, new_src, filename):
    """-> ("same", None) | ("syntax", None) | ("tree", root Node) | ("incomparable", None)"""
    old = compile(old_src, filename, "exec", dont_inherit=True)
    try:
        new = compile(new_src, filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return "syntax", None
    try:
        root = build(old, new)
    except Incomparable:
        return "incomparable", None
    if not root.changed:
        return "same", root
    return "tree", root

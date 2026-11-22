"""Run-or-select decision for one commit, as a pure function of what has been seen so far.

    decide_next(st, model=DEFAULT) -> ("run", i) | ("stop",)

st must offer:
    rem            set of unrun candidate map indices
    passed, failed lists of map indices run so far
    forced         list of indices selected without a run (plan.unrun, import-error failures)
    n_forced_fail  (optional) how many of forced are known failures (import errors)
    secs           index -> recorded duration in ms (the scorer's ms)
    cpu, cls       index -> CPU seconds of the test alone, index -> "file::Class"
    done_cls       classes whose setUpClass has already run in this commit
    cost(i)        CPU seconds to run i now (setUpClass included when its class is not set up yet)
    memo           dict, empty at the start of a commit; the policy's scratch
All CPU costs come in through cpu/cost, so the rule adapts when a test gets cheaper to decide.

Model (commit level, fitted from counts only, never per test): with probability pi nothing in the
candidate set fails; otherwise every candidate fails independently with one q ~ Beta(a, b).
Optionally a separate (pi, a, b) for candidate sets of size <= small_n.

Plans priced at every decision (S = everything that would be selected if we stopped now):
  "run the set Q_L cheapest first, stop at the first failure, select the rest", where Q_L is the L
  unrun tests with the most recorded ms per CPU ms (the runs that take the most off the pack per
  CPU spent), for L on a grid 1..N.  L = N is "clear the commit" (cost 0 if nothing fails).
  Expected cost = sum_t P(first failure at t) (CPU of the first t + bound of what is left)
                 + P(all of Q_L pass) (CPU of Q_L + bound of S minus Q_L).
  After a failure the same plans are priced with the posterior of q (failures stay selected).
  The cheapest test of the best plan runs, if that plan beats stopping now.
Selection cost is priced with pack.bound (the least any split can cost).
"""
import math

WORKER_MS = 2000
MAX_WORKERS = 8
BIG = 64          # candidate sets above this size re-price the plan less often (CPU of the rule itself)


def bound(total_ms, longest_ms, n):
    if n == 0:
        return 0
    return min(max(longest_ms, -(-total_ms // k)) + WORKER_MS * k for k in range(1, min(MAX_WORKERS, n) + 1))


class Model:
    def __init__(self, pi=0.15, a=0.2, b=0.1, small=None, small_n=0):
        self.pi, self.a, self.b = pi, a, b
        self.small = small          # (pi, a, b) for candidate sets of size <= small_n
        self.small_n = small_n

    def params(self, n):
        if self.small is not None and n <= self.small_n:
            return self.small
        return self.pi, self.a, self.b

    def __repr__(self):
        return "Model(pi=%s,a=%s,b=%s,small=%s,small_n=%s)" % (self.pi, self.a, self.b, self.small, self.small_n)


DEFAULT = Model()


def _lbeta(a, b):
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _grid(n):
    """1, 2, 3, 4, 6, 9, 13, ... n"""
    out, x = [], 1
    while x < n:
        out.append(x)
        x = x + 1 if x < 4 else x + x // 2
    if n:
        out.append(n)
    return out


def decide_next(st, model=DEFAULT):
    rem = st.rem
    if not rem:
        return ("stop",)
    secs, cpu, cls = st.secs, st.cpu, st.cls
    memo = st.memo
    base = list(st.failed) + list(st.forced)
    B_tot = sum(secs[i] for i in base)
    B_long = max((secs[i] for i in base), default=0)
    B_n = len(base)
    # tests known to fail without a run (their file cannot be imported) are verdicts like the others:
    # they count as runs, or the pass count below would go negative
    nf = getattr(st, "n_forced_fail", 0)
    m = len(st.passed) + len(st.failed) + nf
    r = len(st.failed) + nf
    if "n0" not in memo:
        memo["n0"] = len(rem) + m
    pi, a, b = model.params(memo["n0"])

    # Fast path for big sets: in the clean phase a pass only makes running more attractive, so a plan
    # chosen earlier is followed without re-pricing until the number of passes doubles.
    plan = memo.get("plan")
    if r == 0 and plan and m < memo.get("recheck", 0):
        while plan and plan[-1] not in rem:
            plan.pop()
        if plan:
            return ("run", plan.pop())
    memo["plan"] = None

    R = list(rem)
    N = len(R)
    U_tot = sum(secs[i] for i in R)
    S_cost = bound(B_tot + U_tot, max(B_long, max(secs[i] for i in R)), B_n + N)

    # CPU per test with setUpClass shared by the class members still unrun (for the ordering only)
    per_cls = {}
    for i in R:
        per_cls[cls[i]] = per_cls.get(cls[i], 0) + 1
    costs = {i: st.cost(i) for i in R}

    def amort(i):
        return (cpu[i] + (costs[i] - cpu[i]) / per_cls[cls[i]]) * 1000.0
    ratio_order = sorted(R, key=lambda i: (-secs[i] / max(amort(i), 1e-3), i))

    if r == 0:
        lb0 = _lbeta(a, b)
        gm = pi + (1 - pi) * math.exp(_lbeta(a, b + m) - lb0)

        def P(k):      # P(next k all pass | history)
            return (pi + (1 - pi) * math.exp(_lbeta(a, b + m + k) - lb0)) / gm
    else:
        ar, br = a + r, b + m - r
        lb0 = _lbeta(ar, br)
        pbar = br / (ar + br)

        def P(k):
            return math.exp(_lbeta(ar, br + k) - lb0)

    done = getattr(st, "done_cls", set())
    best_e, best_first = S_cost, None
    for L in _grid(N):
        Q = sorted(ratio_order[:L], key=lambda i: (costs[i], i))
        if L < N:
            out_long = max(secs[i] for i in ratio_order[L:])
        else:
            out_long = 0
        seen = set(done)
        cum = [0.0]
        for i in Q:
            c = cpu[i] if cls[i] in seen else costs[i]
            seen.add(cls[i])
            cum.append(cum[-1] + c * 1000.0)
        sufl = [0] * (L + 1)
        sufs = [0] * (L + 1)
        for j in range(L - 1, -1, -1):
            sufl[j] = max(sufl[j + 1], secs[Q[j]])
            sufs[j] = sufs[j + 1] + secs[Q[j]]
        rest_tot = U_tot - sufs[0]

        def left(j):     # selection when Q[:j] passed, Q[j:] and the rest of R selected
            return bound(B_tot + rest_tot + sufs[j], max(B_long, out_long, sufl[j]), B_n + N - j)
        PL = P(L)
        if r == 0:
            e = PL * (cum[L] + left(L))
            prev_t, prev_p = 0, 1.0
            for t in _grid(L):
                pt = P(t)
                # a first failure in (prev_t, t] is charged the CPU up to t and the selection after prev_t
                e += (prev_p - pt) * (cum[t] + left(prev_t))
                prev_t, prev_p = t, pt
        else:
            part = bound(B_tot + rest_tot + int(sufs[0] * (1 - pbar)), max(B_long, out_long, sufl[0]), B_n + N)
            e = cum[L] + PL * left(L) + (1 - PL) * min(part, S_cost)
        if e < best_e - 1e-6:
            best_e, best_first, best_Q = e, Q[0], Q
    if best_first is None:
        return ("stop",)
    if r == 0 and N > BIG:
        memo["plan"] = list(reversed(best_Q[1:]))
        memo["recheck"] = max(2 * m, m + 8)
    return ("run", best_first)


def make(model):
    return lambda st: decide_next(st, model)

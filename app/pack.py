"""Splits selected tests over workers. Cost of a commit: largest worker load + 2 s per worker."""
WORKER_MS = 2000
MAX_WORKERS = 8


def bound(total_ms, longest_ms, n):
    """The least any split of such a selection can cost."""
    if n == 0:
        return 0
    return min(max(longest_ms, -(-total_ms // k)) + WORKER_MS * k for k in range(1, min(MAX_WORKERS, n) + 1))


def _lpt(items, k):
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for ms, t in items:
        i = min(range(k), key=loads.__getitem__)
        loads[i] += ms
        bins[i].append((ms, t))
    return loads, bins


def _improve(loads, bins):
    """Moves and swaps out of the fullest worker while that lowers the largest load."""
    k = len(bins)
    for _ in range(200):
        hi = max(range(k), key=loads.__getitem__)
        top = loads[hi]
        best = None
        for a, (ma, ta) in enumerate(bins[hi]):
            for j in range(k):
                if j == hi:
                    continue
                if loads[j] + ma < top:  # move
                    new_top = max(loads[j] + ma, top - ma)
                    if best is None or new_top < best[0]:
                        best = (new_top, a, j, None)
                for b, (mb, tb) in enumerate(bins[j]):
                    if mb < ma and loads[j] - mb + ma < top:  # swap
                        new_top = max(loads[j] - mb + ma, top - ma + mb)
                        if best is None or new_top < best[0]:
                            best = (new_top, a, j, b)
        if best is None or best[0] >= top:
            break
        _, a, j, b = best
        ma, ta = bins[hi].pop(a)
        loads[hi] -= ma
        if b is not None:
            mb, tb = bins[j].pop(b)
            loads[j] -= mb
            bins[hi].append((mb, tb)); loads[hi] += mb
        bins[j].append((ma, ta)); loads[j] += ma
    return loads, bins


def pack(numbers, ms):
    """-> (cost in ms, workers as lists of test numbers)"""
    numbers = list(numbers)
    if not numbers:
        return 0, []
    items = sorted(((ms[t], t) for t in numbers), reverse=True)
    total = sum(m for m, _ in items)
    longest = items[0][0]
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        low = max(longest, -(-total // k)) + WORKER_MS * k
        if best is not None and low >= best[0]:
            continue
        loads, bins = _lpt(items, k)
        if max(loads) + WORKER_MS * k > low and len(items) <= 400:
            loads, bins = _improve(loads, bins)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[0], [[t for _, t in b] for b in best[1] if b]

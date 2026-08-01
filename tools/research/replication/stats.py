"""Shared statistics for replication experiments."""


def taker_fee_c(price_c):
    """Kalshi taker fee in cents/contract at price p cents: 7*p*(1-p)."""
    p = price_c / 100.0
    return 7.0 * p * (1.0 - p)


def cluster_bootstrap_ci(values_a, values_b=None, n_boot=1000, seed=20260727, alpha=0.05,
                         stat=None, rng=None):
    """Cluster bootstrap CI where each element is one cluster (e.g. one market).

    Default statistic: mean(values_a) - mean(values_b)*100 is NOT assumed —
    pass `stat(idx)` for custom statistics; default is mean of values_a.
    Returns (lo, hi) or (None, None) if fewer than 2 clusters.
    """
    import numpy as np
    a = np.asarray(values_a, dtype=float)
    n = len(a)
    if n < 2:
        return None, None
    r = rng or np.random.default_rng(seed)
    idx = r.integers(0, n, size=(n_boot, n))
    if stat is not None:
        bs = stat(idx)
    elif values_b is not None:
        b = np.asarray(values_b, dtype=float)
        bs = a[idx].mean(axis=1) - b[idx].mean(axis=1)
    else:
        bs = a[idx].mean(axis=1)
    lo, hi = np.percentile(bs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)

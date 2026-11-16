"""Monte Carlo of a list of trade results (R): shuffle/redraw them many times and measure how bad
the drawdown and the losing streaks can get, and what risk per trade keeps the worst case
inside a chosen account drawdown."""
from __future__ import annotations
import numpy as np


def max_drawdown(curves: np.ndarray) -> np.ndarray:
    """Largest peak-to-trough fall of each row of cumulative R (the start counts as a peak at 0)."""
    peaks = np.maximum.accumulate(np.concatenate([np.zeros((curves.shape[0], 1)), curves], axis=1), axis=1)[:, 1:]
    return (peaks - curves).max(axis=1)


def longest_losing_streak(results: np.ndarray) -> np.ndarray:
    losing = results < 0
    best = np.zeros(results.shape[0], dtype=np.int64); run = np.zeros_like(best)
    for column in losing.T:
        run = np.where(column, run + 1, 0); best = np.maximum(best, run)
    return best


def simulate(r, n_trades: int | None = None, sims: int = 10_000, seed: int = 5, chunk: int = 2000) -> dict:
    """Bootstrap: each simulation draws n_trades results (with replacement) from r."""
    r = np.asarray([v for v in r if np.isfinite(v)], dtype=float)
    if len(r) < 20: return {'trades': int(len(r)), 'enough': False}
    n = int(n_trades or len(r)); rng = np.random.default_rng(seed)
    dd, streak, final = [], [], []
    for start in range(0, sims, chunk):
        draws = r[rng.integers(0, len(r), size=(min(chunk, sims - start), n))]
        curves = np.cumsum(draws, axis=1)
        dd.append(max_drawdown(curves)); streak.append(longest_losing_streak(draws)); final.append(curves[:, -1])
    dd, streak, final = np.concatenate(dd), np.concatenate(streak), np.concatenate(final)
    q = lambda a, p: float(np.percentile(a, p))
    return {'trades': int(len(r)), 'enough': True, 'n_per_run': n, 'sims': int(sims),
            'expectancy_r': float(r.mean()), 'win_rate': float((r > 0).mean()),
            'drawdown_r': {'p50': q(dd, 50), 'p95': q(dd, 95), 'p99': q(dd, 99)},
            'losing_streak': {'p50': q(streak, 50), 'p95': q(streak, 95), 'p99': q(streak, 99)},
            'final_r': {'p5': q(final, 5), 'p50': q(final, 50), 'p95': q(final, 95)},
            'chance_negative': float((final < 0).mean())}


def recommended_risk(result: dict, max_account_drawdown_pct: float = 15.0, cap_pct: float = 2.0) -> float | None:
    """Risk per trade (%) so that the 95th-percentile drawdown stays within the account limit.
    None when the system loses on average: no risk size makes a losing system safe."""
    if not result.get('enough') or result['expectancy_r'] <= 0: return None
    dd95 = max(result['drawdown_r']['p95'], 1.0)
    return round(min(cap_pct, max_account_drawdown_pct / dd95), 2)

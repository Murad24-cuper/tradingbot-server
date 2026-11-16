import numpy as np
import pytest

from src.research import monte_carlo as mc


def test_max_drawdown_and_streak():
    curves = np.cumsum(np.array([[1, -1, -1, 2, -3, 1]], float), axis=1)
    assert mc.max_drawdown(curves)[0] == pytest.approx(3.0)
    assert mc.longest_losing_streak(np.array([[1, -1, -1, 2, -3, -1, -1, 1]], float))[0] == 3


def test_drawdown_counts_fall_from_start():
    assert mc.max_drawdown(np.cumsum(np.array([[-1.0, -1.0]]), axis=1))[0] == pytest.approx(2.0)


def test_simulate_and_risk_advice():
    rng = np.random.default_rng(1)
    r = np.where(rng.random(400) < 0.45, 1.5, -1.0)
    out = mc.simulate(r, n_trades=200, sims=3000)
    assert out['enough'] and out['drawdown_r']['p50'] <= out['drawdown_r']['p95'] <= out['drawdown_r']['p99']
    assert 0 <= out['chance_negative'] <= 1
    risk = mc.recommended_risk(out, 15.0, cap_pct=2.0)
    assert risk is not None and 0 < risk <= 2.0
    assert risk == pytest.approx(min(2.0, 15.0 / max(out['drawdown_r']['p95'], 1.0)), abs=0.01)


def test_losing_system_gets_no_risk_and_too_few_trades():
    assert mc.recommended_risk(mc.simulate([-0.2, 0.1] * 30, sims=500)) is None
    assert mc.simulate([1.0] * 5)['enough'] is False

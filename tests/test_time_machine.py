import numpy as np
import pandas as pd
import pytest

from src.research import time_machine as tm


def _walk(days=120, seed=3, drift=0.0):
    rng = np.random.default_rng(seed)
    index = pd.date_range('2024-01-01', periods=days * 1440, freq='1min', tz='UTC')
    index = index[index.dayofweek < 5]                       # weekends missing, like a real market
    steps = rng.normal(drift, 0.5, len(index))
    close = 2000 + np.cumsum(steps)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0, 0.3, len(index))
    low = np.minimum(open_, close) - rng.uniform(0, 0.3, len(index))
    m1 = pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close}, index=index)
    m15 = m1.resample('15min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    return m1, m15


@pytest.fixture(scope='module')
def walk_index():
    m1, m15 = _walk()
    return tm.build_index_from_frames(m1, m15)


def _paths(fav, adv, final):
    return {'fav': np.array(fav, float), 'adv': np.array(adv, float), 'final': np.array(final, float)}


def test_evaluate_target_stop_time_and_same_bar_counts_stop():
    p = _paths([[0.5, 2.1, 0.0], [0.0, 3.0, 0.0], [0.2, 0.3, 0.4]],
               [[0.1, 0.2, 0.0], [0.0, 1.5, 0.0], [0.1, 0.2, 0.3]],
               [1.0, 0.0, 0.4])
    out = tm.evaluate(p, stop_atr=1.0, target_atr=2.0, spread_atr=0.0, slip_atr=0.0)
    assert out['codes'].tolist() == [1, -1, 0]                # 2nd path touches both in one bar: stop
    assert out['r'].tolist() == pytest.approx([2.0, -1.0, 0.4])


def test_evaluate_costs_make_hits_harder_and_reduce_r():
    p = _paths([[1.95]], [[0.0]], [0.0])
    assert tm.evaluate(p, 1.0, 2.0, 0.0, 0.0)['codes'][0] == 0
    p = _paths([[2.0]], [[0.0]], [0.0])
    assert tm.evaluate(p, 1.0, 2.0, 0.1, 0.0)['codes'][0] == 0   # needs target + spread
    p = _paths([[0.0]], [[0.95]], [0.0])
    assert tm.evaluate(p, 1.0, 2.0, 0.1, 0.0)['codes'][0] == -1  # stop is a spread closer
    r = tm.evaluate(_paths([[3.0]], [[0.0]], [0.0]), 1.0, 2.0, 0.0, 0.05)['r'][0]
    assert r == pytest.approx(2.0 - 0.1)


def test_trend_uses_only_closed_m15_bars():
    m1, m15 = _walk(days=20)
    a = tm.build_index_from_frames(m1, m15)
    cut = m1.index[len(m1) // 2]
    future = m15.copy(); future.loc[future.index >= cut, 'close'] *= 1.5
    b = tm.build_index_from_frames(m1, future)
    early = a['times'] < (cut.value - 15 * tm.MINUTE)
    assert np.array_equal(a['trend'][early], b['trend'][early])


def test_atr_is_known_before_the_bar():
    m1, m15 = _walk(days=10)
    a = tm.build_index_from_frames(m1, m15)
    changed = m1.copy(); i = 5000; changed.iloc[i, changed.columns.get_loc('high')] += 50
    b = tm.build_index_from_frames(changed, m15)
    assert a['atr'][i] == b['atr'][i] and a['atr'][i + 1] != b['atr'][i + 1]


def test_analogs_skip_market_closes_and_respect_before(walk_index):
    starts, _ = tm.select_analogs(walk_index, hour=10, trend=1, vol=1, minutes=30)
    times = walk_index['times']
    assert len(starts) and np.all(times[starts + 30] - times[starts] <= 35 * tm.MINUTE)
    slots = times[starts] // (15 * tm.MINUTE)
    assert len(np.unique(slots)) == len(slots)                # one per 15 minutes
    cut = int(times[len(times) // 2])
    early, _ = tm.select_analogs(walk_index, hour=10, trend=1, vol=1, minutes=30, before_ns=cut)
    assert np.all(times[early] + 31 * tm.MINUTE < cut)


def test_simulate_grid_and_fan(walk_index):
    out = tm.simulate(walk_index, hour=10, trend=0, direction='sell', price=2000.0, atr=0.6,
                      stop_distance=2.6, target_distance=3.9, spread=0.2, minutes=30, slippage_price=0.01)
    assert out['enough'] and out['analogs'] >= tm.MIN_ANALOGS
    chosen = out['chosen']
    assert chosen['target'] + chosen['stop'] + chosen['time'] == pytest.approx(1.0)
    assert [row['name'] for row in out['grid']] == ['Qısa', 'Normal', 'Geniş']
    assert all(len(row['cells']) == 3 for row in out['grid'])
    assert out['stop'] > 2000 > out['target']                  # sell
    assert len(out['fan']) == 150 and len(out['fan'][0]['p']) == 31 and out['fan'][0]['p'][0] == 2000.0
    # No edge in a driftless walk: costs make the average slightly negative.
    assert -0.3 < chosen['expectancy_r'] < 0.1


def test_simulate_without_enough_history_gives_no_numbers(walk_index):
    tiny = {k: (v[:3000] if isinstance(v, np.ndarray) and v.shape and len(v) == len(walk_index['times']) else v) for k, v in walk_index.items()}
    out = tm.simulate(tiny, hour=10, trend=1, direction='buy', price=2000, atr=0.6, stop_distance=2.6,
                      target_distance=3.9, spread=0.2)
    assert not out['enough'] and 'grid' not in out


def test_calibration_on_a_random_walk_is_close(walk_index):
    report = tm.calibrate(walk_index, samples=400, years_back=1)
    assert report['samples'] >= 300
    assert report['worst_gap'] < 0.12
    assert abs(report['bins'][0]['predicted'] - report['bins'][-1]['predicted']) < 0.25


def test_live_state_ignores_forming_bar():
    m1, m15 = _walk(days=3)
    bars = lambda f: [{'t': str(t), 'o': r.open, 'h': r.high, 'l': r.low, 'c': r.close} for t, r in f.tail(120).iterrows()]
    a = bars(m1); b = [dict(x) for x in a]; b[-1]['h'] += 100
    assert tm.live_state(a, bars(m15)) == tm.live_state(b, bars(m15))

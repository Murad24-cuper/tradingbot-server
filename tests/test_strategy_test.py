import numpy as np
import pandas as pd

from src.research import strategy_test as st


def _m1(prices, spread=10):
    index = pd.date_range('2024-01-02 10:00', periods=len(prices), freq='min', tz='UTC')
    p = np.asarray(prices, dtype=float)
    frame = pd.DataFrame({'open': p, 'high': p + .5, 'low': p - .5, 'close': p, 'spread': float(spread)}, index=index)
    arrays = {k: frame[k].to_numpy() for k in ('open', 'high', 'low', 'close', 'spread')}
    arrays['times'] = index.tz_convert(None).to_numpy().astype('datetime64[ns]').astype(np.int64)
    return frame, arrays


def test_simulate_buy_hits_target_after_costs():
    frame, arrays = _m1([100, 100, 101, 102, 103, 104, 105])
    result = st.simulate(frame, arrays, 0, 'buy', 2.0, 3.0, 60, 0.01, 1)
    # entry 100 + spread 0.10 + slip 0.01; target +3 -> 103.11; exit minus slip
    assert result['reason'] == 'tp' and abs(result['entry'] - 100.11) < 1e-9
    assert abs(result['net_r'] - (3.0 - 0.01) / 2.0) < 1e-9


def test_simulate_stop_first_and_sell_pays_spread():
    frame, arrays = _m1([100, 100, 98, 97])
    sell = st.simulate(frame, arrays, 0, 'sell', 1.0, 1.0, 60, 0.01, 0)
    assert sell['reason'] == 'tp'                                   # low+spread (98-.5+.1) <= target 99
    frame, arrays = _m1([100, 100, 100, 100])
    frame.loc[frame.index[1], ['high', 'low']] = [102, 98]; arrays['high'][1] = 102; arrays['low'][1] = 98
    both = st.simulate(frame, arrays, 0, 'buy', 1.0, 1.0, 60, 0.01, 0)
    assert both['reason'] == 'sl' and both['net_r'] < -0.9          # same bar touches both: stop counts first


def test_simulate_time_exit_uses_last_close_within_limit():
    frame, arrays = _m1([100] * 50)
    result = st.simulate(frame, arrays, 0, 'buy', 5.0, 5.0, 30, 0.01, 0)
    assert result['reason'] == 'time'
    assert result['exit_ns'] == arrays['times'][30] + 60_000_000_000


def test_overlapping_signals_are_dropped_per_strategy_and_symbol():
    trades = pd.DataFrame({'strategy': ['s'] * 4 + ['t'], 'symbol': ['GOLD'] * 5,
                           'entry_ns': [0, 5, 10, 30, 5], 'b_exit_ns': [20, 25, 28, 40, 9], 'b_net_r': [1.0, 1.0, 1.0, -1.0, .5]})
    kept = st.without_overlap(trades, 'b')
    assert kept.groupby('strategy').entry_ns.apply(list).to_dict() == {'s': [0, 30], 't': [5]}


def test_qualification_needs_both_periods_and_enough_trades():
    good = st.metrics(pd.Series([1.0, -0.5] * 60))
    ok, reasons = st.qualifies(good, good, [good, good, good])
    assert ok and not reasons
    bad = st.metrics(pd.Series([-1.0, 0.5] * 60))
    ok, reasons = st.qualifies(bad, good, [good, good, good])
    assert not ok and 'yoxlama illərində mənfi' in reasons
    few = st.metrics(pd.Series([1.0, -0.5] * 10))
    assert not st.qualifies(good, few, [few] * 3)[0]
    assert st.session_of(3) == 'Asiya 00-07' and st.session_of(14) == 'London+NY 12-16'

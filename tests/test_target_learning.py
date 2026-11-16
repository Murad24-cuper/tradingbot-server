from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.db import Database
from src.target_learning import LEVELS, TargetBook, choose_level, learn, path_outcomes, record_paths, session_of


def bars(highs, lows, closes, start=datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)):
    index = pd.date_range(start, periods=len(closes), freq='1min', tz='UTC')
    return pd.DataFrame({'open': closes, 'high': highs, 'low': lows, 'close': closes, 'tick_volume': 1, 'spread': 0}, index=index)


def test_path_outcomes_per_level_with_stop_after_partial_move():
    # buy at 100, risk 1: goes to +0.8R, then falls through the stop
    frame = bars([100.5, 100.8, 100.2, 99.5], [99.9, 100.3, 99.4, 98.9], [100.4, 100.6, 99.6, 99.0])
    result = path_outcomes('buy', 100.0, 1.0, frame)
    assert result['outcomes']['0.5'] == 0.5 and result['outcomes']['0.75'] == 0.75
    assert result['outcomes']['1.0'] == -1.0 and result['outcomes']['2.0'] == -1.0
    assert result['mfe'] == pytest.approx(.8) and result['mae'] == pytest.approx(-1.1)


def test_path_outcomes_time_limit_uses_last_close_and_same_bar_counts_as_stop():
    frame = bars([100.3, 100.4], [99.8, 99.9], [100.1, 100.2])
    assert path_outcomes('sell', 100.0, 1.0, frame)['outcomes']['0.5'] == pytest.approx(-.2)
    both = bars([102.5], [98.5], [100.0])
    assert path_outcomes('buy', 100.0, 1.0, both)['outcomes']['2.0'] == -1.0


def test_session_buckets():
    assert session_of(datetime(2026, 11, 12, 3, tzinfo=timezone.utc)) == 'asia'
    assert session_of(datetime(2026, 11, 12, 9, tzinfo=timezone.utc)) == 'london'
    assert session_of(datetime(2026, 11, 12, 15, tzinfo=timezone.utc)) == 'newyork'
    assert session_of(datetime(2026, 11, 12, 23, tzinfo=timezone.utc)) == 'asia'


def test_choose_level_waits_for_samples_needs_margin_and_moves_one_step():
    stats = {str(l): {'mean': m, 'hit': .5} for l, m in zip(LEVELS, [.10, .08, .02, -.02, -.05, -.10])}
    assert choose_level(stats, 10, 1.5, 1.5, 30, .03) == 1.5          # not enough trades
    assert choose_level(stats, 40, 1.5, 1.5, 30, .03) == 1.25         # one step towards 0.5
    assert choose_level(stats, 40, 0.75, 1.5, 30, .03) == 0.75        # 0.5 better by only 0.02 -> stay


def test_learning_end_to_end_with_database(tmp_path):
    db = Database(tmp_path / 'bot.db')
    rng = np.random.default_rng(1)
    for i in range(40):   # price usually reaches +0.6R then falls back: small target should win
        path = {'outcomes': {'0.5': .5, '0.75': -1.0 if i % 3 else .75, '1.0': -1.0, '1.25': -1.0, '1.5': -1.0, '2.0': -1.0}, 'mfe': .6, 'mae': -1.0, 'cost_r': .05}
        db.save_target_path(i + 1, 'GOLD', 'GOLD', 'london', (datetime(2026, 11, 1, 9, tzinfo=timezone.utc) + timedelta(hours=i)).isoformat(), path)
    for _ in range(5): learn(db, 1.5, min_samples=30)
    rows = {(r['key'], r['session']): r for r in db.get_target_learning()}
    assert rows[('GOLD', 'london')]['level'] == 0.5 and rows[('GOLD', 'all')]['samples'] == 40
    book = TargetBook(db, 30)
    assert book.level('GOLD#', datetime(2026, 11, 12, 10, tzinfo=timezone.utc)) == (0.5, 'london')
    assert book.level('GOLD', datetime(2026, 11, 12, 3, tzinfo=timezone.utc)) == (0.5, 'all')   # asia falls back to all
    assert book.level('US500Cash', datetime(2026, 11, 12, 10, tzinfo=timezone.utc)) is None


def test_record_paths_uses_mt5_bars_and_skips_unfinished_trades(tmp_path, monkeypatch):
    db = Database(tmp_path / 'bot.db')
    opened = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=50)
    with db.connect() as conn:
        for ticket, when in ((1, opened), (2, datetime.now(timezone.utc) - timedelta(minutes=5))):
            conn.execute("INSERT INTO trades(ticket,symbol,direction,volume,open_time,close_time,open_price,initial_risk,status,magic,commission) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (ticket, 'GOLD', 'buy', .1, when.isoformat(), when.isoformat(), 100.0, 1.0, 'closed', 7, 0.0))
    frame = bars([100.2 + .05 * i for i in range(60)], [99.9] * 60, [100.1 + .05 * i for i in range(60)], start=opened - timedelta(minutes=2))
    import src.market_data as md
    monkeypatch.setattr(md, 'get_candles', lambda *a, **k: frame)
    mt5 = SimpleNamespace(symbol_info=lambda s: SimpleNamespace(spread=20, point=.01, trade_tick_value=1.0, trade_tick_size=.01))
    assert record_paths(db, mt5, None, 30, magic=7) == 1
    path = db.get_target_paths()[0]
    assert path['ticket'] == 1 and path['outcomes']['1.0'] == 1.0 and path['cost_r'] == pytest.approx(.2)

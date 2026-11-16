from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.models import Signal
from src.research import time_machine as tm
from src.time_machine_live import LiveTimeMachine


def _rates(n, start=2000.0, step=0.3):
    rows = [(1_700_000_000 + 60 * i, start + step * np.sin(i / 5), start + 0.6 + step * np.sin(i / 5), start - 0.6 + step * np.sin(i / 5), start + 0.1 + step * np.sin(i / 5), 10, 1, 0) for i in range(n)]
    return np.array(rows, dtype=[('time', 'i8'), ('open', 'f8'), ('high', 'f8'), ('low', 'f8'), ('close', 'f8'), ('tick_volume', 'i8'), ('spread', 'i4'), ('real_volume', 'i8')])


@pytest.fixture(scope='module')
def folder(tmp_path_factory):
    root = tmp_path_factory.mktemp('tm'); data = root / 'data'; (data / 'time_machine').mkdir(parents=True)
    rng = np.random.default_rng(9)
    idx = pd.date_range('2024-01-01', periods=120 * 1440, freq='1min', tz='UTC'); idx = idx[idx.dayofweek < 5]
    c = 2000 + np.cumsum(rng.normal(-0.03, 0.4, len(idx))); o = np.r_[c[0], c[:-1]]
    m1 = pd.DataFrame({'open': o, 'high': np.maximum(o, c) + .1, 'low': np.minimum(o, c) - .1, 'close': c}, index=idx)
    m15 = m1.resample('15min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    np.savez(data / 'time_machine' / 'GOLD.npz', **tm.build_index_from_frames(m1, m15))
    return data


def _settings(data, **tm_cfg):
    config = {'universe': {'symbol_aliases': {'GOLD': ['GOLDmicro', 'GOLD']}}, 'risk': {'max_hold_minutes': 30}, 'backtest': {'slippage_points': 1},
              'time_machine': tm_cfg}
    return SimpleNamespace(data_dir=data, config=config)


def _mt5():
    return SimpleNamespace(TIMEFRAME_M1=1, TIMEFRAME_M15=15, symbol_info_tick=lambda s: SimpleNamespace(bid=2000.0, ask=2000.3),
                           copy_rates_from_pos=lambda s, tf, a, n: _rates(n))


NOW = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
SIGNAL = lambda: Signal('GOLDmicro', 'sell', 'M5', 2000.0, 2004.0, 1994.0, .6, 'trend_pullback')


def test_skips_bad_simulation_on_demo_only(folder):
    live = LiveTimeMachine(_settings(folder, block_below_r=5.0))
    info = live.evaluate(_mt5(), SIGNAL(), {'point': .01}, NOW, demo=True)
    assert info['used'] and info['symbol'] == 'GOLD' and info['action'] == 'skip' and info['analogs'] >= tm.MIN_ANALOGS
    real = live.evaluate(_mt5(), SIGNAL(), {'point': .01}, NOW, demo=False)
    assert real['used'] and real['action'] == 'open' and 'REAL' in real['reason']


def test_moves_levels_to_a_better_positive_cell(folder):
    live = LiveTimeMachine(_settings(folder, block_below_r=-5.0, min_improvement_r=-5.0))
    info = live.evaluate(_mt5(), SIGNAL(), {'point': .01}, NOW, demo=True)
    assert info['action'] == 'levels'                       # a falling market: some sell cell is clearly positive
    assert info['sl'] > 2000.0 > info['tp'] and info['choice_r'] > 0 and info['choice_r'] - info['expectancy_r'] >= -5.0


def test_fails_open_without_index_or_when_disabled(tmp_path, folder):
    assert LiveTimeMachine(_settings(tmp_path)).evaluate(_mt5(), SIGNAL(), {}, NOW, True) == {'used': False, 'action': 'open', 'decides': True, 'reason': 'indeks yoxdur', 'symbol': 'GOLD'}
    assert LiveTimeMachine(_settings(folder, enabled=False)).evaluate(_mt5(), SIGNAL(), {}, NOW, True)['action'] == 'open'


def test_log_line_is_written_for_the_panel(folder):
    live = LiveTimeMachine(_settings(folder, block_below_r=5.0))
    info = live.evaluate(_mt5(), SIGNAL(), {'point': .01}, NOW, demo=True)
    live.log(SIGNAL(), info, NOW, 'GOLDmicro')
    line = (folder / 'simulations.jsonl').read_text(encoding='utf-8').strip().splitlines()[-1]
    assert '"source": "bot"' in line and '"action": "skip"' in line


def test_index_found_under_the_broker_name(folder, tmp_path):
    data = tmp_path / 'data'; (data / 'time_machine').mkdir(parents=True)
    (data / 'time_machine' / 'US500Cash.npz').write_bytes((folder / 'time_machine' / 'GOLD.npz').read_bytes())
    settings = _settings(data, block_below_r=5.0); settings.config['universe']['symbol_aliases'] = {'US500': ['US500Cash']}
    info = LiveTimeMachine(settings).evaluate(_mt5(), Signal('US500Cash', 'sell', 'M5', 2000.0, 2004.0, 1994.0, .6, 'trend_pullback'), {'point': .01}, NOW, demo=True)
    assert info['used'] and info['symbol'] == 'US500Cash'


def test_live_board_simulates_every_symbol_both_sides(folder):
    from src.time_machine_live import live_board
    bars = lambda n: [{'o': 2000 + .1 * (i % 7), 'h': 2000.6 + .1 * (i % 7), 'l': 1999.4 + .1 * (i % 7), 'c': 2000.2 + .1 * (i % 7)} for i in range(n)]
    sym = lambda name: {'symbol': name, 'broker': name, 'spread': .3, 'digits': 2, 'candles': {'M1': bars(120), 'M15': bars(120)},
                        'auto': {'buy': {'entry': 2000.3, 'tp': 2006.6, 'stop_distance': 4.0}, 'sell': {'entry': 2000.0, 'tp': 1993.7, 'stop_distance': 4.0}}}
    board = live_board(LiveTimeMachine(_settings(folder)), {'symbols': [sym('GOLD'), sym('NOHISTORY')]}, NOW, fan=10)
    gold, missing = board['symbols']
    assert set(gold['sides']) == {'buy', 'sell'} and gold['sides']['sell']['enough'] and len(gold['sides']['buy']['fan']) == 10
    assert gold['sides']['buy']['stop'] < 2000.3 < gold['sides']['buy']['target']
    assert missing['error'] == 'indeks yoxdur' and missing['sides'] == {}


def _position(**kw):
    from datetime import timedelta
    base = {'ticket': 7, 'symbol': 'GOLD', 'direction': 'buy', 'open_price': 2000.0, 'sl': 1996.0, 'tp': 2006.0, 'current': 2001.0,
            'risk': 4.0, 'opened': NOW - timedelta(minutes=10), 'managed': True}
    return {**base, **kw}


def _market_symbol():
    bars = lambda n: [{'o': 2000 + .1 * (i % 7), 'h': 2000.6 + .1 * (i % 7), 'l': 1999.4 + .1 * (i % 7), 'c': 2000.2 + .1 * (i % 7)} for i in range(n)]
    return {'symbol': 'GOLD', 'broker': 'GOLD', 'candles': {'M1': bars(120), 'M15': bars(120)}}


def test_open_position_buy_in_a_falling_market_is_closed_on_demo(folder):
    from src.time_machine_live import position_outlook
    live = LiveTimeMachine(_settings(folder))                                         # the fixture market falls: holding a buy is worse
    info = position_outlook(live, _position(), _market_symbol(), NOW, demo=True)
    assert info['used'] and info['hold_r'] < info['now_r'] and info['action'] == 'close'
    assert info['now_r'] == pytest.approx(0.25) and 5 <= info['minutes_left'] <= 30
    assert position_outlook(live, _position(), _market_symbol(), NOW, demo=False)['action'] == 'hold'           # REAL: only shown
    assert position_outlook(live, _position(managed=False), _market_symbol(), NOW, demo=True)['action'] == 'hold'
    young = _position(opened=NOW)
    assert position_outlook(live, young, _market_symbol(), NOW, demo=True)['action'] == 'hold'


def test_open_position_sell_in_a_falling_market_is_kept(folder):
    from src.time_machine_live import position_outlook
    live = LiveTimeMachine(_settings(folder))
    info = position_outlook(live, _position(direction='sell', sl=2004.0, tp=1994.0, current=1999.0), _market_symbol(), NOW, demo=True)
    assert info['used'] and info['action'] == 'hold' and info['hold_r'] >= info['now_r'] - 0.10


def test_open_position_without_levels_or_disabled(folder):
    from src.time_machine_live import position_outlook
    live = LiveTimeMachine(_settings(folder))
    assert position_outlook(live, _position(sl=0), _market_symbol(), NOW, True)['used'] is False
    assert position_outlook(LiveTimeMachine(_settings(folder, manage_open=False)), _position(), _market_symbol(), NOW, True)['action'] == 'hold'

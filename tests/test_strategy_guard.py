import json
from datetime import datetime, timedelta, timezone

from src.db import Database
from src.strategy_guard import StrategyGuard, evaluate, DEFAULTS, trade_r
from src.loss_autopsy import LossAutopsy, analyse, tags_of
from src.commands import is_allowed, parse_open_signal

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def _trade(db, ticket, strategy, symbol, r, when, features=None):
    feats = {'process_review': {'outcome_r': r}, **(features or {})}
    db.insert_trade(ticket=ticket, symbol=symbol, strategy=strategy, status='closed', direction='buy', volume=0.1,
                    open_time=(when - timedelta(minutes=20)).isoformat(), close_time=when.isoformat(), profit=r * 10, magic=1,
                    features=json.dumps(feats))


def test_one_trade_moves_the_weight_little_and_a_bad_run_pauses():
    assert evaluate([-1.0], 0.0, DEFAULTS, False)['weight'] > 0.9                 # one loss is mostly luck
    good = evaluate([0.6] * 30, 0.05, DEFAULTS, False)
    assert good['state'] == 'active' and good['weight'] == 1.25                     # capped on demo
    assert evaluate([0.6] * 30, 0.05, DEFAULTS, True)['weight'] == 1.0              # never more on REAL
    bad = evaluate([-0.5] * 25, 0.0, DEFAULTS, False)
    assert bad['state'] == 'paused' and 'son 25' in bad['reason']


def test_guard_pauses_then_fresh_start(tmp_path):
    db = Database(tmp_path / 'g.db')
    for i in range(25): _trade(db, i + 1, 'vwap_reversion', 'GOLD', -0.6, NOW - timedelta(hours=25 - i))
    for i in range(5): _trade(db, 100 + i, 'session_breakout', 'GOLD', 1.0, NOW - timedelta(hours=5 - i))
    _trade(db, 500, 'manual:vwap_reversion', 'GOLD', -1.0, NOW)                       # manual trades never count
    guard = StrategyGuard(db, {}, tmp_path, magic=1, clock=lambda: NOW)
    changes = guard.refresh()
    assert [(c['strategy'], c['state']) for c in changes] == [('vwap_reversion', 'paused')]
    allowed, weight, reason = guard.decision('vwap_reversion', 'GOLD')
    assert not allowed and 'keşikçisi' in reason
    assert guard.decision('session_breakout', 'GOLD')[0] and guard.decision('session_breakout', 'GOLD')[1] > 1.0
    assert guard.decision('nothing', 'GOLD') == (True, 1.0, '')
    guard.reset('vwap_reversion', 'GOLD'); guard.refresh()
    assert guard.decision('vwap_reversion', 'GOLD')[0]                                   # older trades no longer count
    later = StrategyGuard(db, {}, tmp_path, magic=1, clock=lambda: NOW + timedelta(days=8))
    assert later.decision('vwap_reversion', 'GOLD')[0]


def test_trade_r_falls_back_to_money_over_sized_risk():
    assert trade_r({'features': json.dumps({'process_review': {'outcome_r': -0.4}})}) == -0.4
    assert trade_r({'profit': 5, 'commission': -1, 'features': json.dumps({'sizing': {'stressed_risk_money': 8}})}) == 0.5
    assert trade_r({'profit': 5, 'features': '{}'}) is None


def test_autopsy_finds_weak_spot_and_needs_history_to_filter(tmp_path):
    db = Database(tmp_path / 'a.db')
    weak = {'exploration': {'soft_failures': ['main MTF agreement threshold not met']}}
    for i in range(24): _trade(db, i + 1, 'trend_pullback', 'GOLD', -0.8, NOW - timedelta(hours=40 - i), weak)
    for i in range(16): _trade(db, 100 + i, 'trend_pullback', 'GOLD', 0.4, NOW - timedelta(hours=16 - i))
    rows = [dict(r) for r in db.connect().execute("SELECT * FROM trades ORDER BY close_time")]
    spots = analyse(rows)
    assert any(s['tag'] == 'mtf_zeif' for s in spots)
    autopsy = LossAutopsy(db, {}, tmp_path, magic=1)
    assert autopsy.refresh() == []                                                        # no history file: only a suggestion
    with db.connect() as c: assert c.execute("SELECT status FROM learned_filters WHERE tag='mtf_zeif'").fetchone()[0] == 'suggested'
    summary = {'overall': [{'strategy': 'trend_pullback', 'symbol': 'GOLD', 'variant': 'b', 'mtf_filter': False, 'yoxlama': {'trades': 100, 'expectancy_r': -0.1}, 'imtahan': {'trades': 100, 'expectancy_r': -0.1}},
                           {'strategy': 'trend_pullback', 'symbol': 'GOLD', 'variant': 'b', 'mtf_filter': True, 'yoxlama': {'trades': 50, 'expectancy_r': 0.05}, 'imtahan': {'trades': 50, 'expectancy_r': 0.02}}]}
    (tmp_path / 'strategy_test.json').write_text(json.dumps(summary))
    activated = autopsy.refresh()
    assert [a['tag'] for a in activated] == ['mtf_zeif']
    assert 'öyrənilmiş filtr' in autopsy.blocks('trend_pullback', 'GOLD', weak, NOW)
    assert autopsy.blocks('trend_pullback', 'GOLD', {}, NOW) == ''
    with db.connect() as c: filter_id = c.execute("SELECT id FROM learned_filters WHERE tag='mtf_zeif'").fetchone()[0]
    autopsy.remove(filter_id); autopsy.refresh()
    assert autopsy.blocks('trend_pullback', 'GOLD', weak, NOW) == ''                     # the user's "no" sticks


def test_tags_and_commands():
    tags = tags_of({'M15': {'adx': 12}, 'cost_map': {'median_cost_stop_ratio': 0.08}, 'news_context': {'minutes_to_next_event': 10}}, '2026-11-16T08:30:00+00:00')
    assert tags == {'session:London', 'trend_zeif', 'spred_yuksek', 'xeber_yaxin'}
    assert is_allowed('open_signal:12:1.5:1') and is_allowed('breakeven:991') and is_allowed('guard_reset:trend_pullback:GOLD')
    assert not is_allowed('open_signal:12:x:1') and not is_allowed('rm -rf') and not is_allowed('guard_reset:a b:GOLD')
    assert parse_open_signal('open_signal:7:0.5:0') == (7, 0.5, False)


def test_unmanaged_manual_positions_are_left_to_stop_and_target():
    from src.executor import _unmanaged
    assert _unmanaged({'features': json.dumps({'manual_unmanaged': True})})
    assert not _unmanaged({'features': json.dumps({'manual': True})}) and not _unmanaged({}) and not _unmanaged(None)


def test_config_sections_are_added_once_and_keep_the_rest():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('acs', Path(__file__).resolve().parents[1] / 'scripts' / 'add_config_sections.py')
    acs = importlib.util.module_from_spec(spec); spec.loader.exec_module(acs)
    text = 'a: 1  # keep me\nnotify:\n  x: 2\n'
    new, changed = acs.add_sections(text)
    assert changed and new.startswith('a: 1  # keep me\n') and new.index('strategy_guard:') < new.index('notify:')
    assert acs.add_sections(new) == (new, False)

"""New panel API: login flow, data endpoints, commands and settings through the safe editor."""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip('fastapi')                   # the panel runs on the server; a PC without fastapi skips these
pytest.importorskip('httpx')
from fastapi.testclient import TestClient

from src.db import Database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    from src.panel import app as panel_app
    (tmp_path / 'data').mkdir()
    shutil.copy(ROOT / 'tests' / 'fixtures' / 'default_config.yaml', tmp_path / 'config.yaml')
    db = Database(tmp_path / 'data' / 'bot.db')
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO bot_status(id,time,status,mode,account_type,mt5_connected,trade_allowed) VALUES (1,?,?,?,?,1,1)", (now.isoformat(), 'running', 'trade', 'DEMO'))
        c.execute('INSERT INTO equity_snapshots(time,balance,equity,margin,free_margin,open_positions) VALUES (?,?,?,?,?,?)', (now.isoformat(), 1000, 1005, 0, 1000, 0))
    db.insert_trade(ticket=1, symbol='GOLD', strategy='trend_pullback', status='closed', direction='buy', volume=.1, open_time=(now - timedelta(hours=2)).isoformat(),
                    close_time=(now - timedelta(hours=1)).isoformat(), profit=12.5, close_reason='tp', magic=1, features='{}')
    signal_id = db.insert_signal(time=now.isoformat(), symbol='GOLD', strategy='vwap_reversion', direction='sell', timeframe='M15', confidence=.5,
                                 entry=2650, sl=2655, tp=2640, expected_cost=.1, score=0, taken=0, skip_reason='M1/M5/M15 direction disagreement')
    (tmp_path / 'data' / 'strategy_test.json').write_text(json.dumps({'overall': [{'strategy': 'vwap_reversion', 'symbol': 'GOLD', 'variant': 'b', 'mtf_filter': False,
        'imtahan': {'trades': 140, 'expectancy_r': 0.04, 'profit_factor': 1.12}}], 'combinations': [], 'built_at_utc': now.isoformat(), 'test_start': '2023-01-01'}))
    monkeypatch.setattr(panel_app, 'ROOT', tmp_path)
    monkeypatch.setattr(panel_app, '_settings', None)
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'data' / 'bot.db'))
    from src.config import load_settings
    settings = load_settings(config_path=tmp_path / 'config.yaml', require_credentials=False)
    monkeypatch.setattr(panel_app, 'settings', lambda: settings)
    monkeypatch.setattr(panel_app.editor, 'validate_text', lambda text, root: None)
    client = TestClient(panel_app.app, base_url='https://testserver')
    return client, tmp_path, signal_id


def _login(client):
    assert client.post('/api/setup', json={'username': 'murad', 'password': 'uzun-parol-1'}).status_code == 200


def test_setup_only_without_proxy_then_login_required(panel):
    client, _, _ = panel
    assert client.get('/api/overview').status_code == 401
    assert client.post('/api/setup', json={'username': 'x', 'password': 'uzun-parol-1'}, headers={'X-Forwarded-For': '1.2.3.4'}).status_code == 403
    _login(client)
    assert client.get('/api/session').json()['authenticated']
    assert client.post('/api/setup', json={'username': 'y', 'password': 'uzun-parol-2'}).status_code == 403   # only once
    client.post('/api/logout', json={})
    client.cookies.clear()
    assert client.post('/api/login', json={'username': 'murad', 'password': 'səhv'}).status_code == 401
    assert client.post('/api/login', json={'username': 'murad', 'password': 'uzun-parol-1'}).status_code == 200


def test_overview_opportunities_strategies_journal(panel):
    client, _, signal_id = panel; _login(client)
    ov = client.get('/api/overview').json()
    assert ov['equity'] == 1005 and ov['opportunities'] == 1 and ov['status']['status'] == 'running' and not ov['status']['stale']
    opp = client.get('/api/opportunities').json()['items']
    assert opp[0]['id'] == signal_id and opp[0]['history']['trades'] == 140 and abs(opp[0]['reward_risk'] - 2.0) < 1e-9
    strategies = client.get('/api/strategies').json()
    assert any(s['name'] == 'vwap_reversion' and s['test']['expectancy_r'] == 0.04 for s in strategies['strategies'])
    journal = client.get('/api/journal?days=7').json()
    assert journal['summary']['trades'] == 1 and journal['by_strategy'][0]['label']


def test_commands_are_allow_listed_and_need_the_panel_header(panel):
    client, tmp, signal_id = panel; _login(client)
    assert client.post('/api/command', json={'command': 'pause'}).status_code == 400            # no X-Panel header
    h = {'X-Panel': '1'}
    assert client.post('/api/command', json={'command': 'rm -rf /'}, headers=h).status_code == 400
    assert client.post('/api/command', json={'command': 'diagnose_order'}, headers=h).status_code == 400
    ok = client.post('/api/command', json={'command': f'open_signal:{signal_id}:1:1'}, headers=h)
    assert ok.status_code == 200
    assert client.get(f"/api/command/{ok.json()['id']}").json()['status'] == 'pending'
    assert client.post('/api/command', json={'command': f'open_signal:{signal_id}:1:1'}, headers=h).status_code == 409   # already queued


def test_settings_save_through_safe_editor_and_restart(panel):
    client, tmp, _ = panel; _login(client); h = {'X-Panel': '1'}
    fields = {f['path']: f for f in client.get('/api/settings').json()['fields']}
    assert 'protections.enabled' in fields and 'strategy_guard.enabled' in fields
    bad = client.post('/api/settings', json={'changes': {'account.mode': 'real'}}, headers=h)
    assert bad.status_code == 400                                                                 # not whitelisted
    good = client.post('/api/settings', json={'changes': {'protections.enabled': False}}, headers=h)
    assert good.status_code == 200
    assert 'enabled: false' in (tmp / 'config.yaml').read_text().split('protections:')[1][:80]
    assert client.post('/api/strategies/toggle', json={'name': 'vwap_reversion', 'enabled': False}, headers=h).status_code == 200
    from src.dashboard import queries
    assert any(c['command'] == 'restart' for c in queries.get_recent_commands(tmp / 'data' / 'bot.db'))


def test_static_app_is_served(panel):
    client, _, _ = panel
    page = client.get('/')
    assert page.status_code == 200 and '/static/app.js?v=' in page.text and '/static/app.css?v=' in page.text
    assert page.headers['cache-control'] == 'no-store'
    asset = client.get('/static/app.js')
    assert asset.status_code == 200 and asset.headers['cache-control'] == 'no-cache'


def test_market_screen_and_market_order_command(panel):
    client, tmp, _ = panel; _login(client)
    market = {'updated_at': datetime.now(timezone.utc).isoformat(), 'symbols': [{'symbol': 'GOLD', 'broker': 'GOLD', 'bid': 2650.0, 'ask': 2650.3, 'digits': 2,
              'candles': {'M5': [{'t': 'x', 'o': 1, 'h': 2, 'l': .5, 'c': 1.5}]}, 'auto': {'buy': {'sl': 2647, 'tp': 2655}},
              'view': {'trend': 'yuxarı', 'text': 'M15-də trend yuxarıdır'}, 'signals': [{'strategy': 'vwap_reversion', 'direction': 'sell', 'taken': 0, 'skip_reason': 'M1/M5/M15 direction disagreement'}]}]}
    (tmp / 'data' / 'market.json').write_text(json.dumps(market))
    data = client.get('/api/market').json()
    assert data['symbols'][0]['signals'][0]['strategy_label'] and data['demo'] and data['opportunities'] and data['age_seconds'] < 60
    h = {'X-Panel': '1'}
    assert client.post('/api/command', json={'command': 'open_market:GOLD:buy:1:1:0:0'}, headers=h).status_code == 200
    assert client.post('/api/command', json={'command': 'open_market:GOLD:hold:1:1:0:0'}, headers=h).status_code == 400
    assert client.post('/api/command', json={'command': 'open_market:GO LD:buy:1:1:0:0'}, headers=h).status_code == 400


def _walk_history(folder: Path):
    import numpy as np, pandas as pd
    from src.parquet_compat import write_parquet
    folder.mkdir(parents=True)
    rng = np.random.default_rng(4)
    idx = pd.date_range('2024-01-01', periods=90 * 1440, freq='1min', tz='UTC'); idx = idx[idx.dayofweek < 5]
    c = 2650 + np.cumsum(rng.normal(0, .4, len(idx))); o = np.r_[c[0], c[:-1]]
    m1 = pd.DataFrame({'open': o, 'high': np.maximum(o, c) + .1, 'low': np.minimum(o, c) - .1, 'close': c}, index=idx)
    write_parquet(m1, folder / 'GOLD_M1.parquet')
    write_parquet(m1.resample('15min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna(), folder / 'GOLD_M15.parquet')
    (folder / 'manifest.json').write_text(json.dumps({'symbols': [{'symbol': 'GOLD'}]}))
    (folder / 'symbol_specs.json').write_text(json.dumps({'GOLD': {'point': 0.01}}))


def _live_market(tmp):
    bars = lambda n, step: [{'t': str(i), 'o': 2650 + .1 * (i % 7), 'h': 2650.6 + .1 * (i % 7), 'l': 2649.4 + .1 * (i % 7), 'c': 2650.2 + .1 * (i % 7)} for i in range(n)]
    market = {'updated_at': datetime.now(timezone.utc).isoformat(), 'symbols': [{'symbol': 'GOLD', 'broker': 'GOLD', 'bid': 2650.0, 'ask': 2650.3, 'spread': .3, 'digits': 2,
              'money_per_price_lot': 100.0, 'volume_min': .01, 'candles': {'M1': bars(120, 1), 'M15': bars(120, 15)},
              'auto': {'buy': {'entry': 2650.3, 'sl': 2646.3, 'tp': 2656.6, 'stop_distance': 4.0}, 'sell': {'entry': 2650.0, 'sl': 2654.0, 'tp': 2643.7, 'stop_distance': 4.0}}}]}
    (tmp / 'data' / 'market.json').write_text(json.dumps(market))


def test_time_machine_endpoint_grid_fan_and_log(panel):
    client, tmp, _ = panel; _login(client); h = {'X-Panel': '1'}
    _walk_history(tmp / 'data' / 'history_long'); _live_market(tmp)
    missing = client.post('/api/simulate', json={'symbol': 'GOLD', 'direction': 'sell'}, headers=h)
    assert missing.status_code == 400 and 'check_time_machine' in missing.json()['detail']     # the panel never builds the index itself
    from src.research import time_machine as tm
    tm.load_index(tmp / 'data' / 'history_long', 'GOLD', tmp / 'data' / 'time_machine')
    res = client.post('/api/simulate', json={'symbol': 'GOLD', 'direction': 'sell', 'scale': 'Normal', 'minutes': 30}, headers=h)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out['enough'] and len(out['grid']) == 3 and out['fan'] and out['reliability'] == 'yoxlanmayıb'
    assert out['stop'] == pytest.approx(2654.0) and out['target'] == pytest.approx(2643.7)
    items = client.get('/api/simulations').json()['items']
    assert items and items[0]['symbol'] == 'GOLD' and items[0]['real'] in ('gözlənilir', 'açılmadı')
    bad = client.post('/api/simulate', json={'symbol': 'GOLD', 'direction': 'sell', 'sl': 2640, 'tp': 2630}, headers=h)
    assert bad.status_code == 400 and 'Stop' in bad.json()['detail']


def test_today_trades_and_order_check(panel):
    client, tmp, _ = panel; _login(client); h = {'X-Panel': '1'}
    data = client.get('/api/today').json()
    assert data['closed'] == 1 and data['wins'] == 1 and data['net'] == pytest.approx(12.5) and data['items'][0]['symbol'] == 'GOLD'
    assert client.get('/api/today?date=2001-01-01').json()['items'] == []
    _live_market(tmp)
    ok = client.post('/api/order_check', json={'symbol': 'GOLD', 'direction': 'buy', 'risk': 1, 'scale': 'Normal'}, headers=h).json()
    assert ok['can_open'] and any(c['name'] == 'Lot' and c['state'] == 'ok' for c in ok['checks'])
    wide = client.post('/api/order_check', json={'symbol': 'GOLD', 'direction': 'buy', 'risk': .05, 'sl': 2600, 'tp': 2700}, headers=h).json()
    assert not wide['can_open'] and any(c['name'] == 'Lot' and c['state'] == 'fail' for c in wide['checks'])
    (tmp / 'STOP').write_text('HALT')
    halted = client.post('/api/order_check', json={'symbol': 'GOLD', 'direction': 'buy', 'risk': 1}, headers=h).json()
    assert not halted['can_open']


def test_simulation_uses_the_broker_named_index(panel):
    client, tmp, _ = panel; _login(client); h = {'X-Panel': '1'}
    _walk_history(tmp / 'data' / 'history_long'); _live_market(tmp)
    from src.research import time_machine as tm
    tm.load_index(tmp / 'data' / 'history_long', 'GOLD', tmp / 'data' / 'time_machine')
    (tmp / 'data' / 'time_machine' / 'GOLD.npz').rename(tmp / 'data' / 'time_machine' / 'GOLDb.npz')
    market = json.loads((tmp / 'data' / 'market.json').read_text()); market['symbols'][0]['broker'] = 'GOLDb'
    (tmp / 'data' / 'market.json').write_text(json.dumps(market))
    res = client.post('/api/simulate', json={'symbol': 'GOLD', 'direction': 'sell'}, headers=h)
    assert res.status_code == 200 and res.json()['symbol'] == 'GOLD' and res.json()['history_symbol'] == 'GOLDb'


def test_live_simulations_endpoint(panel):
    client, tmp, _ = panel; _login(client)
    assert client.get('/api/simulations/live').json() == {'symbols': [], 'age_seconds': None}
    (tmp / 'data' / 'simulations_live.json').write_text(json.dumps({'updated_at': datetime.now(timezone.utc).isoformat(), 'symbols': [{'symbol': 'GOLD', 'sides': {}}]}))
    data = client.get('/api/simulations/live').json()
    assert data['symbols'][0]['symbol'] == 'GOLD' and data['age_seconds'] < 60


def test_positions_carry_the_time_machine_outlook(panel):
    client, tmp, _ = panel; _login(client)
    db = Database(tmp / 'data' / 'bot.db')
    db.insert_trade(ticket=77, symbol='GOLD', strategy='trend_pullback', status='open', direction='buy', volume=.01, open_time=datetime.now(timezone.utc).isoformat(),
                    magic=1, features='{}', open_price=2650, sl=2646, tp=2656, initial_risk=4)
    (tmp / 'data' / 'simulations_live.json').write_text(json.dumps({'updated_at': datetime.now(timezone.utc).isoformat(), 'symbols': [],
        'positions': [{'ticket': 77, 'used': True, 'action': 'hold', 'hold_r': .2, 'now_r': .1, 'target': .3, 'stop': .2, 'time': .5, 'minutes_left': 20}]}))
    pos = next(p for p in client.get('/api/positions').json()['positions'] if p['ticket'] == 77)
    assert pos['outlook']['hold_r'] == .2 and pos['outlook']['minutes_left'] == 20

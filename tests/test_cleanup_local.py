import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.cleanup_local import bot_running, history_to_remove, junk_paths, keep_keys

FOCUS = ['GOLD', 'US500', 'NAS100', 'US30', 'SILVER']


def test_broker_spellings_of_focus_symbols_are_kept(tmp_path):
    for name in ['GOLD_M1', 'GOLD#_M5', 'US500Cash_M1', 'US100Cash_M1', 'US30Cash_M15', 'SILVER_M1', 'XAUUSD_M1',
                 'EURUSD_M1', 'USDJPY#_M5', 'GER40Cash_M1', 'GOLD24-7_M1', 'BTCUSD_M1']:
        (tmp_path / f'{name}.parquet').write_bytes(b'x')
    removed = {p.stem for p in history_to_remove(tmp_path, FOCUS, recent_symbols={'BTCUSD'})}
    assert removed == {'EURUSD_M1', 'USDJPY#_M5', 'GER40Cash_M1', 'GOLD24-7_M1'}
    assert 'US100' in keep_keys(FOCUS)


def test_junk_paths_never_include_project_files(tmp_path):
    for d in ['update_backup', 'yenilenme', 'yenilenme2', 'src/__pycache__', '.venv/lib/__pycache__', 'src', 'data']:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    for f in ['yenilenme.zip', 'x.patch', 'config.yaml', '.env', 'src/bot.py']: (tmp_path / f).write_text('x')
    names = {str(p.relative_to(tmp_path)).replace('\\', '/') for p in junk_paths(tmp_path)}
    assert names == {'update_backup', 'yenilenme', 'yenilenme2', 'yenilenme.zip', 'x.patch', 'src/__pycache__'}


def test_bot_running_uses_fresh_heartbeat(tmp_path):
    db = tmp_path / 'bot.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE bot_status(id INTEGER PRIMARY KEY, time TEXT, status TEXT)')
        conn.execute('INSERT INTO bot_status VALUES (1,?,?)', (datetime.now(timezone.utc).isoformat(), 'running'))
    assert bot_running(db)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE bot_status SET time=?', ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    assert not bot_running(db)

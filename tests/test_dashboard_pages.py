"""Every dashboard page renders on desktop and phone views; login rules for the public panel."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / 'src' / 'dashboard' / 'app.py'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'bot.db'))
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('DASHBOARD_PASSWORD', '')
    monkeypatch.delenv('DASHBOARD_REQUIRE_PASSWORD', raising=False)
    from src.db import Database
    Database(tmp_path / 'bot.db')
    return tmp_path


def run(**query):
    app = AppTest.from_file(str(APP), default_timeout=60)
    for key, value in query.items(): app.query_params[key] = value
    app.run()
    return app


@pytest.mark.parametrize('view', [None, 'phone'])
@pytest.mark.parametrize('page', ['overview', 'positions', 'history', 'settings', 'brain', 'system'])
def test_page_renders(page, view, env):
    app = run(page=page, **({'view': view} if view else {}))
    assert not app.exception, [str(item.value) for item in app.exception]


def test_settings_page_has_save_button(env):
    app = run(page='settings')
    assert any('Yadda saxla' in button.label for button in app.button)


def test_short_env_password_keeps_public_panel_closed(env, monkeypatch):
    monkeypatch.setenv('DASHBOARD_REQUIRE_PASSWORD', '1'); monkeypatch.setenv('DASHBOARD_PASSWORD', 'short')
    app = run()
    assert not app.exception and any('DASHBOARD_PASSWORD' in e.value for e in app.error) and not app.button


def test_server_panel_asks_to_create_login_through_tunnel_then_logs_in(env, monkeypatch):
    monkeypatch.setenv('DASHBOARD_REQUIRE_PASSWORD', '1')
    app = run()
    assert [t.label for t in app.text_input][:1] == ['İstifadəçi adı'] and not app.exception
    app.text_input[0].input('murad'); app.text_input[1].input('uzun-parol-1'); app.text_input[2].input('uzun-parol-1')
    app.button[0].click(); app.run()
    assert not app.exception and any(b.label and 'Botu' in b.label for b in app.button)   # panel is open
    from src.dashboard import auth
    assert auth.username(env / 'data') == 'murad'
    fresh = run()                                   # a new browser session must log in
    assert [t.label for t in fresh.text_input] == ['İstifadəçi adı', 'Parol']


def test_public_request_detection(monkeypatch):
    from src.dashboard import app as module
    monkeypatch.setattr(module.st, 'context', SimpleNamespace(headers={'X-Forwarded-For': '1.2.3.4'}))
    assert module._public_request()
    monkeypatch.setattr(module.st, 'context', SimpleNamespace(headers={}))
    assert not module._public_request()


def test_auth_hashes_password_and_locks_after_failures(tmp_path):
    from src.dashboard import auth
    with pytest.raises(ValueError): auth.set_user(tmp_path, 'a', 'short')
    auth.set_user(tmp_path, 'murad', 'uzun-parol-1')
    assert 'uzun-parol-1' not in (tmp_path / 'panel_auth.json').read_text()
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    assert auth.verify(tmp_path, 'murad', 'uzun-parol-1', now)
    for i in range(auth.MAX_FAILURES): assert not auth.verify(tmp_path, 'murad', 'wrong', now + timedelta(seconds=i))
    assert auth.locked_until(tmp_path, now + timedelta(minutes=1))
    assert not auth.verify(tmp_path, 'murad', 'uzun-parol-1', now + timedelta(minutes=1))     # locked even with the right password
    assert auth.verify(tmp_path, 'murad', 'uzun-parol-1', now + timedelta(minutes=30))       # unlocked later

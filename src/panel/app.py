"""New web panel: a small JSON API over the bot's database plus one static app.

Run:  uvicorn src.panel.app:app --host 0.0.0.0 --port 8000
Changes never touch MT5 directly: buttons queue allow-listed commands for the bot, and
settings go through the dashboard's safe editor (whitelist, validation, backup, restart).
Login: the PBKDF2 user of the old panel (data/panel_auth.json). The first login can only be
created from the server itself (SSH tunnel); a request that came through the proxy cannot.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_settings
from ..dashboard import auth, history, queries
from ..dashboard import settings_editor as editor
from ..dashboard.presentation import AZ_TZ, close_reason_label, strategy_label, translate_message
from ..commands import is_allowed

ROOT = Path(os.getenv('PANEL_ROOT') or Path(__file__).resolve().parents[2])
STATIC = Path(__file__).with_name('static')
COOKIE = 'panel_session'
SESSION_DAYS = 7
_settings = None


def settings():
    global _settings
    if _settings is None: _settings = load_settings(config_path=ROOT / 'config.yaml', require_credentials=False)
    return _settings


def data_dir() -> Path: return Path(settings().data_dir)
def db_path() -> Path: return Path(os.getenv('BOT_DB_PATH') or settings().db_path)
def config_path() -> Path: return ROOT / 'config.yaml'


def rows(sql: str, params=()) -> list[dict]:
    try:
        with sqlite3.connect(db_path().resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params)]
    except (sqlite3.Error, OSError):
        return []


# ---------------------------------------------------------------- sessions
def _secret() -> bytes:
    path = data_dir() / 'panel_secret'
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding='utf-8')
        try: os.chmod(path, 0o600)
        except OSError: pass
    return path.read_text(encoding='utf-8').strip().encode()


def _sign(user: str, expires: int) -> str:
    body = f'{user}|{expires}'
    mac = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{body}|{mac}'.encode()).decode()


def _user_of(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token: return None
    try:
        user, expires, mac = base64.urlsafe_b64decode(token.encode()).decode().rsplit('|', 2)
        if int(expires) < time.time(): return None
        good = hmac.new(_secret(), f'{user}|{expires}'.encode(), hashlib.sha256).hexdigest()
        return user if hmac.compare_digest(good, mac) and user == auth.username(data_dir()) else None
    except (ValueError, UnicodeDecodeError):
        return None


def is_public(request: Request) -> bool:
    """True for requests that came through the public proxy (Caddy adds X-Forwarded-For)."""
    return bool(request.headers.get('x-forwarded-for'))


def authenticated(request: Request) -> bool:
    return auth.has_user(data_dir()) and _user_of(request) is not None


def _set_cookie(response: Response, request: Request, user: str):
    expires = int(time.time()) + SESSION_DAYS * 86400
    response.set_cookie(COOKIE, _sign(user, expires), max_age=SESSION_DAYS * 86400, httponly=True, samesite='strict',
                        secure=request.headers.get('x-forwarded-proto', request.url.scheme) == 'https', path='/')


def _clean(value):
    """JSON-safe copy: inf/nan become null (profit factor without losses is infinite)."""
    if isinstance(value, float): return value if value == value and value not in (float('inf'), float('-inf')) else None
    if isinstance(value, dict): return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_clean(v) for v in value]
    return value


class SafeJSON(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(_clean(content), ensure_ascii=False, default=str, separators=(',', ':')).encode('utf-8')


app = FastAPI(title='Murad Bot panel', docs_url=None, redoc_url=None, openapi_url=None, default_response_class=SafeJSON)


@app.middleware('http')
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith('/api/') and not path.startswith('/api/session') and path not in ('/api/login', '/api/setup', '/api/logout'):
        if not authenticated(request): return JSONResponse({'error': 'login'}, status_code=401)
        if request.method != 'GET' and request.headers.get('x-panel') != '1':
            return JSONResponse({'error': 'bad request'}, status_code=400)      # CSRF: only our own app sends this header
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if path.startswith('/api/'): response.headers['Cache-Control'] = 'no-store'
    elif path.startswith('/static/'): response.headers['Cache-Control'] = 'no-cache'      # always re-check: a new version shows at once
    return response


# ---------------------------------------------------------------- auth endpoints
@app.get('/api/session')
def session(request: Request):
    has = auth.has_user(data_dir())
    return {'authenticated': authenticated(request), 'has_user': has, 'can_setup': (not has) and not is_public(request),
            'user': _user_of(request)}


@app.post('/api/login')
async def login(request: Request):
    body = await request.json()
    name, password = str(body.get('username', '')), str(body.get('password', ''))
    until = auth.locked_until(data_dir())
    if until: raise HTTPException(429, f'Çox səhv cəhd. {until.astimezone(AZ_TZ):%H:%M}-dən sonra yenidən yoxlayın.')
    if not auth.verify(data_dir(), name, password): raise HTTPException(401, 'İstifadəçi adı və ya parol səhvdir')
    response = JSONResponse({'ok': True}); _set_cookie(response, request, name); return response


@app.post('/api/setup')
async def setup(request: Request):
    if auth.has_user(data_dir()) or is_public(request): raise HTTPException(403, 'Giriş yalnız serverin özündən (SSH tuneli ilə) yaradıla bilər')
    body = await request.json()
    try: auth.set_user(data_dir(), str(body.get('username', '')), str(body.get('password', '')))
    except ValueError as exc: raise HTTPException(400, str(exc))
    response = JSONResponse({'ok': True}); _set_cookie(response, request, str(body.get('username'))); return response


@app.post('/api/logout')
def logout():
    response = JSONResponse({'ok': True}); response.delete_cookie(COOKIE, path='/'); return response


@app.post('/api/password')
async def change_password(request: Request):
    body = await request.json(); user = _user_of(request)
    if not auth.verify(data_dir(), user, str(body.get('old', ''))): raise HTTPException(401, 'Köhnə parol səhvdir')
    try: auth.set_user(data_dir(), user, str(body.get('new', '')))
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {'ok': True}


# ---------------------------------------------------------------- helpers
def _now() -> datetime: return datetime.now(timezone.utc)


def _iso(value: datetime) -> str: return value.isoformat()


def _day_start(now: datetime) -> datetime: return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(now: datetime) -> datetime: return _day_start(now) - timedelta(days=now.weekday())


def _profile(demo: bool) -> dict:
    from ..risk_profile import account_risk_profile
    try: return account_risk_profile(settings().config, demo)
    except Exception: return {'risk_per_trade_pct': 0, 'daily_loss_limit_pct': 0, 'weekly_loss_limit_pct': 0}


def _test_summary() -> dict:
    path = data_dir() / 'strategy_test.json'
    try: return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    except (OSError, ValueError): return {}


def _test_result(summary: dict, strategy: str, symbol: str | None = None) -> dict | None:
    """Long-history test (bot exits, all hours): per symbol, or the best symbol when none is given."""
    best = None
    for row in summary.get('overall', []):
        if row.get('variant') != 'b' or row.get('mtf_filter') or row.get('strategy') != strategy: continue
        if symbol and row.get('symbol') != symbol: continue
        test = row.get('imtahan') or {}
        if test.get('expectancy_r') is None: continue
        if best is None or test['expectancy_r'] > best['expectancy_r']: best = {**test, 'symbol': row.get('symbol')}
    if best is not None:
        passed = [c for c in summary.get('combinations', []) if c.get('passed') and c.get('strategy') == strategy and (not symbol or c.get('symbol') == symbol)]
        best['passed'] = bool(passed)
        best['passed_where'] = [f"{c['symbol']} · {c['session']} · {c['variant'].upper()}" for c in passed[:4]]
    return best


def _status() -> dict:
    status = queries.get_bot_status(db_path()) or {}
    beat = None
    try: beat = (_now() - datetime.fromisoformat(str(status.get('time')).replace('Z', '+00:00'))).total_seconds()
    except (TypeError, ValueError): pass
    paused = (ROOT / 'STOP').exists()
    return {'status': status.get('status'), 'mode': status.get('mode'), 'account_type': status.get('account_type'),
            'mt5_connected': bool(status.get('mt5_connected')), 'trade_allowed': bool(status.get('trade_allowed')),
            'last_error': translate_message(status.get('last_error') or ''), 'heartbeat_seconds': beat,
            'stale': beat is None or beat > 180, 'entries_paused': paused,
            'groups_open': json.loads(status.get('scan_groups_open') or '{}') if status.get('scan_groups_open') else {},
            'location': os.getenv('BOT_LOCATION') or (settings().config.get('runtime') or {}).get('location', 'local')}


def _positions() -> list[dict]:
    open_rows = {int(r['ticket']): r for r in rows("SELECT ticket,symbol,direction,volume,open_time,open_price,sl,tp,profit,commission,swap,strategy,features,initial_risk FROM trades WHERE status='open'")}
    out = []
    from ..position_charts import read_position_chart
    try: outlooks = {int(o['ticket']): o for o in json.loads((data_dir() / 'simulations_live.json').read_text(encoding='utf-8')).get('positions', []) if o.get('ticket')}
    except (OSError, ValueError, TypeError): outlooks = {}
    for ticket, row in open_rows.items():
        chart = read_position_chart(data_dir(), ticket) or {}
        try: features = json.loads(row.get('features') or '{}')
        except (TypeError, ValueError): features = {}
        bars = (chart.get('bars') or {}).get('M1') or []
        out.append({'ticket': ticket, 'symbol': row['symbol'], 'direction': row['direction'], 'volume': row['volume'],
                    'open_time': row['open_time'], 'open_price': chart.get('open_price', row['open_price']),
                    'sl': chart.get('sl', row['sl']), 'tp': chart.get('tp', row['tp']), 'current': chart.get('current'),
                    'profit': float(chart.get('profit', row.get('profit') or 0) or 0) + float(row.get('commission') or 0) + float(row.get('swap') or 0),
                    'strategy': strategy_label(str(row.get('strategy') or '').replace('manual:', '')),
                    'manual': bool(features.get('manual')), 'managed': not bool(features.get('manual_unmanaged')),
                    'analysis': chart.get('analysis'), 'closes': [b.get('c', b.get('close')) for b in bars[-90:]],
                    'bars': [{'t': b.get('t'), 'o': b.get('o'), 'h': b.get('h'), 'l': b.get('l'), 'c': b.get('c')} for b in bars[-60:]],
                    'risk_distance': float(row.get('initial_risk') or 0) or None, 'outlook': outlooks.get(ticket),
                    'max_hold_minutes': None if features.get('manual_unmanaged') else int(settings().config['risk'].get('max_hold_minutes', 30))})
    return sorted(out, key=lambda p: p['open_time'] or '')


def _events(limit=12) -> list[dict]:
    out = []
    for e in queries.get_recent_events(db_path(), 200):
        message = str(e.get('message') or '')
        low = message.lower()
        if e.get('level') in ('WARNING', 'ERROR') or any(k in low for k in ('opened', 'closed', 'əl ilə', 'keşikçi', 'yarılması', 'stop moved', 'restarting')):
            out.append({'time': e['time'], 'level': e['level'], 'text': translate_message(message)})
        if len(out) >= limit: break
    return out


# ---------------------------------------------------------------- data endpoints
@app.get('/api/overview')
def overview(period: str = '1A'):
    now = _now(); status = _status()
    snapshot = queries.get_latest_snapshot(db_path()) or {}
    demo = (status.get('account_type') or 'DEMO') == 'DEMO'; profile = _profile(demo)
    today = queries.get_pnl_since(_iso(_day_start(now)), db_path()); week = queries.get_pnl_since(_iso(_week_start(now)), db_path())
    month = queries.get_pnl_since(_iso(now - timedelta(days=30)), db_path())
    balance = float(snapshot.get('balance') or 0); equity = float(snapshot.get('equity') or 0)
    spans = {'1G': timedelta(days=1), '1H': timedelta(days=7), '1A': timedelta(days=30), 'Hamısı': timedelta(days=3650)}
    curve = queries.get_equity_curve(_iso(now - spans.get(period, spans['1A'])), _iso(now + timedelta(minutes=1)), db_path())
    step = max(1, len(curve) // 400)
    ticker = {}
    try: ticker = json.loads((data_dir() / 'ticker.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): pass
    since = _iso(now - timedelta(minutes=15))
    opp = rows("SELECT COUNT(*) n FROM signals WHERE time>=? AND taken=0 AND skip_reason!='' AND strategy NOT LIKE 'manual%'", (since,))
    return {'status': status, 'balance': balance, 'equity': equity, 'demo': demo,
            'today': {**today, 'net': today['pnl']}, 'week': week, 'month': month,
            'limits': {'daily_pct': profile.get('daily_loss_limit_pct'), 'weekly_pct': profile.get('weekly_loss_limit_pct'),
                       'daily_used_pct': max(0.0, -today['pnl'] / balance * 100) if balance else 0.0,
                       'weekly_used_pct': max(0.0, -week['pnl'] / balance * 100) if balance else 0.0},
            'risk_pct': profile.get('risk_per_trade_pct'),
            'curve': [{'t': c['time'], 'v': c['equity']} for c in curve[::step]],
            'positions': _positions(), 'opportunities': int(opp[0]['n']) if opp else 0,
            'ticker': ticker.get('rows', []), 'events': _events()}


@app.get('/api/positions')
def positions():
    return {'positions': _positions(), 'status': _status()}


@app.get('/api/opportunities')
def opportunities():
    now = _now(); since = _iso(now - timedelta(minutes=15)); summary = _test_summary()
    snapshot = queries.get_latest_snapshot(db_path()) or {}
    seen, out = set(), []
    for row in rows("""SELECT id,time,symbol,strategy,direction,timeframe,confidence,entry,sl,tp,skip_reason FROM signals
                       WHERE time>=? AND taken=0 AND skip_reason!='' AND strategy NOT LIKE 'manual%' AND entry IS NOT NULL AND sl IS NOT NULL AND tp IS NOT NULL
                       ORDER BY time DESC LIMIT 300""", (since,)):
        key = (row['symbol'], row['strategy'], row['direction'])
        if key in seen: continue
        seen.add(key)
        try: age = (now - datetime.fromisoformat(str(row['time']).replace('Z', '+00:00'))).total_seconds() / 60
        except (TypeError, ValueError): age = None
        stop = abs(float(row['entry']) - float(row['sl'])); target = abs(float(row['tp']) - float(row['entry']))
        test = _test_result(summary, row['strategy'], row['symbol'])
        out.append({**row, 'strategy_label': strategy_label(row['strategy']), 'reason': translate_message(row['skip_reason']),
                    'age_minutes': age, 'reward_risk': (target / stop) if stop > 0 else None, 'history': test})
    return {'items': out[:40], 'balance': float(snapshot.get('balance') or 0), 'demo': (_status().get('account_type') or 'DEMO') == 'DEMO',
            'real_risk_cap': _profile(False).get('risk_per_trade_pct')}


@app.get('/api/market')
def market():
    """Trading screen: live candles/quotes per focus symbol (written by the bot), skipped signals, account."""
    data = {}
    try: data = json.loads((data_dir() / 'market.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): pass
    age = None
    try: age = (_now() - datetime.fromisoformat(str(data.get('updated_at')).replace('Z', '+00:00'))).total_seconds()
    except (TypeError, ValueError): pass
    for symbol in data.get('symbols', []):
        for sig in symbol.get('signals', []):
            sig['strategy_label'] = strategy_label(sig.get('strategy') or '')
            sig['reason'] = translate_message(sig.get('skip_reason') or '')
    status = _status(); snapshot = queries.get_latest_snapshot(db_path()) or {}
    demo = (status.get('account_type') or 'DEMO') == 'DEMO'
    return {'symbols': data.get('symbols', []), 'age_seconds': age, 'balance': float(snapshot.get('balance') or 0),
            'equity': float(snapshot.get('equity') or 0), 'demo': demo, 'real_risk_cap': _profile(False).get('risk_per_trade_pct'),
            'entries_paused': status.get('entries_paused'), 'status': status, 'opportunities': opportunities()['items'],
            'confirm_real': bool((settings().config.get('panel') or {}).get('confirm_orders_real', False))}


@app.post('/api/simulate')
async def simulate_trade(request: Request):
    """Time machine: replay what followed similar past moments for this stop/target (history only, no orders)."""
    from . import sim
    body = await request.json()
    try:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(sim.simulate, settings(), data_dir(), body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get('/api/simulations')
def simulations():
    from . import sim
    return {'items': sim.history(data_dir(), rows),
            'calibration': __import__('src.research.time_machine', fromlist=['read_calibration']).read_calibration(data_dir() / 'time_machine' / 'calibration.json')}


@app.get('/api/simulations/live')
def simulations_live():
    """Every symbol, buy and sell, simulated by the bot once a minute (data/simulations_live.json)."""
    try: board = json.loads((data_dir() / 'simulations_live.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return {'symbols': [], 'age_seconds': None}
    try: board['age_seconds'] = (_now() - datetime.fromisoformat(str(board.get('updated_at')))).total_seconds()
    except (TypeError, ValueError): board['age_seconds'] = None
    return board


def _local_time(value) -> str | None:
    try: return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(AZ_TZ).strftime('%H:%M')
    except (TypeError, ValueError): return None


@app.get('/api/today')
def today_trades(date: str | None = None):
    """Trades opened or closed on one Baku-time day (default: today)."""
    from . import sim
    now_local = _now().astimezone(AZ_TZ)
    try: day = datetime.strptime(date, '%Y-%m-%d').date() if date else now_local.date()
    except ValueError: raise HTTPException(400, 'Tarix YYYY-MM-DD olmalıdır')
    start = datetime(day.year, day.month, day.day, tzinfo=AZ_TZ).astimezone(timezone.utc)
    return {'date': day.isoformat(), **sim.day_trades(rows, start, (strategy_label, close_reason_label, _local_time))}


@app.post('/api/order_check')
async def order_check(request: Request):
    """Risk card shown before a manual order: the same checks the bot makes, each with its reason."""
    from . import sim
    body = await request.json()
    market_row = sim.market_symbol(data_dir(), str(body.get('symbol') or ''))
    if market_row is None: raise HTTPException(400, 'Canlı bazar məlumatı yoxdur')
    status = _status(); snapshot = queries.get_latest_snapshot(db_path()) or {}
    demo = (status.get('account_type') or 'DEMO') == 'DEMO'
    stop_file = ROOT / 'STOP'
    halted = stop_file.exists() and 'HALT' in stop_file.read_text(encoding='utf-8', errors='ignore').upper()
    today = queries.get_pnl_since(_iso(_day_start(_now())), db_path())
    return sim.order_check(market_row, status, _profile(demo), demo, float(snapshot.get('equity') or snapshot.get('balance') or 0),
                           float(today.get('pnl') or 0), body, halted)


@app.get('/api/journal')
def journal(days: int = 30):
    now = _now(); start = _iso(now - timedelta(days=max(1, min(int(days), 3650))))
    trades = queries.get_trade_history(start, _iso(now + timedelta(minutes=1)), db_path())
    for t in trades:
        t['strategy_label'] = strategy_label(str(t.get('strategy') or '').replace('manual:', '')) + (' (əl ilə)' if str(t.get('strategy') or '').startswith('manual') else '')
        t['close_reason_label'] = close_reason_label(t.get('close_reason'))
    reviews = queries.get_exit_reviews(start, _iso(now + timedelta(minutes=1)), db_path())
    by_strategy = history.group_by(trades, 'strategy')
    for g in by_strategy: g['label'] = strategy_label(str(g['name']).replace('manual:', ''))
    exit_rows = history.exit_review_summary(reviews)
    for g in exit_rows: g['label'] = close_reason_label(g['reason'])
    return {'summary': history.summary(trades), 'trades': trades[:500],
            'by_strategy': by_strategy, 'by_symbol': history.group_by(trades, 'symbol'),
            'by_hour': history.by_hour(trades, AZ_TZ), 'daily': history.daily_totals(trades, AZ_TZ),
            'exit_reviews': exit_rows, 'insights': history.insights(trades)}


@app.get('/api/strategies')
def strategies():
    config = settings().config; summary = _test_summary()
    guard = rows('SELECT * FROM strategy_guard ORDER BY strategy,symbol')
    filters = rows("SELECT * FROM learned_filters WHERE status!='removed' ORDER BY status,strategy")
    from ..loss_autopsy import label
    out = []
    for name, entry in (config.get('strategies') or {}).items():
        out.append({'name': name, 'label': strategy_label(name), 'enabled': bool(entry.get('enabled')),
                    'groups': entry.get('groups', []), 'test': _test_result(summary, name),
                    'guard': [g for g in guard if g['strategy'] == name]})
    return {'strategies': out, 'filters': [{**f, 'label': label(f['tag'])} for f in filters],
            'tested_at': summary.get('built_at_utc'), 'test_period': summary.get('test_start')}


def _restart_bot():
    try: queries.enqueue_command('restart', db_path(), 120)
    except ValueError: pass                                  # a restart is already queued


def _save_doc(doc) -> list[str]:
    text = editor.dump_document(doc)
    try: editor.save(config_path(), data_dir(), text, ROOT)
    except ValueError as exc: raise HTTPException(400, f'Ayar qəbul olunmadı: {exc}')
    global _settings; _settings = None
    _restart_bot()
    return []


@app.post('/api/strategies/toggle')
async def toggle_strategy(request: Request):
    body = await request.json(); name = str(body.get('name', '')); enabled = body.get('enabled')
    doc = editor.load_document(config_path())
    if name not in (doc.get('strategies') or {}) or not isinstance(enabled, bool): raise HTTPException(400, 'Naməlum strategiya')
    doc['strategies'][name]['enabled'] = enabled
    _save_doc(doc)
    return {'ok': True}


@app.get('/api/settings')
def get_settings():
    doc = editor.load_document(config_path()); values = editor.current_values(doc)
    fields = []
    for f in editor.FIELDS:
        value = values.get(f.path)
        if isinstance(value, (list, tuple)): value = [str(v) for v in value]
        elif value is not None and not isinstance(value, (bool, int, float)): value = str(value)
        fields.append({'path': '.'.join(f.path), 'label': f.label, 'kind': f.kind, 'section': f.section, 'help': f.help,
                       'min': f.minimum, 'max': f.maximum, 'step': f.step, 'choices': list(f.choices), 'value': value})
    return {'sections': list(editor.SECTIONS), 'fields': fields, 'symbols': editor.known_symbols(doc),
            'backups': [p.name for p in editor.list_backups(data_dir())[:10]]}


@app.post('/api/settings')
async def save_settings(request: Request):
    body = await request.json(); changes = body.get('changes') or {}
    doc = editor.load_document(config_path()); before = editor.current_values(doc)
    try: editor.apply_changes(doc, {tuple(str(k).split('.')): v for k, v in changes.items()})
    except ValueError as exc: raise HTTPException(400, str(exc))
    _save_doc(doc)
    return {'ok': True, 'changes': editor.describe_changes(before, editor.current_values(editor.load_document(config_path())))}


@app.post('/api/command')
async def command(request: Request):
    body = await request.json(); text = str(body.get('command', ''))
    if text in {'status', 'today', 'positions', 'health', 'diagnose_order', 'demo_executor_e2e'} or not is_allowed(text):
        raise HTTPException(400, 'Bu əmr paneldən göndərilə bilməz')
    try: command_id = queries.enqueue_command(text, db_path(), 45 if text.startswith('open_signal:') else 120)
    except ValueError as exc: raise HTTPException(409, str(exc))
    return {'ok': True, 'id': command_id}


@app.get('/api/command/{command_id}')
def command_status(command_id: int):
    found = rows('SELECT id,command,status,message,processed_time FROM commands WHERE id=?', (int(command_id),))
    if not found: raise HTTPException(404, 'Yoxdur')
    return found[0]


@app.get('/api/health')
def health():
    now = _now()
    watchdog = {}
    try: watchdog = json.loads((data_dir() / 'watchdog.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): pass
    return {'status': _status(), 'watchdog': watchdog,
            'restarts_24h': queries.get_supervisor_restart_count(_iso(now - timedelta(days=1)), _iso(now), db_path()),
            'commands': queries.get_recent_commands(db_path(), 15),
            'gates': queries.get_adaptive_gates(db_path()),
            'events': [{'time': e['time'], 'level': e['level'], 'source': e['source'], 'text': translate_message(e.get('message') or '')}
                       for e in queries.get_recent_events(db_path(), 40)]}


# ---------------------------------------------------------------- static app
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.get('/')
def index():
    """The page, with file versions in the asset links so a browser never keeps an old app after an update."""
    from fastapi.responses import HTMLResponse
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    for name in ('app.js', 'app.css'):
        try: version = int((STATIC / name).stat().st_mtime)
        except OSError: version = 0
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={version}"')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})

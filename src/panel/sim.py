"""Panel helpers: time-machine simulation from the live market file, the simulation log, today's
trades and the pre-order risk card. Read-only towards the bot (it only reads market.json and the DB)."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import math
import threading

from ..external_history import research_history_dir
from ..research import time_machine as tm
from ..strategy_guard import trade_r

_LOCK = threading.Lock()
_INDEX: dict[str, tuple[float, dict]] = {}
SCALES = {'Qısa': 0.5, 'Normal': 1.0, 'Geniş': 1.5}
LOG_NAME = 'simulations.jsonl'
MIN_LOT_PANEL_CAP_PCT = 2.0


def market_symbol(data_dir: Path, symbol: str) -> dict | None:
    try: data = json.loads((Path(data_dir) / 'market.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return None
    return next((s for s in data.get('symbols', []) if s.get('symbol') == symbol or s.get('broker') == symbol), None)


def _index(settings, data_dir: Path, symbol: str) -> dict:
    history = research_history_dir(settings)
    cache = Path(data_dir) / 'time_machine' / f'{symbol}.npz'
    stamp = cache.stat().st_mtime if cache.is_file() else 0.0
    with _LOCK:
        cached = _INDEX.get(symbol)
        if cached and cached[0] == stamp: return cached[1]
        index = tm.load_index(history, symbol, Path(data_dir) / 'time_machine', build=False)   # never build here: the panel has 512 MB
        if len(_INDEX) >= 2: _INDEX.pop(next(iter(_INDEX)))         # keep memory small: two symbols at most
        _INDEX[symbol] = (stamp, index)
        return index


def _point(settings, symbol: str) -> float:
    try:
        specs = json.loads((research_history_dir(settings) / 'symbol_specs.json').read_text(encoding='utf-8'))
        return float((specs.get(symbol) or {}).get('point') or 0)
    except (OSError, ValueError, TypeError): return 0.0


def levels(m: dict, direction: str, sl: float = 0.0, tp: float = 0.0, scale: str = 'Normal') -> tuple[float, float, float]:
    """Entry price and stop/target distances: the user's own prices, else the bot's automatic ones times the scale."""
    price = float(m['ask'] if direction == 'buy' else m['bid']); sign = 1.0 if direction == 'buy' else -1.0
    if sl and tp:
        stop, target = (price - float(sl)) * sign, (float(tp) - price) * sign
        if stop <= 0: raise ValueError('Stop alışda qiymətdən aşağı, satışda yuxarı olmalıdır')
        if target <= 0: raise ValueError('Hədəf alışda qiymətdən yuxarı, satışda aşağı olmalıdır')
        return price, stop, target
    auto = (m.get('auto') or {}).get(direction)
    if not auto: raise ValueError('Avtomatik stop hesablanmayıb (bazar məlumatı azdır)')
    k = SCALES.get(scale, 1.0)
    return price, float(auto['stop_distance']) * k, abs(float(auto['tp']) - float(auto['entry'])) * k


def simulate(settings, data_dir: Path, body: dict, now: datetime | None = None) -> dict:
    symbol = str(body.get('symbol') or ''); direction = str(body.get('direction') or '')
    if direction not in ('buy', 'sell'): raise ValueError('İstiqamət buy və ya sell olmalıdır')
    minutes = int(body.get('minutes') or 30)
    if minutes not in (15, 30, 60): raise ValueError('Vaxt 15, 30 və ya 60 dəqiqə ola bilər')
    m = market_symbol(data_dir, symbol)
    if m is None: raise ValueError(f'{symbol}: canlı bazar məlumatı yoxdur')
    # The long history may be named after the broker symbol (US500Cash) or the logical one (US500).
    names = [n for n in dict.fromkeys([m.get('broker'), m.get('symbol'), symbol]) if n]
    logical = next((n for n in names if (Path(data_dir) / 'time_machine' / f'{n}.npz').is_file()), m.get('symbol') or symbol)
    price, stop, target = levels(m, direction, float(body.get('sl') or 0), float(body.get('tp') or 0), str(body.get('scale') or 'Normal'))
    state = tm.live_state((m.get('candles') or {}).get('M1') or [], (m.get('candles') or {}).get('M15') or [])
    try: index = _index(settings, data_dir, logical)
    except LookupError as error: raise ValueError(str(error))
    now = now or datetime.now(timezone.utc)
    slippage = float(settings.config.get('backtest', {}).get('slippage_points', 1.0)) * _point(settings, logical)
    out = tm.simulate(index, hour=now.hour, trend=state['trend'], direction=direction, price=price, atr=state['atr'],
                      stop_distance=stop, target_distance=target, spread=float(m.get('spread') or 0), minutes=minutes,
                      slippage_price=slippage)
    calibration = tm.read_calibration(Path(data_dir) / 'time_machine' / 'calibration.json').get('symbols', {}).get(logical)
    out['reliability'] = 'yoxlanmayıb' if not calibration or not calibration.get('samples') else ('etibarlı' if calibration.get('reliable') else 'etibarlı deyil')
    shown = m.get('symbol') or symbol                     # the name the screen uses; the index may be stored under the broker name
    out.update({'symbol': shown, 'history_symbol': logical, 'broker': m.get('broker'), 'direction': direction, 'digits': m.get('digits', 2)})
    if out.get('enough'):
        _log(data_dir, {'time': now.isoformat(), 'symbol': shown, 'broker': m.get('broker'), 'direction': direction,
                        'stop_distance': stop, 'target_distance': target, 'minutes': minutes,
                        'expectancy_r': out['chosen']['expectancy_r'], 'target': out['chosen']['target'], 'best': out.get('best')})
    return out


def _log(data_dir: Path, row: dict) -> None:
    path = Path(data_dir) / LOG_NAME
    try:
        with path.open('a', encoding='utf-8') as handle: handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) > 2000: path.write_text('\n'.join(lines[-1000:]) + '\n', encoding='utf-8')
    except OSError:
        pass


def history(data_dir: Path, rows_fn, limit: int = 30, now: datetime | None = None) -> list[dict]:
    """Recent simulations next to what really happened: a trade on the same symbol and side opened within 10 minutes."""
    now = now or datetime.now(timezone.utc)
    try: lines = (Path(data_dir) / LOG_NAME).read_text(encoding='utf-8').splitlines()[-limit:]
    except OSError: return []
    out = []
    for line in reversed(lines):
        try: row = json.loads(line); at = datetime.fromisoformat(row['time'])
        except (ValueError, KeyError): continue
        found = rows_fn("""SELECT status,profit,commission,swap,features FROM trades WHERE symbol=? AND direction=? AND open_time>=? AND open_time<=?
                           ORDER BY open_time LIMIT 1""", (row.get('broker') or row['symbol'], row['direction'], at.isoformat(), (at + timedelta(minutes=10)).isoformat()))
        if found:
            trade = found[0]
            row['real'] = 'açıq' if trade['status'] == 'open' else trade_r(trade)
        else:
            row['real'] = 'gözlənilir' if now - at < timedelta(minutes=10) else 'açılmadı'
        out.append(row)
    return out


def day_trades(rows_fn, day_start_utc: datetime, labels) -> dict:
    """Trades opened or closed in one local day, with net money and R (R from the sized risk, as the guard)."""
    end = day_start_utc + timedelta(days=1)
    s, e = day_start_utc.isoformat(), end.isoformat()
    found = rows_fn("""SELECT ticket,symbol,direction,volume,open_time,close_time,status,profit,commission,swap,strategy,close_reason,features
                       FROM trades WHERE (open_time>=? AND open_time<?) OR (close_time>=? AND close_time<?) ORDER BY open_time""", (s, e, s, e))
    strategy_label, close_label, local = labels
    items, total, wins, losses, total_r = [], 0.0, 0, 0, 0.0
    for t in found:
        net = float(t.get('profit') or 0) + float(t.get('commission') or 0) + float(t.get('swap') or 0)
        try: features = json.loads(t.get('features') or '{}')
        except (TypeError, ValueError): features = {}
        closed = t['status'] == 'closed'
        r = trade_r(t) if closed else None
        if closed:
            total += net; wins += net > 0; losses += net <= 0
            if r is not None and math.isfinite(r): total_r += r
        strategy = str(t.get('strategy') or '')
        items.append({'ticket': t['ticket'], 'time': local(t['open_time']), 'close_time': local(t['close_time']) if t.get('close_time') else None,
                      'symbol': t['symbol'], 'direction': t['direction'], 'volume': t['volume'],
                      'strategy': strategy_label(strategy.replace('manual:', '')), 'manual': bool(features.get('manual')) or strategy.startswith('manual'),
                      'status': t['status'], 'net': net, 'r': r, 'close_reason': close_label(t.get('close_reason')) if closed else 'açıq',
                      'slippage_points': (features.get('execution') or {}).get('slippage_points')})
    return {'items': items, 'closed': wins + losses, 'wins': wins, 'losses': losses, 'net': total, 'r': total_r}


def order_check(market: dict, status: dict, profile: dict, demo: bool, balance: float, today_pnl: float, body: dict, halted: bool) -> dict:
    """The checks the bot will make, shown before the order: ok / warn / fail with a reason each."""
    checks = []
    add = lambda name, state, text: checks.append({'name': name, 'state': state, 'text': text})
    direction = str(body.get('direction') or 'buy'); risk = float(body.get('risk') or 0)
    add('Bot bağlantısı', 'ok' if status.get('mt5_connected') and not status.get('stale') else 'fail',
        'Bot və MT5 işləyir' if status.get('mt5_connected') and not status.get('stale') else 'Bot cavab vermir və ya MT5 bağlı deyil')
    if halted: add('Təcili dayandırma', 'fail', 'HALT aktivdir: heç bir əmr açılmır')
    elif status.get('entries_paused'): add('Pauza', 'ok', 'Botun öz girişləri pauzadadır, sizin əmriniz keçir')
    add('MT5 icazəsi', 'ok' if status.get('trade_allowed') else 'fail', 'Avtomatik ticarətə icazə var' if status.get('trade_allowed') else 'MT5-də avtomatik ticarət bağlıdır')
    limit = abs(float(profile.get('daily_loss_limit_pct') or 0)); used = max(0.0, -today_pnl / balance * 100) if balance else 0.0
    add('Gündəlik zərər limiti', 'fail' if limit and used >= limit else 'ok', f'{used:.1f}% / {limit:g}% istifadə olunub')
    cap = 5.0 if demo else float(profile.get('risk_per_trade_pct') or 0)
    add('Risk', 'warn' if risk > cap else 'ok', f'{risk:g}%' + (f' → {cap:g}%-ə endiriləcək' if risk > cap else f' (tavan {cap:g}%)'))
    try:
        price, stop, target = levels(market, direction, float(body.get('sl') or 0), float(body.get('tp') or 0), str(body.get('scale') or 'Normal'))
    except (ValueError, KeyError, TypeError) as error:
        add('Stop və hədəf', 'fail', str(error)); return {'checks': checks, 'can_open': False}
    rr = target / stop
    add('Qazanc / risk', 'ok' if rr >= 1 else 'warn', f'Hədəf stopun {rr:.2f} qatıdır' + ('' if rr >= 1 else ' (1-dən az)'))
    spread = float(market.get('spread') or 0)
    add('Stop və spred', 'ok' if stop >= 3 * spread else 'warn', f'Stop spredin {stop / spread:.1f} qatıdır' if spread > 0 else 'Spred məlum deyil')
    money_per = float(market.get('money_per_price_lot') or 0); vmin = float(market.get('volume_min') or 0.01)
    volume = None; risk_used = min(risk, cap)
    if money_per > 0 and balance > 0:
        per_lot = stop * money_per; min_pct = per_lot * vmin / balance * 100
        if min_pct <= risk_used:
            volume = max(vmin, math.floor(balance * risk_used / 100 / per_lot / vmin) * vmin)
            add('Lot', 'ok', f'{volume:g} lot · risk ≈ {volume * per_lot:.2f}')
        elif demo and min_pct <= MIN_LOT_PANEL_CAP_PCT:
            volume = vmin; add('Lot', 'warn', f'Minimum lot ({vmin:g}) real riski {min_pct:.2f}% edəcək')
        else:
            add('Lot', 'fail', f'Minimum lot ({vmin:g}) {min_pct:.2f}% risk edir (tavan {MIN_LOT_PANEL_CAP_PCT if demo else risk_used:g}%). Stopu qısaldın')
    return {'checks': checks, 'can_open': not any(c['state'] == 'fail' for c in checks), 'volume': volume,
            'stop_distance': stop, 'target_distance': target, 'entry': price}

"""Learns how far the take-profit should be, from what the price really did after each trade.

For every closed bot trade the next ``horizon`` minutes of M1 bars (from the entry) are
replayed against the ORIGINAL stop (1R) and a set of candidate targets (0.5R ... 2R):
"would a target at L·R have been reached before the stop?". Costs (spread + commission,
in R) are subtracted. Per market and session (Asia / London / New York) the bot then picks
the target with the best average result, with guards against learning from noise:

* nothing changes before ``min_samples`` trades exist for that market/session;
* the new level must beat the current one by more than ``margin`` R;
* the level moves at most one step per update;
* a market without enough session data falls back to its all-session level, then to the default.

The stop distance is not learned (more prone to overfitting); only the target moves."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LEVELS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
SESSIONS = ('asia', 'london', 'newyork')
SESSION_LABELS = {'asia': 'Asiya', 'london': 'London', 'newyork': 'Nyu-York', 'all': 'Bütün gün'}


def session_of(stamp: datetime) -> str:
    """UTC hour → session: Asia 22–07, London 07–13, New York 13–22."""
    hour = stamp.astimezone(timezone.utc).hour
    if 7 <= hour < 13: return 'london'
    if 13 <= hour < 22: return 'newyork'
    return 'asia'


def _utc(value) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def market_key(symbol: str) -> str:
    from .symbols import underlying_key
    return underlying_key(symbol)


def path_outcomes(direction: str, entry: float, risk: float, bars, levels=LEVELS) -> dict[str, Any]:
    """R result for every candidate target, plus the best (MFE) and worst (MAE) excursion in R.

    A bar touching both the stop and a target counts as the stop (conservative). If neither is
    reached the result is the R at the last bar's close (the time limit)."""
    buy = direction == 'buy'
    stop_hit = None; first_hit = {level: None for level in levels}; mfe = 0.0; mae = 0.0; last_r = 0.0
    for i, (_, bar) in enumerate(bars.iterrows()):
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        best = ((high - entry) if buy else (entry - low)) / risk
        worst = ((low - entry) if buy else (entry - high)) / risk
        mfe = max(mfe, best); mae = min(mae, worst)
        last_r = ((close - entry) if buy else (entry - close)) / risk
        for level in levels:
            if first_hit[level] is None and best >= level: first_hit[level] = i
        if worst <= -1.0: stop_hit = i; break
    outcomes = {}
    for level in levels:
        hit = first_hit[level]
        if hit is not None and (stop_hit is None or hit < stop_hit): outcomes[str(level)] = float(level)
        elif stop_hit is not None: outcomes[str(level)] = -1.0
        else: outcomes[str(level)] = round(last_r, 4)
    return {'outcomes': outcomes, 'mfe': round(mfe, 4), 'mae': round(mae, 4)}


def _bars_for(trade: dict, start: datetime, end: datetime, mt5, history_dir: Path | None):
    """M1 bars covering [start, end]: local history file first, MT5 otherwise."""
    import pandas as pd
    if history_dir is not None:
        from .history import history_path
        path = history_path(history_dir, str(trade['symbol']), 'M1')
        if path.exists():
            try:
                from . import parquet_compat
                frame = parquet_compat.read_parquet(path)
                if not isinstance(frame.index, pd.DatetimeIndex) and 'time' in frame:
                    frame = frame.set_index(pd.to_datetime(frame['time'], utc=True))
                frame.index = pd.to_datetime(frame.index, utc=True)
                part = frame[(frame.index >= start) & (frame.index < end)]
                if len(part) >= 3: return part
            except Exception:
                pass
    if mt5 is None: return None
    from . import market_data
    minutes = int((datetime.now(timezone.utc) - start).total_seconds() // 60) + 5
    if minutes > 5000: return None
    frame = market_data.get_candles(mt5, str(trade['symbol']), 'M1', minutes)
    return frame[(frame.index >= start) & (frame.index < end)]


def cost_in_r(trade: dict, mt5) -> float:
    """Spread + commission of one round trip, expressed in R (best effort; 0 when unknown)."""
    risk = float(trade.get('initial_risk') or 0)
    if risk <= 0 or mt5 is None: return 0.0
    try:
        info = mt5.symbol_info(str(trade['symbol']))
        spread = float(getattr(info, 'spread', 0) or 0) * float(getattr(info, 'point', 0) or 0)
        cost = spread / risk
        tick_value = float(getattr(info, 'trade_tick_value', 0) or 0); tick_size = float(getattr(info, 'trade_tick_size', 0) or 0)
        risk_money = float(trade.get('volume') or 0) * risk / tick_size * tick_value if tick_value > 0 and tick_size > 0 else 0
        if risk_money > 0: cost += abs(float(trade.get('commission') or 0)) / risk_money
        return round(min(cost, 1.0), 4)
    except Exception:
        return 0.0


def record_paths(db, mt5, history_dir: Path | None, horizon_minutes: float, now: datetime | None = None,
                 limit: int = 20, magic: int | None = None) -> int:
    """Store the replayed path of closed trades whose full horizon has passed."""
    now = now or datetime.now(timezone.utc); done = 0
    for trade in db.get_trades_without_target_path((now - timedelta(minutes=horizon_minutes)).isoformat(),
                                                   (now - timedelta(days=30)).isoformat(), limit, magic):
        ticket = int(trade['ticket'])
        try:
            risk = float(trade.get('initial_risk') or 0); entry = float(trade['open_price'])
            if risk <= 0: db.save_target_path(ticket, trade['symbol'], '', '', trade['open_time'], None); continue
            start = _utc(trade['open_time']).replace(second=0, microsecond=0)
            bars = _bars_for(trade, start, start + timedelta(minutes=horizon_minutes), mt5, history_dir)
            if bars is None or len(bars) < 3:
                if _utc(trade['open_time']) < now - timedelta(days=2):
                    db.save_target_path(ticket, trade['symbol'], '', '', trade['open_time'], None)
                continue
            path = path_outcomes(str(trade['direction']), entry, risk, bars)
            path['cost_r'] = cost_in_r(trade, mt5)
            db.save_target_path(ticket, trade['symbol'], market_key(trade['symbol']), session_of(_utc(trade['open_time'])),
                                trade['open_time'], path)
            done += 1
        except Exception:
            continue
    return done


def _stats(rows: list[dict]) -> dict[str, dict[str, float]]:
    out = {}
    for level in LEVELS:
        values = [r['outcomes'][str(level)] - r['cost_r'] for r in rows if str(level) in r['outcomes']]
        if not values: continue
        out[str(level)] = {'mean': round(sum(values) / len(values), 4),
                           'hit': round(sum(1 for r in rows if r['outcomes'].get(str(level)) == level) / len(rows), 3)}
    return out


def choose_level(stats: dict, samples: int, current: float, default: float, min_samples: int, margin: float) -> float:
    if samples < min_samples or not stats: return current
    best = max(stats, key=lambda key: stats[key]['mean']); best_level = float(best)
    here = stats.get(str(current), {}).get('mean')
    if here is not None and stats[best]['mean'] - here <= margin: return current
    ladder = list(LEVELS)
    if current not in ladder: current = min(ladder, key=lambda level: abs(level - current))
    step = 1 if ladder.index(best_level) > ladder.index(current) else -1
    return ladder[ladder.index(current) + step] if best_level != current else current


def learn(db, default_level: float, min_samples: int = 30, margin: float = 0.03, window: int = 100) -> list[dict]:
    """Recompute the learned level for every market × session (and market overall)."""
    rows = db.get_target_paths()
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row['key'], row['session']), []).append(row)
        groups.setdefault((row['key'], 'all'), []).append(row)
    current = {(r['key'], r['session']): float(r['level']) for r in db.get_target_learning()}
    default = min(LEVELS, key=lambda level: abs(level - default_level))
    out = []
    for (key, session), items in groups.items():
        items = sorted(items, key=lambda r: r['open_time'])[-window:]
        stats = _stats(items)
        level = choose_level(stats, len(items), current.get((key, session), default), default, min_samples, margin)
        db.save_target_learning(key, session, level, len(items), stats)
        out.append({'key': key, 'session': session, 'level': level, 'samples': len(items), 'stats': stats})
    return out


class TargetBook:
    """Cached read access for the scanner: learned target (in R) for a symbol at a time, or None."""
    def __init__(self, db, min_samples: int = 30, refresh_seconds: float = 60):
        self.db = db; self.min_samples = min_samples; self.refresh = refresh_seconds; self._loaded = None; self._rows = {}

    def level(self, symbol: str, when: datetime) -> tuple[float, str] | None:
        now = datetime.now(timezone.utc)
        if self._loaded is None or (now - self._loaded).total_seconds() > self.refresh:
            try: self._rows = {(r['key'], r['session']): r for r in self.db.get_target_learning()}
            except Exception: self._rows = {}
            self._loaded = now
        key = market_key(symbol)
        for session in (session_of(when), 'all'):
            row = self._rows.get((key, session))
            if row and int(row['samples']) >= self.min_samples: return float(row['level']), session
        return None

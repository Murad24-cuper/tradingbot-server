"""Trading screen data: candles, quote, the bot's automatic stop/target and its view per symbol.

Built by the bot every few seconds (it owns the MT5 connection) and read by the panel from
data/market.json. Best effort: a symbol whose data is missing is simply left out.
"""
from __future__ import annotations
from datetime import datetime, timedelta

import pandas as pd

from . import indicators as ind
from .time_utils import mt5_epoch_to_utc
from .trade_rules import stop_target_from_atr

TIMEFRAMES = (('M1', 'TIMEFRAME_M1', 120), ('M5', 'TIMEFRAME_M5', 120), ('M15', 'TIMEFRAME_M15', 120))


def _bars(mt5, name: str, constant: str, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(name, getattr(mt5, constant), 0, count)
    if rates is None or len(rates) < 5: return None
    return pd.DataFrame(rates)


def trend_view(m15: pd.DataFrame | None, m5: pd.DataFrame | None) -> dict:
    """Plain-language direction from M15 EMAs and the last half hour on M5."""
    out = {'trend': 'yan', 'text': 'Məlumat azdır'}
    if m15 is None or len(m15) < 55: return out
    close = m15['close'].astype(float)
    fast, slow = ind.ema(close, 20), ind.ema(close, 50)
    slope = (fast.iloc[-1] - fast.iloc[-4]) / max(abs(fast.iloc[-4]), 1e-12)
    if fast.iloc[-1] > slow.iloc[-1] and slope > 0: trend = 'yuxarı'
    elif fast.iloc[-1] < slow.iloc[-1] and slope < 0: trend = 'aşağı'
    else: trend = 'yan'
    move = None
    if m5 is not None and len(m5) >= 7:
        last = m5['close'].astype(float)
        move = (last.iloc[-1] / last.iloc[-7] - 1) * 100
    words = {'yuxarı': 'M15-də trend yuxarıdır', 'aşağı': 'M15-də trend aşağıdır', 'yan': 'M15-də aydın trend yoxdur (yan hərəkət)'}
    text = words[trend] + (f'; son 30 dəqiqədə {move:+.2f}%' if move is not None else '')
    return {'trend': trend, 'text': text, 'move_30m_pct': move}


def recent_signals(db, symbol: str, now: datetime, minutes: int = 30) -> list[dict]:
    since = (now - timedelta(minutes=minutes)).isoformat()
    with db.connect() as connection:
        rows = connection.execute('''SELECT id,time,strategy,direction,taken,skip_reason FROM signals WHERE symbol=? AND time>=?
                                     AND strategy NOT LIKE 'manual%' ORDER BY time DESC LIMIT 40''', (symbol, since)).fetchall()
    seen, out = set(), []
    for row in rows:
        row = dict(row); key = (row['strategy'], row['direction'])
        if key in seen: continue
        seen.add(key); out.append(row)
    return out[:6]


def build_market(mt5, names: dict, config: dict, db, now: datetime) -> dict:
    risk = config.get('risk', {})
    symbols = []
    for logical, name in names.items():
        try:
            tick = mt5.symbol_info_tick(name); info = mt5.symbol_info(name)
            if tick is None or info is None: continue
            bars = {tf: _bars(mt5, name, constant, count) for tf, constant, count in TIMEFRAMES}
            candles = {}
            for tf, frame in bars.items():
                if frame is None: continue
                candles[tf] = [{'t': mt5_epoch_to_utc(int(r.time), mt5).isoformat(), 'o': float(r.open), 'h': float(r.high), 'l': float(r.low), 'c': float(r.close)}
                               for r in frame.tail(120).itertuples()]
            bid, ask = float(tick.bid), float(tick.ask); spread = max(0.0, ask - bid)
            auto = {}
            atr_frame = _bars(mt5, name, 'TIMEFRAME_M1', 250)
            if atr_frame is not None and len(atr_frame) >= 30:
                atr = float(ind.atr(atr_frame).iloc[-1])
                for side, price in (('buy', ask), ('sell', bid)):
                    sl, tp = stop_target_from_atr(price, side, atr, spread, float(risk.get('sl_move_mult', .8)), float(risk.get('tp_move_mult', 1.2)),
                                                  float(risk.get('move_horizon_minutes', 30)))
                    auto[side] = {'entry': price, 'sl': sl, 'tp': tp, 'stop_distance': abs(price - sl)}
            spec = info._asdict() if hasattr(info, '_asdict') else {}
            tick_value = float(spec.get('trade_tick_value_loss', spec.get('trade_tick_value', 0)) or 0)
            tick_size = float(spec.get('trade_tick_size', 0) or 0)
            symbols.append({'symbol': logical, 'broker': name, 'bid': bid, 'ask': ask, 'spread': spread, 'digits': int(getattr(info, 'digits', 2) or 2),
                            'money_per_price_lot': (tick_value / tick_size) if tick_value and tick_size else None,
                            'volume_min': float(getattr(info, 'volume_min', 0.01) or 0.01), 'candles': candles, 'auto': auto,
                            'view': trend_view(bars.get('M15'), bars.get('M5')), 'signals': recent_signals(db, name, now)})
        except Exception:                                    # one symbol must never break the screen
            continue
    return {'updated_at': now.isoformat(), 'symbols': symbols}

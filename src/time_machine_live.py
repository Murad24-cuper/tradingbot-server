"""The bot's own use of the time machine before every entry it makes.

For each candidate the real paths that followed similar past moments are replayed with the
candidate's stop and target (src/research/time_machine.py). Then, on DEMO (and on REAL only when
`demo_only: false`):
  - a candidate whose simulated result is worse than `block_below_r` is not opened;
  - when another stop/target cell of the 3x3 grid is clearly better (by `min_improvement_r`) and
    positive, the candidate's stop and target are moved to it (the lot is sized afterwards, so the
    money risk stays the same).
Everything fails open: no index, too few similar moments or any error = the entry goes ahead as
before, with the reason recorded. Every result is written to the trade's features and to
data/simulations.jsonl so the panel can compare the prediction with the real outcome.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import json

import numpy as np

from .research import time_machine as tm

DEFAULTS = {'enabled': True, 'block_below_r': -0.10, 'choose_levels': True, 'min_improvement_r': 0.03,
            'require_reliable': False, 'demo_only': True, 'manage_open': True, 'close_margin_r': 0.10, 'min_age_minutes': 3}


def config_of(config: dict) -> dict:
    return {**DEFAULTS, **(config.get('time_machine') or {})}


class LiveTimeMachine:
    def __init__(self, settings):
        self.settings = settings
        self._index: dict[str, tuple[float, dict]] = {}

    @property
    def folder(self) -> Path:
        return Path(self.settings.data_dir) / 'time_machine'

    def logical(self, broker: str) -> str:
        aliases = (self.settings.config.get('universe') or {}).get('symbol_aliases') or {}
        for logical, names in aliases.items():
            if broker == logical or broker in (names or []): return logical
        return broker

    def index(self, logical: str) -> dict | None:
        path = self.folder / f'{logical}.npz'
        if not path.is_file(): return None
        stamp = path.stat().st_mtime
        cached = self._index.get(logical)
        if cached and cached[0] == stamp: return cached[1]
        index = tm.load_index(self.folder, logical, self.folder, build=False)   # the check script builds it; never here
        self._index[logical] = (stamp, index)
        return index

    def reliable(self, logical: str) -> str:
        info = tm.read_calibration(self.folder / 'calibration.json').get('symbols', {}).get(logical)
        if not info or not info.get('samples'): return 'yoxlanmayıb'
        return 'etibarlı' if info.get('reliable') else 'etibarlı deyil'

    def evaluate(self, mt5, signal, spec: dict, now: datetime, demo: bool) -> dict:
        """Simulation for one candidate and the decision: 'open', 'skip' or 'levels' (with new sl/tp)."""
        cfg = config_of(self.settings.config)
        out = {'used': False, 'action': 'open', 'decides': bool(demo or not cfg['demo_only'])}
        if not cfg['enabled']: out['reason'] = 'söndürülüb'; return out
        # The history may be stored under the broker name (US500Cash) or the logical one (US500): use whichever exists.
        names = list(dict.fromkeys([str(signal.symbol), self.logical(str(signal.symbol))]))
        logical = next((name for name in names if (self.folder / f'{name}.npz').is_file()), names[-1]); out['symbol'] = logical
        index = self.index(logical)
        if index is None: out['reason'] = 'indeks yoxdur'; return out
        tick = mt5.symbol_info_tick(signal.symbol)
        m1 = mt5.copy_rates_from_pos(signal.symbol, mt5.TIMEFRAME_M1, 0, 60)
        m15 = mt5.copy_rates_from_pos(signal.symbol, mt5.TIMEFRAME_M15, 0, 120)
        if tick is None or m1 is None or m15 is None or len(m1) < 20 or len(m15) < 56: out['reason'] = 'canlı şam azdır'; return out
        bars = lambda rates: [{'o': float(r['open']), 'h': float(r['high']), 'l': float(r['low']), 'c': float(r['close'])} for r in rates]
        state = tm.live_state(bars(m1), bars(m15))
        direction = signal.direction; sign = 1.0 if direction == 'buy' else -1.0
        entry = float(signal.entry_hint)
        stop, target = abs(entry - float(signal.sl_price)), abs(float(signal.tp_price) - entry)
        point = float(spec.get('point') or 0)
        slip = float((self.settings.config.get('backtest') or {}).get('slippage_points', 1.0)) * point
        minutes = int(self.settings.config['risk'].get('max_hold_minutes', 30))
        sim = tm.simulate(index, hour=now.hour, trend=state['trend'], direction=direction, price=entry, atr=state['atr'],
                          stop_distance=stop, target_distance=target, spread=max(0.0, float(tick.ask) - float(tick.bid)),
                          minutes=minutes, slippage_price=slip, fan=0)
        out.update({'analogs': sim['analogs'], 'match': sim['match'], 'reliability': self.reliable(logical)})
        if not sim['enough']: out['reason'] = f"oxşar an azdır ({sim['analogs']})"; return out
        chosen = sim['chosen']; out.update({'used': True, 'expectancy_r': chosen['expectancy_r'], 'target': chosen['target'],
                                            'stop': chosen['stop'], 'best': sim['best']})
        if cfg['require_reliable'] and out['reliability'] != 'etibarlı': out['reason'] = 'etibarlılıq təsdiqlənməyib, yalnız qeyd'; return out
        if not out['decides']: out['reason'] = 'REAL hesab: yalnız qeyd'; return out
        if chosen['expectancy_r'] < float(cfg['block_below_r']):
            out.update({'action': 'skip', 'reason': f"simulyasiya {chosen['expectancy_r']:+.2f}R < {float(cfg['block_below_r']):+.2f}R"}); return out
        if cfg['choose_levels']:
            best = max(((row, cell) for row in sim['grid'] for cell in row['cells']), key=lambda item: item[1]['expectancy_r'])
            row, cell = best
            if cell['expectancy_r'] > 0 and cell['expectancy_r'] - chosen['expectancy_r'] >= float(cfg['min_improvement_r']):
                out.update({'action': 'levels', 'sl': entry - sign * row['stop_distance'], 'tp': entry + sign * cell['target_distance'],
                            'choice': f"{row['name']} · {cell['ratio']}×", 'choice_r': cell['expectancy_r'],
                            'reason': f"{row['name']} · {cell['ratio']}× daha yaxşıdır ({cell['expectancy_r']:+.2f}R, indiki {chosen['expectancy_r']:+.2f}R)"})
                return out
        out['reason'] = 'proqnoz qəbul ediləndir'
        return out

    def log(self, signal, info: dict, now: datetime, broker: str) -> None:
        """Same log the panel's simulations use, marked as the bot's own."""
        if not info.get('used'): return
        row = {'time': now.isoformat(), 'symbol': info.get('symbol'), 'broker': broker, 'direction': signal.direction, 'source': 'bot',
               'action': info.get('action'), 'expectancy_r': info.get('expectancy_r'), 'target': info.get('target'), 'best': info.get('best'),
               'reason': info.get('reason')}
        try:
            path = Path(self.settings.data_dir) / 'simulations.jsonl'
            with path.open('a', encoding='utf-8') as handle: handle.write(json.dumps(row, ensure_ascii=False, default=float) + '\n')
        except OSError:
            pass


def live_board(live: LiveTimeMachine, market: dict, now: datetime, fan: int = 40) -> dict:
    """Simulation of every focus symbol, both sides, with the bot's automatic stop/target, from the
    same candles the trading screen shows. Written by the bot every minute (data/simulations_live.json)."""
    minutes = int(live.settings.config['risk'].get('max_hold_minutes', 30))
    slip_points = float((live.settings.config.get('backtest') or {}).get('slippage_points', 1.0))
    out = []
    for sym in market.get('symbols', []):
        row = {'symbol': sym.get('symbol'), 'broker': sym.get('broker'), 'sides': {}}
        try:
            names = [n for n in dict.fromkeys([sym.get('broker'), sym.get('symbol')]) if n]
            name = next((n for n in names if (live.folder / f'{n}.npz').is_file()), None)
            if name is None: row['error'] = 'indeks yoxdur'; out.append(row); continue
            index = live.index(name); row['history_symbol'] = name; row['reliability'] = live.reliable(name)
            candles = sym.get('candles') or {}
            state = tm.live_state(candles.get('M1') or [], candles.get('M15') or [])
            spread = float(sym.get('spread') or 0); point = 10 ** -int(sym.get('digits', 2) or 2)
            for side in ('buy', 'sell'):
                auto = (sym.get('auto') or {}).get(side)
                if not auto: continue
                entry = float(auto['entry']); stop = float(auto['stop_distance']); target = abs(float(auto['tp']) - entry)
                sim = tm.simulate(index, hour=now.hour, trend=state['trend'], direction=side, price=entry, atr=state['atr'],
                                  stop_distance=stop, target_distance=target, spread=spread, minutes=minutes,
                                  slippage_price=slip_points * point, fan=fan)
                row['state'] = sim.get('state'); row['analogs'] = sim.get('analogs')
                row['sides'][side] = {k: sim.get(k) for k in ('enough', 'analogs', 'match', 'chosen', 'grid', 'best', 'fan', 'entry', 'stop', 'target',
                                                             'stop_distance', 'target_distance', 'minutes', 'state', 'years')}
        except Exception as exc:                      # one symbol never stops the others
            row['error'] = f'{type(exc).__name__}: {exc}'
        out.append(row)
    return {'updated_at': now.isoformat(), 'minutes': minutes, 'symbols': out}


def position_outlook(live: LiveTimeMachine, position: dict, market_symbol: dict, now: datetime, demo: bool) -> dict:
    """An open position simulated from the current price with its own stop/target and the time it has left.

    position: ticket, symbol, direction, open_price, sl, tp, current (exit-side price), risk (original
    stop distance, price), opened (datetime), managed (bool). Results are in ORIGINAL R: `now_r` is what
    closing now gives, `hold_r` what holding is expected to give. On DEMO a managed position is closed
    when holding is worse than closing by more than `close_margin_r`."""
    cfg = config_of(live.settings.config)
    out = {'ticket': position['ticket'], 'symbol': position['symbol'], 'used': False, 'action': 'hold'}
    if not (cfg['enabled'] and cfg['manage_open']): out['reason'] = 'söndürülüb'; return out
    sign = 1.0 if position['direction'] == 'buy' else -1.0
    cur, entry, risk = float(position['current']), float(position['open_price']), float(position['risk'] or 0)
    sl, tp = float(position.get('sl') or 0), float(position.get('tp') or 0)
    if not (risk > 0 and sl and tp): out['reason'] = 'stop/hədəf və ya risk yoxdur'; return out
    stop_left, target_left = (cur - sl) * sign, (tp - cur) * sign
    if stop_left <= 0 or target_left <= 0: out['reason'] = 'qiymət səviyyədən keçib'; return out
    names = [n for n in dict.fromkeys([position['symbol'], market_symbol.get('broker'), market_symbol.get('symbol')]) if n]
    name = next((n for n in names if (live.folder / f'{n}.npz').is_file()), None)
    if name is None: out['reason'] = 'indeks yoxdur'; return out
    max_hold = int(live.settings.config['risk'].get('max_hold_minutes', 30))
    age = max(0.0, (now - position['opened']).total_seconds() / 60)
    minutes = int(min(60, max(5, (max_hold - age) if position.get('managed', True) else 60)))
    candles = market_symbol.get('candles') or {}
    state = tm.live_state(candles.get('M1') or [], candles.get('M15') or [])
    sim = tm.simulate(live.index(name), hour=now.hour, trend=state['trend'], direction=position['direction'], price=cur, atr=state['atr'],
                      stop_distance=stop_left, target_distance=target_left, spread=0.0, minutes=minutes, slippage_price=0.0, fan=0)
    now_r = (cur - entry) * sign / risk
    out.update({'minutes_left': minutes, 'now_r': now_r, 'analogs': sim['analogs']})
    if not sim['enough']: out['reason'] = f"oxşar an azdır ({sim['analogs']})"; return out
    c = sim['chosen']; hold_r = now_r + c['expectancy_r'] * stop_left / risk
    out.update({'used': True, 'target': c['target'], 'stop': c['stop'], 'time': c['time'], 'hold_r': hold_r})
    if hold_r < now_r - float(cfg['close_margin_r']):
        if not position.get('managed', True): out['reason'] = 'əl ilə, idarəsiz: yalnız göstərilir'
        elif not (demo or not cfg['demo_only']): out['reason'] = 'REAL hesab: yalnız göstərilir'
        elif age < float(cfg['min_age_minutes']): out['reason'] = 'mövqe çox təzədir'
        else: out.update({'action': 'close', 'reason': f'saxlamaq {hold_r:+.2f}R, indi bağlamaq {now_r:+.2f}R'})
    else:
        out['reason'] = f'saxlamaq {hold_r:+.2f}R ≥ indi {now_r:+.2f}R'
    return out

"""Long-history test of the bot's own strategy classes (the exact live signal code).

For every strategy x symbol the live `generate_signal` is called on closed-bar windows,
like the scanner does, at the close of each bar of the strategy's cadence timeframe.
Each signal is entered at the next M1 open with the real (scaled) spread and slippage,
and exited two ways:
  A  the strategy's own stop/target (rebased to the executable entry), long time limit;
  B  the bot's live handling: ATR(M1)*sqrt(horizon) stop/target and the hard time limit.
Results are R multiples after costs. Overlap (a new signal while the previous trade of the
same strategy+symbol is open) is removed later, per variant, in the report.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import math

import numpy as np
import pandas as pd

from .. import indicators as ind
from .. import parquet_compat
from ..strategies import REGISTRY
from ..trade_rules import strategy_mtf_allowed

TF_MINUTES = {'M1': 1, 'M5': 5, 'M15': 15, 'H1': 60, 'H4': 240}
# (cadence timeframe, finer timeframe used only after a "fine" skip reason)
CADENCE = {'trend_pullback': ('M5', None), 'mean_reversion': ('M15', 'M1'), 'breakout': ('M15', None),
           'volatility_scalp': ('M1', None), 'session_breakout': ('M15', None), 'opening_range_breakout': ('M15', None),
           'vwap_reversion': ('M15', None), 'donchian_breakout': ('H4', None), 'ts_momentum': ('H4', None),
           'index_pullback_200d': ('H1', None)}
FINE_REASONS = {'mean_reversion': {'M1_reversal_not_confirmed'}}
LONG_LIMIT = {'M1': 1440, 'M5': 1440, 'M15': 1440, 'H1': 4320, 'H4': 14400, 'D1': 28800}


def group_of(symbol: str, specs: dict) -> str:
    group = str(specs.get(symbol, {}).get('asset_group', '') or '')
    if group: return group
    upper = symbol.upper()
    if any(x in upper for x in ('GOLD', 'XAU', 'SILVER', 'XAG')): return 'metals'
    if any(x in upper for x in ('US30', 'US100', 'US500', 'NAS', 'SPX', 'GER', 'CASH')): return 'indices'
    return 'forex'


def point_of(symbol: str, specs: dict) -> float:
    spec = specs.get(symbol, {})
    return float(spec.get('point_size', spec.get('point', spec.get('trade_tick_size', 0.01))) or 0.01)


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'tick_volume': 'sum', 'spread': 'median'}
    agg = {key: value for key, value in agg.items() if key in frame}
    return frame.resample(rule, label='left', closed='left').agg(agg).dropna(subset=['open'])


# ---------------------------------------------------------------- indicator memo (research only)
# Strategies recompute the same indicators on an unchanged higher-timeframe window at every
# lower-timeframe step (e.g. H1 ADX at each M5 close). Results are memoised per window
# (identified by length, first/last timestamp and a value fingerprint), so the live code runs
# unchanged but each distinct window is computed once.
_MEMO: dict = {}
_MEMO_LIMIT = 4096


def _fingerprint(value):
    if isinstance(value, pd.DataFrame):
        if not len(value): return ('df', 0)
        close = value['close'].to_numpy() if 'close' in value else value.iloc[:, 0].to_numpy()
        return ('df', len(value), value.index[0].value, value.index[-1].value, tuple(value.columns), float(close[0]), float(close[-1]), float(close[len(close) // 2]))
    if isinstance(value, pd.Series):
        if not len(value): return ('s', 0)
        data = value.to_numpy()
        first = value.index[0]; last = value.index[-1]
        return ('s', value.name, len(value), getattr(first, 'value', first), getattr(last, 'value', last), float(data[0]), float(data[-1]), float(data[len(data) // 2]))
    return value


def _memoised(function):
    def wrapper(*args, **kwargs):
        try: key = (function.__module__, function.__name__, tuple(_fingerprint(a) for a in args), tuple(sorted(kwargs.items())))
        except (TypeError, ValueError, IndexError): return function(*args, **kwargs)
        hit = _MEMO.get(key)
        if hit is not None: return hit
        result = function(*args, **kwargs)
        if len(_MEMO) >= _MEMO_LIMIT: _MEMO.clear()
        _MEMO[key] = result
        return result
    wrapper.__wrapped__ = function
    return wrapper


def install_memo() -> None:
    """Memoise the heavy pure indicator functions used by strategies (idempotent)."""
    from .. import features
    from ..strategies import common, research_families, trend_pullback, mean_reversion, breakout, volatility_scalp
    names = ('atr', 'adx', 'rsi', 'ema', 'bollinger', 'nearest_levels', 'true_range')
    for name in names:
        original = getattr(ind, name)
        if not hasattr(original, '__wrapped__'): setattr(ind, name, _memoised(original))
    if not hasattr(features._timeframe_features, '__wrapped__'):
        wrapped = _memoised(features._timeframe_features); features._timeframe_features = wrapped
        mean_reversion._timeframe_features = wrapped


@dataclass
class SymbolData:
    symbol: str
    frames: dict            # tf -> DataFrame (UTC index = bar open)
    opens_ns: dict          # tf -> int64 open times
    point: float
    group: str


def load_symbol(history: Path, symbol: str, specs: dict, spread_mult: float = 1.25,
                scalp_params: dict | None = None) -> SymbolData:
    columns = ['open', 'high', 'low', 'close', 'tick_volume', 'spread']
    frames = {}
    for tf in ('M1', 'M5', 'M15'):
        path = history / f'{symbol}_{tf}.parquet'
        available = parquet_compat.read_parquet_columns(path)
        frame = parquet_compat.read_parquet(path, columns=[c for c in columns if c in available])
        index = pd.DatetimeIndex(frame.index)
        frame.index = index.tz_localize('UTC') if index.tz is None else index.tz_convert('UTC')
        if not frame.index.is_monotonic_increasing: frame = frame.sort_index()
        frame = frame[~frame.index.duplicated(keep='last')]
        for column in ('open', 'high', 'low', 'close'): frame[column] = frame[column].astype('float64')
        if 'spread' in frame: frame['spread'] = frame['spread'].astype('float64') * float(spread_mult)
        else: frame['spread'] = 0.0
        if 'tick_volume' not in frame: frame['tick_volume'] = 1
        frames[tf] = frame
    frames['H1'] = _resample(frames['M15'], '1h'); frames['H4'] = _resample(frames['M15'], '4h')
    point = point_of(symbol, specs)
    m1 = frames['M1']
    # Pre-computed inputs the live volatility scalp accepts (same formula, computed once).
    params = scalp_params or {}
    period = int(params.get('atr_period', 14)); lookback = int(params.get('percentile_lookback_bars', 7200))
    atr = ind.atr(m1, period)
    m1['__scalp_atr'] = atr
    m1['__scalp_percentile_threshold'] = atr.shift(1).rolling(lookback - 1, min_periods=100).quantile(float(params.get('atr_percentile', .70)))
    m1['__scalp_spread_price'] = m1['spread'] * point
    m1['__atr14'] = ind.atr(m1, 14)
    opens = {tf: frame.index.tz_convert(None).to_numpy().astype('datetime64[ns]').astype(np.int64) for tf, frame in frames.items()}
    return SymbolData(symbol, frames, opens, point, group_of(symbol, specs))


def _windows(data: SymbolData, decision_ns: int, timeframes, counts: dict) -> dict:
    out = {}
    for tf in timeframes:
        if tf not in data.frames: continue
        end = int(np.searchsorted(data.opens_ns[tf], decision_ns - TF_MINUTES[tf] * 60_000_000_000, side='right'))
        if end <= 0: continue
        start = max(0, end - counts.get(tf, 250))
        out[tf] = data.frames[tf].iloc[start:end]
    return {tf: frame for tf, frame in out.items() if len(frame) >= 60}


def simulate(m1: pd.DataFrame, arrays: dict, first: int, direction: str, stop_distance: float, target_distance: float,
             timeout_minutes: int, point: float, slippage_points: float) -> dict | None:
    """Same rules as the brain's label_trade: entry at the bar open (+spread for buys), stop first
    when both are touched in one bar, sells exit at the ask, slippage on both sides."""
    if first >= len(m1) or not (stop_distance > 0) or not (target_distance > 0): return None
    sign = 1.0 if direction == 'buy' else -1.0
    spread = max(0.0, float(arrays['spread'][first])) * point; slip = max(0.0, slippage_points) * point
    entry = float(arrays['open'][first]) + (spread if direction == 'buy' else 0.0) + slip * sign
    stop = entry - sign * stop_distance; target = entry + sign * target_distance
    deadline = arrays['times'][first] + int(timeout_minutes) * 60_000_000_000
    end = int(np.searchsorted(arrays['times'], deadline, side='right'))
    end = max(end, first + 1)
    lows = arrays['low'][first:end]; highs = arrays['high'][first:end]
    if direction == 'buy': sl_hit = lows <= stop; tp_hit = highs >= target
    else: sl_hit = highs + spread >= stop; tp_hit = lows + spread <= target
    big = end - first + 1
    first_sl = int(sl_hit.argmax()) if sl_hit.any() else big
    first_tp = int(tp_hit.argmax()) if tp_hit.any() else big
    if first_sl < big and first_sl <= first_tp: exit_price, reason, at = stop, 'sl', first + first_sl
    elif first_tp < big: exit_price, reason, at = target, 'tp', first + first_tp
    else:
        at = end - 1; exit_price, reason = float(arrays['close'][at]) + (spread if direction == 'sell' else 0.0), 'time'
    exit_price -= slip * sign
    net = (exit_price - entry) * sign
    return {'entry': entry, 'exit': exit_price, 'reason': reason, 'net_r': net / stop_distance,
            'exit_ns': int(arrays['times'][at]) + 60_000_000_000, 'stop_distance': stop_distance,
            'cost_r': (spread + 2 * slip) / stop_distance}


def run_task(data: SymbolData, strategy_name: str, params: dict, timeframes: list, start: pd.Timestamp, end: pd.Timestamp,
             config: dict) -> list[dict]:
    """All signals of one strategy on one symbol between start and end (decision times)."""
    install_memo(); _MEMO.clear()
    strategy = REGISTRY[strategy_name](params)
    risk = config['risk']; backtest = config.get('backtest', {}); scanner = config.get('scanner', {})
    slippage = float(backtest.get('slippage_points', 1.0))
    sl_mult = float(risk['sl_move_mult']); tp_mult = float(risk['tp_move_mult'])
    horizon = float(risk['move_horizon_minutes']); hold_b = int(risk.get('max_hold_minutes', 30))
    counts = {tf: int(scanner.get('candle_count', 250)) for tf in TF_MINUTES}
    needed = sorted(set(timeframes) | {'M1', 'M5', 'M15'})
    cadence, fine = CADENCE.get(strategy_name, ('M15', None))
    fine_reasons = FINE_REASONS.get(strategy_name, set())
    m1 = data.frames['M1']
    arrays = {'open': m1['open'].to_numpy(), 'high': m1['high'].to_numpy(), 'low': m1['low'].to_numpy(),
              'close': m1['close'].to_numpy(), 'spread': m1['spread'].to_numpy(), 'times': data.opens_ns['M1'],
              'atr': m1['__atr14'].to_numpy()}
    step = TF_MINUTES[cadence] * 60_000_000_000
    opens = data.opens_ns[cadence]
    lo = int(np.searchsorted(opens, start.value, side='left')); hi = int(np.searchsorted(opens, end.value, side='left'))
    rows = []

    def evaluate(decision_ns: int):
        candles = _windows(data, decision_ns, needed, counts)
        if any(tf not in candles for tf in strategy.required_timeframes if tf in TF_MINUTES): return None, 'missing'
        strategy.last_skip_reason = 'conditions_not_met'
        signal = strategy.generate_signal(data.symbol, candles)
        return (signal, candles), getattr(strategy, 'last_skip_reason', '')

    def record(signal, candles, decision_ns: int):
        first = int(np.searchsorted(arrays['times'], decision_ns, side='left'))
        if first >= len(m1) or arrays['times'][first] - decision_ns > 5 * 60_000_000_000: return    # market closed at entry
        atr_m1 = float(arrays['atr'][first - 1]) if first > 0 else float('nan')
        spread_price = max(0.0, float(arrays['spread'][first])) * data.point
        row = {'strategy': strategy_name, 'symbol': data.symbol, 'group': data.group, 'direction': signal.direction,
               'signal_tf': signal.timeframe, 'decision_ns': decision_ns, 'entry_ns': int(arrays['times'][first]),
               'mtf_ok': bool(strategy_mtf_allowed(strategy_name, candles, signal.direction,
                                                   enabled=bool(scanner.get('require_mtf_agreement', True)),
                                                   fast=int(scanner.get('mtf_ema_fast', 5)), slow=int(scanner.get('mtf_ema_slow', 13)),
                                                   min_agree=int(scanner.get('mtf_min_agree', 3)))),
               'confidence': float(signal.confidence)}
        stop_a = abs(float(signal.entry_hint) - float(signal.sl_price)); target_a = abs(float(signal.tp_price) - float(signal.entry_hint))
        a = simulate(m1, arrays, first, signal.direction, stop_a, target_a, LONG_LIMIT.get(str(signal.timeframe), 1440), data.point, slippage)
        b = None
        if math.isfinite(atr_m1) and atr_m1 > 0:
            root = math.sqrt(max(1.0, horizon))
            b = simulate(m1, arrays, first, signal.direction, atr_m1 * root * sl_mult, atr_m1 * root * tp_mult + spread_price,
                         hold_b, data.point, slippage)
        for name, result in (('a', a), ('b', b)):
            if result is None: row.update({f'{name}_net_r': np.nan, f'{name}_reason': '', f'{name}_exit_ns': 0, f'{name}_cost_r': np.nan})
            else: row.update({f'{name}_net_r': result['net_r'], f'{name}_reason': result['reason'],
                              f'{name}_exit_ns': result['exit_ns'], f'{name}_cost_r': result['cost_r']})
        rows.append(row)

    fine_step = TF_MINUTES[fine] * 60_000_000_000 if fine else None
    for position in range(lo, hi):
        decision = int(opens[position]) + step
        result, reason = evaluate(decision)
        if result is not None and result[0] is not None:
            record(result[0], result[1], decision); continue
        if fine and reason in fine_reasons:
            # The coarse conditions hold; the finer frame (e.g. M1 reversal) can confirm until the next coarse close.
            for k in range(1, step // fine_step):
                at = decision + k * fine_step
                result, reason = evaluate(at)
                if result is not None and result[0] is not None:
                    record(result[0], result[1], at); break
                if reason not in fine_reasons: break
    return rows


SESSIONS = ((0, 7, 'Asiya 00-07'), (7, 12, 'London 07-12'), (12, 16, 'London+NY 12-16'), (16, 21, 'New York 16-21'), (21, 24, 'Gec 21-24'))


def session_of(hour: int) -> str:
    for lo, hi, name in SESSIONS:
        if lo <= hour < hi: return name
    return SESSIONS[-1][2]


def without_overlap(trades: pd.DataFrame, variant: str) -> pd.DataFrame:
    """Keep a signal only when the previous kept trade of the same strategy+symbol has exited."""
    column = f'{variant}_net_r'; exits = f'{variant}_exit_ns'
    usable = trades[trades[column].notna()].sort_values(['strategy', 'symbol', 'entry_ns'])
    keep = np.zeros(len(usable), dtype=bool)
    entry = usable['entry_ns'].to_numpy(); leave = usable[exits].to_numpy()
    keys = (usable['strategy'] + '|' + usable['symbol']).to_numpy()
    busy_until = {}
    for i in range(len(usable)):
        if entry[i] >= busy_until.get(keys[i], -1):
            keep[i] = True; busy_until[keys[i]] = leave[i]
    return usable[keep]


def metrics(values: pd.Series) -> dict:
    r = values.to_numpy(dtype=float)
    if not len(r): return {'trades': 0, 'win_rate': None, 'expectancy_r': None, 'profit_factor': None, 'max_dd_r': None}
    wins = r[r > 0].sum(); losses = -r[r < 0].sum()
    equity = np.cumsum(r); dd = float((np.maximum.accumulate(np.r_[0, equity])[1:] - equity).max())
    return {'trades': int(len(r)), 'win_rate': float((r > 0).mean()), 'expectancy_r': float(r.mean()),
            'profit_factor': float(wins / losses) if losses > 0 else (float('inf') if wins > 0 else None), 'max_dd_r': dd}


def period_of(stamp_ns: np.ndarray, validation_start: pd.Timestamp, test_start: pd.Timestamp) -> np.ndarray:
    return np.where(stamp_ns >= test_start.value, 'imtahan', np.where(stamp_ns >= validation_start.value, 'yoxlama', 'oyrenme'))


def qualifies(validation: dict, test: dict, test_thirds: list[dict], min_trades: int = 100, min_pf: float = 1.1) -> tuple[bool, list[str]]:
    reasons = []
    if not validation['trades'] or (validation['expectancy_r'] or 0) <= 0: reasons.append('yoxlama illərində mənfi')
    if test['trades'] < min_trades: reasons.append(f'imtahanda {test["trades"]} ticarət (<{min_trades})')
    if not test['trades'] or (test['expectancy_r'] or 0) <= 0: reasons.append('imtahanda mənfi')
    if not test['profit_factor'] or test['profit_factor'] <= min_pf: reasons.append(f'imtahanda PF ≤ {min_pf}')
    if sum(1 for part in test_thirds if part['trades'] and (part['expectancy_r'] or 0) > 0) < 2: reasons.append('imtahanın 3 hissəsindən 2-si müsbət deyil')
    return not reasons, reasons

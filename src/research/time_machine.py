"""Time machine: simulate a trade from "now" with the real price paths that followed similar
moments in the long history.

Similar = same symbol, same UTC hour (±1), same M15 trend state, same volatility third.
Every input of a historical moment uses closed bars only (ATR of the previous M1 bars, the last
CLOSED M15 bar for the trend), so nothing leaks from the future. Each analog path is measured in
ATR units from its entry and rescaled to the current ATR.

Costs follow the strategy test: buys enter at the ask, sells exit at the ask (the bars are bid),
slippage on both sides, and when one bar touches both stop and target the stop counts.
Moments whose window crosses a market close are dropped. With fewer than MIN_ANALOGS distinct
moments no numbers are given.
"""
from __future__ import annotations
from pathlib import Path
import json
import math

import numpy as np
import pandas as pd

from .. import indicators as ind
from .. import parquet_compat

MINUTE = 60_000_000_000
DAY = 1440 * MINUTE
MIN_ANALOGS = 200
MAX_ANALOGS = 3000
STOP_SCALES = (('Qısa', 0.5), ('Normal', 1.0), ('Geniş', 1.5))
TARGET_RATIOS = (1.0, 1.5, 2.0)
TREND_WORDS = {1: 'yuxarı', -1: 'aşağı', 0: 'yan'}
VOL_WORDS = {0: 'aşağı', 1: 'orta', 2: 'yüksək'}


def trend_codes(close: pd.Series) -> np.ndarray:
    """+1 up, -1 down, 0 sideways: EMA20 vs EMA50 and the EMA20 slope over 3 bars (as the panel's view)."""
    close = close.astype('float64')
    fast, slow = ind.ema(close, 20), ind.ema(close, 50)
    slope = fast - fast.shift(3)
    up = ((fast > slow) & (slope > 0)).to_numpy(); down = ((fast < slow) & (slope < 0)).to_numpy()
    return np.where(up, 1, np.where(down, -1, 0)).astype(np.int8)


def _frame(path: Path) -> pd.DataFrame:
    frame = parquet_compat.read_parquet(path, columns=['open', 'high', 'low', 'close'])
    index = pd.DatetimeIndex(frame.index)
    frame.index = index.tz_localize('UTC') if index.tz is None else index.tz_convert('UTC')
    frame = frame[~frame.index.duplicated(keep='last')].sort_index()
    return frame.astype('float64')


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    return index.tz_convert(None).to_numpy().astype('datetime64[ns]').astype(np.int64)


def build_index_from_frames(m1: pd.DataFrame, m15: pd.DataFrame) -> dict:
    times = _ns(m1.index)
    atr = ind.atr(m1, 14).shift(1).to_numpy()                 # known at the open of bar i
    t15 = _ns(m15.index); code15 = trend_codes(m15['close'])
    j = np.searchsorted(t15 + 15 * MINUTE, times, side='right') - 1   # last M15 bar CLOSED by this open
    trend = np.where(j >= 0, code15[np.clip(j, 0, None)], 0).astype(np.int8)
    opens = m1['open'].to_numpy()
    rel = atr / opens
    recent = times >= times[-1] - 730 * DAY
    usable = np.isfinite(rel) & recent
    edges = np.nanquantile(rel[usable], [1 / 3, 2 / 3]) if usable.any() else np.array([np.inf, np.inf])
    vol = np.digitize(np.nan_to_num(rel, nan=-1.0), edges).astype(np.int8)
    vol[~np.isfinite(rel)] = -1
    f32 = lambda a: np.asarray(a, dtype=np.float32)      # ~60 MB per 5 years of M1 instead of ~150 MB; ample precision
    return {'times': times, 'open': f32(opens), 'high': f32(m1['high'].to_numpy()), 'low': f32(m1['low'].to_numpy()),
            'close': f32(m1['close'].to_numpy()), 'atr': f32(atr), 'trend': trend,
            'hour': ((times // (3600 * 10**9)) % 24).astype(np.int8), 'vol': vol, 'edges': np.asarray(edges, dtype='float64')}


def load_index(history: Path, symbol: str, cache_dir: Path | None = None, build: bool = True) -> dict:
    """Index of one symbol, cached as .npz and rebuilt when the history is newer.
    build=False (the panel, which runs with little memory) only reads an existing cache."""
    source = Path(history) / f'{symbol}_M1.parquet'
    cache = (Path(cache_dir) if cache_dir else Path(history) / 'time_machine') / f'{symbol}.npz'
    if not build:
        if not cache.is_file(): raise LookupError(f'{symbol}: zaman maşınının indeksi hazır deyil (serverdə scripts/check_time_machine.py işlədin)')
        with np.load(cache) as data: return {key: data[key] for key in data.files}
    if not source.is_file(): raise FileNotFoundError(f'{symbol}: uzun tarixçə yoxdur')
    if cache.is_file() and cache.stat().st_mtime >= source.stat().st_mtime:
        with np.load(cache) as data: return {key: data[key] for key in data.files}
    index = build_index_from_frames(_frame(source), _frame(Path(history) / f'{symbol}_M15.parquet'))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix('.tmp.npz'); np.savez(tmp, **index); tmp.replace(cache)
    except OSError:
        pass
    return index


def vol_bucket(index: dict, atr: float, price: float) -> int:
    if not (atr > 0 and price > 0): return -1
    return int(np.digitize([atr / price], index['edges'])[0])


def select_analogs(index: dict, hour: int, trend: int, vol: int, minutes: int, before_ns: int | None = None,
                   seed: int = 11, max_n: int = MAX_ANALOGS) -> tuple[np.ndarray, str]:
    """Distinct similar moments (at most one per 15 minutes), loosening the match only when too few."""
    times = index['times']; last = len(times) - minutes - 1
    if last <= 0: return np.array([], dtype=np.int64), 'tarixçə qısadır'
    span = times[minutes:minutes + last] - times[:last]
    atr = index['atr'][:last]
    base = np.isfinite(atr) & (atr > 0) & (span <= (minutes + 5) * MINUTE)
    if before_ns is not None: base &= times[:last] + (minutes + 1) * MINUTE < before_ns
    hdist = np.abs(((index['hour'][:last].astype(np.int16) - int(hour) + 12) % 24) - 12)
    same_trend = index['trend'][:last] == int(trend); same_vol = index['vol'][:last] == int(vol)
    levels = (('eyni saat (±1), trend və volatillik', (hdist <= 1) & same_trend & same_vol),
              ('yaxın saat (±2), trend və volatillik', (hdist <= 2) & same_trend & same_vol),
              ('istənilən saat, eyni trend və volatillik', same_trend & same_vol),
              ('istənilən saat, eyni volatillik', same_vol))
    chosen = np.array([], dtype=np.int64); label = levels[-1][0]
    for label, mask in levels:
        chosen = np.flatnonzero(base & mask)
        if len(chosen):
            slot = times[chosen] // (15 * MINUTE)
            chosen = chosen[np.r_[True, slot[1:] != slot[:-1]]]
        if len(chosen) >= MIN_ANALOGS: break
    if len(chosen) > max_n:
        chosen = np.sort(np.random.default_rng(seed).choice(chosen, max_n, replace=False))
    return chosen, label


def paths(index: dict, starts: np.ndarray, direction: str, minutes: int) -> dict:
    """Favourable and adverse excursion per bar and the final move, in ATR units of each analog."""
    sign = 1.0 if direction == 'buy' else -1.0
    o = index['open'][starts]; a = index['atr'][starts]
    window = starts[:, None] + np.arange(minutes)[None, :]
    up = (index['high'][window] - o[:, None]) / a[:, None]
    down = (o[:, None] - index['low'][window]) / a[:, None]
    fav, adv = (up, down) if sign > 0 else (down, up)
    close = (index['close'][window] - o[:, None]) / a[:, None]
    return {'fav': fav, 'adv': adv, 'final': close[:, -1] * sign, 'close': close}


def evaluate(p: dict, stop_atr: float, target_atr: float, spread_atr: float, slip_atr: float) -> dict:
    """Outcome of every analog for one stop/target (ATR units). Returns codes 1 target, -1 stop, 0 time."""
    fav, adv = p['fav'], p['adv']; n, m = fav.shape
    if n == 0 or not (stop_atr > 0) or not (target_atr > 0):
        return {'n': int(n), 'codes': np.zeros(n, dtype=np.int8), 'r': np.zeros(n)}
    stop_hit = adv >= stop_atr - spread_atr          # bid-based bars: the executable side is a spread worse
    tgt_hit = fav >= target_atr + spread_atr
    big = m + 1
    first_stop = np.where(stop_hit.any(1), stop_hit.argmax(1), big)
    first_tgt = np.where(tgt_hit.any(1), tgt_hit.argmax(1), big)
    stopped = (first_stop < big) & (first_stop <= first_tgt)
    targeted = ~stopped & (first_tgt < big)
    r = np.where(stopped, -1.0, np.where(targeted, target_atr / stop_atr, (p['final'] - spread_atr) / stop_atr))
    r = r - 2 * slip_atr / stop_atr
    codes = np.where(targeted, 1, np.where(stopped, -1, 0)).astype(np.int8)
    return {'n': int(n), 'codes': codes, 'r': r}


def summarise(result: dict) -> dict:
    n = result['n']
    if not n: return {'n': 0, 'target': None, 'stop': None, 'time': None, 'win': None, 'expectancy_r': None}
    codes, r = result['codes'], result['r']
    return {'n': n, 'target': float((codes == 1).mean()), 'stop': float((codes == -1).mean()), 'time': float((codes == 0).mean()),
            'win': float((r > 0).mean()), 'expectancy_r': float(r.mean())}


def live_state(m1: list[dict], m15: list[dict]) -> dict:
    """ATR and trend of now from the panel's candles; the last (still forming) bar is ignored."""
    if len(m1) < 20 or len(m15) < 56: raise ValueError('Canlı şam azdır')
    frame = pd.DataFrame(m1[:-1]).rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close'}).astype({'open': float, 'high': float, 'low': float, 'close': float})
    atr = float(ind.atr(frame, 14).iloc[-1])
    trend = int(trend_codes(pd.Series([float(bar['c']) for bar in m15[:-1]]))[-1])
    return {'atr': atr, 'trend': trend}


def simulate(index: dict, *, hour: int, trend: int, direction: str, price: float, atr: float,
             stop_distance: float, target_distance: float, spread: float, minutes: int = 30,
             slippage_price: float = 0.0, fan: int = 150, seed: int = 11) -> dict:
    """Everything the screen shows: chosen stop/target, the 3x3 grid and a fan of rescaled paths."""
    if direction not in ('buy', 'sell'): raise ValueError('direction must be buy or sell')
    if not (atr > 0 and stop_distance > 0 and target_distance > 0): raise ValueError('ATR, stop and target must be positive')
    vol = vol_bucket(index, atr, price)
    starts, level = select_analogs(index, hour, trend, vol, minutes, seed=seed)
    out = {'analogs': int(len(starts)), 'match': level, 'enough': len(starts) >= MIN_ANALOGS, 'minutes': minutes,
           'state': {'hour_utc': int(hour), 'trend': TREND_WORDS.get(int(trend), 'yan'), 'volatility': VOL_WORDS.get(vol, '?')},
           'stop_distance': stop_distance, 'target_distance': target_distance}
    if len(starts):
        years = pd.to_datetime(index['times'][starts[[0, -1]]]).year
        out['years'] = [int(years[0]), int(years[1])]
    if not out['enough']: return out
    p = paths(index, starts, direction, minutes)
    spread_atr = max(0.0, spread) / atr; slip_atr = max(0.0, slippage_price) / atr
    s_user, t_user = stop_distance / atr, target_distance / atr
    chosen = evaluate(p, s_user, t_user, spread_atr, slip_atr)
    out['chosen'] = summarise(chosen)
    grid = []
    for name, scale in STOP_SCALES:
        row = {'name': name, 'scale': scale, 'stop_distance': stop_distance * scale, 'cells': []}
        for ratio in TARGET_RATIOS:
            stats = summarise(evaluate(p, s_user * scale, s_user * scale * ratio, spread_atr, slip_atr))
            row['cells'].append({'ratio': ratio, 'target_distance': stop_distance * scale * ratio, **stats})
        grid.append(row)
    out['grid'] = grid
    best = max(((row['name'], cell) for row in grid for cell in row['cells']), key=lambda item: item[1]['expectancy_r'])
    out['best'] = {'stop': best[0], 'ratio': best[1]['ratio'], 'expectancy_r': best[1]['expectancy_r']}
    rng = np.random.default_rng(seed + 1)
    pick = np.sort(rng.choice(len(starts), min(fan, len(starts)), replace=False))
    sign = 1.0 if direction == 'buy' else -1.0
    out['fan'] = [{'o': int(chosen['codes'][i]), 'p': [round(price + float(v) * atr, 6) for v in np.r_[0.0, p['close'][i]]]} for i in pick]
    out['entry'] = price
    out['stop'] = price - sign * stop_distance; out['target'] = price + sign * target_distance
    return out


def calibrate(index: dict, *, samples: int = 1500, minutes: int = 30, stop_atr: float | None = None,
              ratio: float = 1.5, years_back: int = 2, seed: int = 7) -> dict:
    """Out-of-sample check: random past moments are simulated with only the history before them
    and compared with what really happened. Calibrated = predicted target rates match reality."""
    stop_atr = float(stop_atr or math.sqrt(30) * 0.8); target_atr = stop_atr * ratio
    times = index['times']; last = len(times) - minutes - 1
    rng = np.random.default_rng(seed)
    span = times[minutes:minutes + last] - times[:last]
    candidates = np.flatnonzero((times[:last] >= times[-1] - years_back * 365 * DAY) & np.isfinite(index['atr'][:last])
                                & (index['atr'][:last] > 0) & (span <= (minutes + 5) * MINUTE) & (index['vol'][:last] >= 0))
    if len(candidates) == 0: return {'samples': 0}
    moments = np.sort(rng.choice(candidates, min(samples, len(candidates)), replace=False))
    rows = []
    for k, m in enumerate(moments):
        direction = 'buy' if k % 2 == 0 else 'sell'
        starts, _ = select_analogs(index, int(index['hour'][m]), int(index['trend'][m]), int(index['vol'][m]), minutes,
                                   before_ns=int(times[m]) - DAY, seed=seed + k, max_n=1500)
        if len(starts) < MIN_ANALOGS: continue
        predicted = summarise(evaluate(paths(index, starts, direction, minutes), stop_atr, target_atr, 0.0, 0.0))
        actual = evaluate(paths(index, np.array([m]), direction, minutes), stop_atr, target_atr, 0.0, 0.0)
        rows.append((predicted['target'], predicted['expectancy_r'], float(actual['codes'][0] == 1), float(actual['r'][0])))
    if not rows: return {'samples': 0}
    arr = np.array(rows); p, er, hit, r = arr.T
    base = hit.mean()
    brier = float(((p - hit) ** 2).mean()); brier_base = float(((base - hit) ** 2).mean())
    edges = np.quantile(p, np.linspace(0, 1, 6)); bins = []
    for i in range(5):
        mask = (p >= edges[i]) & ((p < edges[i + 1]) if i < 4 else (p <= edges[i + 1]))
        if mask.sum(): bins.append({'n': int(mask.sum()), 'predicted': float(p[mask].mean()), 'actual': float(hit[mask].mean()),
                                    'expected_r': float(er[mask].mean()), 'actual_r': float(r[mask].mean())})
    worst = max((abs(b['predicted'] - b['actual']) for b in bins if b['n'] >= 50), default=1.0)
    reliable = bool(len(rows) >= 300 and worst <= 0.08 and brier <= brier_base * 1.01)
    return {'samples': int(len(rows)), 'base_rate': float(base), 'brier': brier, 'brier_base': brier_base,
            'worst_gap': float(worst), 'bins': bins, 'reliable': reliable, 'stop_atr': stop_atr, 'ratio': ratio, 'minutes': minutes}


def read_calibration(path: Path) -> dict:
    try: return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}

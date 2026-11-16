"""Stop / target / time-limit test for every strategy x symbol, on the strategy test's own signals.

The signals in reports/strategy_test/trades.parquet are re-exited with the bot's handling (B) on a
grid: stop = ATR(M1) * sqrt(30) * {0.4, 0.6, 0.8}, target = stop * {1.0, 1.5, 2.0} (+ spread, as
live), time limit {15, 30, 60} minutes. The best setting is CHOSEN ON THE VALIDATION YEARS ONLY and
then judged on the test years with the same pass rules. Nothing in the bot changes.

  python scripts/run_exit_grid.py --workers 8

Writes reports/strategy_test/exit_grid.json and exit_grid.md.
"""
from __future__ import annotations
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import argparse
import itertools
import json
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd

from src.config import load_settings
from src.external_history import research_history_dir
from src.research import strategy_test as st

STOPS = (0.4, 0.6, 0.8)
RATIOS = (1.0, 1.5, 2.0)
LIMITS = (15, 30, 60)
GRID = list(itertools.product(STOPS, RATIOS, LIMITS))


def _symbol_job(job):
    history, symbol, specs, spread_mult, slippage, horizon, frame = job
    data = st.load_symbol(Path(history), symbol, specs, spread_mult)
    m1 = data.frames['M1']
    arrays = {'open': m1['open'].to_numpy(), 'high': m1['high'].to_numpy(), 'low': m1['low'].to_numpy(),
              'close': m1['close'].to_numpy(), 'spread': m1['spread'].to_numpy(), 'times': data.opens_ns['M1'],
              'atr': m1['__atr14'].to_numpy()}
    root = math.sqrt(max(1.0, horizon))
    out = {f'g{k}_{c}': np.full(len(frame), np.nan if c == 'net_r' else 0, dtype=float if c == 'net_r' else np.int64)
           for k in range(len(GRID)) for c in ('net_r', 'exit_ns')}
    entries = frame['entry_ns'].to_numpy(); directions = frame['direction'].to_numpy()
    for row in range(len(frame)):
        first = int(np.searchsorted(arrays['times'], entries[row], side='left'))
        if first <= 0 or first >= len(m1): continue
        atr = float(arrays['atr'][first - 1])
        if not (math.isfinite(atr) and atr > 0): continue
        spread_price = max(0.0, float(arrays['spread'][first])) * data.point
        for k, (stop, ratio, limit) in enumerate(GRID):
            distance = atr * root * stop
            result = st.simulate(m1, arrays, first, directions[row], distance, distance * ratio + spread_price, limit, data.point, slippage)
            if result is None: continue
            out[f'g{k}_net_r'][row] = result['net_r']; out[f'g{k}_exit_ns'][row] = result['exit_ns']
    return symbol, frame.index.to_numpy(), out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workers', type=int, default=4); parser.add_argument('--spread-mult', type=float, default=1.25)
    parser.add_argument('--min-validation', type=int, default=100)
    args = parser.parse_args()
    settings = load_settings(require_credentials=False); config = settings.config
    folder = settings.root / 'reports/strategy_test'
    if not (folder / 'trades.parquet').is_file(): print('Əvvəlcə scripts/run_strategy_test.py'); return 1
    trades = pd.read_parquet(folder / 'trades.parquet').reset_index(drop=True)
    history = research_history_dir(settings)
    specs = json.loads((history / 'symbol_specs.json').read_text(encoding='utf-8')) if (history / 'symbol_specs.json').is_file() else {}
    slippage = float(config.get('backtest', {}).get('slippage_points', 1.0)); horizon = float(config['risk']['move_horizon_minutes'])
    jobs = [(str(history), symbol, specs, args.spread_mult, slippage, horizon, part[['entry_ns', 'direction']])
            for symbol, part in trades.groupby('symbol')]
    print(f'Exit grid: {len(trades):,} signals, {len(GRID)} settings, {len(jobs)} symbols', flush=True)
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for symbol, rows, out in pool.map(_symbol_job, jobs):
            for column, values in out.items():
                if column not in trades: trades[column] = np.nan if column.endswith('net_r') else 0
                trades.loc[rows, column] = values
            print(f'  {symbol} done', flush=True)
    current = (float(config['risk']['sl_move_mult']), round(float(config['risk']['tp_move_mult']) / float(config['risk']['sl_move_mult']), 2),
               int(config['risk'].get('max_hold_minutes', 30)))
    results = []
    for mtf_only in (False, True):
        base = trades[trades['mtf_ok']] if mtf_only else trades
        for (strategy, symbol), frame in base.groupby(['strategy', 'symbol']):
            options = []
            for k, (stop, ratio, limit) in enumerate(GRID):
                kept = st.without_overlap(frame, f'g{k}')
                column = f'g{k}_net_r'
                validation = st.metrics(kept.loc[kept['period'] == 'yoxlama', column])
                test_part = kept.loc[kept['period'] == 'imtahan']
                test = st.metrics(test_part[column])
                options.append({'stop': stop, 'ratio': ratio, 'limit': limit, 'validation': validation, 'test': test,
                                'thirds': _thirds(test_part, column)})
            usable = [o for o in options if o['validation']['trades'] >= args.min_validation]
            if not usable: continue
            best = max(usable, key=lambda o: o['validation']['expectancy_r'])
            now = next((o for o in options if (o['stop'], o['ratio'], o['limit']) == current), None)
            ok, reasons = st.qualifies(best['validation'], best['test'], best['thirds'])
            results.append({'strategy': strategy, 'symbol': symbol, 'mtf_filter': mtf_only,
                            'best': {k: best[k] for k in ('stop', 'ratio', 'limit', 'validation', 'test')}, 'passed': ok, 'reasons': reasons,
                            'current': None if now is None else {k: now[k] for k in ('validation', 'test')}})
    report = {'built_at_utc': datetime.now(timezone.utc).isoformat(), 'grid': {'stop': STOPS, 'ratio': RATIOS, 'limit': LIMITS},
              'current': {'stop': current[0], 'ratio': current[1], 'limit': current[2]}, 'results': results}
    (folder / 'exit_grid.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    (folder / 'exit_grid.md').write_text(render(report), encoding='utf-8')
    print(f"Done: {sum(r['passed'] for r in results)} of {len(results)} strategy x symbol pass with their best setting. Report: {folder / 'exit_grid.md'}")
    return 0


def _thirds(frame, column):
    if frame.empty: return [st.metrics(frame[column])] * 3
    edges = np.linspace(frame['entry_ns'].min(), frame['entry_ns'].max() + 1, 4)
    return [st.metrics(frame.loc[(frame['entry_ns'] >= edges[i]) & (frame['entry_ns'] < edges[i + 1]), column]) for i in range(3)]


def render(report: dict) -> str:
    f = lambda v: '–' if v is None else f'{v:+.3f}'
    c = report['current']
    lines = ['# Stop / hədəf / vaxt sınağı', '',
             f"İndiki ayar: stop {c['stop']}, hədəf stopun {c['ratio']} qatı, vaxt {c['limit']} dəq. "
             'Ən yaxşı ayar YALNIZ yoxlama illərində seçilib, imtahan illəri onu təsdiqləməlidir.', '',
             '| Strategiya | Simvol | MTF | İndiki: imtahan R (n) | Ən yaxşı: stop · hədəf · vaxt | Yoxlama R | İmtahan R (n) | İmtahan PF | Keçdi |',
             '|---|---|---|---:|---|---:|---:|---:|---|']
    for r in sorted(report['results'], key=lambda r: -(r['best']['test']['expectancy_r'] or -9)):
        b = r['best']; now = r['current']
        lines.append(f"| {r['strategy']} | {r['symbol']} | {'bəli' if r['mtf_filter'] else 'xeyr'} | "
                     f"{'–' if now is None else f(now['test']['expectancy_r']) + ' (' + str(now['test']['trades']) + ')'} | "
                     f"{b['stop']} · {b['ratio']}× · {b['limit']} dəq | {f(b['validation']['expectancy_r'])} | "
                     f"{f(b['test']['expectancy_r'])} ({b['test']['trades']}) | "
                     f"{'–' if not b['test']['profit_factor'] else round(b['test']['profit_factor'], 2)} | {'✓' if r['passed'] else '✗ ' + '; '.join(r['reasons'])} |")
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    raise SystemExit(main())

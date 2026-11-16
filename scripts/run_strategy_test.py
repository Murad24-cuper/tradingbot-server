"""Test every bot strategy on the long history (exact live signal code, real costs).

  python scripts/run_strategy_test.py                    # all strategies, all long-history symbols
  python scripts/run_strategy_test.py --workers 12
  python scripts/run_strategy_test.py --strategies session_breakout --symbols GOLD --from 2022-01-01   # quick look

Periods: learning (before --validation-start), validation (until --test-start), test (after).
Writes reports/strategy_test/: trades.parquet, summary.json, summary.md. Nothing in the bot changes.
"""
from __future__ import annotations
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd

from src.config import load_settings
from src.external_history import research_history_dir
from src.parquet_compat import write_parquet
from src.research import strategy_test as st
from src.strategies import REGISTRY

_CACHE: dict = {}


def _task(job):
    history, symbol, specs, spread_mult, scalp_params, strategy, params, timeframes, start, end, config = job
    if _CACHE.get('symbol') != symbol:
        _CACHE.clear(); _CACHE['symbol'] = symbol
        _CACHE['data'] = st.load_symbol(Path(history), symbol, specs, spread_mult, scalp_params)
    began = time.perf_counter()
    rows = st.run_task(_CACHE['data'], strategy, params, timeframes, pd.Timestamp(start), pd.Timestamp(end), config)
    return strategy, symbol, start[:4], len(rows), time.perf_counter() - began, rows


def _default_workers() -> int:
    cores = max(1, (os.cpu_count() or 2) - 2)
    try:
        import psutil
        by_memory = int(psutil.virtual_memory().total / 2**30 / 1.8)
    except ImportError:
        by_memory = 4
    return max(1, min(12, cores, by_memory))


def _fmt(value, kind='r'):
    if value is None or (isinstance(value, float) and not np.isfinite(value)): return '–' if value is None else '∞'
    return f'{value:.0%}' if kind == '%' else f'{value:+.3f}' if kind == 'r' else f'{value:.2f}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--strategies', nargs='*'); parser.add_argument('--symbols', nargs='*')
    parser.add_argument('--from', dest='start', default=None); parser.add_argument('--to', dest='end', default=None)
    parser.add_argument('--validation-start', default='2019-01-01'); parser.add_argument('--test-start', default='2023-01-01')
    parser.add_argument('--spread-mult', type=float, default=1.25)
    parser.add_argument('--workers', type=int, default=_default_workers(), help='Parallel processes (each holds one symbol, ~1.5 GB)')
    parser.add_argument('--out', default='reports/strategy_test')
    args = parser.parse_args()
    settings = load_settings(require_credentials=False); config = settings.config
    history = research_history_dir(settings)
    manifest = json.loads((history / 'manifest.json').read_text(encoding='utf-8')) if (history / 'manifest.json').is_file() else {'symbols': []}
    symbols = args.symbols or [entry['symbol'] for entry in manifest.get('symbols', [])]
    symbols = [s for s in symbols if (history / f'{s}_M1.parquet').is_file()]
    specs = json.loads((history / 'symbol_specs.json').read_text(encoding='utf-8')) if (history / 'symbol_specs.json').is_file() else {}
    entries = config.get('strategies', {})
    strategies = args.strategies or [name for name in REGISTRY if name in entries]
    scalp_params = entries.get('volatility_scalp', {}).get('params', {})
    jobs = []
    for symbol in symbols:
        group = st.group_of(symbol, specs)
        m1_index = pd.read_parquet(history / f'{symbol}_M15.parquet', columns=['close'], engine='fastparquet').index
        first = pd.Timestamp(args.start or m1_index.min()); first = first.tz_localize('UTC') if first.tz is None else first
        last = pd.Timestamp(args.end) if args.end else pd.Timestamp(m1_index.max()); last = last.tz_localize('UTC') if last.tz is None else last
        first = first + pd.Timedelta(days=14)        # warm-up for the indicator windows
        for name in strategies:
            entry = entries.get(name, {})
            if group not in entry.get('groups', [group]): continue
            timeframes = list(entry.get('timeframes', REGISTRY[name].required_timeframes))
            for year in range(first.year, last.year + 1):
                lo = max(first, pd.Timestamp(f'{year}-01-01', tz='UTC')); hi = min(last, pd.Timestamp(f'{year + 1}-01-01', tz='UTC'))
                if lo < hi:
                    jobs.append((str(history), symbol, specs, args.spread_mult, scalp_params, name, entry.get('params', {}), timeframes,
                                 lo.isoformat(), hi.isoformat(), config))
    print(f'Strategy test: {len(strategies)} strategies x {len(symbols)} symbols = {len(jobs)} yearly tasks, {args.workers} workers; '
          f'spread x{args.spread_mult}; validation from {args.validation_start}, test from {args.test_start}', flush=True)
    rows = []; started = time.perf_counter(); done = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for strategy, symbol, year, count, seconds, part in pool.map(_task, jobs):
            rows.extend(part); done += 1
            print(f'  [{done}/{len(jobs)}] {strategy} {symbol} {year}: {count} signals ({seconds:.0f} s)', flush=True)
    out = settings.root / args.out; out.mkdir(parents=True, exist_ok=True)
    trades = pd.DataFrame(rows)
    if trades.empty:
        print('No signals.'); return 1
    validation_start = pd.Timestamp(args.validation_start, tz='UTC'); test_start = pd.Timestamp(args.test_start, tz='UTC')
    trades['period'] = st.period_of(trades['entry_ns'].to_numpy(), validation_start, test_start)
    hours = pd.to_datetime(trades['entry_ns'], utc=True).dt.hour
    trades['hour_utc'] = hours; trades['session'] = hours.map(st.session_of)
    write_parquet(trades, out / 'trades.parquet', index=False)
    summary = build_summary(trades, test_start)
    summary.update({'built_at_utc': datetime.now(timezone.utc).isoformat(), 'symbols': symbols, 'strategies': strategies,
                    'spread_mult': args.spread_mult, 'validation_start': args.validation_start, 'test_start': args.test_start,
                    'elapsed_minutes': round((time.perf_counter() - started) / 60, 1)})
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
    (out / 'summary.md').write_text(render(summary), encoding='utf-8')
    passed = [row for row in summary['combinations'] if row['passed']]
    print(f'Done in {summary["elapsed_minutes"]} min: {len(trades):,} signals; {len(passed)} combinations pass. Report: {out / "summary.md"}')
    return 0


def _thirds(frame: pd.DataFrame, column: str) -> list[dict]:
    if frame.empty: return [st.metrics(frame[column])] * 3
    edges = np.linspace(frame['entry_ns'].min(), frame['entry_ns'].max() + 1, 4)
    return [st.metrics(frame.loc[(frame['entry_ns'] >= edges[i]) & (frame['entry_ns'] < edges[i + 1]), column]) for i in range(3)]


def build_summary(trades: pd.DataFrame, test_start: pd.Timestamp) -> dict:
    combos, overall = [], []
    for variant in ('a', 'b'):
        for mtf_only in (False, True):
            kept = st.without_overlap(trades[trades['mtf_ok']] if mtf_only else trades, variant)
            column = f'{variant}_net_r'
            label = {'a': 'A strategiyanin oz stop/hedefi', 'b': 'B botun idaresi (ATR stop, vaxt limiti)'}[variant]
            for (strategy, symbol), frame in kept.groupby(['strategy', 'symbol']):
                per = {period: st.metrics(frame.loc[frame['period'] == period, column]) for period in ('oyrenme', 'yoxlama', 'imtahan')}
                overall.append({'strategy': strategy, 'symbol': symbol, 'variant': variant, 'mtf_filter': mtf_only, **{f'{p}': m for p, m in per.items()}})
                levels = [('hamisi', frame)] + [(session, part) for session, part in frame.groupby('session')]
                for session, part in levels:
                    validation = st.metrics(part.loc[part['period'] == 'yoxlama', column])
                    test_part = part.loc[part['period'] == 'imtahan']
                    test = st.metrics(test_part[column])
                    ok, reasons = st.qualifies(validation, test, _thirds(test_part, column))
                    combos.append({'strategy': strategy, 'symbol': symbol, 'session': session, 'variant': variant, 'variant_label': label,
                                   'mtf_filter': mtf_only, 'learning': st.metrics(part.loc[part['period'] == 'oyrenme', column]),
                                   'validation': validation, 'test': test, 'passed': ok, 'reasons': reasons})
    return {'overall': overall, 'combinations': combos}


def render(summary: dict) -> str:
    lines = ['# Strategiya testi (uzun tarix)', '',
             f"Simvollar: {', '.join(summary['symbols'])}. Spred x{summary['spread_mult']}. "
             f"Yoxlama {summary['validation_start']}-dan, imtahan {summary['test_start']}-dan. Hesablama {summary['elapsed_minutes']} dəq.", '',
             'R = stop məsafəsi vahidi ilə nəticə (xərclərdən sonra). PF = profit factor. Üst-üstə düşən siqnallar çıxarılıb.', '',
             '## Keçən birləşmələr', '']
    passed = [row for row in summary['combinations'] if row['passed']]
    if not passed: lines += ['Heç bir birləşmə bütün şərtləri keçmədi.', '']
    else:
        lines += ['| Strategiya | Simvol | Sessiya | Çıxış | MTF | Yoxlama R | İmtahan ticarət | İmtahan uduş | İmtahan R | İmtahan PF |', '|---|---|---|---|---|---:|---:|---:|---:|---:|']
        for row in sorted(passed, key=lambda r: -(r['test']['expectancy_r'] or 0)):
            lines.append(f"| {row['strategy']} | {row['symbol']} | {row['session']} | {row['variant'].upper()} | {'bəli' if row['mtf_filter'] else 'xeyr'} | "
                         f"{_fmt(row['validation']['expectancy_r'])} | {row['test']['trades']} | {_fmt(row['test']['win_rate'], '%')} | "
                         f"{_fmt(row['test']['expectancy_r'])} | {_fmt(row['test']['profit_factor'], 'f')} |")
        lines.append('')
    lines += ['## Hər strategiya × simvol (bütün saatlar)', '',
              '| Strategiya | Simvol | Çıxış | MTF | Öyrənmə R (n) | Yoxlama R (n) | İmtahan R (n) | İmtahan PF | İmtahan uduş |', '|---|---|---|---|---:|---:|---:|---:|---:|']
    for row in summary['overall']:
        cell = lambda m: f"{_fmt(m['expectancy_r'])} ({m['trades']})"
        lines.append(f"| {row['strategy']} | {row['symbol']} | {row['variant'].upper()} | {'bəli' if row['mtf_filter'] else 'xeyr'} | "
                     f"{cell(row['oyrenme'])} | {cell(row['yoxlama'])} | {cell(row['imtahan'])} | {_fmt(row['imtahan']['profit_factor'], 'f')} | {_fmt(row['imtahan']['win_rate'], '%')} |")
    lines += ['', 'Keçmə şərtləri: yoxlama və imtahan illərində orta R > 0, imtahanda ≥100 ticarət, PF > 1.1, imtahanın 3 hissəsindən ≥2 müsbət.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    raise SystemExit(main())

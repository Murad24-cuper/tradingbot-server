"""Monte Carlo of trade results: how deep can the drawdown get, how long the losing streaks,
and which risk per trade keeps the bad case inside the account limit.

  python scripts/monte_carlo.py                  # strategy-test results (reports/strategy_test)
  python scripts/monte_carlo.py --source live    # the bot's own closed trades (data/bot.db)
  python scripts/monte_carlo.py --max-dd 15      # account drawdown limit (%) for the risk advice

Writes reports/monte_carlo/summary.json and summary.md. Reads only; nothing in the bot changes.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd

from src.config import load_settings
from src.research import monte_carlo as mc
from src.research import strategy_test as st
from src.strategy_guard import trade_r


def from_strategy_test(folder: Path) -> tuple[dict, str]:
    trades = pd.read_parquet(folder / 'trades.parquet')
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
    passed = [row for row in summary['combinations'] if row['passed']]
    note = 'strategiya testində keçən birləşmələr, imtahan dövrü'
    if not passed:
        passed = [row for row in summary['combinations'] if row['session'] == 'hamisi' and row['variant'] == 'b' and not row['mtf_filter']]
        note = 'HEÇ BİR birləşmə keçmədi: bütün strategiyalar (B çıxışı) göstərilir, yalnız məlumat üçün'
    groups = {}
    for row in passed:
        frame = trades[(trades['strategy'] == row['strategy']) & (trades['symbol'] == row['symbol'])]
        if row['mtf_filter']: frame = frame[frame['mtf_ok']]
        kept = st.without_overlap(frame, row['variant'])
        kept = kept[kept['period'] == 'imtahan']
        if row['session'] != 'hamisi': kept = kept[kept['session'] == row['session']]
        name = f"{row['strategy']} · {row['symbol']} · {row['session']} · {row['variant'].upper()}{' · MTF' if row['mtf_filter'] else ''}"
        groups[name] = kept[[f"{row['variant']}_net_r", 'entry_ns']].rename(columns={f"{row['variant']}_net_r": 'r'})
    return groups, note


def from_live(db: Path) -> tuple[dict, str]:
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        rows = pd.read_sql_query("""SELECT strategy, symbol, open_time, profit, commission, swap, features FROM trades
                                    WHERE status='closed' AND strategy NOT LIKE 'manual%'""", connection)
    # initial_risk is a PRICE distance; R in money comes from the sized risk (same as the strategy guard).
    rows['r'] = [trade_r(row) for row in rows.to_dict('records')]
    rows = rows[rows['r'].notna()]
    rows['entry_ns'] = pd.to_datetime(rows['open_time'], utc=True, format='mixed').astype('int64')
    return {f'{s}': part[['r', 'entry_ns']] for s, part in rows.groupby('strategy')}, 'botun bağlanmış əməliyyatları (əl ilə açılanlar xaric)'


def per_year(frame: pd.DataFrame) -> int:
    if frame.empty: return 0
    years = max((frame['entry_ns'].max() - frame['entry_ns'].min()) / (365.25 * 86400e9), 1 / 12)
    return int(round(len(frame) / years))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', choices=['test', 'live'], default='test')
    parser.add_argument('--max-dd', type=float, default=15.0, help='Account drawdown limit in %% for the risk advice')
    parser.add_argument('--sims', type=int, default=10_000)
    parser.add_argument('--out', default='reports/monte_carlo')
    args = parser.parse_args()
    settings = load_settings(require_credentials=False)
    if args.source == 'test':
        folder = settings.root / 'reports/strategy_test'
        if not (folder / 'trades.parquet').is_file(): print('Strategiya testinin nəticəsi yoxdur: əvvəlcə scripts/run_strategy_test.py'); return 1
        groups, note = from_strategy_test(folder)
    else:
        groups, note = from_live(settings.db_path)
    cap = float(settings.config['risk'].get('risk_per_trade_pct', 1.0)) * 2
    results = {}
    for name, frame in groups.items():
        res = mc.simulate(frame['r'].to_numpy(), n_trades=max(50, per_year(frame)), sims=args.sims)
        res['recommended_risk_pct'] = mc.recommended_risk(res, args.max_dd, cap)
        results[name] = res
    pooled = pd.concat(groups.values()) if groups else pd.DataFrame(columns=['r', 'entry_ns'])
    total = mc.simulate(pooled['r'].to_numpy(), n_trades=max(50, per_year(pooled)), sims=args.sims)
    total['recommended_risk_pct'] = mc.recommended_risk(total, args.max_dd, cap)
    out = settings.root / args.out; out.mkdir(parents=True, exist_ok=True)
    report = {'built_at_utc': datetime.now(timezone.utc).isoformat(), 'source': args.source, 'note': note, 'max_dd_pct': args.max_dd,
              'portfolio': total, 'groups': results}
    (out / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (out / 'summary.md').write_text(render(report), encoding='utf-8')
    print(render(report))
    return 0


def render(report: dict) -> str:
    f = lambda v, k='r': '–' if v is None else (f'{v:.0%}' if k == '%' else f'{v:+.2f}' if k == 'r' else f'{v:.1f}')
    lines = ['# Monte Carlo', '', f"Mənbə: {report['note']}. Hər birləşmə 1 illik ticarət sayı ilə 10 000 dəfə təkrar oynanıb.", '',
             f"Tövsiyə olunan risk: 95% halda düşüş {report['max_dd_pct']:g}%-i keçməsin. Orta nəticə mənfidirsə, risk tövsiyəsi verilmir.", '',
             '| Birləşmə | Ticarət | Orta R | Uduş | Düşüş R (median / 95% / 99%) | Ardıcıl zərər (95%) | İl sonu R (5% / median) | Mənfi bitmə | Risk % |',
             '|---|---:|---:|---:|---|---:|---|---:|---:|']
    rows = [('**Hamısı birlikdə**', report['portfolio'])] + list(report['groups'].items())
    for name, r in rows:
        if not r.get('enough'): lines.append(f'| {name} | {r["trades"]} | az məlumat | | | | | | |'); continue
        lines.append(f"| {name} | {r['trades']} | {f(r['expectancy_r'])} | {f(r['win_rate'], '%')} | "
                     f"{f(r['drawdown_r']['p50'], 'n')} / {f(r['drawdown_r']['p95'], 'n')} / {f(r['drawdown_r']['p99'], 'n')} | "
                     f"{r['losing_streak']['p95']:.0f} | {f(r['final_r']['p5'])} / {f(r['final_r']['p50'])} | {f(r['chance_negative'], '%')} | "
                     f"{'–' if r['recommended_risk_pct'] is None else r['recommended_risk_pct']} |")
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    raise SystemExit(main())

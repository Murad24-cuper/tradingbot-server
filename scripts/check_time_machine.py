"""Build the time-machine index for every long-history symbol and check it out of sample.

For random past moments (last 2 years) the simulation is run with only the history BEFORE that
moment and compared with what really happened. A symbol is marked reliable when the predicted
target rates match reality within 8 points in every fifth of predictions and the predictions are
at least as good as the plain average. The panel shows unreliable symbols with a warning.

  python scripts/check_time_machine.py
  python scripts/check_time_machine.py --symbols GOLD --samples 800

Writes data/time_machine/calibration.json and reports/time_machine/calibration.md.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import load_settings
from src.external_history import research_history_dir
from src.research import time_machine as tm


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--symbols', nargs='*'); parser.add_argument('--samples', type=int, default=1500)
    parser.add_argument('--minutes', type=int, default=30)
    args = parser.parse_args()
    settings = load_settings(require_credentials=False); config = settings.config
    history = research_history_dir(settings)
    manifest = json.loads((history / 'manifest.json').read_text(encoding='utf-8')) if (history / 'manifest.json').is_file() else {'symbols': []}
    symbols = args.symbols or [entry['symbol'] for entry in manifest.get('symbols', [])]
    risk = config['risk']
    stop_atr = (max(1.0, float(risk.get('move_horizon_minutes', 30)))) ** 0.5 * float(risk.get('sl_move_mult', 0.8))
    ratio = float(risk.get('tp_move_mult', 1.2)) / float(risk.get('sl_move_mult', 0.8))
    cache = Path(settings.data_dir) / 'time_machine'          # where the panel reads it
    results = {}
    for symbol in symbols:
        began = time.perf_counter()
        try: index = tm.load_index(history, symbol, cache)
        except FileNotFoundError as error: print(f'{symbol}: {error}'); continue
        report = tm.calibrate(index, samples=args.samples, minutes=args.minutes, stop_atr=stop_atr, ratio=ratio)
        report['seconds'] = round(time.perf_counter() - began, 1)
        results[symbol] = report
        word = 'ETİBARLI' if report.get('reliable') else 'etibarlı deyil'
        print(f"{symbol}: {word} · {report.get('samples', 0)} yoxlama · ən böyük fərq {report.get('worst_gap', 1):.0%} · {report['seconds']} s", flush=True)
    payload = {'built_at_utc': datetime.now(timezone.utc).isoformat(), 'minutes': args.minutes, 'symbols': results}
    cache.mkdir(parents=True, exist_ok=True)
    (cache / 'calibration.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    out = settings.root / 'reports/time_machine'; out.mkdir(parents=True, exist_ok=True)
    (out / 'calibration.md').write_text(render(payload), encoding='utf-8')
    return 0


def render(payload: dict) -> str:
    lines = ['# Zaman maşını: kalibrləmə yoxlaması', '',
             'Keçmişdəki təsadüfi anlar yalnız onlardan ƏVVƏLKİ tarixçə ilə simulyasiya olunub və real nəticə ilə müqayisə edilib.', '']
    for symbol, r in payload['symbols'].items():
        if not r.get('samples'): lines += [f'## {symbol}: məlumat yoxdur', '']; continue
        lines += [f"## {symbol}: {'etibarlı' if r['reliable'] else 'ETİBARLI DEYİL'}", '',
                  f"{r['samples']} yoxlama. Hədəfə çatma (orta): {r['base_rate']:.0%}. Brier {r['brier']:.4f} (sadə orta ilə {r['brier_base']:.4f}).", '',
                  '| Proqnoz qrupu | n | Proqnoz hədəf % | Real hədəf % | Proqnoz R | Real R |', '|---|---:|---:|---:|---:|---:|']
        for i, b in enumerate(r['bins'], 1):
            lines.append(f"| {i} (aşağıdan yuxarı) | {b['n']} | {b['predicted']:.0%} | {b['actual']:.0%} | {b['expected_r']:+.3f} | {b['actual_r']:+.3f} |")
        lines.append('')
    lines.append('Etibarlı: hər qrupda proqnoz ilə real arasında fərq ≤ 8 bənd və proqnoz sadə ortadan pis deyil. '
                 'Real R yuxarı qruplarda daha yüksəkdirsə, simulyasiya yaxşı əməliyyatları seçməyə kömək edir.')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    raise SystemExit(main())

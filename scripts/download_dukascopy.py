"""Build long M1/M5/M15 history (XM + older Dukascopy bars) for research and the brain.

Usage (MT5 is not needed; on the server it runs in the dashboard image):
  python scripts/download_dukascopy.py                 # every year Dukascopy has (indices from 2011/2013)
  python scripts/download_dukascopy.py --years 5       # only the last 5 years
  python scripts/download_dukascopy.py --symbols GOLD US500

Raw downloads are cached per month in data/history_external/dukascopy/ so the
script can be stopped and resumed. The merged result is written to the folder in
config history.research_dir (default data/history_long). The live bot never
reads it; research and brain scripts use it automatically once it exists.
"""
from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
import argparse
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import parquet_compat
from src.config import load_settings
from src.external_history import INSTRUMENTS, download_instrument, first_day, infer_point, merge_long, merge_timeframe, to_prices, write_manifest


HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
           'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9', 'Referer': 'https://www.dukascopy.com/'}


def http_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404: return b''            # no data that day (holiday)
        raise OSError(f'HTTP {exc.code}') from exc


def xm_file(history: Path, logical: str, aliases: dict, timeframe: str) -> Path | None:
    """The XM history file the brain uses for this logical symbol (plain name first)."""
    patterns = [str(value) for value in aliases.get(logical, [logical])] + [logical]
    for pattern in patterns:
        exact = history / f'{pattern}_{timeframe}.parquet'
        if exact.is_file(): return exact
    for pattern in patterns:
        matches = sorted(history.glob(f'{pattern}*_{timeframe}.parquet'))
        if matches: return matches[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--years', type=float, default=None, help='Years back (default: everything Dukascopy has)')
    parser.add_argument('--symbols', nargs='*', help='Logical symbols (default: focus symbols with a Dukascopy instrument)')
    parser.add_argument('--workers', type=int, default=1, help='Parallel downloads (the server refuses fast clients)')
    parser.add_argument('--delay', type=float, default=1.5, help='Pause in seconds after every request (be gentle: Dukascopy answers 503 when hurried)')
    parser.add_argument('--attempts', type=int, default=8, help='Tries per day before it is skipped (refilled on the next run)')
    parser.add_argument('--max-pause', type=float, default=120.0, help='Longest wait between tries of one day')
    args = parser.parse_args()
    settings = load_settings(require_credentials=False)
    universe = settings.config['universe']; aliases = universe.get('symbol_aliases', {})
    history = settings.root / settings.config['history']['data_dir']
    research = settings.root / str(settings.config['history'].get('research_dir') or 'data/history_long')
    research = research if research.is_absolute() else settings.root / research
    cache = settings.data_dir / 'history_external' / 'dukascopy'
    wanted = args.symbols or [str(s) for s in universe.get('focus_symbols', [])]
    end = date.today() - timedelta(days=1)
    print(f'Long history up to {end} -> {research}')
    research.mkdir(parents=True, exist_ok=True)
    manifest = []
    for logical in wanted:
        instrument = INSTRUMENTS.get(str(logical).upper())
        if not instrument:
            print(f'Skipping {logical}: no Dukascopy instrument'); continue
        start = first_day(instrument, args.years)
        xm_m1_path = xm_file(history, logical, aliases, 'M1')
        if xm_m1_path is None:
            print(f'Skipping {logical}: no XM M1 file in {history} (run download_history.py first)'); continue
        name = xm_m1_path.name[:-len('_M1.parquet')]
        xm_m1 = parquet_compat.read_parquet(xm_m1_path)
        print(f'{logical}: downloading {instrument} ...', flush=True)
        raw = download_instrument(instrument, start, end, http_fetch, cache, args.workers, progress=lambda line: print('  ' + line, flush=True),
                                  attempts=args.attempts, delay=args.delay, max_pause=args.max_pause)
        if raw.empty:
            print(f'  {instrument}: no Dukascopy data'); continue
        overlap = raw.index.intersection(xm_m1.index)
        point = infer_point(raw.loc[overlap, 'close'] if len(overlap) else raw['close'].tail(0), xm_m1.loc[overlap, 'close'] if len(overlap) else None, instrument)
        external = to_prices(raw, point)
        spread = float(xm_m1['spread'].median()) if 'spread' in xm_m1 and len(xm_m1) else 0.0
        long_m1 = merge_long(xm_m1, external, spread)
        parquet_compat.write_parquet(long_m1, research / f'{name}_M1.parquet')
        for timeframe, rule in (('M5', '5min'), ('M15', '15min')):
            path = xm_file(history, logical, aliases, timeframe)
            xm = parquet_compat.read_parquet(path) if path else None
            parquet_compat.write_parquet(merge_timeframe(xm, long_m1, rule), research / f'{name}_{timeframe}.parquet')
        manifest.append({'symbol': name, 'instrument': instrument, 'point': point, 'm1_bars': len(long_m1),
                         'from': str(long_m1.index.min()), 'xm_from': str(xm_m1.index.min()), 'to': str(long_m1.index.max())})
        years = (long_m1.index.max() - long_m1.index.min()).days / 365.25
        print(f'  {name}: {len(long_m1)} M1 bars from {long_m1.index.min():%Y-%m-%d} ({years:.1f} years; XM from {xm_m1.index.min():%Y-%m-%d}), point {point:g}'
              + ('' if years >= 5 else '  WARNING: less than 5 years'))
    # Symbols without Dukascopy data (indices, crypto, weekend symbols) keep their XM files,
    # so the brain sees the same universe from the research folder.
    built = {f"{entry['symbol']}_{tf}.parquet" for entry in manifest for tf in ('M1', 'M5', 'M15')}
    copied = 0
    for source in sorted(history.glob('*.parquet')):
        target = research / source.name
        if source.name in built: continue
        if not target.exists() or source.stat().st_mtime > target.stat().st_mtime:
            target.write_bytes(source.read_bytes()); copied += 1
    specs = history / 'symbol_specs.json'
    if specs.is_file(): (research / 'symbol_specs.json').write_bytes(specs.read_bytes())
    print(f'Copied {copied} other XM files unchanged.')
    if not manifest:
        print('Nothing built.'); return 1
    write_manifest(research, manifest)
    print(f'Done: {len(manifest)} symbols. Brain/research scripts now read {research}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Build long M1/M5/M15 history (XM + older histdata.com bars) for research and the brain.

Fast: histdata publishes one zip per year, so each symbol takes a few minutes.
Usage (MT5 is not needed):
  python scripts/download_histdata.py                    # GOLD, SILVER, US500, NAS100: every year available
  python scripts/download_histdata.py --years 5          # only the last 5 years
  python scripts/download_histdata.py --symbols GOLD

Raw zips are cached as parquet in data/history_external/histdata/, so a stopped run resumes.
The result goes to history.research_dir (default data/history_long), the same folder the
Dukascopy script writes; the live bot never reads it. US30 is not on histdata.
"""
from __future__ import annotations
from datetime import date, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
import argparse
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import parquet_compat
from src.config import load_settings
from src.external_history import merge_long, merge_timeframe, write_manifest
from src.histdata import PAIRS, download_pair, price_gap, proxy_volume

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
           'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def fetch_page(url: str) -> str:
    with OPENER.open(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
        return response.read().decode('utf-8', 'replace')


def fetch_zip(url: str, form: dict, referer: str) -> bytes:
    body = urllib.parse.urlencode(form).encode()
    headers = dict(HEADERS, **{'Referer': referer, 'Origin': 'https://www.histdata.com',
                               'Content-Type': 'application/x-www-form-urlencoded'})
    with OPENER.open(urllib.request.Request(url, data=body, headers=headers), timeout=180) as response:
        return response.read()


def xm_file(history: Path, logical: str, aliases: dict, timeframe: str) -> Path | None:
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
    parser.add_argument('--years', type=float, default=None, help='Years back (default: everything histdata has)')
    parser.add_argument('--symbols', nargs='*', help='Logical symbols (default: focus symbols histdata has)')
    parser.add_argument('--delay', type=float, default=2.0, help='Pause after each download')
    args = parser.parse_args()
    settings = load_settings(require_credentials=False)
    universe = settings.config['universe']; aliases = universe.get('symbol_aliases', {})
    history = settings.root / settings.config['history']['data_dir']
    research = Path(str(settings.config['history'].get('research_dir') or 'data/history_long'))
    research = research if research.is_absolute() else settings.root / research
    cache = settings.data_dir / 'history_external' / 'histdata'
    wanted = args.symbols or [str(s) for s in universe.get('focus_symbols', []) if str(s).upper() in PAIRS]
    since = date.today() - timedelta(days=int(args.years * 365.25)) if args.years else None
    print(f'Long history (histdata) -> {research}', flush=True)
    research.mkdir(parents=True, exist_ok=True)
    manifest = []
    for logical in wanted:
        pair = PAIRS.get(str(logical).upper())
        if not pair:
            print(f'Skipping {logical}: not on histdata'); continue
        xm_m1_path = xm_file(history, logical, aliases, 'M1')
        if xm_m1_path is None:
            print(f'Skipping {logical}: no XM M1 file in {history} (run download_history.py first)'); continue
        name = xm_m1_path.name[:-len('_M1.parquet')]
        xm_m1 = parquet_compat.read_parquet(xm_m1_path)
        print(f'{logical}: downloading {pair} ...', flush=True)
        raw = download_pair(pair, cache, fetch_page, fetch_zip, since=since, delay=args.delay,
                            progress=lambda line: print('  ' + line, flush=True))
        if raw.empty:
            print(f'  {pair}: no histdata data'); continue
        gap = price_gap(raw, xm_m1)
        if gap is not None and gap > 0.03:
            print(f'  {pair}: prices differ from XM by {gap:.1%} on overlapping minutes - skipped (wrong instrument?)'); continue
        external = proxy_volume(raw, xm_m1)
        spread = float(xm_m1['spread'].median()) if 'spread' in xm_m1 and len(xm_m1) else 0.0
        long_m1 = merge_long(xm_m1, external, spread)
        parquet_compat.write_parquet(long_m1, research / f'{name}_M1.parquet')
        for timeframe, rule in (('M5', '5min'), ('M15', '15min')):
            path = xm_file(history, logical, aliases, timeframe)
            xm = parquet_compat.read_parquet(path) if path else None
            parquet_compat.write_parquet(merge_timeframe(xm, long_m1, rule), research / f'{name}_{timeframe}.parquet')
        years = (long_m1.index.max() - long_m1.index.min()).days / 365.25
        manifest.append({'symbol': name, 'instrument': pair, 'source': 'histdata', 'volume': 'proxy from bar range',
                         'price_gap_vs_xm': gap, 'm1_bars': len(long_m1), 'from': str(long_m1.index.min()),
                         'xm_from': str(xm_m1.index.min()), 'to': str(long_m1.index.max())})
        print(f'  {name}: {len(long_m1)} M1 bars from {long_m1.index.min():%Y-%m-%d} ({years:.1f} years; XM from {xm_m1.index.min():%Y-%m-%d})'
              + ('' if gap is None else f', price gap vs XM {gap:.2%}') + ('' if years >= 5 else '  WARNING: less than 5 years'), flush=True)
    # Every other XM file (US30 etc.) is copied unchanged so the brain sees the same universe.
    built = {f"{entry['symbol']}_{tf}.parquet" for entry in manifest for tf in ('M1', 'M5', 'M15')}
    copied = 0
    for source in sorted(history.glob('*.parquet')):
        target = research / source.name
        if source.name in built: continue
        if target.exists() and source.stat().st_mtime <= target.stat().st_mtime: continue
        if target.exists() and _is_long(research, source.name): continue      # keep a longer Dukascopy build
        target.write_bytes(source.read_bytes()); copied += 1
    specs = history / 'symbol_specs.json'
    if specs.is_file(): (research / 'symbol_specs.json').write_bytes(specs.read_bytes())
    print(f'Copied {copied} other XM files unchanged.')
    if not manifest:
        print('Nothing built.'); return 1
    write_manifest(research, manifest)
    print(f'Done: {len(manifest)} symbols. Brain/research scripts now read {research}.')
    return 0


def _is_long(research: Path, filename: str) -> bool:
    """True when the research folder already holds a long build for this symbol (from the manifest)."""
    import json
    path = research / 'manifest.json'
    if not path.is_file(): return False
    try: symbols = json.loads(path.read_text(encoding='utf-8')).get('symbols', [])
    except (ValueError, OSError): return False
    return any(filename.startswith(f"{entry.get('symbol')}_") for entry in symbols)


if __name__ == '__main__':
    raise SystemExit(main())

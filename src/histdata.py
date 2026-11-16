"""Long M1 history from histdata.com (one zip per year, so a whole symbol takes minutes).

histdata.com publishes free 1-minute bars as yearly zips (monthly for the current year).
Timestamps are New York standard time with no daylight saving (always UTC-5).
The files have no volume, so a stand-in tick volume is built from each bar's range
and scaled to the XM tick volume (busy minutes move more), see proxy_volume().
"""
from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
import io
import re
import zipfile

import pandas as pd

from . import parquet_compat

# Logical bot symbol -> histdata pair. US30 (Dow) is not published by histdata.
PAIRS = {'GOLD': 'XAUUSD', 'XAUUSD': 'XAUUSD', 'SILVER': 'XAGUSD', 'XAGUSD': 'XAGUSD',
         'US500': 'SPXUSD', 'NAS100': 'NSXUSD'}
PAGE = 'https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{period}'
POST = 'https://www.histdata.com/get.php'
UTC_OFFSET = timedelta(hours=5)          # EST without DST -> UTC
EMPTY_COLUMNS = ['open', 'high', 'low', 'close', 'tick_volume']


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=EMPTY_COLUMNS, index=pd.DatetimeIndex([], tz='UTC', name='time'))


def page_url(pair: str, year: int, month: int | None = None) -> str:
    period = f'{year}' if month is None else f'{year}/{month}'
    return PAGE.format(pair=pair.lower(), period=period)


def form_for(pair: str, year: int, month: int | None, html: str) -> dict | None:
    """The download form fields; None when the page has no token (no data for that period)."""
    match = re.search(r'<input[^>]*id=["\']tk["\'][^>]*>', html, flags=re.I)
    token = re.search(r'value=["\']([^"\']+)["\']', match.group(0)) if match else None
    if not token: return None
    return {'tk': token.group(1), 'date': str(year),
            'datemonth': f'{year}{month:02d}' if month else str(year),
            'platform': 'ASCII', 'timeframe': 'M1', 'fxpair': pair.upper()}


def parse_zip(payload: bytes) -> pd.DataFrame:
    """histdata ASCII M1 zip -> UTC-indexed OHLC (tick_volume 0, filled later)."""
    if not payload or payload[:2] != b'PK': return _empty()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if not names: return _empty()
        text = archive.read(names[0])
    frame = pd.read_csv(io.BytesIO(text), sep=';', header=None, names=['stamp', 'open', 'high', 'low', 'close', 'volume'],
                        dtype={'stamp': str})
    if frame.empty: return _empty()
    stamps = pd.to_datetime(frame['stamp'], format='%Y%m%d %H%M%S') + UTC_OFFSET
    out = frame[['open', 'high', 'low', 'close']].astype('float64')
    out['tick_volume'] = 0
    out.index = pd.DatetimeIndex(stamps.dt.tz_localize('UTC'), name='time')
    out = out[(out['low'] <= out[['open', 'close']].min(axis=1)) & (out[['open', 'close']].max(axis=1) <= out['high'])]
    return out[~out.index.duplicated(keep='last')].sort_index()


def periods(today: date) -> list[tuple[int, int | None]]:
    """Newest first: finished months of this year, then whole past years back to 2000."""
    current = [(today.year, month) for month in range(today.month - 1, 0, -1)]
    return current + [(year, None) for year in range(today.year - 1, 1999, -1)]


def download_pair(pair: str, cache_dir: Path, fetch_page: Callable[[str], str], fetch_zip: Callable[[str, dict, str], bytes],
                  today: date | None = None, since: date | None = None, progress: Callable[[str], None] | None = None,
                  sleep: Callable[[float], None] | None = None, delay: float = 2.0, attempts: int = 4) -> pd.DataFrame:
    """Download every period newest -> oldest until two empty years in a row (the data has ended).

    Each finished period is cached as parquet, so a stopped run resumes without downloading again."""
    import time
    sleep = sleep or time.sleep
    today = today or date.today()
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames, empty_years = [], 0
    for year, month in periods(today):
        if since and (date(year, 12, 31) if month is None else date(year, month, 28)) < since: break
        label = f'{year}' if month is None else f'{year}-{month:02d}'
        chunk = cache_dir / f'{pair}_{label}.parquet'
        none_marker = cache_dir / f'{pair}_{label}.none'
        if chunk.is_file():
            frame = parquet_compat.read_parquet(chunk)
        elif none_marker.is_file():
            frame = _empty()
        else:
            frame = None
            for attempt in range(max(1, attempts)):
                try:
                    referer = page_url(pair, year, month)
                    form = form_for(pair, year, month, fetch_page(referer))
                    frame = parse_zip(fetch_zip(POST, form, referer)) if form else _empty()
                    break
                except (OSError, TimeoutError, ValueError, zipfile.BadZipFile) as exc:
                    if attempt < attempts - 1:
                        pause = min(60.0, 5.0 * 2 ** attempt)
                        if progress: progress(f'{pair} {label}: {exc} - waiting {pause:.0f} s')
                        sleep(pause)
            if delay > 0: sleep(float(delay))
            if frame is None:
                if progress: progress(f'{pair} {label}: failed (run again to retry)')
                continue
            if not frame.empty: parquet_compat.write_parquet(frame, chunk)
            elif month is None: none_marker.write_text('')     # a past year with no data stays empty

        if progress: progress(f'{pair} {label}: {len(frame)} bars' if len(frame) else f'{pair} {label}: no data')
        if frame.empty:
            if month is None:
                empty_years += 1
                if empty_years >= 2: break
            continue
        empty_years = 0
        frames.append(frame)
    if not frames: return _empty()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep='last')]
    return out.loc[out.index >= pd.Timestamp(since, tz='UTC')] if since else out


def price_gap(external: pd.DataFrame, xm: pd.DataFrame | None) -> float | None:
    """Median relative difference of closes on overlapping minutes (None without overlap)."""
    if xm is None or xm.empty or external.empty: return None
    overlap = external.index.intersection(xm.index)
    if len(overlap) < 100: return None
    return float(((external.loc[overlap, 'close'] / xm.loc[overlap, 'close']) - 1).abs().median())


def proxy_volume(external: pd.DataFrame, xm: pd.DataFrame | None) -> pd.DataFrame:
    """Stand-in tick volume = bar range scaled so its median matches XM's median tick volume.

    Volume filters (breakouts, VWAP, relative volume) then behave like they do on XM data:
    quiet minutes get small numbers, busy minutes large ones."""
    out = external.copy()
    span = (out['high'] - out['low']).clip(lower=0)
    positive = span[span > 0]
    if positive.empty:
        out['tick_volume'] = 1; return out
    target = 1.0
    if xm is not None and not xm.empty and 'tick_volume' in xm and float(xm['tick_volume'].median()) > 0:
        target = float(xm['tick_volume'].median())
        xm_span = (xm['high'] - xm['low'])
        ratio = float(xm['tick_volume'].median()) / float(xm_span[xm_span > 0].median()) if (xm_span > 0).any() else None
        if ratio and ratio > 0:
            out['tick_volume'] = (span * ratio).round().clip(lower=1).astype('int64'); return out
    out['tick_volume'] = (span / float(positive.median()) * target).round().clip(lower=1).astype('int64')
    return out

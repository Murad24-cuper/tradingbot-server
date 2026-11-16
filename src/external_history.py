"""Long M1 history from Dukascopy for research and brain training.

XM serves only a few months of M1 bars. Dukascopy publishes free M1 candles for
many years. These helpers download them, convert to the bot's bar format and
merge them with the XM bars into a separate long-history folder:

  data/history_external/dukascopy/<INSTRUMENT>_M1.parquet   raw Dukascopy bars
  data/history_long/<XM_SYMBOL>_{M1,M5,M15}.parquet          XM + older Dukascopy

XM bars always win where both sources overlap. The live bot never reads these
folders; only research and brain scripts do (see ``research_history_dir``).
Prices come from another venue, so costs/spreads always stay XM's own.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
import json
import lzma
import math
import struct

import pandas as pd

from . import parquet_compat

URL='https://datafeed.dukascopy.com/datafeed/{instrument}/{year:04d}/{month0:02d}/{day:02d}/BID_candles_min_1.bi5'
RECORD=struct.Struct('>5if')          # seconds from day start, open, close, low, high (int points), volume
# Logical bot symbol -> Dukascopy instrument
INSTRUMENTS={'EURUSD':'EURUSD','GBPUSD':'GBPUSD','USDJPY':'USDJPY','AUDUSD':'AUDUSD','USDCAD':'USDCAD',
             'USDCHF':'USDCHF','NZDUSD':'NZDUSD','EURJPY':'EURJPY','GBPJPY':'GBPJPY','EURGBP':'EURGBP',
             'GOLD':'XAUUSD','XAUUSD':'XAUUSD','SILVER':'XAGUSD','XAGUSD':'XAGUSD',
             'US500':'USA500IDXUSD','NAS100':'USATECHIDXUSD','US30':'USA30IDXUSD'}
# First day Dukascopy serves M1 candles (earlier days are empty); used for "as many years as exist".
FIRST_DAY={'USA500IDXUSD':date(2011,9,19),'USATECHIDXUSD':date(2011,9,19),'USA30IDXUSD':date(2013,9,30),
           'XAUUSD':date(2008,1,1),'XAGUSD':date(2008,1,1)}
DEFAULT_FIRST_DAY=date(2005,1,1)
COLUMNS=['open','high','low','close','tick_volume','spread']


def day_url(instrument: str, day: date) -> str:
    """Dukascopy URLs use a zero-based month."""
    return URL.format(instrument=instrument,year=day.year,month0=day.month-1,day=day.day)


def decode_candles(payload: bytes, day: date) -> pd.DataFrame:
    """Decode one LZMA-compressed daily M1 file into raw integer-point OHLC rows.

    Empty payloads (weekends/holidays) give an empty frame. Rows with no volume are
    flat filler minutes while the market is closed and are dropped."""
    if not payload: return pd.DataFrame(columns=['open','high','low','close','tick_volume'],index=pd.DatetimeIndex([],tz='UTC',name='time'))
    raw=lzma.decompress(payload)
    if len(raw)%RECORD.size: raise ValueError(f'Corrupt Dukascopy candle file for {day}: {len(raw)} bytes')
    start=datetime(day.year,day.month,day.day,tzinfo=timezone.utc)
    rows=[]; total=0; broken=0
    for seconds,o,c,low,high,volume in RECORD.iter_unpack(raw):
        if volume<=0: continue
        total+=1
        if not low<=min(o,c)<=max(o,c)<=high: broken+=1; continue
        rows.append((start+timedelta(seconds=int(seconds)),o,high,low,c,float(volume)))
    # A few odd candles are dropped; many mean the field layout assumption is wrong.
    if total and broken/total>.01: raise ValueError(f'Unexpected Dukascopy candle layout on {day}: {broken}/{total} candles inconsistent')
    frame=pd.DataFrame(rows,columns=['time','open','high','low','close','tick_volume'])
    return frame.set_index(pd.DatetimeIndex(frame.pop('time'),name='time'))


def infer_point(raw_close: pd.Series, reference_close: pd.Series|None, instrument: str) -> float:
    """Price of one Dukascopy integer point.

    With overlapping XM bars the power of ten that maps Dukascopy onto XM prices is
    measured; otherwise the usual convention (3 decimals for JPY/metals, 5 for FX)."""
    if reference_close is not None and len(reference_close) and len(raw_close):
        ratio=float(reference_close.median())/float(raw_close.median())
        if ratio>0 and math.isfinite(ratio):
            point=10.0**round(math.log10(ratio))
            if abs(float(raw_close.median())*point/float(reference_close.median())-1)<.05: return point
    return 1e-3 if instrument.endswith('JPY') or instrument.startswith(('XAU','XAG')) or 'IDX' in instrument else 1e-5


def to_prices(raw: pd.DataFrame, point: float) -> pd.DataFrame:
    frame=raw.copy()
    for column in ('open','high','low','close'): frame[column]=frame[column].astype('float64')*point
    return frame


def download_instrument(instrument: str, start: date, end: date, fetch: Callable[[str], bytes],
                        cache_dir: Path, workers: int=2, progress: Callable[[str],None]|None=None,
                        attempts: int=6, sleep: Callable[[float],None]|None=None,
                        delay: float=0.0, max_pause: float=60.0) -> pd.DataFrame:
    """Download raw daily files (resumable, cached as monthly parquet chunks).

    The server answers 503/429 when asked too fast: every day is retried with a growing pause
    (2, 4, 8 ... up to max_pause s) and every request is followed by `delay` seconds, so a
    rate-limited server is not hammered. A day that still fails is skipped; the month is cached
    with a <month>.missing.json list, so the next run fetches only the missing days."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    sleep=sleep or time.sleep
    cache_dir.mkdir(parents=True,exist_ok=True)
    frames=[]
    month=date(start.year,start.month,1)
    empty=lambda: pd.DataFrame(columns=['open','high','low','close','tick_volume'],index=pd.DatetimeIndex([],tz='UTC',name='time'))
    while month<=end:
        next_month=(month.replace(day=28)+timedelta(days=4)).replace(day=1)
        chunk=cache_dir/f'{instrument}_{month:%Y-%m}.parquet'
        gaps=cache_dir/f'{instrument}_{month:%Y-%m}.missing.json'
        complete=next_month<=date.today()-timedelta(days=1)
        month_days=[month+timedelta(days=i) for i in range((next_month-month).days)]
        month_days=[day for day in month_days if day.weekday()!=5]          # Saturdays never trade
        if chunk.is_file():
            base=parquet_compat.read_parquet(chunk)
            known_missing=[date.fromisoformat(x) for x in json.loads(gaps.read_text())] if gaps.is_file() else []
            days=[day for day in known_missing if start<=day<=end]
            if not days:
                frames.append(base); month=next_month; continue
        else:
            base=empty(); known_missing=[]
            days=[day for day in month_days if start<=day<=end]
        failed=[]
        def one(day):
            for attempt in range(max(1,int(attempts))):
                try:
                    payload=fetch(day_url(instrument,day))
                    if delay>0: sleep(float(delay))
                    return decode_candles(payload,day)
                except (OSError,TimeoutError) as exc:
                    if attempt<attempts-1:
                        pause=min(float(max_pause),2.0**(attempt+1))
                        if progress and attempt>=1: progress(f'{instrument} {day:%Y-%m-%d}: {exc} - waiting {pause:.0f} s (try {attempt+2}/{attempts})')
                        sleep(pause)
            failed.append(day); return None
        with ThreadPoolExecutor(max_workers=max(1,int(workers))) as pool:
            parts=[part for part in pool.map(one,days) if part is not None and not part.empty]
        pieces=[frame for frame in [base,*parts] if not frame.empty]
        frame=pd.concat(pieces).sort_index() if pieces else empty()
        frame=frame[~frame.index.duplicated(keep='last')]
        if complete:
            # Keep what we got; remember which days are still missing (failed, or before this run's start)
            # so the next run fetches only those instead of the whole month again.
            fetched=set(days)-set(failed)
            missing=sorted((set(known_missing)|{day for day in month_days if day<start}|set(failed))-fetched)
            parquet_compat.write_parquet(frame,chunk)
            if missing: gaps.write_text(json.dumps([day.isoformat() for day in missing]))
            elif gaps.exists(): gaps.unlink()
        frames.append(frame)
        if progress:
            refill=' (refilled missing days)' if known_missing else ''
            progress(f'{instrument} {month:%Y-%m}: {len(frame)} bars{refill}'+(f' · {len(failed)} days failed (run again to fill only those)' if failed else ''))
        month=next_month
    frames=[frame for frame in frames if not frame.empty]
    if not frames: return pd.DataFrame(columns=['open','high','low','close','tick_volume'],index=pd.DatetimeIndex([],tz='UTC',name='time'))
    out=pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep='last')]


def merge_long(xm: pd.DataFrame|None, external: pd.DataFrame, typical_spread: float) -> pd.DataFrame:
    """Older external bars + every XM bar. XM wins on any overlapping timestamp,
    and external bars are only used before the first XM bar."""
    ext=external.copy()
    if xm is not None and not xm.empty:
        ext=ext.loc[ext.index<xm.index.min()]
    ext['spread']=float(typical_spread)
    ext['tick_volume']=ext['tick_volume'].round().astype('int64')
    parts=[ext[COLUMNS]]
    if xm is not None and not xm.empty: parts.append(xm[[c for c in COLUMNS if c in xm.columns]])
    merged=pd.concat(parts).sort_index()
    merged.index=pd.DatetimeIndex(merged.index,name='time')
    return merged[~merged.index.duplicated(keep='last')]


def resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """M1 -> M5/M15 with the usual OHLC rules (bar labelled by its open time)."""
    agg={'open':'first','high':'max','low':'min','close':'last','tick_volume':'sum','spread':'median'}
    out=frame.resample(rule,label='left',closed='left').agg(agg).dropna(subset=['open'])
    out['tick_volume']=out['tick_volume'].astype('int64')
    return out


def merge_timeframe(xm: pd.DataFrame|None, long_m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Higher timeframe: XM bars where they exist, resampled long M1 before that."""
    built=resample(long_m1,rule)
    if xm is None or xm.empty: return built
    built=built.loc[built.index<xm.index.min()]
    merged=pd.concat([built,xm[[c for c in COLUMNS if c in xm.columns]]]).sort_index()
    return merged[~merged.index.duplicated(keep='last')]


def first_day(instrument: str, years: float|None, today: date|None=None) -> date:
    """Start of the download: the requested number of years, or everything Dukascopy has (years=None)."""
    today=today or date.today(); earliest=FIRST_DAY.get(instrument,DEFAULT_FIRST_DAY)
    if years is None: return earliest
    return max(earliest,today-timedelta(days=int(float(years)*365.25)))


def research_history_dir(settings) -> Path:
    """Folder research/brain scripts should read: the long history when it was built."""
    configured=str(settings.config.get('history',{}).get('research_dir','') or 'data/history_long')
    base=settings.root/settings.config['history']['data_dir']
    long_dir=Path(configured) if Path(configured).is_absolute() else settings.root/configured
    return long_dir if (long_dir/'manifest.json').is_file() else base


def write_manifest(folder: Path, entries: Iterable[dict]) -> None:
    """Write/merge the research-folder manifest: entries replace earlier ones for the same symbol,
    so histdata and Dukascopy runs can each add their symbols."""
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'manifest.json'
    previous=[]
    if path.is_file():
        try: previous=json.loads(path.read_text(encoding='utf-8')).get('symbols',[])
        except (ValueError,OSError): previous=[]
    entries=list(entries); names={entry['symbol'] for entry in entries}
    merged=[entry for entry in previous if entry.get('symbol') not in names]+entries
    path.write_text(json.dumps({'built_at_utc':datetime.now(timezone.utc).isoformat(),'source':'XM + older M1 bars (Dukascopy / histdata)',
                                'symbols':merged},indent=2),encoding='utf-8')

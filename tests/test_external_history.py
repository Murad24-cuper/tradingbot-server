import json
import lzma
import struct
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src import external_history as eh


def _bi5(rows):
    return lzma.compress(b''.join(struct.pack('>5if', *row) for row in rows), format=lzma.FORMAT_ALONE)


def test_day_url_uses_zero_based_month():
    assert eh.day_url('EURUSD', date(2024, 1, 15)).endswith('/EURUSD/2024/00/15/BID_candles_min_1.bi5')


def test_decode_reads_open_close_low_high_and_drops_closed_minutes():
    payload = _bi5([(0, 110000, 110020, 109990, 110030, 5.0), (60, 110020, 110010, 110005, 110025, 0.0), (120, 110010, 110040, 110000, 110050, 2.5)])
    frame = eh.decode_candles(payload, date(2024, 1, 15))
    assert list(frame.index) == [pd.Timestamp('2024-01-15 00:00', tz='UTC'), pd.Timestamp('2024-01-15 00:02', tz='UTC')]
    first = frame.iloc[0]
    assert (first.open, first.high, first.low, first.close) == (110000, 110030, 109990, 110020)
    assert eh.decode_candles(b'', date(2024, 1, 13)).empty


def test_decode_rejects_wrong_layout():
    # Fields in O,H,L,C order would make "low" above "close" on most rows.
    rows = [(i * 60, 100, 130, 90, 120, 1.0) for i in range(50)]
    with pytest.raises(ValueError, match='layout'):
        eh.decode_candles(_bi5(rows), date(2024, 1, 15))


def test_point_is_measured_against_xm_prices():
    raw = pd.Series([110000.0, 110020.0]); xm = pd.Series([1.1, 1.1002])
    assert eh.infer_point(raw, xm, 'EURUSD') == pytest.approx(1e-5)
    assert eh.infer_point(pd.Series([150123.0]), pd.Series([150.12]), 'USDJPY') == pytest.approx(1e-3)
    assert eh.infer_point(pd.Series([1.0]), None, 'XAUUSD') == 1e-3
    assert eh.infer_point(pd.Series([1.0]), None, 'GBPUSD') == 1e-5


def _bars(start, minutes, price, spread=None):
    index = pd.date_range(start, periods=minutes, freq='min', tz='UTC', name='time')
    data = {'open': price, 'high': price + .001, 'low': price - .001, 'close': price, 'tick_volume': 10}
    if spread is not None: data['spread'] = spread
    return pd.DataFrame(data, index=index)


def test_merge_keeps_every_xm_bar_and_only_older_external_bars():
    xm = _bars('2024-01-01 10:00', 60, 2.0, spread=12)
    ext = _bars('2024-01-01 09:00', 120, 1.0).drop(columns=[])
    merged = eh.merge_long(xm, ext, typical_spread=12)
    assert merged.index.is_monotonic_increasing and not merged.index.duplicated().any()
    assert (merged.loc[merged.index >= xm.index.min(), 'close'] == 2.0).all()      # XM wins
    assert (merged.loc[merged.index < xm.index.min(), 'close'] == 1.0).all()
    assert (merged['spread'] == 12).all() and len(merged) == 120


def test_higher_timeframes_are_resampled_before_xm_history():
    long_m1 = eh.merge_long(_bars('2024-01-01 10:00', 30, 2.0, spread=5), _bars('2024-01-01 09:00', 90, 1.0), 5)
    xm_m5 = eh.resample(_bars('2024-01-01 10:00', 30, 2.0, spread=5), '5min')
    m5 = eh.merge_timeframe(xm_m5, long_m1, '5min')
    assert len(m5) == 18 and (m5.loc[m5.index < pd.Timestamp('2024-01-01 10:00', tz='UTC'), 'close'] == 1.0).all()
    assert m5.iloc[0]['tick_volume'] == 50


def test_download_is_resumable_and_skips_saturdays(tmp_path):
    calls = []
    def fetch(url):
        calls.append(url)
        day = int(url.split('/')[-2])
        return _bi5([(600, 110000 + day, 110010 + day, 109990 + day, 110020 + day, 1.0)])
    frame = eh.download_instrument('EURUSD', date(2024, 1, 1), date(2024, 1, 31), fetch, tmp_path)
    assert len(frame) == 27 and len(calls) == 27            # 31 days minus 4 Saturdays
    assert (tmp_path / 'EURUSD_2024-01.parquet').is_file()
    calls.clear()
    again = eh.download_instrument('EURUSD', date(2024, 1, 1), date(2024, 1, 31), fetch, tmp_path)
    assert calls == [] and len(again) == 27                   # served from the monthly cache


def test_research_dir_used_only_once_built(tmp_path):
    settings = SimpleNamespace(root=tmp_path, config={'history': {'data_dir': 'data/history', 'research_dir': 'data/history_long'}})
    assert eh.research_history_dir(settings) == tmp_path / 'data/history'
    eh.write_manifest(tmp_path / 'data/history_long', [{'symbol': 'EURUSD'}])
    assert eh.research_history_dir(settings) == tmp_path / 'data/history_long'
    assert json.loads((tmp_path / 'data/history_long/manifest.json').read_text())['symbols'][0]['symbol'] == 'EURUSD'


def test_live_bot_never_reads_the_research_folder():
    root = Path(__file__).resolve().parents[1]
    for name in ('bot.py', 'scanner.py', 'executor.py', 'history.py', 'market_data.py'):
        assert 'research_dir' not in (root / 'src' / name).read_text(encoding='utf-8')


def test_us_indices_map_to_dukascopy_and_max_years_uses_first_day():
    from datetime import date
    from src.external_history import INSTRUMENTS, first_day
    assert (INSTRUMENTS['US500'], INSTRUMENTS['NAS100'], INSTRUMENTS['US30']) == ('USA500IDXUSD', 'USATECHIDXUSD', 'USA30IDXUSD')
    assert first_day('USA30IDXUSD', None) == date(2013, 9, 30)
    assert first_day('USA500IDXUSD', 5, today=date(2026, 11, 13)) == date(2021, 11, 13)
    assert first_day('USA30IDXUSD', 30, today=date(2026, 11, 13)) == date(2013, 9, 30)   # never before the data exists

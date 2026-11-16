"""Loss autopsy: find the conditions under which a strategy keeps losing, prove it, then filter.

Every closed trade is tagged with the conditions it was opened under (session, weak trend,
direction disagreement across M1/M5/M15, wide spread, news nearby, low confidence). For each
strategy x symbol x tag the live trades with the tag are compared with those without it.
A tag becomes a *weak spot* when, over enough trades, trades with it lose clearly more.

A weak spot is turned into a filter (the bot then skips that strategy under that condition)
only when the long-history strategy test agrees, for the tags the test can see (session and
the direction check); other weak spots are shown in the panel as suggestions. The user can
remove any learned filter there.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json

from .strategy_guard import trade_r

TABLE = '''CREATE TABLE IF NOT EXISTS learned_filters(
 id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, symbol TEXT NOT NULL, tag TEXT NOT NULL, status TEXT NOT NULL,
 live_with_r REAL, live_without_r REAL, trades_with INTEGER, history_r REAL, evidence TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, UNIQUE(strategy, symbol, tag))'''
SESSIONS = ((0, 7, 'Asiya'), (7, 12, 'London'), (12, 16, 'London+NY'), (16, 21, 'New York'), (21, 24, 'Gecə'))
LABELS = {'mtf_zeif': 'M1/M5/M15 istiqaməti uyğun deyil', 'trend_zeif': 'trend zəifdir (ADX < 18)',
          'spred_yuksek': 'spred stopun 6%-dən çoxu', 'xeber_yaxin': 'yüksək təsirli xəbərə ≤30 dəq',
          'guven_asagi': 'güvən minimumdan aşağı'}
DEFAULTS = {'enabled': True, 'min_trades_with': 20, 'min_gap_r': 0.2, 'lookback_trades': 400}


def label(tag: str) -> str:
    if tag.startswith('session:'): return f"{tag.split(':', 1)[1]} sessiyası"
    return LABELS.get(tag, tag)


def session_name(hour: int) -> str:
    return next((name for lo, hi, name in SESSIONS if lo <= hour < hi), 'Gecə')


def tags_of(features: dict, opened_at: str | datetime | None) -> set[str]:
    tags = set()
    try:
        stamp = opened_at if isinstance(opened_at, datetime) else datetime.fromisoformat(str(opened_at).replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        tags.add('session:' + session_name(stamp.hour))
    except (TypeError, ValueError):
        pass
    soft = ' '.join(str(item) for item in ((features.get('exploration') or {}).get('soft_failures') or [])).lower()
    if 'mtf' in soft: tags.add('mtf_zeif')
    if 'confidence' in soft: tags.add('guven_asagi')
    adx = (features.get('M15') or {}).get('adx')
    try:
        if adx is not None and float(adx) < 18: tags.add('trend_zeif')
    except (TypeError, ValueError): pass
    ratio = (features.get('cost_map') or {}).get('median_cost_stop_ratio')
    try:
        if ratio is not None and float(ratio) >= 0.06: tags.add('spred_yuksek')
    except (TypeError, ValueError): pass
    minutes = (features.get('news_context') or (features.get('observations') or {}).get('news_context') or {}).get('minutes_to_next_event')
    try:
        if minutes is not None and float(minutes) <= 30: tags.add('xeber_yaxin')
    except (TypeError, ValueError): pass
    return tags


def _history_check(summary: dict, strategy: str, symbol: str, tag: str) -> float | None:
    """Expectancy R the long-history test gives for this condition (None when the test cannot see it)."""
    if not summary: return None
    if tag.startswith('session:'):
        wanted = tag.split(':', 1)[1]
        for row in summary.get('combinations', []):
            if (row.get('strategy'), row.get('symbol'), row.get('variant'), row.get('mtf_filter')) == (strategy, symbol, 'b', False) \
                    and str(row.get('session', '')).startswith(wanted + ' '):
                parts = [row.get('validation') or {}, row.get('test') or {}]
                trades = sum(int(p.get('trades') or 0) for p in parts)
                if not trades: return None
                return sum((p.get('expectancy_r') or 0) * int(p.get('trades') or 0) for p in parts) / trades
    if tag == 'mtf_zeif':
        rows = {bool(r.get('mtf_filter')): r for r in summary.get('overall', [])
                if (r.get('strategy'), r.get('symbol'), r.get('variant')) == (strategy, symbol, 'b')}
        if True in rows and False in rows:
            def exp(r):
                parts = [r.get('yoxlama') or {}, r.get('imtahan') or {}]; n = sum(int(p.get('trades') or 0) for p in parts)
                return sum((p.get('expectancy_r') or 0) * int(p.get('trades') or 0) for p in parts) / n if n else None
            all_r, agreed_r = exp(rows[False]), exp(rows[True])
            if all_r is not None and agreed_r is not None: return all_r - agreed_r     # < 0: disagreeing trades are worse
    return None


def analyse(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    """Weak spots from closed trades: [{strategy, symbol, tag, with_r, without_r, trades_with}]."""
    cfg = {**DEFAULTS, **(cfg or {})}
    tagged: dict = {}
    for row in rows[-int(cfg['lookback_trades']):]:
        r = trade_r(row)
        if r is None: continue
        strategy = str(row.get('strategy') or '')
        if not strategy or strategy.startswith('manual'): continue
        try: features = json.loads(row.get('features') or '{}')
        except (TypeError, ValueError): features = {}
        tagged.setdefault((strategy, str(row.get('symbol'))), []).append((tags_of(features, row.get('open_time')), r))
    spots = []
    for (strategy, symbol), items in tagged.items():
        for tag in sorted({t for tags, _ in items for t in tags}):
            with_tag = [r for tags, r in items if tag in tags]; without = [r for tags, r in items if tag not in tags]
            if len(with_tag) < int(cfg['min_trades_with']) or len(without) < 10: continue
            with_r = sum(with_tag) / len(with_tag); without_r = sum(without) / len(without)
            if with_r < 0 and with_r <= without_r - float(cfg['min_gap_r']):
                spots.append({'strategy': strategy, 'symbol': symbol, 'tag': tag, 'with_r': with_r, 'without_r': without_r, 'trades_with': len(with_tag)})
    return spots


class LossAutopsy:
    def __init__(self, db, config: dict, data_dir: str | Path, magic: int | None = None):
        self.db = db; self.config = config; self.data_dir = Path(data_dir); self.magic = magic
        with self.db.connect() as connection: connection.execute(TABLE)
        self._active: set = set(); self._load()

    def _load(self):
        with self.db.connect() as connection:
            self._active = {(r['strategy'], r['symbol'], r['tag']) for r in connection.execute("SELECT strategy,symbol,tag FROM learned_filters WHERE status='active'")}

    def refresh(self) -> list[dict]:
        """Re-run the autopsy; returns newly activated filters (for events/Telegram)."""
        cfg = {**DEFAULTS, **(self.config.get('loss_autopsy') or {})}
        if not cfg.get('enabled', True): return []
        sql = "SELECT ticket,symbol,strategy,open_time,close_time,profit,commission,swap,features FROM trades WHERE status='closed'"
        params = []
        if self.magic is not None: sql += ' AND magic=?'; params.append(int(self.magic))
        with self.db.connect() as connection: rows = [dict(r) for r in connection.execute(sql + ' ORDER BY close_time', params)]
        summary_path = self.data_dir / 'strategy_test.json'
        try: summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.is_file() else {}
        except (OSError, ValueError): summary = {}
        now = datetime.now(timezone.utc).isoformat(); activated = []
        with self.db.connect() as connection:
            existing = {(r['strategy'], r['symbol'], r['tag']): dict(r) for r in connection.execute('SELECT * FROM learned_filters')}
            for spot in analyse(rows, cfg):
                key = (spot['strategy'], spot['symbol'], spot['tag'])
                if existing.get(key, {}).get('status') == 'removed': continue          # the user said no
                history = _history_check(summary, *key)
                confirmed = history is not None and history < 0
                status = 'active' if confirmed else 'suggested'
                evidence = (f"canlı: bu şərtdə {spot['with_r']:+.2f}R ({spot['trades_with']} ticarət), qalanlarında {spot['without_r']:+.2f}R"
                            + (f"; tarix: {history:+.2f}R" if history is not None else '; tarix bu şərti yoxlaya bilmir'))
                connection.execute('''INSERT INTO learned_filters(strategy,symbol,tag,status,live_with_r,live_without_r,trades_with,history_r,evidence,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy,symbol,tag) DO UPDATE SET status=CASE WHEN learned_filters.status='removed' THEN 'removed' ELSE excluded.status END,
                    live_with_r=excluded.live_with_r,live_without_r=excluded.live_without_r,trades_with=excluded.trades_with,history_r=excluded.history_r,
                    evidence=excluded.evidence,updated_at=excluded.updated_at''',
                    (*key, status, spot['with_r'], spot['without_r'], spot['trades_with'], history, evidence, now, now))
                if status == 'active' and existing.get(key, {}).get('status') != 'active': activated.append({**spot, 'evidence': evidence})
        self._load()
        return activated

    def remove(self, filter_id: int) -> None:
        with self.db.connect() as connection:
            connection.execute("UPDATE learned_filters SET status='removed',updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), int(filter_id)))
        self._load()

    def blocks(self, strategy: str, symbol: str, features: dict, now: datetime) -> str:
        """Reason text when an active learned filter rejects this entry, else ''."""
        if not self._active: return ''
        for tag in tags_of(features or {}, now):
            if (strategy, symbol, tag) in self._active: return f'öyrənilmiş filtr: {label(tag)}'
        return ''

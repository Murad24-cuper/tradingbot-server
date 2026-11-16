"""Strategy guard and learning selector: learns from every closed trade, changes slowly.

For each strategy x symbol the last closed trades (in R after costs) are blended with the
long-history test result (the prior), so one lucky or unlucky trade moves little:

    blended = (sum of live R + PRIOR_WEIGHT * test R) / (live trades + PRIOR_WEIGHT)

* weight (risk multiplier) = clip(1 + 2 * blended, 0.5, max_weight): a pair that keeps
  earning gets up to 25% more risk on DEMO (never more than the configured risk on REAL),
  one that keeps losing gets as little as half;
* paused when there are enough live trades and the blended result is clearly negative, or the
  live drawdown is far beyond what the test saw. A pause ends by itself after `pause_days`
  (then only newer trades count) or when the user presses "fresh start" in the panel.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

TABLE = '''CREATE TABLE IF NOT EXISTS strategy_guard(
 strategy TEXT NOT NULL, symbol TEXT NOT NULL, state TEXT NOT NULL, weight REAL NOT NULL, trades INTEGER NOT NULL,
 live_r REAL, blended_r REAL, prior_r REAL, reason TEXT, paused_until TEXT, reset_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(strategy, symbol))'''
PRIOR_WEIGHT = 20
DEFAULTS = {'enabled': True, 'window': 50, 'min_trades': 20, 'pause_below_r': -0.15, 'pause_drawdown_r': 12.0,
            'pause_days': 7, 'max_weight_demo': 1.25, 'max_weight_real': 1.0, 'min_weight': 0.5}


def settings_of(config: dict) -> dict:
    return {**DEFAULTS, **(config.get('strategy_guard') or {})}


def trade_r(row: dict) -> float | None:
    """R of a closed trade after costs (the executor's own review, else money / sized risk)."""
    try: features = json.loads(row.get('features') or '{}')
    except (TypeError, ValueError): features = {}
    review = features.get('process_review') or {}
    if review.get('outcome_r') is not None:
        try: return float(review['outcome_r'])
        except (TypeError, ValueError): pass
    risk = float((features.get('sizing') or {}).get('stressed_risk_money') or 0)
    net = float(row.get('profit') or 0) + float(row.get('commission') or 0) + float(row.get('swap') or 0)
    return net / risk if risk > 0 else None


def load_priors(data_dir: str | Path) -> dict:
    """(strategy, symbol) -> test expectancy R for the bot's own exits (variant B, all hours, no MTF filter)."""
    path = Path(data_dir) / 'strategy_test.json'
    if not path.is_file(): return {}
    try: summary = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}
    priors = {}
    for row in summary.get('overall', []):
        if row.get('variant') != 'b' or row.get('mtf_filter'): continue
        test = row.get('imtahan') or {}
        if test.get('expectancy_r') is not None: priors[(row['strategy'], row['symbol'])] = float(test['expectancy_r'])
    return priors


def evaluate(results: list[float], prior: float, cfg: dict, account_real: bool) -> dict:
    n = len(results); total = float(sum(results))
    blended = (total + PRIOR_WEIGHT * prior) / (n + PRIOR_WEIGHT)
    cap = float(cfg['max_weight_real'] if account_real else cfg['max_weight_demo'])
    weight = max(float(cfg['min_weight']), min(cap, 1.0 + 2.0 * blended))
    equity = peak = drawdown = 0.0
    for r in results:                                   # oldest -> newest
        equity += r; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    state, reason = 'active', ''
    if n >= int(cfg['min_trades']) and blended <= float(cfg['pause_below_r']):
        state, reason = 'paused', f'son {n} ticarət: orta {total / n:+.2f}R (tarixlə birlikdə {blended:+.2f}R)'
    elif n >= int(cfg['min_trades']) and drawdown >= float(cfg['pause_drawdown_r']):
        state, reason = 'paused', f'geriləmə {drawdown:.1f}R testdəkindən xeyli böyükdür'
    return {'state': state, 'weight': round(weight, 3), 'trades': n, 'live_r': (total / n if n else None),
            'blended_r': blended, 'prior_r': prior, 'reason': reason, 'drawdown_r': drawdown}


class StrategyGuard:
    """Reads closed trades, keeps the guard table up to date and answers the bot's questions."""

    def __init__(self, db, config: dict, data_dir: str | Path, magic: int | None = None, clock=None):
        self.db = db; self.config = config; self.data_dir = Path(data_dir); self.magic = magic
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict = {}; self._loaded_at = None
        with self.db.connect() as connection: connection.execute(TABLE)

    def _rows(self) -> list[dict]:
        sql = "SELECT ticket,symbol,strategy,close_time,profit,commission,swap,features FROM trades WHERE status='closed'"
        params = []
        if self.magic is not None: sql += ' AND magic=?'; params.append(int(self.magic))
        sql += ' ORDER BY close_time'
        with self.db.connect() as connection: return [dict(row) for row in connection.execute(sql, params)]

    def _stored(self) -> dict:
        with self.db.connect() as connection:
            return {(row['strategy'], row['symbol']): dict(row) for row in connection.execute('SELECT * FROM strategy_guard')}

    def refresh(self, account_real: bool = False) -> list[dict]:
        """Recompute every pair; returns the pairs whose state changed (for events/Telegram)."""
        cfg = settings_of(self.config); now = self.clock(); priors = load_priors(self.data_dir)
        stored = self._stored(); groups: dict = {}
        for row in self._rows():
            strategy = str(row.get('strategy') or '')
            if not strategy or strategy.startswith('manual'): continue
            groups.setdefault((strategy, str(row['symbol'])), []).append(row)
        changes = []
        for key in set(groups) | set(stored):
            old = stored.get(key, {}); reset_at = old.get('reset_at')
            rows = [r for r in groups.get(key, []) if not reset_at or str(r.get('close_time') or '') > reset_at]
            results = [r for r in (trade_r(row) for row in rows[-int(cfg['window']):]) if r is not None]
            result = evaluate(results, priors.get(key, 0.0), cfg, account_real)
            paused_until = old.get('paused_until')
            if old.get('state') == 'paused' and paused_until and paused_until > now.isoformat():
                result.update(state='paused', reason=old.get('reason') or result['reason'])
            elif old.get('state') == 'paused' and paused_until and paused_until <= now.isoformat():
                # The pause served its time: start fresh with only newer trades.
                reset_at = paused_until; result = evaluate([], priors.get(key, 0.0), cfg, account_real); paused_until = None
            elif result['state'] == 'paused':
                paused_until = (now + timedelta(days=float(cfg['pause_days']))).isoformat()
            if result['state'] != (old.get('state') or 'active'):
                changes.append({'strategy': key[0], 'symbol': key[1], **result})
            with self.db.connect() as connection:
                connection.execute('''INSERT INTO strategy_guard(strategy,symbol,state,weight,trades,live_r,blended_r,prior_r,reason,paused_until,reset_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy,symbol) DO UPDATE SET state=excluded.state,weight=excluded.weight,
                    trades=excluded.trades,live_r=excluded.live_r,blended_r=excluded.blended_r,prior_r=excluded.prior_r,reason=excluded.reason,
                    paused_until=excluded.paused_until,reset_at=excluded.reset_at,updated_at=excluded.updated_at''',
                    (key[0], key[1], result['state'], result['weight'], result['trades'], result['live_r'], result['blended_r'], result['prior_r'],
                     result['reason'], paused_until, reset_at, now.isoformat()))
        self._cache = self._stored(); self._loaded_at = now
        return changes

    def reset(self, strategy: str, symbol: str) -> None:
        now = self.clock().isoformat()
        with self.db.connect() as connection:
            connection.execute(TABLE)
            connection.execute('''INSERT INTO strategy_guard(strategy,symbol,state,weight,trades,reason,reset_at,updated_at) VALUES (?,?,'active',1.0,0,'',?,?)
                ON CONFLICT(strategy,symbol) DO UPDATE SET state='active',weight=1.0,trades=0,reason='',paused_until=NULL,reset_at=excluded.reset_at,updated_at=excluded.updated_at''',
                (strategy, symbol, now, now))
        self._cache = self._stored()

    def decision(self, strategy: str, symbol: str) -> tuple[bool, float, str]:
        """(allowed, risk multiplier, reason)."""
        if not settings_of(self.config).get('enabled', True): return True, 1.0, ''
        if not self._cache: self._cache = self._stored()
        row = self._cache.get((strategy, symbol))
        if not row: return True, 1.0, ''
        if row.get('state') == 'paused': return False, 0.0, f"strategiya keşikçisi: {row.get('reason') or 'dayandırılıb'}"
        return True, float(row.get('weight') or 1.0), ''

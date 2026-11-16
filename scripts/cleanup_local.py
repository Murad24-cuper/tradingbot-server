"""Tidy the local install: leftover update folders, caches, history of symbols no longer traded, old DB rows.

Default is a dry run that only lists what would be removed:
    .venv\\Scripts\\python.exe scripts\\cleanup_local.py
Remove for real (stop the bot first: stop_bot.bat):
    .venv\\Scripts\\python.exe scripts\\cleanup_local.py --apply

Never touches: .env, config.yaml, trades, brain data, DB backups, history of focus symbols or of open positions.
A full copy of data/bot.db is written to data/backups/ before any database change."""
from __future__ import annotations

import argparse, shutil, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

# Broker names for the same market differ (NAS100 = US100Cash, US30 = DJ30 ...).
ALIASES = {'NAS100': {'US100', 'USTEC', 'NDX'}, 'US500': {'SPX500', 'SP500', 'US500'}, 'US30': {'DJ30', 'WS30', 'DOW'},
           'GOLD': {'XAUUSD', 'GOLD'}, 'SILVER': {'XAGUSD', 'SILVER'}}
JUNK_DIRS = ('update_backup', '.pytest_cache')
JUNK_GLOBS = ('yenilenme*', 'update_backup.zip', '*.patch', 'test_log.txt', '.coverage')


def keep_keys(focus: list[str]) -> set[str]:
    from src.symbols import underlying_key
    keys = set()
    for symbol in focus:
        key = underlying_key(symbol); keys.add(key); keys |= ALIASES.get(key, set())
    return keys


def history_to_remove(history_dir: Path, focus: list[str], recent_symbols: set[str]) -> list[Path]:
    """Parquet files of symbols that are neither in the focus list (any broker spelling) nor traded recently."""
    from src.symbols import underlying_key
    keys = keep_keys(focus) | {underlying_key(s) for s in recent_symbols}
    out = []
    for path in sorted(history_dir.glob('*.parquet')) if history_dir.is_dir() else []:
        symbol = path.stem.rsplit('_', 1)[0]
        if underlying_key(symbol) not in keys: out.append(path)
    return out


def junk_paths(root: Path) -> list[Path]:
    found = [root / name for name in JUNK_DIRS if (root / name).exists()]
    for pattern in JUNK_GLOBS: found += [p for p in root.glob(pattern) if p.exists()]
    found += [p for p in root.rglob('__pycache__') if '.venv' not in p.parts and '.git' not in p.parts]
    return sorted(set(found))


def size(path: Path) -> int:
    if path.is_file(): return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


def bot_running(db_path: Path) -> bool:
    try:
        with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as conn:
            row = conn.execute('SELECT time,status FROM bot_status WHERE id=1').fetchone()
    except sqlite3.Error: return False
    if not row or not row[0]: return False
    stamp = datetime.fromisoformat(row[0]); stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() < 120 and row[1] != 'stopped'


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument('--apply', action='store_true'); args = parser.parse_args()
    import yaml
    config = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    db_path = ROOT / 'data' / 'bot.db'
    history_dir = Path(config.get('history', {}).get('data_dir', 'data/history'))
    history_dir = history_dir if history_dir.is_absolute() else ROOT / history_dir
    recent = set()
    if db_path.exists():
        with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as conn:
            recent = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM trades WHERE status='open'")}
    focus = list((config.get('universe', {}) or {}).get('focus_symbols') or [])

    files = junk_paths(ROOT) + history_to_remove(history_dir, focus, recent)
    total = sum(size(p) for p in files)
    print(f"{'SİLİNİR' if args.apply else 'Siləcək (sınaq rejimi)'}: {len(files)} element, {total / 1e6:.1f} MB")
    for path in files: print('  -', path.relative_to(ROOT), f'({size(path) / 1e6:.1f} MB)')

    if not args.apply:
        print('\nBaza: köhnə fasilələr silinəcək, köhnə qeydlər təmizlənəcək, fayl sıxılacaq (VACUUM).')
        print('Həqiqətən silmək üçün: botu dayandırın və --apply ilə işə salın.')
        return 0
    if bot_running(db_path):
        print('XƏTA: bot işləyir. Əvvəl stop_bot.bat ilə dayandırın.'); return 1
    for path in files:
        try: shutil.rmtree(path) if path.is_dir() else path.unlink()
        except OSError as exc: print('  alınmadı:', path.name, exc)
    if db_path.exists():
        from src.db import Database, backup_database
        backup = backup_database(db_path, ROOT / 'data' / 'backups', keep_days=int(config.get('backup', {}).get('keep_days', 7)))
        print('Baza nüsxəsi:', backup or 'bu günün nüsxəsi artıq var')
        before = db_path.stat().st_size
        Database(db_path).maintain()
        with sqlite3.connect(db_path) as conn:
            removed = conn.execute('DELETE FROM adaptive_gates WHERE disabled_until<?', (datetime.now(timezone.utc).isoformat(),)).rowcount
            from src.symbols import underlying_key
            keys = keep_keys(focus)
            for name, in conn.execute("SELECT name FROM adaptive_gates WHERE scope='symbol'").fetchall():
                if underlying_key(name) not in keys:
                    removed += conn.execute("DELETE FROM adaptive_gates WHERE scope='symbol' AND name=?", (name,)).rowcount
            conn.commit()
        conn = sqlite3.connect(db_path); conn.execute('VACUUM'); conn.close()
        print(f'Baza: {removed} köhnə fasilə silindi, ölçü {before / 1e6:.1f} MB → {db_path.stat().st_size / 1e6:.1f} MB')
    print('HAZIR')
    return 0


if __name__ == '__main__': raise SystemExit(main())

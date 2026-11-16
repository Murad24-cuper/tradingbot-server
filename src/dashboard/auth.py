"""Panel login stored on the server (no password in .env needed).

* The login (username + password) is created once in the panel itself. Creation is only allowed
  from a private connection (SSH tunnel / the computer itself), never from the public HTTPS address,
  so nobody on the internet can claim the panel first.
* Passwords are stored as PBKDF2-SHA256 hashes with a random salt (data/panel_auth.json).
* After 5 wrong attempts within 10 minutes every login is locked for 10 minutes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

ITERATIONS = 240_000
MIN_PASSWORD = 8
MAX_FAILURES = 5
WINDOW = timedelta(minutes=10)


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / 'panel_auth.json'


def _load(data_dir: Path) -> dict:
    try: return json.loads(_path(data_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}


def _save(data_dir: Path, state: dict) -> None:
    path = _path(data_dir); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(state, indent=1), encoding='utf-8')
    os.replace(tmp, path)
    try: os.chmod(path, 0o600)
    except OSError: pass


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), ITERATIONS).hex()


def has_user(data_dir: Path) -> bool:
    return bool(_load(data_dir).get('username'))


def username(data_dir: Path) -> str | None:
    return _load(data_dir).get('username')


def set_user(data_dir: Path, name: str, password: str) -> None:
    name = (name or '').strip()
    if not name: raise ValueError('İstifadəçi adı boş ola bilməz.')
    if len(password or '') < MIN_PASSWORD: raise ValueError(f'Parol ən azı {MIN_PASSWORD} simvol olmalıdır.')
    state = _load(data_dir); salt = secrets.token_hex(16)
    state.update(username=name, salt=salt, hash=_hash(password, salt), updated_at=datetime.now(timezone.utc).isoformat(), failures=[])
    _save(data_dir, state)


def locked_until(data_dir: Path, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    recent = [datetime.fromisoformat(t) for t in _load(data_dir).get('failures', [])]
    recent = [t for t in recent if now - t < WINDOW]
    return max(recent) + WINDOW if len(recent) >= MAX_FAILURES else None


def verify(data_dir: Path, name: str, password: str, now: datetime | None = None) -> bool:
    """True for the right username and password; wrong attempts are counted (also while locked)."""
    now = now or datetime.now(timezone.utc); state = _load(data_dir)
    if not state.get('username') or locked_until(data_dir, now): return False
    ok = hmac.compare_digest((name or '').strip(), state['username']) & hmac.compare_digest(_hash(password or '', state['salt']), state['hash'])
    failures = [t for t in state.get('failures', []) if now - datetime.fromisoformat(t) < WINDOW]
    state['failures'] = [] if ok else failures + [now.isoformat()]
    _save(data_dir, state)
    return bool(ok)

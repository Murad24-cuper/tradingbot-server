#!/usr/bin/env python3
"""Hang watchdog for the server (runs on the host every minute from a systemd timer).

The bot writes a heartbeat to data/bot.db every few seconds. If the bot says it is running but
the heartbeat is older than HANG_SECONDS, the whole MT5 container is restarted (this also
recovers a frozen MT5 terminal, which the in-container supervisor cannot). Guards: the
container must have been up for a while, and restarts are at least COOLDOWN apart.
State goes to data/watchdog.json (shown in the panel), an event to the bot's log table,
and a Telegram message when the token is in .env. Standard library only.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import subprocess
import urllib.parse
import urllib.request

ROOT = Path('/opt/tradingbot')
DB = ROOT / 'data' / 'bot.db'
STATE = ROOT / 'data' / 'watchdog.json'
CONTAINER = 'tradingbot-mt5'
HANG_SECONDS = 300
MIN_UPTIME = 600
COOLDOWN = 900


def now() -> datetime: return datetime.now(timezone.utc)


def parse(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00')[:32])
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError): return None


def heartbeat() -> tuple[str | None, float | None]:
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10) as c:
        row = c.execute('SELECT status,time FROM bot_status WHERE id=1').fetchone()
    if not row: return None, None
    stamp = parse(row[1])
    return row[0], ((now() - stamp).total_seconds() if stamp else None)


def uptime() -> float | None:
    out = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}} {{.State.StartedAt}}', CONTAINER], capture_output=True, text=True, timeout=30)
    if out.returncode != 0: return None
    running, started = (out.stdout.strip().split(' ', 1) + [''])[:2]
    stamp = parse(started[:26] + '+00:00') if started else None
    return (now() - stamp).total_seconds() if running == 'true' and stamp else None


def telegram(text: str) -> None:
    env = {}
    try:
        for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1); env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError: return
    token, chat = env.get('TELEGRAM_BOT_TOKEN'), env.get('TELEGRAM_CHAT_ID')
    if not token or not chat: return
    data = urllib.parse.urlencode({'chat_id': chat, 'text': text}).encode()
    try: urllib.request.urlopen(f'https://api.telegram.org/bot{token}/sendMessage', data=data, timeout=15).read()
    except OSError: pass


def log_event(message: str) -> None:
    try:
        with sqlite3.connect(DB, timeout=10) as c:
            c.execute('INSERT INTO bot_events(time,level,source,message) VALUES (?,?,?,?)', (now().isoformat(), 'WARNING', 'watchdog', message))
    except sqlite3.Error: pass


def main() -> int:
    state = {}
    try: state = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError): pass
    state.update(checked_at=now().isoformat(), last_error='')
    try:
        status, age = heartbeat(); up = uptime()
        state.update(bot_status=status, heartbeat_age=age, container_uptime=up)
        last = parse(state.get('last_restart'))
        cooled = last is None or (now() - last).total_seconds() >= COOLDOWN
        if status == 'running' and age is not None and age > HANG_SECONDS and up is not None and up > MIN_UPTIME and cooled:
            minutes = int(age // 60)
            log_event(f'Hang watchdog: no heartbeat for {minutes} min; restarting the MT5 container')
            result = subprocess.run(['docker', 'restart', CONTAINER], capture_output=True, text=True, timeout=180)
            state.update(last_restart=now().isoformat(), restarts=int(state.get('restarts', 0)) + 1,
                         last_restart_ok=result.returncode == 0)
            telegram(f'⚠️ Bot {minutes} dəqiqə cavab vermədi. İlişmə keşikçisi onu yenidən başlatdı'
                     + ('.' if result.returncode == 0 else f', amma alınmadı: {result.stderr.strip()[:200]}'))
    except Exception as exc:                                  # never crash the timer; show it in the panel
        state['last_error'] = str(exc)[:300]
    tmp = STATE.with_suffix('.tmp'); tmp.write_text(json.dumps(state, indent=1), encoding='utf-8'); tmp.replace(STATE)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

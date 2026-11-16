"""The one list of commands the dashboard, Telegram and the new panel may queue for the bot."""
from __future__ import annotations
import re

SIMPLE = {'close_all', 'pause', 'resume', 'stop', 'close_all_stop', 'restart', 'status', 'today', 'positions', 'health',
          'diagnose_order', 'demo_executor_e2e'}
_PATTERNS = (
    re.compile(r'close_position:\d+'),
    re.compile(r'breakeven:\d+'),                                         # move the stop to the entry price
    re.compile(r'open_signal:\d+:(?:\d+(?:\.\d+)?):[01]'),                # signal id, risk %, bot manages exits (1/0)
    re.compile(r'guard_reset:[A-Za-z0-9_]+:[A-Za-z0-9_.#\-]+'),          # strategy, symbol: give a paused pair a fresh start
    re.compile(r'filter_remove:\d+'),                                     # drop a learned filter
    # symbol, side, risk %, bot manages exits (1/0), stop price, target price (0 = the bot computes it)
    re.compile(r'open_market:[A-Za-z0-9_.#\-]+:(?:buy|sell):\d+(?:\.\d+)?:[01]:\d+(?:\.\d+)?:\d+(?:\.\d+)?'),
)
QUICK = ('open_market:', 'open_signal:', 'close_position:', 'breakeven:')


def is_allowed(command: str) -> bool:
    command = str(command)
    return command in SIMPLE or any(pattern.fullmatch(command) for pattern in _PATTERNS)


def parse_open_signal(command: str) -> tuple[int, float, bool]:
    _, signal_id, risk, managed = str(command).split(':')
    return int(signal_id), float(risk), managed == '1'


def parse_open_market(command: str) -> tuple[str, str, float, bool, float, float]:
    _, symbol, side, risk, managed, sl, tp = str(command).split(':')
    return symbol, side, float(risk), managed == '1', float(sl), float(tp)

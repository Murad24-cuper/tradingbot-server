"""Add the new config sections (strategy guard, loss autopsy, time machine, panel) to an existing config.yaml.

Idempotent and comment-preserving (plain text insert before `notify:`); everything else in
the file stays exactly as it is. Usage:  python scripts/add_config_sections.py [path/to/config.yaml]
"""
from __future__ import annotations
from pathlib import Path
import sys

BLOCK = '''strategy_guard:                 # Learns from every closed trade; pauses strategy/symbol pairs that keep losing
  enabled: true
  window: 50                     # Last N closed trades per strategy and symbol
  min_trades: 20                 # Never judge on fewer trades
  pause_below_r: -0.15           # Pause when the blended result (live + long-history test) is this bad
  pause_days: 7                  # A pause ends by itself after this; then only newer trades count
loss_autopsy:                    # Finds conditions that keep losing; filters them when the long-history test agrees
  enabled: true
'''


TIME_MACHINE = '''time_machine:                    # The bot simulates every own entry on the long history first (DEMO decides; REAL only records)
  enabled: true
  block_below_r: -0.10           # Do not open when the simulated result after costs is worse than this
  choose_levels: true            # Move stop/target to a clearly better grid cell (lot is sized after, risk stays the same)
  min_improvement_r: 0.03
  require_reliable: false        # true = decide only for symbols that passed scripts/check_time_machine.py
  demo_only: true
  manage_open: true              # Every minute each open position is simulated from the current price (DEMO may close it)
  close_margin_r: 0.10           # Close when holding is expected to give this much less (original R) than closing now
  min_age_minutes: 3
'''
PANEL = '''panel:
  confirm_orders_real: false    # true = the panel asks before a manual order on a REAL account (DEMO never asks)
'''


def _insert(text: str, block: str) -> str:
    marker = '\nnotify:\n'
    if marker in text: return text.replace(marker, '\n' + block + 'notify:\n', 1)
    return text.rstrip('\n') + '\n' + block


def add_sections(text: str) -> tuple[str, bool]:
    changed = False
    if '\nstrategy_guard:' not in '\n' + text: text = _insert(text, BLOCK); changed = True
    if '\ntime_machine:' not in '\n' + text: text = _insert(text, TIME_MACHINE); changed = True
    if '\npanel:' not in '\n' + text: text = _insert(text, PANEL); changed = True
    # Keys added to time_machine later: put them into an existing section too.
    start = text.find('\ntime_machine:')
    if start >= 0 and '\n  manage_open:' not in text[start:]:
        at = text.find('\n  demo_only:', start)
        if at >= 0:
            end = text.find('\n', at + 1)
            end = len(text) if end < 0 else end
            extra = ''.join(line + '\n' for line in TIME_MACHINE.splitlines() if line.startswith(('  manage_open:', '  close_margin_r:', '  min_age_minutes:')))
            text = text[:end + 1] + extra + text[end + 1:]; changed = True
    return text, changed


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'config.yaml')
    text = path.read_text(encoding='utf-8')
    new, changed = add_sections(text)
    if changed:
        path.with_suffix('.yaml.before-sections').write_text(text, encoding='utf-8')
        path.write_text(new, encoding='utf-8')
    from src.config import load_settings                    # the result must still load
    load_settings(config_path=path, require_credentials=False)
    print('added' if changed else 'already present', path)
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())

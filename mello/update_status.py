"""How this speaker's updates are going, for the settings menu.

pi/auto-update.sh writes ~/.mello-update-status after every run. A speaker
that cannot update otherwise looks exactly like one that is up to date, so
the menu says so once it has been going on for a while.
"""
import time
from pathlib import Path
from typing import Optional

STATUS_FILE = Path.home() / '.mello-update-status'
# Written by pi/update-target.sh while newer versions need Spotify Soloist.
NEEDS_SOLOIST_FILE = Path.home() / '.mello-needs-soloist'

# One failed night (WiFi asleep, GitHub down) is not worth mentioning.
FAILING_FOR = 36 * 3600
# Runs every night: nothing written for this long means it does not run.
SILENT_FOR = 3 * 24 * 3600


def _read(path: Path) -> dict:
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


def last_run_label(path: Path = STATUS_FILE) -> Optional[str]:
    """How the run that just ended went, for the update button (None: fine)."""
    try:
        result = _read(path).get('result')
    except OSError:
        return None
    if result == 'no-internet':
        return 'No internet'
    if result == 'failed':
        return 'Update failed'
    return None


def update_problem(path: Path = STATUS_FILE, now: Optional[float] = None) -> Optional[str]:
    """A few words when updates have not worked for a while, else None."""
    try:
        fields = _read(path)
        last_run = float(fields['time'])
    except (OSError, KeyError, ValueError):
        return None      # never ran on this version yet
    now = time.time() if now is None else now
    if now - last_run > SILENT_FOR:
        return 'Updates not running'
    result = fields.get('result')
    if result == 'ok':
        return None
    try:
        last_ok = float(fields.get('last_ok') or 0)
    except ValueError:
        last_ok = 0
    if now - last_ok < FAILING_FOR:
        return None
    return 'Update failed: no internet' if result == 'no-internet' else 'Update failed'


def needs_soloist(path: Path = NEEDS_SOLOIST_FILE) -> bool:
    """Newer versions need Spotify Soloist, which this speaker does not use yet."""
    return path.exists()

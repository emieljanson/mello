"""
Settings Manager - Persistent user-configurable values stored in settings.json.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from ..config import SETTINGS_PATH

logger = logging.getLogger(__name__)

# Defaults (matching existing config.py values)
DEFAULT_AUTO_PAUSE_MINUTES = 30
DEFAULT_PROGRESS_EXPIRY_HOURS = 48

# Allowed options for the setup menu
AUTO_PAUSE_OPTIONS = [15, 30, 60, 120]  # minutes
PROGRESS_EXPIRY_OPTIONS = [12, 24, 48, 96]  # hours


class Settings:
    """Loads/saves user-configurable values from settings.json."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or SETTINGS_PATH
        self._auto_pause_minutes = DEFAULT_AUTO_PAUSE_MINUTES
        self._progress_expiry_hours = DEFAULT_PROGRESS_EXPIRY_HOURS
        self._last_bt_device_mac: Optional[str] = None
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self._auto_pause_minutes = data.get('auto_pause_minutes', DEFAULT_AUTO_PAUSE_MINUTES)
                self._progress_expiry_hours = data.get('progress_expiry_hours', DEFAULT_PROGRESS_EXPIRY_HOURS)
                self._last_bt_device_mac = data.get('last_bt_device_mac')
                logger.info(f'Settings loaded: auto_pause={self._auto_pause_minutes}min, expiry={self._progress_expiry_hours}h')
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning(f'Could not load settings, using defaults: {e}')

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                'auto_pause_minutes': self._auto_pause_minutes,
                'progress_expiry_hours': self._progress_expiry_hours,
                'last_bt_device_mac': self._last_bt_device_mac,
            }, indent=2))
        except Exception as e:
            logger.warning(f'Could not save settings: {e}')

    # --- Auto-pause ---

    @property
    def auto_pause_minutes(self) -> int:
        return self._auto_pause_minutes

    @property
    def auto_pause_timeout(self) -> int:
        """Auto-pause timeout in seconds (used by AutoPauseManager)."""
        return self._auto_pause_minutes * 60

    def cycle_auto_pause(self) -> int:
        """Advance to the next auto-pause option and save. Returns new value in minutes."""
        idx = AUTO_PAUSE_OPTIONS.index(self._auto_pause_minutes) if self._auto_pause_minutes in AUTO_PAUSE_OPTIONS else 0
        self._auto_pause_minutes = AUTO_PAUSE_OPTIONS[(idx + 1) % len(AUTO_PAUSE_OPTIONS)]
        self._save()
        logger.info(f'Auto-pause set to {self._auto_pause_minutes} minutes')
        return self._auto_pause_minutes

    # --- Progress expiry ---

    @property
    def progress_expiry_hours(self) -> int:
        return self._progress_expiry_hours

    def cycle_progress_expiry(self) -> int:
        """Advance to the next expiry option and save. Returns new value in hours."""
        idx = PROGRESS_EXPIRY_OPTIONS.index(self._progress_expiry_hours) if self._progress_expiry_hours in PROGRESS_EXPIRY_OPTIONS else 0
        self._progress_expiry_hours = PROGRESS_EXPIRY_OPTIONS[(idx + 1) % len(PROGRESS_EXPIRY_OPTIONS)]
        self._save()
        logger.info(f'Progress expiry set to {self._progress_expiry_hours} hours')
        return self._progress_expiry_hours

    # --- Bluetooth ---

    @property
    def last_bt_device_mac(self) -> Optional[str]:
        return self._last_bt_device_mac

    def set_last_bt_device_mac(self, mac: Optional[str]):
        self._last_bt_device_mac = mac
        self._save()

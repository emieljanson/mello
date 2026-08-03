"""
Settings Manager - Persistent user-configurable values stored in settings.json.
"""
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

from ..config import (SETTINGS_PATH, DEFAULT_VOLUME_LEVELS, VOLUME_RANGE,
                      VOLUME_ADJUST_STEP)

logger = logging.getLogger(__name__)

# Defaults (matching existing config.py values)
DEFAULT_AUTO_PAUSE_MINUTES = 30
DEFAULT_PROGRESS_EXPIRY_HOURS = 96
DEFAULT_QUIET_START = None       # None = quiet hours off
DEFAULT_QUIET_END = 7 * 60       # 07:00

# Allowed options for the setup menu
AUTO_PAUSE_OPTIONS = [15, 30, 60, 120]  # minutes
PROGRESS_EXPIRY_OPTIONS = [12, 24, 48, 96]  # hours

# Bedtime window, as minutes since midnight. Tap-to-cycle like the other rows:
# the screen has no keyboard, so a short list of plausible times beats a picker.
QUIET_START_OPTIONS = [None, 18 * 60 + 30, 19 * 60, 19 * 60 + 30,
                       20 * 60, 20 * 60 + 30, 21 * 60]
QUIET_END_OPTIONS = [6 * 60, 6 * 60 + 30, 7 * 60, 7 * 60 + 30, 8 * 60]


def format_time(minutes: Optional[int]) -> str:
    """Render minutes-since-midnight as 'HH:MM', or 'Off' for None."""
    if minutes is None:
        return 'Off'
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


class Settings:
    """Loads/saves user-configurable values from settings.json."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or SETTINGS_PATH
        self._auto_pause_minutes = DEFAULT_AUTO_PAUSE_MINUTES
        self._progress_expiry_hours = DEFAULT_PROGRESS_EXPIRY_HOURS
        self._quiet_start: Optional[int] = DEFAULT_QUIET_START
        self._quiet_end: int = DEFAULT_QUIET_END
        self._last_bt_device_mac: Optional[str] = None
        self._volume_overrides: Optional[list] = None  # None = use defaults
        self._share_usage_data: bool = True  # Set once during install, not changeable via UI
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self._auto_pause_minutes = data.get('auto_pause_minutes', DEFAULT_AUTO_PAUSE_MINUTES)
                self._progress_expiry_hours = data.get('progress_expiry_hours', DEFAULT_PROGRESS_EXPIRY_HOURS)
                self._quiet_start = data.get('quiet_hours_start', DEFAULT_QUIET_START)
                self._quiet_end = data.get('quiet_hours_end', DEFAULT_QUIET_END)
                self._last_bt_device_mac = data.get('last_bt_device_mac')
                self._volume_overrides = data.get('volume_levels')
                if 'share_usage_data' in data:
                    self._share_usage_data = bool(data['share_usage_data'])
                logger.info(f'Settings loaded: auto_pause={self._auto_pause_minutes}min, expiry={self._progress_expiry_hours}h')
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning(f'Could not load settings, using defaults: {e}')

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'auto_pause_minutes': self._auto_pause_minutes,
                'progress_expiry_hours': self._progress_expiry_hours,
                'quiet_hours_start': self._quiet_start,
                'quiet_hours_end': self._quiet_end,
                'last_bt_device_mac': self._last_bt_device_mac,
                'share_usage_data': self._share_usage_data,
            }
            if self._volume_overrides is not None:
                data['volume_levels'] = self._volume_overrides
            self._path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f'Could not save settings: {e}')

    # --- Usage data sharing (set once during install) ---

    @property
    def share_usage_data(self) -> bool:
        return self._share_usage_data

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

    # --- Quiet hours ---

    @property
    def quiet_hours(self) -> Tuple[Optional[int], int]:
        """Bedtime window as (start, end) minutes since midnight. Start None = off."""
        return self._quiet_start, self._quiet_end

    @property
    def quiet_start_label(self) -> str:
        return format_time(self._quiet_start)

    @property
    def quiet_end_label(self) -> str:
        return format_time(self._quiet_end)

    def cycle_quiet_start(self) -> Optional[int]:
        """Advance the bedtime start (including Off) and save."""
        idx = QUIET_START_OPTIONS.index(self._quiet_start) if self._quiet_start in QUIET_START_OPTIONS else 0
        self._quiet_start = QUIET_START_OPTIONS[(idx + 1) % len(QUIET_START_OPTIONS)]
        self._save()
        logger.info(f'Quiet hours start set to {format_time(self._quiet_start)}')
        return self._quiet_start

    def cycle_quiet_end(self) -> int:
        """Advance the wake time and save."""
        idx = QUIET_END_OPTIONS.index(self._quiet_end) if self._quiet_end in QUIET_END_OPTIONS else 0
        self._quiet_end = QUIET_END_OPTIONS[(idx + 1) % len(QUIET_END_OPTIONS)]
        self._save()
        logger.info(f'Quiet hours end set to {format_time(self._quiet_end)}')
        return self._quiet_end

    # --- Bluetooth ---

    @property
    def last_bt_device_mac(self) -> Optional[str]:
        return self._last_bt_device_mac

    def set_last_bt_device_mac(self, mac: Optional[str]):
        self._last_bt_device_mac = mac
        self._save()

    # --- Volume levels ---

    def get_volume_levels(self) -> list:
        """Return volume levels (3 dicts with speaker, bt, icon keys)."""
        if self._volume_overrides is None:
            return [dict(d) for d in DEFAULT_VOLUME_LEVELS]
        # Merge overrides with defaults (ensure icon key is always present)
        result = []
        for i, default in enumerate(DEFAULT_VOLUME_LEVELS):
            entry = dict(default)
            if i < len(self._volume_overrides):
                override = self._volume_overrides[i]
                for key in ('speaker', 'bt'):
                    if key in override:
                        entry[key] = override[key]
            result.append(entry)
        return result

    def adjust_volume(self, level_index: int, output_type: str, delta: int) -> int:
        """Nudge a volume value one step in the direction of delta (+1 or -1).

        delta is a direction, not a magnitude: one tap moves VOLUME_ADJUST_STEP
        percentage points so retuning a preset isn't dozens of taps.
        """
        levels = self.get_volume_levels()
        if level_index < 0 or level_index >= len(levels):
            return 0
        lo, hi = VOLUME_RANGE.get(output_type, (0, 100))
        step = VOLUME_ADJUST_STEP if delta > 0 else -VOLUME_ADJUST_STEP
        new_val = max(lo, min(hi, levels[level_index][output_type] + step))
        # Initialize overrides from current effective values
        if self._volume_overrides is None:
            self._volume_overrides = [
                {'speaker': d['speaker'], 'bt': d['bt']}
                for d in DEFAULT_VOLUME_LEVELS
            ]
        self._volume_overrides[level_index][output_type] = new_val
        self._save()
        return new_val

    def reset_volume_levels(self):
        """Reset volume levels to defaults."""
        self._volume_overrides = None
        self._save()
        logger.info('Volume levels reset to defaults')

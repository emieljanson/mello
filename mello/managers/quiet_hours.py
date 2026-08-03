"""
Quiet Hours - Bedtime window where the device refuses to wake up.

During the window the screen stays on its dim clock and taps are ignored, so
there is no play button to reach. A parent can hold the screen to override
until the window ends, and casting from Spotify still works (parent's phone).
"""
import datetime
import logging
import os
from typing import Optional

from ..config import WAKE_WINDOW_MINUTES

logger = logging.getLogger(__name__)

# systemd-timesyncd creates this once NTP has actually set the clock.
TIMESYNC_MARKER = '/run/systemd/timesync/synchronized'

# Oldest year we treat as a real clock reading rather than an unsynced boot.
MIN_TRUSTED_YEAR = 2025


_trusted = False  # latched: a synced clock never becomes unsynced


def clock_is_trusted(now: Optional[datetime.datetime] = None) -> bool:
    """True when the system clock is good enough for time-of-day decisions.

    The Pi has no RTC, so before NTP lands the clock can read 1970. Enforcing
    bedtime on that would silence the device at an arbitrary time of day, so
    quiet hours stays off until the clock is believable. Latched once true so
    the sleep loop isn't stat()ing the marker every tick all night.
    """
    global _trusted
    if _trusted:
        return True

    now = now or datetime.datetime.now()
    # ponytail: year sniff covers non-systemd setups; read a real RTC if one is ever fitted
    if os.path.exists(TIMESYNC_MARKER) or now.year >= MIN_TRUSTED_YEAR:
        _trusted = True
    return _trusted


def is_within(start_min: Optional[int], end_min: Optional[int],
              now: Optional[datetime.datetime] = None) -> bool:
    """True if now falls in the [start, end) window, wrapping past midnight.

    Args:
        start_min: Window start as minutes since midnight, or None for disabled.
        end_min: Window end as minutes since midnight.
    """
    if start_min is None or end_min is None or start_min == end_min:
        return False

    now = now or datetime.datetime.now()
    minutes = now.hour * 60 + now.minute

    if start_min < end_min:
        return start_min <= minutes < end_min          # same day: 13:00-15:00
    return minutes >= start_min or minutes < end_min   # wraps midnight: 19:30-07:00


class QuietHours:
    """Tracks whether bedtime is currently being enforced."""

    def __init__(self, settings):
        self.settings = settings
        self._in_window = False
        self._overridden = False
        self._active = False

    def in_wake_window(self, now: Optional[datetime.datetime] = None) -> bool:
        """True for a stretch after the wake time — the 'OK to wake' sun.

        Only meaningful when a bedtime is set: without one there is no morning
        boundary to signal.
        """
        start, end = self.settings.quiet_hours
        if start is None or end is None:
            return False
        if not clock_is_trusted(now):
            return False
        return is_within(end, (end + WAKE_WINDOW_MINUTES) % (24 * 60), now)

    @property
    def active(self) -> bool:
        """True when playback and wake should be blocked. Refreshed by update()."""
        return self._active

    @property
    def overridden(self) -> bool:
        return self._overridden

    def update(self, now: Optional[datetime.datetime] = None) -> bool:
        """Refresh state. Returns True on the transition *into* quiet hours."""
        start, end = self.settings.quiet_hours
        in_window = clock_is_trusted(now) and is_within(start, end, now)

        if not in_window and self._overridden:
            logger.info('Quiet hours: window ended, parent override cleared')
            self._overridden = False

        started = in_window and not self._in_window
        self._in_window = in_window
        self._active = in_window and not self._overridden

        if started:
            logger.info('Quiet hours: started')
        return started

    def override(self):
        """Parent held the sleeping screen: stop enforcing until the window ends."""
        if not self._overridden:
            logger.info('Quiet hours: overridden by hold-to-wake')
        self._overridden = True
        self._active = False

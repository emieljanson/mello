"""Tests for quiet hours: the midnight-wrapping window and the no-RTC guard."""
import datetime
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.managers import quiet_hours as qh
from mello.managers.quiet_hours import (
    QuietHours, clock_is_trusted, is_within, MIN_TRUSTED_YEAR, TIMESYNC_MARKER,
)


@pytest.fixture(autouse=True)
def reset_trust_latch():
    """clock_is_trusted latches once true — clear it so tests stay independent."""
    qh._trusted = False
    yield
    qh._trusted = False


def at(hour, minute=0):
    return datetime.datetime(2026, 8, 3, hour, minute)


class FakeSettings:
    """Minimal stand-in exposing just the quiet_hours pair."""

    def __init__(self, start, end):
        self.quiet_hours = (start, end)


# --- Window logic ---

@pytest.mark.parametrize('now, expected', [
    (at(19, 29), False),   # a minute before bedtime
    (at(19, 30), True),    # start is inclusive
    (at(23, 59), True),    # before midnight
    (at(0, 0), True),      # after midnight — the wrap case
    (at(6, 59), True),     # a minute before wake
    (at(7, 0), False),     # end is exclusive
    (at(12, 0), False),    # midday
])
def test_window_wraps_midnight(now, expected):
    assert is_within(19 * 60 + 30, 7 * 60, now) is expected


@pytest.mark.parametrize('now, expected', [
    (at(12, 59), False),
    (at(13, 0), True),
    (at(14, 59), True),
    (at(15, 0), False),
    (at(2, 0), False),     # must NOT wrap for a same-day window
])
def test_same_day_window(now, expected):
    assert is_within(13 * 60, 15 * 60, now) is expected


def test_disabled_or_degenerate_windows():
    assert is_within(None, 7 * 60, at(2, 0)) is False   # bedtime off
    assert is_within(19 * 60, None, at(2, 0)) is False
    assert is_within(19 * 60, 19 * 60, at(19, 0)) is False  # zero-length


# --- Clock trust (the Pi has no RTC) ---

def test_untrusted_clock_before_ntp(monkeypatch):
    monkeypatch.setattr('os.path.exists', lambda p: False)
    assert clock_is_trusted(datetime.datetime(1970, 1, 1, 20, 0)) is False


def test_trusted_by_timesync_marker_despite_old_date(monkeypatch):
    monkeypatch.setattr('os.path.exists', lambda p: p == TIMESYNC_MARKER)
    assert clock_is_trusted(datetime.datetime(1970, 1, 1, 20, 0)) is True


def test_trusted_by_plausible_year_without_marker(monkeypatch):
    monkeypatch.setattr('os.path.exists', lambda p: False)
    assert clock_is_trusted(datetime.datetime(MIN_TRUSTED_YEAR, 6, 1, 20, 0)) is True


def test_bedtime_not_enforced_on_unsynced_clock(monkeypatch):
    """An unsynced clock reading 1970 must not silence the device."""
    monkeypatch.setattr('os.path.exists', lambda p: False)
    quiet = QuietHours(FakeSettings(19 * 60 + 30, 7 * 60))
    quiet.update(datetime.datetime(1970, 1, 1, 20, 0))  # inside the window by time-of-day
    assert quiet.active is False


# --- Manager state ---

def test_reports_start_transition_once():
    quiet = QuietHours(FakeSettings(19 * 60 + 30, 7 * 60))
    assert quiet.update(at(19, 0)) is False   # before bedtime
    assert quiet.update(at(19, 30)) is True   # crossing into the window
    assert quiet.update(at(20, 0)) is False   # already inside, no repeat
    assert quiet.active is True


def test_override_lasts_until_window_ends():
    quiet = QuietHours(FakeSettings(19 * 60 + 30, 7 * 60))
    quiet.update(at(20, 0))
    assert quiet.active is True

    quiet.override()                 # parent held to wake
    assert quiet.active is False

    quiet.update(at(23, 0))          # still bedtime, still overridden
    assert quiet.active is False

    quiet.update(at(8, 0))           # window ended, override expires
    assert quiet.overridden is False

    assert quiet.update(at(19, 30)) is True  # next night enforces again
    assert quiet.active is True


def test_bedtime_off_is_never_active():
    quiet = QuietHours(FakeSettings(None, 7 * 60))
    assert quiet.update(at(2, 0)) is False
    assert quiet.active is False

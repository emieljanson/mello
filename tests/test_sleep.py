"""
Tests for SleepManager and touch-wake safety guards.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.handlers.evdev_touch import EvdevTouchHandler
from mello.managers.sleep import SleepManager


def make_sleep_manager(monkeypatch):
    monkeypatch.setattr(SleepManager, '_detect_backlight', lambda self: None)
    monkeypatch.setattr(SleepManager, '_detect_drm_connector', lambda self: None)
    monkeypatch.setattr(SleepManager, '_set_low_power_cpu', lambda self, low: None)
    monkeypatch.setattr(SleepManager, '_set_led', lambda self, on: None)
    return SleepManager()


def test_sleep_allowed_when_enabled(monkeypatch):
    mgr = make_sleep_manager(monkeypatch)
    mgr.last_activity = 100

    with patch('mello.managers.sleep.time.time', return_value=100 + 121):
        assert mgr.check_sleep(is_playing=False) is True

    assert mgr.is_sleeping is True


def test_sleep_blocked_when_disabled(monkeypatch):
    mgr = make_sleep_manager(monkeypatch)
    mgr.last_activity = 100
    mgr.disable_sleep('touch wake unavailable')

    with patch('mello.managers.sleep.time.time', return_value=100 + 121):
        assert mgr.check_sleep(is_playing=False) is False

    assert mgr.is_sleeping is False
    assert mgr.sleep_enabled is False
    assert mgr.sleep_disabled_reason == 'touch wake unavailable'


def test_disable_sleep_wakes_display(monkeypatch):
    display = MagicMock()
    monkeypatch.setattr(SleepManager, '_detect_backlight', lambda self: None)
    monkeypatch.setattr(SleepManager, '_detect_drm_connector', lambda self: None)
    monkeypatch.setattr(SleepManager, '_set_low_power_cpu', lambda self, low: None)
    monkeypatch.setattr(SleepManager, '_set_led', lambda self, on: None)
    monkeypatch.setattr(SleepManager, '_set_display', display)

    mgr = SleepManager()
    mgr.is_sleeping = True
    mgr.disable_sleep('touch read error')

    assert mgr.is_sleeping is False
    display.assert_called_with(True)


def test_touch_failure_reason_is_consumed_once():
    handler = EvdevTouchHandler(720, 1280)

    handler._mark_failed('touch read loop exited')

    assert handler.is_available is False
    assert handler.consume_failure_reason() == 'touch read loop exited'
    assert handler.consume_failure_reason() is None

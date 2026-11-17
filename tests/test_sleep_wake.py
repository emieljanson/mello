"""A touch from before the screen went off must not wake it again."""
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.app import Mello


def test_falling_asleep_forgets_earlier_touches():
    app = Mello.__new__(Mello)
    app.evdev_touch = SimpleNamespace(wake_event=threading.Event())
    app.bluetooth = SimpleNamespace(pause_monitoring=MagicMock())
    app.sleep_manager = SimpleNamespace(last_activity=0.0)
    app.tracker = SimpleNamespace(on_sleep=MagicMock())

    app.evdev_touch.wake_event.set()          # a tap two minutes ago
    app._on_fell_asleep()

    assert not app.evdev_touch.wake_event.is_set()
    app.bluetooth.pause_monitoring.assert_called_once()
    app.tracker.on_sleep.assert_called_once()

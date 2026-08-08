import sys
import types
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.app import Mello


def _make_app():
    app = Mello.__new__(Mello)
    app.mock_mode = False
    app._status_failure_started_at = 100.0
    app._last_librespot_restart_at = 0.0
    app._connection_fail_count = 20
    app._show_toast = MagicMock()
    app._poll_wake_event = MagicMock()
    return app


@patch('mello.app.subprocess.run')
def test_sustained_status_failure_restarts_librespot(mock_run):
    app = _make_app()
    mock_run.return_value = MagicMock(returncode=0, stderr='')

    app._maybe_restart_librespot(now=161.0)

    mock_run.assert_called_once_with(
        ['sudo', 'systemctl', 'restart', 'mello-librespot'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    app._show_toast.assert_called_once_with('Spotify herstellen...')
    app._poll_wake_event.set.assert_called_once()
    assert app._last_librespot_restart_at == 161.0


@patch('mello.app.subprocess.run')
def test_short_status_failure_does_not_restart_librespot(mock_run):
    app = _make_app()

    app._maybe_restart_librespot(now=159.0)

    mock_run.assert_not_called()
    app._show_toast.assert_not_called()


@patch('mello.app.subprocess.run')
def test_restart_cooldown_prevents_restart_loop(mock_run):
    app = _make_app()
    app._last_librespot_restart_at = 150.0

    app._maybe_restart_librespot(now=200.0)

    mock_run.assert_not_called()
    app._show_toast.assert_not_called()


@patch('mello.app.subprocess.run')
def test_failed_restart_obeys_cooldown_without_waking_poll(mock_run):
    app = _make_app()
    mock_run.return_value = MagicMock(returncode=1, stderr='permission denied')

    app._maybe_restart_librespot(now=161.0)
    app._maybe_restart_librespot(now=200.0)

    assert mock_run.call_count == 1
    app._poll_wake_event.set.assert_not_called()
    assert app._connection_fail_count == 20


@patch('mello.app.subprocess.run')
def test_timed_out_restart_obeys_cooldown(mock_run):
    app = _make_app()
    mock_run.side_effect = subprocess.TimeoutExpired('systemctl', 10)

    app._maybe_restart_librespot(now=161.0)
    app._maybe_restart_librespot(now=200.0)

    assert mock_run.call_count == 1
    app._poll_wake_event.set.assert_not_called()


@patch('mello.app.subprocess.run')
def test_restart_becomes_eligible_when_cooldown_expires(mock_run):
    app = _make_app()
    app._last_librespot_restart_at = 150.0
    mock_run.return_value = MagicMock(returncode=0, stderr='')

    app._maybe_restart_librespot(now=450.0)

    mock_run.assert_called_once()

"""The "Check for updates" button in Settings.

Toasts are not drawn while the menu is open, so the button itself has to
say what happened. And one tap is enough: a newer version is installed
right away instead of asking for a second tap.
"""
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mello.managers import setup_menu as sm
from mello.managers.setup_menu import SetupMenu
from mello import update_status as sm_status


class _InlineThread:
    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _menu():
    return SetupMenu(
        catalog_manager=MagicMock(), settings=MagicMock(),
        on_toast=MagicMock(), on_invalidate=MagicMock(),
        on_library_cleared=MagicMock(),
    )


def _fake_git(local, remote, fetch_rc=0, status=None):
    """git rev-parse --git-dir, then pi/update-target.sh (see its header)."""
    def run(cmd, **kwargs):
        if cmd[:1] == ['git']:
            return SimpleNamespace(returncode=0, stdout='.git', stderr='')
        assert cmd[0] == 'bash' and cmd[1].endswith('pi/update-target.sh'), cmd
        if fetch_rc:
            return SimpleNamespace(returncode=2, stdout='', stderr='Could not resolve host')
        st = status or ('current' if local == remote else 'update')
        return SimpleNamespace(returncode=0, stdout=f'{st} stable origin/main {remote}\n', stderr='')
    return run


@pytest.fixture
def started(monkeypatch):
    monkeypatch.setattr(sm.threading, 'Thread', _InlineThread)
    popen = MagicMock()
    monkeypatch.setattr(sm.subprocess, 'Popen', popen)
    return popen


def test_idle_label():
    assert _menu().update_button_label == 'Check for updates'


def test_newer_version_installs_with_one_tap(monkeypatch, started):
    monkeypatch.setattr(sm.subprocess, 'run', _fake_git('a' * 40, 'b' * 40))
    menu = _menu()
    menu._check_for_update()
    started.assert_called_once()
    assert menu.update_button_label == 'Updating...'


def test_up_to_date_shows_on_the_button_then_goes_back(monkeypatch, started):
    monkeypatch.setattr(sm.subprocess, 'run', _fake_git('a' * 40, 'a' * 40))
    menu = _menu()
    menu._check_for_update()
    started.assert_not_called()
    assert menu.update_button_label == 'Up to date'
    menu._update_result_time -= sm._UPDATE_RESULT_SECONDS + 1
    menu.update()
    assert menu.update_button_label == 'Check for updates'


def test_no_internet_shows_on_the_button(monkeypatch, started):
    monkeypatch.setattr(sm.subprocess, 'run', _fake_git('a' * 40, 'b' * 40, fetch_rc=128))
    menu = _menu()
    menu._check_for_update()
    started.assert_not_called()
    assert menu.update_button_label == 'No internet'


def test_failed_update_shows_on_the_button(monkeypatch, started):
    monkeypatch.setattr(sm.subprocess, 'run', _fake_git('a' * 40, 'b' * 40))
    menu = _menu()
    menu._check_for_update()
    started.return_value.poll.return_value = 1
    menu.update()
    assert menu.update_button_label == 'Update failed'


def test_version_that_failed_here_before_says_so(monkeypatch, started):
    monkeypatch.setattr(sm.subprocess, 'run', _fake_git('a' * 40, 'b' * 40, status='failed'))
    menu = _menu()
    menu._check_for_update()
    started.assert_not_called()
    assert menu.update_button_label == 'Update failed'


def test_manual_update_without_internet_says_so(monkeypatch, started, tmp_path):
    status = tmp_path / 'status'
    status.write_text('time=1\nresult=no-internet\nmessage=x\nlast_ok=\n')
    monkeypatch.setattr(sm, 'last_run_label', lambda: sm_status.last_run_label(status))
    monkeypatch.setattr(sm.subprocess, 'run', _fake_git('a' * 40, 'b' * 40))
    menu = _menu()
    menu._check_for_update()
    started.return_value.poll.return_value = 0
    menu.update()
    assert menu.update_button_label == 'No internet'

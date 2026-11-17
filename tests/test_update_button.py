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


def _fake_git(local, remote, fetch_rc=0):
    def run(cmd, **kwargs):
        args = cmd[3:]
        if args[:1] == ['fetch']:
            return SimpleNamespace(returncode=fetch_rc, stdout=b'', stderr=b'Could not resolve host')
        if args == ['rev-parse', '--git-dir']:
            return SimpleNamespace(returncode=0, stdout='.git', stderr='')
        if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
            return SimpleNamespace(returncode=0, stdout='main\n', stderr='')
        if args == ['rev-parse', 'HEAD']:
            return SimpleNamespace(returncode=0, stdout=local + '\n', stderr='')
        if args == ['rev-parse', 'origin/main']:
            return SimpleNamespace(returncode=0, stdout=remote + '\n', stderr='')
        raise AssertionError(f'unexpected command {cmd}')
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

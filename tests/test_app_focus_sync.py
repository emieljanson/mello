"""
Tests for Berry remote Spotify focus sync behavior.
"""
import time
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from berry.app import Berry
from berry.config import CONTEXT_SWITCH_WATCHDOG_TIMEOUT
from berry.models import CatalogItem, NowPlaying


def _item(item_id: str, uri: str, name: str) -> CatalogItem:
    return CatalogItem(id=item_id, uri=uri, name=name, type='album')


def _make_berry(items: list[CatalogItem], now_playing: NowPlaying) -> Berry:
    """Build a lightweight Berry instance for unit-level sync tests."""
    app = Berry.__new__(Berry)
    app.catalog_manager = SimpleNamespace(items=items)
    app.temp_item = None
    app.selected_index = 0
    app.carousel = SimpleNamespace(set_target=MagicMock(), settled=True)
    app.touch = SimpleNamespace(dragging=False)
    app.user_interacting = False
    app.play_timer = SimpleNamespace(item=None)
    app.playback = SimpleNamespace(
        last_context_uri=None,
        has_pending_play=False,
        pause_intent_active=False,
        play_in_progress=False,
        play_state=SimpleNamespace(should_show_loading=False),
        stop_all=MagicMock(),
        is_item_playing=lambda item, now_playing: item.uri == now_playing.context_uri and now_playing.playing,
    )
    app.api = SimpleNamespace(pause=MagicMock())
    app.volume = SimpleNamespace(unmute=MagicMock(), mute=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._focus_epoch = 0
    app._pending_focus_uri = None
    app._pending_focus_since = 0.0
    app._pending_external_focus_uri = None
    app._requested_focus_epoch = None
    app._requested_focus_uri = None
    app._requested_focus_since = 0.0
    app._user_driving = False
    app._user_driving_since = 0.0
    app._manual_pause_lock = False
    app._manual_pause_context_uri = None
    app._user_activated_playback = True
    app._context_switch_stall_since = 0.0
    app._last_context_watchdog_log = 0.0
    app._status_unknown = False
    app._connected_lock = threading.Lock()
    app._connected = True
    app._show_toast = MagicMock()
    app._now_playing_lock = threading.Lock()
    app._now_playing = now_playing
    return app


class TestRemoteFocusSync:
    """Guards and behavior for remote Spotify context focus sync."""

    def test_sync_moves_focus_when_remote_context_is_playing(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_berry(items, NowPlaying(playing=True, context_uri='spotify:album:b'))

        app._sync_to_playing()

        assert app.selected_index == 1
        app.carousel.set_target.assert_called_once_with(1)
        app.renderer.invalidate.assert_called_once()
        assert app._focus_epoch == 1
        assert app._pending_external_focus_uri is None

    def test_sync_defers_while_user_intent_is_active(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_berry(items, NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._user_driving = True

        app._sync_to_playing()

        assert app.selected_index == 0
        assert app._pending_external_focus_uri == 'spotify:album:b'
        app.carousel.set_target.assert_not_called()

    def test_sync_applies_pending_remote_focus_after_user_intent_clears(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_berry(items, NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._user_driving = True

        app._sync_to_playing()
        assert app.selected_index == 0
        assert app._pending_external_focus_uri == 'spotify:album:b'

        app._user_driving = False
        app._sync_to_playing()
        assert app.selected_index == 1
        assert app._pending_external_focus_uri is None

    def test_sync_does_not_move_focus_when_spotify_not_playing(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_berry(items, NowPlaying(playing=False, paused=True, context_uri='spotify:album:b'))

        app._sync_to_playing()

        assert app.selected_index == 0
        assert app._pending_external_focus_uri is None
        app.carousel.set_target.assert_not_called()

    def test_sync_keeps_pending_when_context_not_in_display_items(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_berry(items, NowPlaying(playing=True, context_uri='spotify:album:external'))

        app._sync_to_playing()

        assert app.selected_index == 0
        assert app._pending_external_focus_uri == 'spotify:album:external'
        app.carousel.set_target.assert_not_called()

    def test_sync_does_not_unmute_when_pause_intent_is_active(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_berry(items, NowPlaying(playing=True, context_uri='spotify:album:a'))
        app.playback.pause_intent_active = True

        app._sync_to_playing()

        app.volume.unmute.assert_not_called()

    def test_sync_does_not_unmute_when_manual_pause_lock_is_active(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_berry(items, NowPlaying(playing=True, context_uri='spotify:album:a'))
        app._manual_pause_lock = True

        app._sync_to_playing()

        app.volume.unmute.assert_not_called()


class TestRemoteFocusPriority:
    """Rules that stop focused auto-play from overriding remote playback."""

    def test_prioritizes_remote_focus_on_mismatch_without_user_intent(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_berry([focused], NowPlaying(playing=True, context_uri='spotify:album:b'))

        assert app._should_prioritize_remote_focus(focused) is True

    def test_does_not_prioritize_remote_focus_while_user_driving(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_berry([focused], NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._user_driving = True

        assert app._should_prioritize_remote_focus(focused) is False

    def test_does_not_prioritize_remote_focus_when_context_matches(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_berry([focused], NowPlaying(playing=True, context_uri='spotify:album:a'))

        assert app._should_prioritize_remote_focus(focused) is False


class TestContextSwitchWatchdog:
    """Safety net for stuck context-switch mute/loading states."""

    def test_watchdog_does_not_trigger_before_timeout(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_berry([focused], NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._requested_focus_epoch = app._focus_epoch
        app._requested_focus_uri = focused.uri
        app._requested_focus_since = time.time() - 30.0
        app._context_switch_stall_since = time.time() - (CONTEXT_SWITCH_WATCHDOG_TIMEOUT - 1.0)

        app._check_context_switch_watchdog(focused)

        app.playback.stop_all.assert_not_called()
        app.volume.mute.assert_not_called()
        app._show_toast.assert_not_called()

    def test_watchdog_triggers_hard_fail_safe_after_timeout(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_berry([focused], NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._pending_focus_uri = focused.uri
        app._requested_focus_epoch = app._focus_epoch
        app._requested_focus_uri = focused.uri
        app._requested_focus_since = time.time() - (CONTEXT_SWITCH_WATCHDOG_TIMEOUT + 2.0)
        app._context_switch_stall_since = time.time() - (CONTEXT_SWITCH_WATCHDOG_TIMEOUT + 1.0)
        app._user_driving = True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr('berry.app.run_async', lambda fn, *a: fn(*a))
            app._check_context_switch_watchdog(focused)

        app.playback.stop_all.assert_called_once()
        app.api.pause.assert_called_once()
        app.volume.mute.assert_called_once()
        app._show_toast.assert_called_once_with('Loading cancelled, try again')
        assert app._pending_focus_uri is None
        assert app._requested_focus_epoch is None
        assert app._requested_focus_uri is None
        assert app._requested_focus_since == 0.0
        assert app._user_driving is False
        assert app._context_switch_stall_since == 0.0

    def test_watchdog_resets_timer_when_stall_condition_clears(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_berry([focused], NowPlaying(playing=False, paused=True, context_uri='spotify:album:a'))
        app._context_switch_stall_since = time.time() - 10.0
        app._pending_focus_uri = None
        app._requested_focus_uri = None
        app._user_driving = False

        app._check_context_switch_watchdog(focused)

        assert app._context_switch_stall_since == 0.0

"""
Tests for Mello remote Spotify focus sync behavior.
"""
import time
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.app import Mello
from mello.api.catalog import CatalogManager
from mello.api.librespot import LibrespotAPI
from mello.config import CONTEXT_SWITCH_WATCHDOG_TIMEOUT
from mello.controllers.playback import PlaybackController
from mello.handlers.events import EventListener
from mello.models import CatalogItem, MenuState, NowPlaying


def _item(item_id: str, uri: str, name: str) -> CatalogItem:
    return CatalogItem(id=item_id, uri=uri, name=name, type='album')


def _make_mello(items: list[CatalogItem], now_playing: NowPlaying) -> Mello:
    """Build a lightweight Mello instance for unit-level sync tests."""
    app = Mello.__new__(Mello)
    app.catalog_manager = SimpleNamespace(items=items)
    app.temp_item = None
    app.selected_index = 0
    app.carousel = SimpleNamespace(set_target=MagicMock(), settled=True)
    app.touch = SimpleNamespace(dragging=False)
    app.user_interacting = False
    app.play_timer = SimpleNamespace(item=None)
    app.playback = SimpleNamespace(
        has_pending_play=False,
        pause_intent_active=False,
        play_in_progress=False,
        play_state=SimpleNamespace(should_show_loading=False),
        stop_all=MagicMock(),
        is_item_playing=lambda item, now_playing: item.uri == now_playing.context_uri and now_playing.playing,
    )
    app.api = SimpleNamespace(pause=MagicMock(), set_repeat_context=MagicMock(return_value=True))
    app.volume = SimpleNamespace(unmute=MagicMock(), mute=MagicMock())
    app.renderer = SimpleNamespace(menu_button_rects={}, invalidate=MagicMock())
    app._focus_epoch = 0
    app._pending_focus_uri = None
    app._pending_focus_since = 0.0
    app._requested_focus_epoch = None
    app._requested_focus_uri = None
    app._requested_focus_since = 0.0
    app._user_driving = False
    app._manual_pause_lock = False
    app._manual_pause_context_uri = None
    app._context_switch_stall_since = 0.0
    app._last_context_watchdog_log = 0.0
    app._repeat_context_uri = None
    app._repeat_context_last_attempt = 0.0
    app._status_unknown = False
    app._connected_lock = threading.Lock()
    app._poll_wake_event = threading.Event()
    app._spotify_inactive = threading.Event()
    app._connected = True
    app._show_toast = MagicMock()
    app._now_playing_lock = threading.Lock()
    app._now_playing = now_playing
    return app


@pytest.fixture
def playback_app(monkeypatch):
    items = [_item('1', 'spotify:album:a', 'A'), _item('2', 'spotify:album:b', 'B')]
    app = _make_mello(items, NowPlaying())
    app.mock_mode = False
    app._startup_ready = True
    app._last_status_ok_at = 0.0
    app._status_failure_started_at = app._last_status_unknown_log = 0.0
    app._last_snap_pause_at = 0.0
    app._connection_fail_count = 0
    app._connection_grace_threshold = 3
    app._pressed_button = app._volume_hold_start = app._cover_collect_context = None
    app._check_touch_health = MagicMock()
    app._update_temp_item = MagicMock()
    app.carousel.update = MagicMock()
    app.touch.check_long_press = lambda: False
    app.play_timer.cancel = MagicMock()
    app.setup_menu = SimpleNamespace(
        state=MenuState.CLOSED, is_open=False, update=MagicMock(),
        handle_press=MagicMock(), handle_release=MagicMock(),
    )
    app.sleep_manager = SimpleNamespace(is_sleeping=False, check_sleep=MagicMock())
    app.tracker = MagicMock()
    app.auto_pause = MagicMock()
    app.bluetooth = MagicMock()
    app.volume.ensure_spotify_at_100 = MagicMock()
    app.api = LibrespotAPI('http://localhost:3678')
    app.api.session.post = MagicMock(return_value=MagicMock(status_code=200, ok=True))
    app.api.session.get = MagicMock(return_value=MagicMock(status_code=200))
    app.events = EventListener(
        'ws://localhost:3678/events', app._on_ws_update, on_inactive=app._on_spotify_inactive,
    )
    app.playback = PlaybackController(
        app.api, MagicMock(get_progress=MagicMock(return_value=None)), app.volume,
        is_request_current=app._is_play_request_current,
        on_play_committed=app._on_play_committed,
        on_play_failed=app._on_play_failed,
    )

    def run_inline(fn, *args):
        fn(*args)

    monkeypatch.setattr('mello.app.run_async', run_inline)
    monkeypatch.setattr('mello.controllers.playback.run_async', run_inline)
    return app


def _report_status(app, raw=None):
    response = app.api.session.get.return_value
    response.status_code = 204 if raw is None else 200
    response.json.return_value = raw
    app._refresh_status()


def _play_posts(app):
    return [
        request.kwargs['json']['uri']
        for request in app.api.session.post.call_args_list
        if request.args[0].endswith('/player/play')
    ]


def test_periodic_diagnostics_run_after_playback_handoff_changes(playback_app, caplog):
    playback_app._last_fps_log = 0
    playback_app._fps_log_interval = 30
    playback_app.perf_monitor = SimpleNamespace(current_fps=10)

    with caplog.at_level('INFO'):
        playback_app._log_fps_if_due(10)

    assert 'fps=10/10' in caplog.text


@pytest.mark.parametrize('started_on_mello', [True, False])
@pytest.mark.parametrize('inactive_event_delivered', [True, False])
def test_transfer_to_computer_does_not_start_mello_again(
    playback_app, monkeypatch, started_on_mello, inactive_event_delivered,
):
    app = playback_app
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    stopped = {'stopped': True, 'paused': False, 'track': None}
    _report_status(app, stopped)
    if started_on_mello:
        app._toggle_play()
    else:
        # Closing a settings panel is not a request to take over Spotify.
        app.setup_menu.is_open = True
        app._handle_touch_down((0, 0))
        app.setup_menu.is_open = False

    for _ in range(3):
        app.events._on_message(None, '{"type":"playing","data":{"context_uri":"spotify:album:a"}}')
        _report_status(app, {'stopped': False, 'paused': False, 'repeat_context': True,
                             'track': {'uri': 'spotify:track:a'}})
        app._update(0.016)

        if inactive_event_delivered:
            app.events._on_message(None, '{"type":"inactive"}')
        app._apply_spotify_inactive()
        _report_status(app)  # The engine logs out during transfer.
        app._update(0.016)
        clock.now += 1.1
        _report_status(app, stopped)  # Stored credentials restore an idle session.
        app._update(0.016)
        clock.now += 1.1

    assert _play_posts(app) == (['spotify:album:a'] if started_on_mello else [])
    assert app.now_playing.context_uri is None


def test_swiping_still_mutes_immediately_and_plays_once_when_settled(playback_app, monkeypatch):
    app = playback_app
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    _report_status(app, {'stopped': False, 'paused': False, 'context_uri': 'spotify:album:a',
                         'repeat_context': True, 'track': {'uri': 'spotify:track:a'}})

    app._snap_to(1)

    app.volume.mute.assert_called_once()
    assert _play_posts(app) == []
    app._update(0.016)
    clock.now += 1.1
    app._update(0.016)
    assert _play_posts(app) == ['spotify:album:b']

    # A missing status confirmation must not cause a second play request.
    for _ in range(3):
        clock.now += 13
        _report_status(app, {'stopped': False, 'paused': True, 'context_uri': 'spotify:album:a',
                             'track': {'uri': 'spotify:track:a'}})
        app._update(0.016)
    assert _play_posts(app) == ['spotify:album:b']


@pytest.mark.parametrize('inactive_event_delivered', [True, False])
def test_transfer_cancels_swipe_but_new_play_can_take_over(
    playback_app, monkeypatch, inactive_event_delivered,
):
    app = playback_app
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    _report_status(app, {'stopped': False, 'paused': False, 'context_uri': 'spotify:album:a',
                         'repeat_context': True, 'track': {'uri': 'spotify:track:a'}})
    app._snap_to(1)
    app._update(0.016)

    if inactive_event_delivered:
        app.events._on_message(None, '{"type":"inactive"}')
    clock.now += 2
    _report_status(app, {'stopped': True, 'paused': False, 'track': None})
    app._apply_spotify_inactive()
    app._update(0.016)
    assert _play_posts(app) == []
    assert not app.playback.has_pending_play

    app._toggle_play()
    assert _play_posts(app) == ['spotify:album:b']


def test_stale_poll_cannot_restore_playing_after_inactive(playback_app):
    app = playback_app
    playing = {'stopped': False, 'paused': False, 'context_uri': 'spotify:album:a',
               'repeat_context': True, 'track': {'uri': 'spotify:track:a'}}
    _report_status(app, playing)
    assert app.now_playing.playing

    def late_response(*args, **kwargs):
        app.events._on_message(None, '{"type":"inactive"}')
        return MagicMock(status_code=200, json=MagicMock(return_value=playing))

    app.api.session.get.side_effect = late_response
    app._refresh_status()

    assert app.now_playing.stopped
    assert app.now_playing.context_uri is None


def test_playing_another_album_from_spotify_keeps_saved_progress(playback_app, temp_dir):
    app = playback_app
    catalog = CatalogManager(temp_dir / 'catalog.json', temp_dir / 'images',
                             progress_path=temp_dir / 'progress.json')
    app.playback.catalog_manager = catalog
    _report_status(app, {'stopped': False, 'paused': False, 'context_uri': 'spotify:album:a',
                         'repeat_context': True, 'track': {'uri': 'spotify:track:a', 'position': 50000}})
    app._update(0.016)
    saved_progress = catalog.get_progress('spotify:album:a')
    assert saved_progress['position'] == 50000

    _report_status(app, {'stopped': False, 'paused': False, 'context_uri': 'spotify:album:b',
                         'repeat_context': True, 'track': {'uri': 'spotify:track:b'}})
    app._update(0.016)

    assert catalog.get_progress('spotify:album:a') == saved_progress


class TestRemoteFocusSync:
    """Guards and behavior for remote Spotify context focus sync."""

    def test_sync_moves_focus_when_remote_context_is_playing(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:b'))

        app._sync_to_playing()

        assert app.selected_index == 1
        app.carousel.set_target.assert_called_once_with(1)
        app.renderer.invalidate.assert_called_once()
        assert app._focus_epoch == 1


class TestRepeatContextSync:
    """Remote Spotify playback should also get repeat-context protection."""

    def test_external_album_playback_enables_repeat_context(self):
        app = _make_mello(
            [_item('1', 'spotify:album:a', 'A')],
            NowPlaying(playing=True, context_uri='spotify:album:a', repeat_context=False),
        )

        with patch('mello.app.run_async') as mock_run:
            mock_run.side_effect = lambda fn, *a: fn(*a)
            app._ensure_repeat_context_for_current_status()

        app.api.set_repeat_context.assert_called_once_with(True)
        assert app._repeat_context_uri == 'spotify:album:a'

    def test_external_album_repeat_true_does_not_call_api(self):
        app = _make_mello(
            [_item('1', 'spotify:album:a', 'A')],
            NowPlaying(playing=True, context_uri='spotify:album:a', repeat_context=True),
        )

        app._ensure_repeat_context_for_current_status()

        app.api.set_repeat_context.assert_not_called()
        assert app._repeat_context_uri == 'spotify:album:a'

    def test_external_track_playback_does_not_enable_repeat_context(self):
        app = _make_mello(
            [_item('1', 'spotify:track:a', 'A')],
            NowPlaying(playing=True, context_uri='spotify:track:a', repeat_context=False),
        )

        app._ensure_repeat_context_for_current_status()

        app.api.set_repeat_context.assert_not_called()

    def test_sync_defers_while_user_intent_is_active(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._user_driving = True

        app._sync_to_playing()

        assert app.selected_index == 0
        app.carousel.set_target.assert_not_called()

    def test_sync_applies_pending_remote_focus_after_user_intent_clears(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._user_driving = True

        app._sync_to_playing()
        assert app.selected_index == 0

        app._user_driving = False
        app._sync_to_playing()
        assert app.selected_index == 1

    def test_sync_uses_latest_spotify_context_after_a_deferred_change(self):
        items = [_item('1', 'spotify:album:a', 'A'), _item('2', 'spotify:album:b', 'B')]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:unsaved'))
        app._sync_to_playing()
        app.now_playing = NowPlaying(playing=True, context_uri='spotify:album:b')

        app._sync_to_playing()

        assert app.selected_index == 1

    def test_sync_does_not_move_focus_when_spotify_not_playing(self):
        items = [
            _item('1', 'spotify:album:a', 'A'),
            _item('2', 'spotify:album:b', 'B'),
        ]
        app = _make_mello(items, NowPlaying(playing=False, paused=True, context_uri='spotify:album:b'))

        app._sync_to_playing()

        assert app.selected_index == 0
        app.carousel.set_target.assert_not_called()

    def test_sync_waits_until_the_playing_cover_is_available(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:external'))

        app._sync_to_playing()

        assert app.selected_index == 0
        app.carousel.set_target.assert_not_called()

        items.append(_item('2', 'spotify:album:external', 'External'))
        app._sync_to_playing()

        assert app.selected_index == 1

    def test_sync_does_not_unmute_when_pause_intent_is_active(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:a'))
        app.playback.pause_intent_active = True

        app._sync_to_playing()

        app.volume.unmute.assert_not_called()

    def test_sync_does_not_unmute_when_manual_pause_lock_is_active(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_mello(items, NowPlaying(playing=True, context_uri='spotify:album:a'))
        app._manual_pause_lock = True

        app._sync_to_playing()

        app.volume.unmute.assert_not_called()


class TestContextSwitchWatchdog:
    """Safety net for stuck context-switch mute/loading states."""

    def test_watchdog_does_not_trigger_before_timeout(self):
        focused = _item('1', 'spotify:album:a', 'A')
        app = _make_mello([focused], NowPlaying(playing=True, context_uri='spotify:album:b'))
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
        app = _make_mello([focused], NowPlaying(playing=True, context_uri='spotify:album:b'))
        app._pending_focus_uri = focused.uri
        app._requested_focus_epoch = app._focus_epoch
        app._requested_focus_uri = focused.uri
        app._requested_focus_since = time.time() - (CONTEXT_SWITCH_WATCHDOG_TIMEOUT + 2.0)
        app._context_switch_stall_since = time.time() - (CONTEXT_SWITCH_WATCHDOG_TIMEOUT + 1.0)
        app._user_driving = True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr('mello.app.run_async', lambda fn, *a: fn(*a))
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
        app = _make_mello([focused], NowPlaying(playing=False, paused=True, context_uri='spotify:album:a'))
        app._context_switch_stall_since = time.time() - 10.0
        app._pending_focus_uri = None
        app._requested_focus_uri = None
        app._user_driving = False

        app._check_context_switch_watchdog(focused)

        assert app._context_switch_stall_since == 0.0


class TestPausedSameFocusContext:
    """Paused on the focused album must not trigger focus-dwell auto-play (auto-pause / manual pause)."""

    def test_true_when_paused_and_uri_matches(self):
        uri = 'spotify:album:a'
        items = [_item('1', uri, 'A')]
        app = _make_mello(
            items,
            NowPlaying(playing=False, paused=True, stopped=False, context_uri=uri),
        )
        assert app._is_paused_same_focus_context(items[0]) is True

    def test_false_when_playing(self):
        uri = 'spotify:album:a'
        items = [_item('1', uri, 'A')]
        app = _make_mello(
            items,
            NowPlaying(playing=True, paused=False, context_uri=uri),
        )
        assert app._is_paused_same_focus_context(items[0]) is False

    def test_false_when_different_context(self):
        items = [_item('1', 'spotify:album:a', 'A')]
        app = _make_mello(
            items,
            NowPlaying(playing=False, paused=True, stopped=False, context_uri='spotify:album:b'),
        )
        assert app._is_paused_same_focus_context(items[0]) is False


def test_new_player_that_was_never_chosen_in_spotify_is_noticed(tmp_path, monkeypatch):
    # Right after switching, Spotify's player knows no account until someone
    # plays to Mello once. A restart after that must not look "unpaired".
    import mello.app as app_module
    monkeypatch.setattr(app_module, 'SPOTIFY_ENGINE', 'soloist')
    monkeypatch.setattr(app_module, 'SOLOIST_DATA_DIR', tmp_path)
    app = SimpleNamespace(mock_mode=False, api=SimpleNamespace(logged_in=False),
                          _pairing_known=True, _pairing_checked_at=0.0)

    assert Mello._spotify_needs_pairing(app) is True         # no saved login at all

    users = tmp_path / 'cache' / 'Users'
    users.mkdir(parents=True)
    (users / 'someone').mkdir()
    app._pairing_checked_at = 0.0
    assert Mello._spotify_needs_pairing(app) is False        # still logging in after a restart

    app.api.logged_in = True
    assert Mello._spotify_needs_pairing(app) is False

    monkeypatch.setattr(app_module, 'SPOTIFY_ENGINE', 'librespot')
    app.api.logged_in = False
    assert Mello._spotify_needs_pairing(app) is False        # the old player has no such step

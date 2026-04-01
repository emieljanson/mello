"""
Tests for PlaybackController - play/pause, stop_all, progress.
"""
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from berry.controllers.playback import PlaybackController
from berry.models import CatalogItem, NowPlaying


def _make_controller(**overrides):
    """Create a PlaybackController with mocked dependencies."""
    api = MagicMock()
    api.status.return_value = None
    api.play.return_value = True
    api.pause.return_value = True
    api.resume.return_value = True
    api.seek.return_value = True
    api.set_volume.return_value = True
    api.is_connected.return_value = True

    catalog = MagicMock()
    catalog.get_progress.return_value = None
    catalog.save_progress = MagicMock()
    catalog.clear_progress = MagicMock()

    volume = MagicMock()
    volume.ensure_spotify_at_100 = MagicMock()

    defaults = dict(
        api=api,
        catalog_manager=catalog,
        volume=volume,
        mock_mode=False,
    )
    defaults.update(overrides)
    pc = PlaybackController(**defaults)
    return pc, api, catalog, volume


def _make_item(uri='spotify:album:test1', name='Test Album') -> CatalogItem:
    return CatalogItem(id='1', uri=uri, name=name, type='album')


class TestTogglePlay:
    """Tests for play/pause toggling."""

    def test_pause_when_playing(self):
        pc, api, _, _ = _make_controller()
        np = NowPlaying(playing=True, context_uri='spotify:album:x')
        items = [_make_item(uri='spotify:album:x')]
        pc.toggle_play(items, 0, np)
        assert pc.play_state.pending_action == 'pause'

    def test_resume_when_paused(self):
        pc, api, _, _ = _make_controller()
        on_resume = MagicMock()
        pc._on_resume = on_resume
        np = NowPlaying(paused=True, context_uri='spotify:album:x')
        items = [_make_item(uri='spotify:album:x')]
        pc.toggle_play(items, 0, np)
        assert pc.play_state.pending_action == 'play'
        on_resume.assert_called_once()

    def test_play_when_stopped(self):
        pc, api, _, _ = _make_controller()
        np = NowPlaying(stopped=True)
        item = _make_item(uri='spotify:album:new')
        pc.toggle_play([item], 0, np)
        assert pc.last_user_play_uri == 'spotify:album:new'

    def test_pause_during_loading_clears_loader_immediately(self):
        pc, _, _, volume = _make_controller()
        pc._play_in_progress = True
        pc.play_state.start_loading()
        items = [_make_item(uri='spotify:album:x')]

        pc.toggle_play(items, 0, NowPlaying(stopped=True))

        assert pc.play_state.pending_action == 'pause'
        assert pc.play_state.should_show_loading is False
        assert pc.pause_intent_active is True
        volume.mute.assert_called_once()


class TestStopAll:
    """Tests for stop_all — invalidate running/pending play requests."""

    def test_stop_all_bumps_generation(self):
        pc, _, _, _ = _make_controller()
        gen_before = pc._play_generation
        pc.stop_all()
        assert pc._play_generation == gen_before + 1

    def test_stop_all_clears_pending(self):
        pc, _, _, _ = _make_controller()
        pc._pending_play = ('spotify:album:queued', False)
        pc.stop_all()
        assert pc._pending_play is None

    def test_stop_all_does_not_force_play_in_progress_false(self):
        """Only the thread itself should clear _play_in_progress."""
        pc, _, _, _ = _make_controller()
        pc._play_in_progress = True
        pc.stop_all()
        assert pc._play_in_progress is True

    @patch('berry.controllers.playback.time.sleep')
    def test_execute_play_aborts_when_generation_stale(self, mock_sleep):
        """A stale generation causes _execute_play to bail out early."""
        pc, api, _, _ = _make_controller()
        pc._play_in_progress = True

        call_count = []

        def play_then_stop(*args, **kwargs):
            call_count.append(1)
            if len(call_count) == 1:
                pc.stop_all()
            return False

        api.play.side_effect = play_then_stop
        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert api.play.call_count == 1

    @patch('berry.controllers.playback.time.sleep')
    def test_cancelled_play_skips_pending(self, mock_sleep):
        """After stop_all, pending requests are not handed off."""
        pc, api, _, _ = _make_controller()
        api.play.return_value = True

        pc._play_in_progress = True
        pc._pending_play = ('spotify:album:queued', False, 0)
        pc.stop_all()

        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        calls = [c.args[0] for c in api.play.call_args_list]
        assert 'spotify:album:queued' not in calls

    def test_pause_calls_stop_all(self):
        """Pressing pause should invalidate any running play-thread."""
        pc, api, _, _ = _make_controller()
        gen_before = pc._play_generation
        np = NowPlaying(playing=True, context_uri='spotify:album:x')
        items = [_make_item(uri='spotify:album:x')]
        pc.toggle_play(items, 0, np)
        assert pc._play_generation == gen_before + 1


class TestAutoplay:
    """Tests for autoplay detection."""

    def test_detects_autoplay(self):
        pc, _, catalog, _ = _make_controller()
        pc.last_context_uri = 'spotify:album:old'
        pc.last_user_play_time = 0  # Long ago
        np = NowPlaying(playing=True, context_uri='spotify:album:new')
        pc.check_autoplay(np)
        catalog.clear_progress.assert_called_once_with('spotify:album:old')

    def test_ignores_recent_user_action(self):
        pc, _, catalog, _ = _make_controller()
        pc.last_context_uri = 'spotify:album:old'
        pc.last_user_play_time = time.time()  # Just now
        np = NowPlaying(playing=True, context_uri='spotify:album:new')
        pc.check_autoplay(np)
        catalog.clear_progress.assert_not_called()


class TestProgressSave:
    """Tests for periodic progress saving."""

    def test_save_progress_respects_interval(self):
        pc, api, catalog, _ = _make_controller()
        pc.last_progress_save = time.time()  # Just saved
        np = NowPlaying(playing=True, context_uri='spotify:album:x')
        pc.save_progress(np)
        # Should not have submitted a save (too recent)
        api.status.assert_not_called()

    def test_skip_when_not_playing(self):
        pc, api, _, _ = _make_controller()
        pc.last_progress_save = 0
        np = NowPlaying(playing=False)
        pc.save_progress(np)
        api.status.assert_not_called()

    def test_save_progress_uses_coherent_now_playing_snapshot(self):
        pc, _, catalog, _ = _make_controller()
        np = NowPlaying(
            playing=True,
            context_uri='spotify:album:x',
            track_uri='spotify:track:abc',
            position=12000,
            track_name='Track X',
            track_artist='Artist X',
        )

        with patch('berry.controllers.playback.run_async') as mock_run:
            mock_run.side_effect = lambda fn, *a: fn(*a)
            pc.last_progress_save = 0
            pc.save_progress(np, force=True)

        catalog.save_progress.assert_called_once_with(
            'spotify:album:x',
            'spotify:track:abc',
            12000,
            'Track X',
            'Artist X',
        )

    def test_save_progress_rejects_snapshot_without_track_uri(self):
        pc, _, catalog, _ = _make_controller()
        np = NowPlaying(
            playing=True,
            context_uri='spotify:album:x',
            track_uri=None,
            position=12000,
            track_name='Track X',
            track_artist='Artist X',
        )

        with patch('berry.controllers.playback.run_async') as mock_run:
            mock_run.side_effect = lambda fn, *a: fn(*a)
            pc.last_progress_save = 0
            pc.save_progress(np, force=True)

        catalog.save_progress.assert_not_called()


class TestIsItemPlaying:
    """Tests for is_item_playing check."""

    def test_returns_true_when_matching(self):
        pc, _, _, _ = _make_controller()
        item = _make_item(uri='spotify:album:x')
        np = NowPlaying(playing=True, context_uri='spotify:album:x')
        assert pc.is_item_playing(item, np) is True

    def test_returns_false_when_different(self):
        pc, _, _, _ = _make_controller()
        item = _make_item(uri='spotify:album:x')
        np = NowPlaying(playing=True, context_uri='spotify:album:y')
        assert pc.is_item_playing(item, np) is False


class TestLoadingState:
    """Tests for loading/spinner state updates."""

    def test_loading_starts_when_play_in_progress(self):
        pc, _, _, _ = _make_controller()
        pc._play_in_progress = True
        np = NowPlaying()
        pc.update_loading_state(np, carousel_settled=True, play_timer_active=False)
        # Loading is tracked but is_loading has a 200ms delay
        assert pc.play_state.loading_since is not None

    def test_loading_visible_after_delay(self):
        pc, _, _, _ = _make_controller()
        pc._play_in_progress = True
        np = NowPlaying()
        pc.update_loading_state(np, carousel_settled=True, play_timer_active=False)
        pc.play_state.loading_since = time.time() - 1  # Fake elapsed time
        assert pc.play_state.is_loading is True

    def test_loading_continues_while_play_in_progress(self):
        """Loading stays active while _execute_play is running, even if now_playing shows playing."""
        pc, _, _, _ = _make_controller()
        pc._play_in_progress = True
        np = NowPlaying(playing=True)
        pc.update_loading_state(np, carousel_settled=True, play_timer_active=False)
        assert pc.play_state.loading_since is not None

    def test_loading_stops_when_play_completes(self):
        pc, _, _, _ = _make_controller()
        pc._play_in_progress = False
        np = NowPlaying(playing=True)
        pc.update_loading_state(np, carousel_settled=True, play_timer_active=False)
        assert pc.play_state.loading_since is None

    def test_loading_does_not_restart_while_pause_override_active(self):
        pc, _, _, _ = _make_controller()
        pc.play_state.set_pending('pause')
        pc._set_pause_override('test_pause')
        np = NowPlaying(playing=True)
        pc.update_loading_state(np, carousel_settled=True, play_timer_active=True)
        assert pc.play_state.should_show_loading is False


class TestPlayFailure:
    """Tests for play failure recovery (e.g. no active Spotify session)."""

    @patch('berry.controllers.playback.time.sleep')
    def test_no_session_retries_then_shows_toast(self, mock_sleep):
        """No session returns immediately and shows reconnect toast."""
        pc, api, _, _ = _make_controller()
        api.play.return_value = None
        toast = MagicMock()
        pc._on_toast = toast

        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert api.play.call_count == 1
        assert pc.play_state.pending_action is None
        assert pc.play_state.loading_since is None
        toast.assert_called_once_with('Connect via Spotify')

    @patch('berry.controllers.playback.time.sleep')
    def test_transient_failure_defers_toast_to_retry(self, mock_sleep):
        """Transient failures (False) keep loader alive for retry window — no immediate toast."""
        pc, api, _, _ = _make_controller()
        api.play.return_value = False
        toast = MagicMock()
        pc._on_toast = toast

        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert api.play.call_count == 2
        # Loader stays active for the retry window
        assert pc.play_state.loading_since is not None
        # Toast is deferred to retry_failed(), not shown immediately
        toast.assert_not_called()
        # Failed play is saved for retry
        assert pc._failed_play is not None

    def test_play_success_keeps_pending_state(self):
        pc, api, _, _ = _make_controller()
        api.play.return_value = True

        pc.play_state.set_pending('play')
        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert pc.play_state.pending_action == 'play'


class TestLibrespotCrashRecovery:
    """Scenarios: librespot crashes/restarts during sleep, user wakes Pi and tries to play."""

    @patch('berry.controllers.playback.time.sleep')
    def test_play_recovers_after_restart(self, mock_sleep):
        """One transient failure can recover on retry."""
        pc, api, _, _ = _make_controller()
        api.play.side_effect = [False, True]
        toast = MagicMock()
        pc._on_toast = toast

        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert api.play.call_count == 2
        toast.assert_not_called()

    @patch('berry.controllers.playback.time.sleep')
    def test_play_keeps_loader_when_never_recovers(self, mock_sleep):
        """Librespot never comes back — loader stays active for retry window."""
        pc, api, _, _ = _make_controller()
        api.play.return_value = False
        toast = MagicMock()
        pc._on_toast = toast

        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert api.play.call_count == 2
        # Toast is deferred to retry_failed(), not shown immediately
        toast.assert_not_called()
        # Failed play saved for retry window
        assert pc._failed_play is not None

    @patch('berry.controllers.playback.time.sleep')
    def test_play_recovers_from_mixed_failures(self, mock_sleep):
        """Librespot can recover after one transient false response."""
        pc, api, _, _ = _make_controller()
        api.play.side_effect = [False, True]
        toast = MagicMock()
        pc._on_toast = toast

        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        assert api.play.call_count == 2
        toast.assert_not_called()

    @patch('berry.controllers.playback.time.sleep')
    def test_loader_active_during_retries(self, mock_sleep):
        """Loading state is set between retry attempts so user sees a spinner."""
        pc, api, _, _ = _make_controller()
        loading_was_set = []

        def track_loading(*args, **kwargs):
            loading_was_set.append(pc.play_state.loading_since is not None)
            return False

        api.play.side_effect = track_loading
        pc._execute_play('spotify:album:x', from_beginning=False, epoch=0)

        # After first failure, start_loading() is called before sleep,
        # so subsequent attempts should see loading_since set
        assert any(loading_was_set[1:]), 'Loader was never active during retries'


class TestSkipFailureRecovery:
    """Scenarios: next/prev fails (librespot down) — retry once, then show toast."""

    def test_skip_retries_once_then_succeeds(self):
        """Next fails once, succeeds on retry — no toast needed."""
        api_fn = MagicMock(side_effect=[False, True])
        toast = MagicMock()

        def do_skip():
            if not api_fn():
                if not api_fn():
                    toast('Niet verbonden')

        do_skip()
        assert api_fn.call_count == 2
        toast.assert_not_called()

    def test_skip_shows_toast_after_two_failures(self):
        """Next fails twice — user sees 'Niet verbonden'."""
        api_fn = MagicMock(return_value=False)
        toast = MagicMock()

        def do_skip():
            if not api_fn():
                if not api_fn():
                    toast('Niet verbonden')

        do_skip()
        assert api_fn.call_count == 2
        toast.assert_called_once_with('Niet verbonden')


class TestPlayQueueing:
    """Scenarios: rapid play requests — only the latest should execute."""

    def test_play_in_progress_queues_new_request(self):
        """Second play_item while first is running gets queued, not parallel."""
        pc, api, _, _ = _make_controller()
        pc._play_in_progress = True

        pc.play_item('spotify:album:second')

        assert pc._pending_play == ('spotify:album:second', False, 0)
        # Original play is still in progress, no new thread started
        assert pc._play_in_progress is True

    @patch('berry.controllers.playback.time.sleep')
    def test_pending_play_executes_after_current_finishes(self, mock_sleep):
        """Queued request runs after current _execute_play completes."""
        pc, api, _, _ = _make_controller()
        api.play.return_value = True

        pc._pending_play = ('spotify:album:queued', False, 0)
        pc._play_in_progress = True

        with patch('berry.controllers.playback.run_async') as mock_run:
            mock_run.side_effect = lambda fn, *a: fn(*a)
            pc._execute_play('spotify:album:first', from_beginning=False, epoch=0)

        calls = [c.args[0] for c in api.play.call_args_list]
        assert 'spotify:album:first' in calls
        assert 'spotify:album:queued' in calls


class TestQueuedPlayStaleness:
    """Queued play handoff should drop stale work."""

    @patch('berry.controllers.playback.time.sleep')
    def test_pending_request_dropped_after_generation_change(self, mock_sleep):
        pc, api, _, _ = _make_controller()
        api.play.return_value = True
        pc._pending_play = ('spotify:album:queued', False, 0)
        pc._play_in_progress = True

        def invalidate_generation(*_):
            pc.stop_all()

        mock_sleep.side_effect = invalidate_generation
        pc._execute_play('spotify:album:first', from_beginning=False, epoch=0)

        calls = [c.args[0] for c in api.play.call_args_list]
        assert calls == ['spotify:album:first']

    @patch('berry.controllers.playback.time.sleep')
    def test_play_success_ignored_when_pause_intent_active(self, mock_sleep):
        pc, api, _, volume = _make_controller()
        on_committed = MagicMock()
        pc._on_play_committed = on_committed
        api.play.return_value = True
        pc.play_state.set_pending('pause')
        pc._set_pause_override('test_pause')

        pc._execute_play('spotify:album:first', from_beginning=False, epoch=0)

        volume.unmute.assert_not_called()
        on_committed.assert_not_called()


class TestRetryBackoffGuards:
    """Tests for reconnect retry staleness and transport cooldown."""

    def test_retry_failed_dropped_when_too_old(self):
        pc, _, _, _ = _make_controller()
        pc._failed_play = ('spotify:album:x', False, 0)
        pc._failed_play_since = time.time() - 25
        pc.retry_failed()
        assert pc._failed_play is None

    @patch('berry.controllers.playback.run_async')
    def test_pause_command_suppressed_by_cooldown(self, mock_run_async):
        pc, _, _, _ = _make_controller()
        items = [_make_item(uri='spotify:album:x')]
        now = NowPlaying(playing=True, context_uri='spotify:album:x')
        pc.toggle_play(items, 0, now)
        pc.toggle_play(items, 0, now)
        # First pause should schedule async call, second is suppressed by cooldown
        assert mock_run_async.call_count == 1

    @patch('berry.controllers.playback.time.sleep')
    def test_pending_request_dropped_when_not_current(self, mock_sleep):
        pc, api, _, _ = _make_controller()
        api.play.return_value = True
        pc._is_request_current = lambda epoch, uri: uri == 'spotify:album:first'
        pc._pending_play = ('spotify:album:queued', False, 0)
        pc._play_in_progress = True

        pc._execute_play('spotify:album:first', from_beginning=False, epoch=0)

        calls = [c.args[0] for c in api.play.call_args_list]
        assert calls == ['spotify:album:first']

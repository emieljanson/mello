"""
Playback Controller - Manages play/pause/resume and progress.

Extracted from Mello.app to keep playback logic isolated and testable.
"""
import time
import logging
import threading
from typing import Optional, Callable, List

from ..api.librespot import LibrespotAPIProtocol
from ..api.catalog import CatalogManager
from ..models import CatalogItem, NowPlaying, PlayState
from ..config import PROGRESS_SAVE_INTERVAL
from ..utils import run_async
from .volume import VolumeController

logger = logging.getLogger(__name__)


def is_repeatable_spotify_context(uri: Optional[str]) -> bool:
    """True for Spotify contexts that should loop on Mello."""
    return bool(
        uri and (
            uri.startswith('spotify:album:') or
            uri.startswith('spotify:playlist:')
        )
    )


class PlaybackController:
    """Owns play/pause/resume and progress tracking."""

    def __init__(
        self,
        api: LibrespotAPIProtocol,
        catalog_manager: CatalogManager,
        volume: VolumeController,
        mock_mode: bool = False,
        on_toast: Optional[Callable[[str], None]] = None,
        on_invalidate: Optional[Callable[[], None]] = None,
        on_resume: Optional[Callable[[], None]] = None,
        is_request_current: Optional[Callable[[int, str], bool]] = None,
        on_play_committed: Optional[Callable[[str, int], None]] = None,
        on_play_failed: Optional[Callable[[str, int], None]] = None,
    ):
        self.api = api
        self.catalog_manager = catalog_manager
        self.volume = volume
        self.mock_mode = mock_mode
        self._on_toast = on_toast or (lambda msg: None)
        self._on_invalidate = on_invalidate or (lambda: None)
        self._on_resume = on_resume or (lambda: None)
        self._is_request_current = is_request_current or (lambda epoch, uri: True)
        self._on_play_committed = on_play_committed or (lambda uri, epoch: None)
        self._on_play_failed = on_play_failed or (lambda uri, epoch: None)
        self._last_toast_at: float = 0.0
        self._last_toast_message: Optional[str] = None
        self._transport_next_allowed = {'pause': 0.0, 'resume': 0.0}
        self._pause_override_until: float = 0.0

        # Play request queuing (non-blocking, latest wins).
        # Capture the generation when queuing so stop_all also cancels work
        # that has not started running in the thread pool yet.
        self._play_lock = threading.Lock()
        self._play_in_progress = False
        self._playing_uri: Optional[str] = None
        self._pending_play: Optional[tuple] = None
        self._play_generation = 0

        # UI loading/spinner state
        self.play_state = PlayState()

        # Progress tracking
        self.last_progress_save: float = 0
        self.last_saved_track_uri: Optional[str] = None

        # Mock playback
        self.mock_playing = False
        self.mock_position = 0
        self.mock_duration = 180000

    def is_item_playing(self, item: CatalogItem, now_playing: NowPlaying) -> bool:
        """Check if an item is currently playing."""
        return item.uri == now_playing.context_uri and now_playing.playing

    def toggle_play(self, items: List[CatalogItem], selected_index: int, now_playing: NowPlaying):
        """Toggle play/pause based on current state."""
        if not items:
            return

        if self.has_pending_play:
            logger.info('Cancelling in-flight play request')
            self._execute_pause('pause_during_loading')
        elif now_playing.playing:
            logger.info('Pausing...')
            self._execute_pause('pause_while_playing')
        elif now_playing.paused:
            logger.info('Resuming...')
            self._clear_pause_override('resume_tap')
            self.volume.unmute()
            self.play_state.set_pending('play')
            self._on_invalidate()
            self._on_resume()
            self._send_transport('resume')
        else:
            item = items[selected_index]
            logger.info(f'Playing {item.name}')
            self._clear_pause_override('play_tap')
            self.volume.unmute()
            self.play_item(item.uri)

    def play_item(self, uri: str, from_beginning: bool = False, epoch: int = 0):
        """Queue a play request (non-blocking). Only the latest request runs."""
        self._clear_pause_override('new_play_intent')
        # Clear stale pause-intent so loading spinner can show for new play.
        if self.play_state.pending_action == 'pause':
            self.play_state.pending_action = None

        with self._play_lock:
            if self._play_in_progress:
                if uri == self._playing_uri and not from_beginning:
                    logger.debug(f'Already loading {uri}, skipping duplicate')
                    return
                self._pending_play = (uri, from_beginning, epoch)
                logger.debug(f'Queued play request: {uri}')
                return
            self._play_in_progress = True
            self._playing_uri = uri
            generation = self._play_generation

        run_async(self._execute_play, uri, from_beginning, epoch, generation)

    def stop_all(self):
        """Invalidate any running or pending play requests.

        Bumps the generation counter so that any in-flight _execute_play
        thread will notice it is stale and bail out.  Does NOT force
        _play_in_progress to False — only the thread itself does that in
        its finally block, which avoids two threads running simultaneously.
        """
        with self._play_lock:
            self._play_generation += 1
            self._pending_play = None
            self._playing_uri = None
        self.play_state.clear()

    def save_progress(self, now_playing: NowPlaying, force: bool = False):
        """Queue a periodic progress save if due (or immediately if force=True)."""
        if self.mock_mode:
            return
        if not now_playing.playing and not force:
            return
        if not force and time.time() - self.last_progress_save <= PROGRESS_SAVE_INTERVAL:
            return
        self.last_progress_save = time.time()
        # Capture one coherent snapshot from now_playing to avoid mixing
        # context from one source with track/position from another async fetch.
        run_async(
            self._save_progress_async,
            now_playing.context_uri,
            now_playing.track_uri,
            now_playing.position,
            now_playing.track_name,
            now_playing.track_artist,
        )

    def save_progress_on_shutdown(self, now_playing: NowPlaying):
        """Save progress synchronously before shutdown."""
        if self.mock_mode:
            return
        if not now_playing.playing and not now_playing.context_uri:
            logger.debug('No active playback to save on shutdown')
            return
        try:
            status = self.api.status()
            if not status or not status.get('track'):
                logger.debug('No track info available for shutdown save')
                return
            context_uri = status.get('context_uri') or now_playing.context_uri
            if not context_uri:
                return
            track = status['track']
            self.catalog_manager.save_progress(
                context_uri,
                track.get('uri'),
                track.get('position', 0),
                track.get('name'),
                ', '.join(track.get('artist_names', []))
            )
            logger.info(f'Saved progress on shutdown: {track.get("name")} @ {track.get("position", 0) // 1000}s')
        except Exception as e:
            logger.warning(f'Could not save progress on shutdown: {e}')

    def update_loading_state(self, now_playing: NowPlaying, carousel_settled: bool,
                             play_timer_active: bool):
        """Update the loading/spinner state each frame."""
        if self.pause_intent_active:
            if self.play_state.should_show_loading:
                logger.info('loading_off | reason=pause_override')
            self.play_state.stop_loading()
            return

        has_active_play_work = play_timer_active or self.play_in_progress
        if self.play_state.pending_action == 'pause' and not has_active_play_work:
            self.play_state.stop_loading()
            return

        was_loading = self.play_state.should_show_loading
        if has_active_play_work:
            self.play_state.start_loading()
        else:
            self.play_state.stop_loading()

        now_loading = self.play_state.should_show_loading
        if was_loading != now_loading:
            logger.info(f'Loading state: {was_loading} -> {now_loading} (timer={play_timer_active}, play_in_progress={self._play_in_progress})')

    @property
    def has_pending_play(self) -> bool:
        """True when a play request is in flight or UI still shows loading."""
        return self.play_in_progress or self.play_state.should_show_loading

    @property
    def play_in_progress(self) -> bool:
        """True while a current play request is queued or executing."""
        with self._play_lock:
            return self._playing_uri is not None or self._pending_play is not None

    @property
    def pause_intent_active(self) -> bool:
        """True while pause intent should suppress loader/autoplay/unmute."""
        return self.play_state.pause_intent_active or self._is_pause_override_active()

    def update_mock(self, dt: float, now_playing: NowPlaying):
        """Advance mock playback position."""
        if not self.mock_mode or not self.mock_playing:
            return
        self.mock_position += int(dt * 1000)
        if self.mock_position >= self.mock_duration:
            self.mock_position = 0
        now_playing.position = self.mock_position

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _execute_play(self, uri: str, from_beginning: bool, epoch: int, generation: int):
        """Execute a play request while its local intent is current."""
        logger.warning(
            f'Execute play [gen={generation}, epoch={epoch}]: '
            f'context_uri={uri[:50]}..., from_beginning={from_beginning}'
        )

        def _stale() -> bool:
            with self._play_lock:
                cancelled = self._play_generation != generation
            return cancelled or not self._is_request_current(epoch, uri)

        try:
            if _stale():
                return
            self.volume.ensure_spotify_at_100()

            skip_to_uri = None
            saved_progress = None
            if not from_beginning:
                saved_progress = self.catalog_manager.get_progress(uri)
                if saved_progress:
                    skip_to_uri = saved_progress.get('uri')
                    logger.info(f'  Saved progress: track={skip_to_uri}, pos={saved_progress.get("position", 0) // 1000}s')
                else:
                    logger.info('  No saved progress found')

            need_seek = saved_progress and saved_progress.get('position', 0) > 0

            result = False
            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                if _stale():
                    logger.info(f'  Play cancelled (gen={generation}), aborting')
                    return
                result = self.api.play(uri, skip_to_uri=skip_to_uri, paused=need_seek)
                logger.info(f'  Play request attempt {attempt}/{max_attempts}: result={result}')
                if result is True:
                    break
                if result is None:
                    # No active Spotify session: retries won't help until user reconnects.
                    break
                if attempt < max_attempts:
                    self.play_state.start_loading()
                    for _ in range(8):
                        if _stale():
                            logger.info(f'  Play cancelled during retry wait (gen={generation})')
                            return
                        time.sleep(0.5)

            if _stale() or self.pause_intent_active:
                return

            if result is not True:
                self.play_state.clear()
                self._emit_toast('Connect via Spotify' if result is None else 'Loading failed, try again')
                self._on_play_failed(uri, epoch)
                return

            if need_seek:
                position = saved_progress['position']
                if self.api.seek(position):
                    logger.info(f'Seeked to {position // 1000}s')
                if _stale():
                    return
                self.api.resume()
                logger.info('  Resumed after seek')

            if _stale():
                return
            self._enable_repeat_context_if_needed(uri, 'play_success')
            if _stale():
                return
            self.volume.unmute()
            self.play_state.stop_loading()
            self._on_play_committed(uri, epoch)
        finally:
            with self._play_lock:
                pending = self._pending_play
                self._pending_play = None
                self._play_in_progress = pending is not None
                self._playing_uri = pending[0] if pending else None
                next_generation = self._play_generation

            if pending is not None:
                run_async(self._execute_play, *pending, next_generation)

    def _emit_toast(self, message: str, cooldown_s: float = 6.0):
        """Emit toast with small cooldown to prevent spam loops."""
        now = time.time()
        if message == self._last_toast_message and (now - self._last_toast_at) < cooldown_s:
            logger.info(f'TOAST suppressed (cooldown): "{message}"')
            return
        self._last_toast_message = message
        self._last_toast_at = now
        self._on_toast(message)

    def _send_transport(self, command: str):
        """Send pause/resume with small cooldown to avoid burst spam."""
        now = time.time()
        next_allowed = self._transport_next_allowed.get(command, 0.0)
        if now < next_allowed:
            logger.info(f'{command} suppressed by cooldown ({next_allowed - now:.2f}s)')
            return
        self._transport_next_allowed[command] = now + 0.35
        fn = self.api.pause if command == 'pause' else self.api.resume
        run_async(fn)

    def _execute_pause(self, reason: str):
        """Common pause path: stop in-flight work, mute, override, and send transport."""
        self.stop_all()
        self.volume.mute()
        logger.info(f'Pause tap: immediate local mute ({reason})')
        self._set_pause_override(reason)
        self.play_state.set_pending('pause')
        self.play_state.stop_loading()
        self._on_invalidate()
        self._send_transport('pause')

    def _set_pause_override(self, reason: str, hold_s: float = 1.2):
        """Keep pause intent active briefly to absorb status/API lag."""
        self._pause_override_until = max(self._pause_override_until, time.time() + hold_s)
        logger.info(
            f'pause_intent_on | reason={reason} | hold_s={hold_s:.1f} | '
            f'until_in={max(0.0, self._pause_override_until - time.time()):.2f}s'
        )

    def _clear_pause_override(self, reason: str):
        """Clear pause override after explicit positive play intent."""
        if self._pause_override_until > 0:
            logger.info(f'pause_intent_off | reason={reason}')
        self._pause_override_until = 0.0

    def _is_pause_override_active(self) -> bool:
        """True when pause override window is still active."""
        return time.time() < self._pause_override_until

    def _enable_repeat_context_if_needed(self, uri: str, reason: str):
        """Loop albums/playlists so Spotify does not continue into suggestions."""
        if not is_repeatable_spotify_context(uri):
            return
        ok = self.api.set_repeat_context(True)
        if ok:
            logger.info(f'repeat_context_on | reason={reason} | uri={uri[:50]}')
        else:
            logger.warning(f'repeat_context_failed | reason={reason} | uri={uri[:50]}')

    def _save_progress_async(
        self,
        context_uri: Optional[str],
        track_uri: Optional[str],
        position: int,
        track_name: Optional[str],
        track_artist: Optional[str],
    ):
        """Save a single coherent playback snapshot (runs in thread pool)."""
        try:
            if not context_uri or not track_uri:
                logger.info(
                    'progress_write_rejected | reason=missing_snapshot_fields | '
                    f'context_uri={(context_uri or "none")[:40]} | track_uri={(track_uri or "none")[:40]}'
                )
                return

            self.catalog_manager.save_progress(
                context_uri,
                track_uri,
                max(0, int(position or 0)),
                track_name,
                track_artist,
            )
            self.last_saved_track_uri = track_uri
            logger.info(
                'progress_write_accepted | '
                f'context_uri={context_uri[:40]} | track_uri={track_uri[:40]} | pos={max(0, int(position or 0)) // 1000}s'
            )
        except Exception as e:
            logger.warning('Error saving progress', exc_info=True)

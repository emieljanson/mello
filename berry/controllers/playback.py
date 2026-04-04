"""
Playback Controller - Manages play/pause/resume and progress.

Extracted from Berry.app to keep playback logic isolated and testable.
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
        # _play_generation is an incrementing counter; each _execute_play
        # thread captures its generation at start and bails out whenever
        # the current generation has moved on (i.e. stop_all was called).
        self._play_lock = threading.Lock()
        self._play_in_progress = False
        self._playing_uri: Optional[str] = None
        self._pending_play: Optional[tuple] = None
        self._play_generation = 0

        # UI loading/spinner state
        self.play_state = PlayState()

        # Track user-initiated plays (for autoplay detection)
        self.last_user_play_time: float = 0
        self.last_user_play_uri: Optional[str] = None

        # Failed play request to retry on reconnect
        self._failed_play: Optional[tuple] = None
        self._failed_play_since: float = 0.0

        # Progress tracking
        self.last_context_uri: Optional[str] = None
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

        if self._play_in_progress or self.play_state.should_show_loading:
            logger.info('Cancelling in-flight play request')
            self.stop_all()
            self.volume.mute()
            logger.info('Pause tap: immediate local mute (while play/loading)')
            self._set_pause_override('pause_during_loading')
            self.play_state.set_pending('pause')
            self.play_state.stop_loading()
            self._on_invalidate()
            self._send_transport('pause')
        elif now_playing.playing:
            logger.info('Pausing...')
            self.stop_all()
            self.volume.mute()
            logger.info('Pause tap: immediate local mute (while playing)')
            self._set_pause_override('pause_while_playing')
            self.play_state.set_pending('pause')
            self.play_state.stop_loading()
            self._on_invalidate()
            self._send_transport('pause')
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
        self.last_user_play_time = time.time()
        self.last_user_play_uri = uri
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

        run_async(self._execute_play, uri, from_beginning, epoch)

    def retry_failed(self):
        """Retry a previously failed play request (call on reconnect)."""
        failed = self._failed_play
        if not failed:
            return
        failed_age = time.time() - self._failed_play_since if self._failed_play_since else 0.0
        if failed_age > 20.0:
            logger.info(f'Dropping stale failed retry by age: {failed_age:.1f}s')
            self._failed_play = None
            self._failed_play_since = 0.0
            self._emit_toast('Loading failed, try again')
            logger.warning(
                'TOAST shown | message="Loading failed, try again" '
                f'| reason=retry_window_expired | age={failed_age:.1f}s'
            )
            return
        uri, from_beginning, epoch = failed
        if not self._is_request_current(epoch, uri):
            logger.info(f'Dropping stale failed retry: {uri[:50]}')
            self._failed_play = None
            self._failed_play_since = 0.0
            return
        self._failed_play = None
        self._failed_play_since = 0.0
        logger.info(f'Retrying failed play on reconnect: {uri[:50]}')
        self.play_item(uri, from_beginning, epoch)

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
        self._failed_play = None
        self._failed_play_since = 0.0
        self.play_state.clear()

    def check_autoplay(self, now_playing: NowPlaying):
        """Detect autoplay and clear progress when context finishes naturally."""
        new_context = now_playing.context_uri
        old_context = self.last_context_uri

        if (old_context and new_context and
                old_context != new_context and
                now_playing.playing):
            recent_user_action = time.time() - self.last_user_play_time < 5
            expected_context = new_context == self.last_user_play_uri
            if not recent_user_action and not expected_context:
                logger.info(f'Context finished: {old_context}')
                self.catalog_manager.clear_progress(old_context)

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

        has_active_play_work = play_timer_active or self._play_in_progress or self._failed_play is not None
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
        return self._play_in_progress or self.play_state.should_show_loading

    @property
    def play_in_progress(self) -> bool:
        """True when a play request thread is actively executing."""
        return self._play_in_progress

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

    def _execute_play(self, uri: str, from_beginning: bool, epoch: int):
        """Execute the play request (runs in thread pool).

        Captures _play_generation at start so it can bail out early when
        stop_all() has been called (generation moves on).
        """
        with self._play_lock:
            my_gen = self._play_generation

        logger.warning(
            f'Execute play [gen={my_gen}, epoch={epoch}]: '
            f'context_uri={uri[:50]}..., from_beginning={from_beginning}'
        )

        def _stale() -> bool:
            with self._play_lock:
                return self._play_generation != my_gen

        try:
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
            retry_delay = 3
            for attempt in range(1, max_attempts + 1):
                if _stale():
                    logger.info(f'  Play cancelled (gen={my_gen}), aborting')
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
                            logger.info(f'  Play cancelled during retry wait (gen={my_gen})')
                            return
                        time.sleep(0.5)

            if _stale():
                return

            success = result is True
            if not success:
                self._failed_play = (uri, from_beginning, epoch)
                self._failed_play_since = time.time()
                status_ctx = None
                status_playing = None
                try:
                    status = self.api.status()
                    if isinstance(status, dict):
                        status_ctx = status.get('context_uri')
                        status_playing = status.get('playing')
                except Exception:
                    pass
                logger.warning(
                    'Play failed, saved for retry: '
                    f'uri={uri[:50]} | epoch={epoch} | from_beginning={from_beginning} | '
                    f'status_ctx={(status_ctx or "none")[:40]} | status_playing={status_playing}'
                )
                if result is None:
                    # No active Spotify session: definitive failure, clear loader immediately.
                    self.play_state.clear()
                    self._emit_toast('Connect via Spotify')
                    logger.warning(
                        'TOAST shown | message="Connect via Spotify" '
                        f'| failed_uri={uri[:50]} | epoch={epoch}'
                    )
                else:
                    # Timeout/network error: keep loader on while retry window is open.
                    # Toast and loader-stop happen in retry_failed() if retry also fails.
                    logger.warning(
                        'Keeping loader alive for retry window '
                        f'| failed_uri={uri[:50]} | epoch={epoch}'
                    )
                self._on_play_failed(uri, epoch)

            if success and need_seek:
                position = saved_progress['position']
                if self.api.seek(position):
                    logger.info(f'Seeked to {position // 1000}s')
                self.api.resume()
                logger.info('  Resumed after seek')

            if success:
                if not self._is_request_current(epoch, uri):
                    logger.info(f'Play success ignored (stale epoch={epoch}): {uri[:50]}')
                    self.play_state.stop_loading()
                    return
                if self.pause_intent_active:
                    logger.info(
                        f'stale_play_dropped | reason=pause_intent_active | epoch={epoch} | uri={uri[:50]}'
                    )
                    self.play_state.stop_loading()
                    return
                self._failed_play = None
                self._failed_play_since = 0.0
                self.volume.unmute()
                self.play_state.stop_loading()
                self._on_play_committed(uri, epoch)
        finally:
            with self._play_lock:
                self._play_in_progress = False
                self._playing_uri = None
                stale = self._play_generation != my_gen
                pending = self._pending_play if not stale else None
                self._pending_play = None

            if pending:
                should_execute_pending = True
                time.sleep(0.5)
                with self._play_lock:
                    if self._play_generation != my_gen:
                        logger.debug('Dropping queued request after generation change')
                        should_execute_pending = False
                    if self._pending_play:
                        pending = self._pending_play
                        self._pending_play = None
                if should_execute_pending and not self._is_request_current(pending[2], pending[0]):
                    logger.debug(f'Dropping stale queued request: {pending[0][:50]}')
                    should_execute_pending = False
                if should_execute_pending and self.pause_intent_active:
                    logger.info(
                        f'stale_play_dropped | reason=pause_intent_active_queued | uri={pending[0][:50]}'
                    )
                    should_execute_pending = False
                if should_execute_pending:
                    logger.debug(f'Executing queued request: {pending[0]}')
                    self.play_item(pending[0], pending[1], pending[2])

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

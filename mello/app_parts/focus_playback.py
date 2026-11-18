"""
Focus and playback for the Mello app: which cover the user has chosen, when
that becomes a play request, play/pause/skip, the manual-pause lock and the
watchdog for a context switch that never happens.
"""
import logging
import time
from typing import Optional

from ..config import PLAY_TIMER_DELAY, CONTEXT_SWITCH_WATCHDOG_TIMEOUT
from ..models import CatalogItem, NowPlaying
from ..utils import run_async

logger = logging.getLogger('mello.app')


class FocusPlaybackMixin:
    """Chosen cover, play requests, play/pause/skip and the context-switch watchdog."""

    def _focus_last_played(self):
        """Open on the album that played last instead of the first one.

        After the nightly update or a power cut a child would otherwise land
        on album 1 and have to find their story again. It only moves the
        carousel: nothing starts playing until the child taps or swipes.
        """
        items = self.catalog_manager.items
        best, best_at = None, ''
        for index, item in enumerate(items):
            played_at = (item.current_track or {}).get('updatedAt') or ''
            if played_at > best_at:
                best, best_at = index, played_at
        if best is None:
            return
        self.selected_index = best
        self.carousel.target_index = best
        self.carousel.scroll_x = float(best)
        self.carousel.settled = True
        logger.info(
            f'Start on the last played album: "{items[best].name}" '
            f'({best + 1}/{len(items)}, played {best_at[:16]})'
        )
    
    def _bump_focus_epoch(self, reason: str):
        """Increment focus epoch so stale play responses can be ignored."""
        self._focus_epoch += 1
        self._reset_pending_focus(reason)
        self._clear_requested_focus()
        logger.info(f'Focus epoch -> {self._focus_epoch} ({reason})')

    def _clear_requested_focus(self):
        self._requested_focus_epoch = None
        self._requested_focus_uri = None
        self._requested_focus_since = 0.0

    def _current_focused_uri(self) -> Optional[str]:
        """Return currently focused URI, or None."""
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return None
        return items[self.selected_index].uri

    def _is_play_request_current(self, epoch: int, uri: str) -> bool:
        """True when play response still matches latest focus intent."""
        return (
            not self._spotify_inactive.is_set()
            and epoch == self._focus_epoch
            and uri == self._current_focused_uri()
        )

    def _has_active_user_focus_intent(self) -> bool:
        """True while user intent should block remote context focus sync."""
        requested_focus_active = (
            self._requested_focus_epoch == self._focus_epoch and
            self._requested_focus_uri is not None
        )
        return (
            self.touch.dragging
            or self._user_driving
            or self.play_timer.item is not None
            or requested_focus_active
        )

    def _focus_on_uri_without_interrupt(self, context_uri: str, reason: str) -> bool:
        """Move focus to context URI without interrupting playback."""
        items = self.display_items
        if not items:
            return False
        target_index = next((i for i, item in enumerate(items) if item.uri == context_uri), None)
        if target_index is None:
            return False
        if target_index == self.selected_index:
            return True

        old_index = self.selected_index
        self.selected_index = target_index
        self.carousel.set_target(target_index)
        self._bump_focus_epoch(f'{reason} {old_index}->{target_index}')
        self._user_driving = False
        self.renderer.invalidate()
        logger.info(
            'SYNC applied | remote focus moved '
            f'{old_index}->{target_index} | ctx={context_uri[:40]}'
        )
        return True

    def _set_manual_pause_lock(self, reason: str):
        """Block auto-play until explicit positive user intent."""
        self._manual_pause_lock = True
        self._manual_pause_context_uri = self.now_playing.context_uri
        logger.info(
            f'Manual pause lock set ({reason}) | '
            f'ctx={(self._manual_pause_context_uri or "none")[:40]}'
        )

    def _clear_manual_pause_lock(self, reason: str):
        """Allow auto-play again after explicit user intent."""
        if self._manual_pause_lock:
            logger.info(
                f'Manual pause lock cleared ({reason}) | '
                f'ctx={(self._manual_pause_context_uri or "none")[:40]}'
            )
        self._manual_pause_lock = False
        self._manual_pause_context_uri = None

    def _on_play_committed(self, uri: str, epoch: int):
        """Called by PlaybackController when a play request is accepted."""
        if not self._is_play_request_current(epoch, uri):
            return
        self._user_driving = False
        # Keep the marker for loading feedback until /status confirms playback.
        self._last_play_commit_uri = uri
        self._last_play_commit_at = time.time()
        # The wait for Spotify to confirm starts now, not when the request was
        # queued: reaching a late chapter can take longer than the whole
        # confirmation window, and ended in a false "Loading failed".
        self._requested_focus_since = self._last_play_commit_at
        self._poll_wake_event.set()
        logger.info(f'Play committed: uri={uri[:40]} epoch={epoch}')

    def _on_play_failed(self, uri: str, epoch: int):
        """Called by PlaybackController when play request failed."""
        if self._is_play_request_current(epoch, uri):
            self._clear_requested_focus()
            self._user_driving = False
            logger.warning(f'Play failed for current focus: uri={uri[:40]} epoch={epoch}')
        else:
            logger.info(f'Play failed for stale request: uri={uri[:40]} epoch={epoch}')

    def _reset_pending_focus(self, reason: str = ''):
        """Clear pending focus-stability request timer."""
        if self._pending_focus_uri and reason:
            logger.debug(
                f'Pending focus cleared | reason={reason} '
                f'| uri={self._pending_focus_uri[:40]}'
            )
        self._pending_focus_uri = None
        self._pending_focus_since = 0.0

    def _reset_context_switch_watchdog(self):
        """Clear context-switch watchdog timer."""
        self._context_switch_stall_since = 0.0

    def _trigger_context_switch_watchdog(self, focused_item: CatalogItem, stall_age: float):
        """Fail-safe when context-switch loading appears stuck for too long."""
        logger.error(
            'WATCHDOG tripped | context-switch stuck -> hard silent stop | '
            f'age={stall_age:.1f}s | focused="{focused_item.name}" | '
            f'focused_uri={focused_item.uri[:40]} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} | '
            f'connected={self.connected} | status_unknown={self._status_unknown} | '
            f'pending_focus={(self._pending_focus_uri or "none")[:40]} | '
            f'requested_uri={(self._requested_focus_uri or "none")[:40]} | '
            f'requested_epoch={self._requested_focus_epoch} | focus_epoch={self._focus_epoch}'
        )
        self.tracker.on_watchdog_tripped(stall_age)
        self.playback.stop_all()
        self._bump_focus_epoch('watchdog_trip')
        self._user_driving = False
        self.volume.mute()
        run_async(self.api.pause)
        self._show_toast('Loading cancelled, try again')

    def _check_context_switch_watchdog(self, focused_item: Optional[CatalogItem]):
        """Detect and break out of a stuck context-switch loading state."""
        if focused_item is None or focused_item.is_temp:
            self._reset_context_switch_watchdog()
            return

        focused_uri = focused_item.uri
        requested_current_focus = (
            self._requested_focus_epoch == self._focus_epoch
            and self._requested_focus_uri == focused_uri
        )
        waiting_for_switch_commit = (
            self.playback.play_in_progress
            or self.playback.play_state.should_show_loading
            or self._pending_focus_uri == focused_uri
            or requested_current_focus
        )
        context_mismatch = bool(
            self.now_playing.context_uri
            and self.now_playing.context_uri != focused_uri
        )
        stalled_switch = (
            not self._manual_pause_lock
            and not self._is_item_playing(focused_item)
            and (waiting_for_switch_commit or (self._user_driving and context_mismatch))
        )

        if not stalled_switch:
            self._reset_context_switch_watchdog()
            return

        now = time.time()
        if self._context_switch_stall_since <= 0.0:
            self._context_switch_stall_since = now
            return

        stall_age = now - self._context_switch_stall_since
        if stall_age >= CONTEXT_SWITCH_WATCHDOG_TIMEOUT:
            self._trigger_context_switch_watchdog(focused_item, stall_age)
            self._reset_context_switch_watchdog()
            return

        if now - self._last_context_watchdog_log > 5.0:
            logger.warning(
                'WATCHDOG armed | waiting for context-switch commit | '
                f'age={stall_age:.1f}s/{CONTEXT_SWITCH_WATCHDOG_TIMEOUT:.0f}s | '
                f'focused_uri={focused_uri[:40]} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} | '
                f'waiting_for_commit={waiting_for_switch_commit} | user_driving={self._user_driving}'
            )
            self._last_context_watchdog_log = now

    def _is_item_playing(self, item: CatalogItem) -> bool:
        """Check if an item is currently playing."""
        return self.playback.is_item_playing(item, self.now_playing)

    def _is_paused_same_focus_context(self, item: CatalogItem) -> bool:
        """Spotify is paused on the same context as carousel focus (do not auto-resume from dwell)."""
        return (
            item.uri == self.now_playing.context_uri
            and self.now_playing.paused
        )

    def _skip_track(self, api_fn):
        """Save progress, then skip prev/next."""
        self.playback.save_progress(self.now_playing, force=True)

        def _do_skip():
            if not api_fn():
                time.sleep(1)
                if not api_fn():
                    self._show_toast('Not connected')

        run_async(_do_skip)

    def _toggle_play(self):
        """Toggle play/pause."""
        if self.mock_mode:
            self._toggle_mock_play()
            return
        items = self.display_items
        now_playing = self.now_playing
        if now_playing.playing or self.playback.has_pending_play:
            if now_playing.playing:
                # Remember the exact spot: play after pause resumes from saved
                # progress, which is otherwise up to 10s old.
                self.playback.save_progress(now_playing, force=True)
            self._set_manual_pause_lock('pause_tap')
            self._bump_focus_epoch('pause_tap')
            self._user_driving = False
        else:
            self._clear_manual_pause_lock('play_tap')
            if self.selected_index < len(items):
                item = items[self.selected_index]
                if not now_playing.paused or not item.is_temp:
                    self._play_item(item.uri)
                    return
        self.playback.toggle_play(items, self.selected_index, now_playing)
    
    def _toggle_mock_play(self):
        """Toggle mock playback (no real API calls)."""
        items = self.display_items
        self.playback.mock_playing = not self.playback.mock_playing
        if self.playback.mock_playing and items:
            item = items[self.selected_index]
            ct = item.current_track if isinstance(item.current_track, dict) else None
            self.now_playing = NowPlaying(
                playing=True,
                context_uri=item.uri,
                track_uri=ct.get('uri') if ct else None,
                track_name=ct.get('name', item.name) if ct else item.name,
                track_artist=ct.get('artist', item.artist) if ct else item.artist,
                position=self.playback.mock_position,
                duration=self.playback.mock_duration,
            )
        else:
            self.now_playing = NowPlaying(paused=True, context_uri=self.now_playing.context_uri)
    
    def _play_item(self, uri: str, from_beginning: bool = False):
        """Queue a play request via the playback controller."""
        logger.warning(f'PLAY enqueue | uri={uri[:40]} | epoch={self._focus_epoch} | from_beginning={from_beginning}')
        self._reset_pending_focus('play_enqueued')
        self._user_driving = True
        self._requested_focus_epoch = self._focus_epoch
        self._requested_focus_uri = uri
        self._requested_focus_since = time.time()
        self.playback.play_state.start_loading()
        now_playing = self.now_playing
        if now_playing.context_uri and now_playing.context_uri != uri:
            self.playback.save_progress(now_playing, force=True)
        self.playback.play_item(uri, from_beginning, self._focus_epoch)
    
    def _update_focus_playback(self, item: Optional[CatalogItem]):
        """Play a settled local selection once, then wait for status confirmation."""
        if self._spotify_inactive.is_set() or item is None:
            return

        now = time.time()
        status_ready = not self._status_unknown and now - self._last_status_ok_at < 4.0
        if self._requested_focus_uri == item.uri:
            if self.playback.play_in_progress:
                return
            if status_ready and self._is_item_playing(item):
                self._clear_requested_focus()
            elif now - self._requested_focus_since >= 12.0:
                self._clear_requested_focus()
                logger.warning(f'PLAY confirmation timed out | uri={item.uri[:40]}')
                self._show_toast('Loading failed, try again')
            return

        if self._pending_focus_uri != item.uri:
            return
        if not (
            self._startup_ready and self.connected and status_ready
            and self.carousel.settled and not self.touch.dragging
            and not self._manual_pause_lock
            and not self.playback.play_in_progress and not self.playback.pause_intent_active
        ):
            self._pending_focus_since = 0.0
            return

        if self._is_item_playing(item) or self._is_paused_same_focus_context(item):
            self._reset_pending_focus('already_on_focused_context')
            self._user_driving = False
        elif self._pending_focus_since == 0.0:
            self._pending_focus_since = now
        elif now - self._pending_focus_since >= PLAY_TIMER_DELAY:
            self._play_item(item.uri)

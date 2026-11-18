"""
Keeping the Mello app in step with Spotify: polling the engine's status,
following what plays, the temporary 'now playing' item, network checks and
restarting a stuck librespot.
"""
import logging
import subprocess
import time

from ..config import SPOTIFY_ENGINE_SERVICE
from ..models import CatalogItem, NowPlaying, LibrespotStatus
from ..controllers import is_repeatable_spotify_context
from ..utils import run_async

logger = logging.getLogger('mello.app')

LIBRESPOT_RESTART_AFTER = 60.0
LIBRESPOT_RESTART_COOLDOWN = 300.0


class SpotifySyncMixin:
    """Status polling, following playback, the temp item and librespot restarts."""

    def _poll_status(self):
        """Poll librespot status in background.
        
        Intervals adapt to state: fast when disconnected, slow when idle,
        near-zero during sleep (WebSocket can signal instant wake via
        _poll_wake_event).
        """
        was_fast_polling = False
        while self.running:
            # During sleep: wait up to 30s, but wake instantly on WS signal
            if self.sleep_manager.is_sleeping:
                self._poll_wake_event.wait(timeout=30)
                self._poll_wake_event.clear()
                if not self.running:
                    break
            
            try:
                self._refresh_status()
            except Exception as e:
                self._connection_fail_count += 1
                if self._connection_fail_count >= self._connection_grace_threshold:
                    if self.connected:
                        logger.error(f'Status poll error: {e}')
                    self.connected = False
            
            # Poll faster when disconnected for quicker recovery
            is_fast_polling = not self.connected
            if is_fast_polling != was_fast_polling:
                if is_fast_polling:
                    logger.debug('Fast polling mode (disconnected)')
                else:
                    logger.debug('Normal polling mode (connected)')
                was_fast_polling = is_fast_polling
            
            if is_fast_polling:
                poll_interval = 0.5
            elif not self.now_playing.playing:
                poll_interval = 3.0
            else:
                poll_interval = 1.0
            self._poll_wake_event.wait(timeout=poll_interval)
            self._poll_wake_event.clear()
    
    def _refresh_status(self):
        """Refresh playback status from librespot."""
        previous = self.now_playing
        raw = self.api.status()
        was_connected = self.connected
        
        # Determine connection with grace period
        has_connection = raw is not None or self.api.is_connected()
        if has_connection:
            if self._connection_fail_count > 0:
                logger.debug(f'Connection recovered after {self._connection_fail_count} failures')
            self._connection_fail_count = 0
            self.connected = True
        else:
            self._connection_fail_count += 1
            if self._connection_fail_count >= self._connection_grace_threshold:
                self.connected = False
        
        # Log connection state changes
        if was_connected != self.connected:
            if self.connected:
                logger.info('CONNECTION RESTORED (was disconnected)')
                self._startup_ready = True
                if self._connection_lost_at:
                    self.tracker.on_engine_reconnected(time.time() - self._connection_lost_at)
                    self._connection_lost_at = 0.0
            else:
                logger.warning(f'CONNECTION LOST after {self._connection_fail_count} failures')
                self._connection_lost_at = time.time()
            logger.info(f'  fail_count={self._connection_fail_count}, status={raw is not None}')
        
        if raw and isinstance(raw, dict):
            api_context_uri = raw.get('context_uri')
            ws_context_uri = None
            if not raw.get('stopped', True) or raw.get('paused', False):
                ws_context_uri = self.events.context_uri
            if api_context_uri and ws_context_uri and api_context_uri != ws_context_uri:
                logger.warning(
                    'CONTEXT source mismatch | '
                    f'api_ctx={api_context_uri[:40]} | ws_ctx={ws_context_uri[:40]} | '
                    f'track="{(raw.get("track") or {}).get("name") if isinstance(raw.get("track"), dict) else "none"}"'
                )
            status = LibrespotStatus.from_dict(raw, context_uri=ws_context_uri)
            current = NowPlaying(
                playing=status.playing,
                paused=status.paused,
                stopped=status.stopped,
                context_uri=status.context_uri,
                track_name=status.track_name,
                track_artist=status.track_artist,
                track_album=status.track_album,
                track_cover=status.track_cover,
                track_uri=status.track_uri,
                position=status.position,
                duration=status.duration,
                repeat_context=status.repeat_context,
            )
            with self._now_playing_lock:
                if self._now_playing is not previous:
                    return  # An inactive event or a fresher status replaced this snapshot.
                # Older daemons may briefly return no session while handing off.
                # Also handle a missed inactive event if the session is already back but idle.
                if not previous.stopped and status.stopped and not status.paused and not status.track_uri:
                    self._spotify_inactive.set()
                self._now_playing = current
                self._status_failure_started_at = 0.0
                self._last_status_ok_at = time.time()
                self._status_unknown = False

            if status.context_uri != previous.context_uri or status.playing != previous.playing:
                state = 'playing' if status.playing else 'buffering'
                if status.paused or status.stopped:
                    state = 'paused' if status.paused else 'stopped'
                logger.info(f'SPOTIFY changed | {state} "{status.track_name}" | ctx={status.context_uri or "none"}')

            pending_action = self.playback.play_state.pending_action
            if pending_action == 'pause':
                if status.paused or status.stopped:
                    self.playback.play_state.pending_action = None
                    logger.info('pending_action_cleared | action=pause | status_ack=not_playing')
                else:
                    logger.info('pending_action_hold | action=pause | awaiting confirmation')
            elif pending_action == 'play' and status.playing:
                self.playback.play_state.pending_action = None
                logger.info('pending_action_cleared | action=play | status_ack=playing')
            self.tracker.update(self.now_playing)
            
            self._update_temp_item()
            self._ensure_repeat_context_for_current_status()
            
            if status.playing and self.sleep_manager.is_sleeping:
                self._wake_from_sleep('spotify_playing')
            
            if status.playing:
                self.auto_pause.on_play(status.context_uri)
                self.auto_pause.check(is_playing=True)
                # Ensure audio goes to BT headphone if active
                self.bluetooth.ensure_stream_on_desired_sink()
            elif status.paused or status.stopped:
                self.auto_pause.on_stop()
        else:
            # Transport/timeout errors are "unknown", not "stopped".
            # Keep last known now_playing to avoid duplicate play re-triggers.
            self._status_unknown = True
            now = time.time()
            if self._status_failure_started_at == 0.0:
                self._status_failure_started_at = now
            if now - self._last_status_unknown_log > 3.0:
                logger.warning(
                    'STATUS unknown | preserving last now_playing snapshot '
                    f'| connected={self.connected} | fail_count={self._connection_fail_count}'
                )
                self._last_status_unknown_log = now
            self._maybe_restart_librespot(now)

    def _maybe_restart_librespot(self, now: float):
        """Restart a locally hung Spotify engine after a sustained outage."""
        failure_started_at = self._status_failure_started_at
        if failure_started_at == 0.0:
            return
        if now - failure_started_at < LIBRESPOT_RESTART_AFTER:
            return
        if (
            self._last_librespot_restart_at > 0.0
            and now - self._last_librespot_restart_at < LIBRESPOT_RESTART_COOLDOWN
        ):
            return

        self._last_librespot_restart_at = now
        self._show_toast('Spotify herstellen...')
        logger.warning(
            'LIBRESPOT recovery | restarting unresponsive service '
            f'after {now - failure_started_at:.1f}s'
        )
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', SPOTIFY_ENGINE_SERVICE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                self._status_failure_started_at = now
                self._connection_fail_count = 0
                self._poll_wake_event.set()
                logger.info('LIBRESPOT recovery | restart requested successfully')
            else:
                logger.error(
                    'LIBRESPOT recovery | restart failed '
                    f'rc={result.returncode} stderr={result.stderr.strip()}'
                )
            self.tracker.on_engine_restarted(now - failure_started_at, ok=result.returncode == 0)
        except Exception as exc:
            logger.error(f'LIBRESPOT recovery | restart error: {exc}', exc_info=True)
            self.tracker.on_engine_restarted(now - failure_started_at, ok=False)
    
    def _ensure_repeat_context_for_current_status(self):
        """Keep externally-started albums/playlists inside their context."""
        np = self.now_playing
        context_uri = np.context_uri
        if not np.playing or not is_repeatable_spotify_context(context_uri):
            return

        if np.repeat_context:
            self._repeat_context_uri = context_uri
            return

        now = time.time()
        if self._repeat_context_uri == context_uri and now - self._repeat_context_last_attempt < 15.0:
            return

        self._repeat_context_uri = context_uri
        self._repeat_context_last_attempt = now
        logger.info(f'repeat_context_request | reason=status_playing | uri={context_uri[:50]}')

        def _set_repeat():
            ok = self.api.set_repeat_context(True)
            if ok:
                logger.info(f'repeat_context_on | reason=status_playing | uri={context_uri[:50]}')
            else:
                logger.warning(f'repeat_context_failed | reason=status_playing | uri={context_uri[:50]}')

        run_async(_set_repeat)
    
    def _update_temp_item(self):
        """Update tempItem based on current playback context.
        
        Thread-safe: uses _temp_item_lock since download threads also write temp_item.
        """
        context_uri = self.now_playing.context_uri
        
        if not context_uri:
            with self._temp_item_lock:
                had_temp = self.temp_item is not None
                self.temp_item = None
            if had_temp:
                self._update_carousel_max_index()
                self.renderer.invalidate()
            return
        
        # Check if in catalog (with valid image)
        catalog_item = next((item for item in self.catalog_manager.items if item.uri == context_uri), None)
        if catalog_item and catalog_item.image:
            with self._temp_item_lock:
                had_temp = self.temp_item is not None
                self.temp_item = None
            if had_temp:
                self._update_carousel_max_index()
                self.renderer.invalidate()
            return
        
        if not self.now_playing.track_uri:
            # Spotify switched to this album but has not said which track is
            # playing yet; its name and cover would come from nowhere.
            return

        # Create/update tempItem
        is_playlist = 'playlist' in context_uri
        collected_covers = self.catalog_manager.get_collected_covers(context_uri) if is_playlist else None
        track_cover = self.now_playing.track_cover
        
        start_download = False
        
        with self._temp_item_lock:
            current_cover_count = len(self.temp_item.images or []) if self.temp_item else 0
            new_cover_count = len(collected_covers or [])
            
            uri_changed = not self.temp_item or self.temp_item.uri != context_uri
            
            needs_update = (
                uri_changed or
                new_cover_count > current_cover_count
            )
            
            if not needs_update:
                return
            
            # Only preserve local image if same URI (prevents wrong cover on wrong item)
            if not uri_changed and self.temp_item.image and self.temp_item.image.startswith('/images/'):
                local_image = self.temp_item.image
            else:
                local_image = None
            
            self.temp_item = CatalogItem(
                id='temp',
                uri=context_uri,
                name=self.now_playing.track_album or ('Playlist' if is_playlist else 'Album'),
                type='playlist' if is_playlist else 'album',
                artist=self.now_playing.track_artist,
                image=local_image or track_cover,
                images=collected_covers,
                is_temp=True
            )
            
            start_download = not local_image and bool(track_cover)
        
        self._update_carousel_max_index()
        self.renderer.invalidate()
        logger.info(f'TempItem: {self.temp_item.name}')
        
        # Download cover in background if we don't have a local image
        if start_download:
            run_async(self._download_temp_cover_async, context_uri, track_cover)
    
    def _download_temp_cover_async(self, context_uri: str, cover_url: str):
        """Download temp item cover in background thread."""
        try:
            local_path = self.catalog_manager.download_temp_image(cover_url)
            if not local_path:
                return

            # Thread-safe update of temp_item
            with self._temp_item_lock:
                if self.temp_item and self.temp_item.uri == context_uri:
                    # Update temp item with downloaded image
                    self.temp_item = CatalogItem(
                        id=self.temp_item.id,
                        uri=self.temp_item.uri,
                        name=self.temp_item.name,
                        type=self.temp_item.type,
                        artist=self.temp_item.artist,
                        image=local_path,
                        images=self.temp_item.images,
                        is_temp=True
                    )
            self.renderer.invalidate()
            logger.info(f'TempItem cover downloaded: {local_path}')
        except Exception as e:
            logger.debug(f'Temp cover download failed: {e}')
    
    def _has_network_connection(self) -> bool:
        """Check if any network interface is connected via NetworkManager."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'STATE', 'general'],
                capture_output=True, text=True, timeout=3,
            )
            return result.stdout.strip().lower().startswith('connected')
        except Exception:
            return True

    def _get_cached_network_status(self) -> bool:
        """Return cached network status, refreshing every 10 seconds."""
        now = time.time()
        if now - self._network_check_time >= 10:
            self._network_check_time = now
            # Called from the draw loop: asking NetworkManager takes a
            # process launch (a stutter every 10s), so do it off to the side.
            run_async(self._refresh_network_status)
        return self._cached_has_network

    def _refresh_network_status(self):
        self._cached_has_network = self._has_network_connection()

    def _initial_connect(self):
        """Initial connection with fast retries then slower backoff.

        Tries quickly first (every 2s for ~20s) since go-librespot usually
        boots within 10s on Pi. Falls back to slower retries after that.
        """
        start_time = time.time()
        max_retries = 20
        for attempt in range(max_retries):
            try:
                self._refresh_status()
                if self.connected:
                    logger.info(f'Connected to librespot (attempt {attempt + 1})')
                    break
            except Exception as e:
                logger.warning(f'Connection attempt {attempt + 1}/{max_retries} failed: {e}')

            # Fast retries first (2s), then slow down (max 10s)
            delay = 2 if attempt < 10 else min(2 ** (attempt - 10), 10)
            time.sleep(delay)
        else:
            logger.error(f'Failed to connect to librespot after {max_retries} attempts')

        self._startup_ready = True

        # Give NetworkManager time to auto-connect to a known network
        elapsed = time.time() - start_time
        if elapsed < 10:
            time.sleep(10 - elapsed)

        if not self._has_network_connection():
            logger.info('No network connection detected, opening WiFi setup')
            self.setup_menu.show_wifi()
    
    def _save_temp_item(self):
        """Save the current temp item to catalog."""
        if self._saving:
            return
        self._saving = True
        
        try:
            with self._temp_item_lock:
                temp = self.temp_item
            
            if not temp:
                return
            
            if not temp.image:
                logger.warning(f'Cannot save item without image: {temp.name}')
                return
            
            logger.info(f'Saving: {temp.name}')
            
            item_data = {
                'type': temp.type,
                'uri': temp.uri,
                'name': temp.name,
                'artist': temp.artist,
                'image': temp.image,
            }
            
            success = self.catalog_manager.save_item(item_data)
            
            if success:
                self.catalog_manager.load()
                self._update_carousel_max_index()
                with self._temp_item_lock:
                    if self.temp_item and self.temp_item.uri == temp.uri:
                        self.temp_item = None
                self.renderer.invalidate()
        finally:
            self._saving = False
    
    def _collect_cover_async(self, context_uri: str, cover_url: str):
        """Collect playlist cover in background thread."""
        try:
            new_cover_added = self.catalog_manager.collect_cover_for_playlist(
                context_uri, cover_url
            )
            if new_cover_added:
                # Schedule UI update on next frame (thread-safe)
                self._update_temp_item()
                self.renderer.invalidate()
        except Exception as e:
            logger.debug(f'Cover collection failed: {e}')
    
    def _sync_to_playing(self):
        """Sync carousel to currently playing item.

        While _user_driving is True (user recently swiped/played), only accept
        confirmation of our own play request. While False, accept anything
        (external Spotify control, autoplay).
        """
        items = self.display_items
        if not items:
            return

        now_playing = self.now_playing
        context_uri = now_playing.context_uri
        if not context_uri:
            return

        focused = items[self.selected_index].name if self.selected_index < len(items) else '?'
        focused_uri = items[self.selected_index].uri if self.selected_index < len(items) else None

        # This runs every frame. Log a line only when the situation changes:
        # repeating it 20x per second kept the SD card busy from the draw loop.
        situation = (
            context_uri, focused_uri, self._user_driving, self._focus_epoch,
            now_playing.playing, self._manual_pause_lock,
        )
        situation_changed = situation != self._last_sync_situation
        self._last_sync_situation = situation

        def log_sync(outcome: str, message: str):
            if situation_changed or outcome != self._last_sync_outcome:
                logger.info(message)
            self._last_sync_outcome = outcome

        if situation_changed:
            logger.info(
                f'SYNC check | spotify={context_uri[:40]} | focused="{focused}" '
                f'| driving={self._user_driving} | epoch={self._focus_epoch}'
            )

        if focused_uri == context_uri:
            if (
                now_playing.playing
                and not self.playback.has_pending_play
                and not self._manual_pause_lock
                and not self.playback.pause_intent_active
            ):
                self.volume.unmute()
            elif now_playing.playing and (self._manual_pause_lock or self.playback.pause_intent_active):
                log_sync(
                    'ok_unmute_blocked',
                    'unmute_guard_blocked | reason=pause_intent_or_manual_lock '
                    f'| manual_pause_lock={self._manual_pause_lock} | '
                    f'pause_intent_active={self.playback.pause_intent_active}'
                )
            else:
                log_sync('ok', 'SYNC ok | focused context already matches Spotify')
            return

        if not now_playing.playing:
            log_sync('hold', 'SYNC hold | spotify not playing, skip focus sync')
            return

        if self._has_active_user_focus_intent():
            log_sync(
                'blocked',
                'SYNC blocked | active user intent, deferring remote focus '
                f'ctx={context_uri[:40]}'
            )
            return

        # Safe remote sync path: move UI focus only, never pause/mute/stop playback.
        if self._focus_on_uri_without_interrupt(context_uri, reason='remote_sync'):
            return

        # The next frame can sync once the current context has a cover.
        log_sync(
            'pending',
            'SYNC pending | remote context not in display_items yet '
            f'ctx={context_uri[:40]}'
        )

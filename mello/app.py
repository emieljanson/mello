"""
Mello Application - Main application class.
"""
import time
import signal
import logging
import threading
from typing import Optional, List

from . import crash_log
from .config import (
    LOG_DIR,
    SCREEN_WIDTH, SCREEN_HEIGHT,
    SPOTIFY_ENGINE, SOLOIST_WS, DIRECT_PLAY,
    SOLOIST_HOME, SOLOIST_DATA_DIR, ENGINE_SETUP_PORT,
    CATALOG_PATH, PROGRESS_PATH, IMAGES_DIR, ICONS_DIR, DATA_DIR,
    MOCK_MODE,
    BUTTON_PRESS_DURATION, MENU_HOLD_TIME,
    POSTHOG_API_KEY, POSTHOG_HOST, ANALYTICS_DISTINCT_ID,
    ANALYTICS_INCLUDE_CONTENT, ANALYTICS_USE_MACHINE_ID,
)
from .models import CatalogItem, NowPlaying
from .api import NullPlayerAPI, SoloistAPI, CatalogManager
from .api.play_orders import PlayOrders
from .handlers import TouchHandler, EvdevTouchHandler, InputEvent
from .managers import SleepManager, SmoothCarousel, PlayTimer, PerformanceMonitor, AnimationTimer, AutoPauseManager, SetupMenu, Settings, UsageTracker, BluetoothManager
from .managers.engine_setup import EngineSetup, PROMPT_BENEFITS, PROMPT_SCAN_HINT
from .managers.wifi_keeper import WifiKeeper
from .controllers import VolumeController, PlaybackController
from .ui import Bitmaps, ImageCache, Renderer, RenderContext
from .ui.slint_display import SlintDisplay
from .app_parts.diagnostics import DiagnosticsMixin, HEALTH_REPORT_EVERY  # noqa: F401 (re-exported)
from .app_parts.spotify_sync import SpotifySyncMixin
from .app_parts.touch_input import TouchInputMixin
from .app_parts.focus_playback import FocusPlaybackMixin
from .utils import run_async, get_runtime_version_label, set_system_volume

logger = logging.getLogger(__name__)



class ScreenUnavailable(Exception):
    """Slint could not start (for example an older Raspberry Pi OS)."""


class Mello(DiagnosticsMixin, SpotifySyncMixin, TouchInputMixin, FocusPlaybackMixin):
    """Main Mello application."""

    # What _sync_to_playing last logged (it runs every frame, see there).
    _last_sync_situation: Optional[tuple] = None
    _last_sync_outcome: Optional[str] = None
    _loader_mismatch_logged = False
    # Headphone seen connected last frame and since when, for analytics.
    _bt_seen: Optional[str] = None
    _bt_seen_since = 0.0
    _next_health_report = float('inf')   # set when analytics starts
    
    def __init__(self, fullscreen: bool = False):
        # Restore display before Slint takes over the screen. A previous run
        # may have been killed during sleep, leaving backlight/DPMS off.
        SleepManager.restore_display()
        self.fullscreen = fullscreen
        # Slint shows the boot splash as soon as its event loop runs;
        # everything else is set up from inside that loop (start()).
        self.bitmaps = Bitmaps(ICONS_DIR)
        try:
            self.slint_display = SlintDisplay(self.bitmaps)
        except Exception as e:
            raise ScreenUnavailable(str(e)) from e
        logger.info('Screen: Slint')

    def _init_components(self):
        """Initialize all application components."""
        self.app_version_label = get_runtime_version_label()
        logger.info(f'App version: {self.app_version_label}')

        # Mock mode
        self.mock_mode = MOCK_MODE
        
        # API & Catalog (use NullAPI in mock mode)
        if self.mock_mode:
            self.api = NullPlayerAPI()
        else:
            # Soloist has one socket for commands and events, so the same
            # object is both the API and the event listener. (Without a key
            # it does not run yet: it simply cannot connect.)
            self.api = SoloistAPI(
                SOLOIST_WS, self._on_ws_update, self._on_ws_reconnect,
                on_inactive=self._on_spotify_inactive,
                play_orders=PlayOrders(DATA_DIR / 'play_orders.json') if DIRECT_PLAY else None,
            )
        self.settings = Settings()
        self.catalog_manager = CatalogManager(
            CATALOG_PATH, IMAGES_DIR, mock_mode=self.mock_mode,
            progress_path=PROGRESS_PATH,
            get_progress_expiry=lambda: self.settings.progress_expiry_hours,
        )
        self.catalog_manager.load()
        
        # UI Components
        self.image_cache = ImageCache(IMAGES_DIR)
        self.renderer = Renderer(self.bitmaps)
        
        # Handlers
        self.touch = TouchHandler()
        self.events = self.api
        
        # Evdev touch handler for KMSDRM mode (reads /dev/input directly)
        self.evdev_touch = EvdevTouchHandler(SCREEN_WIDTH, SCREEN_HEIGHT)
        touch_available = self.evdev_touch.start()  # Starts background thread if touchscreen found
        if not touch_available:
            # A computer: mouse and keys from the window stand in for touch.
            self.slint_display.forward_input(
                lambda kind, pos: self.evdev_touch.post(InputEvent(kind, pos, time.time())),
                lambda key: self.evdev_touch.post(InputEvent('key', key=key)),
            )
        
        # Managers
        self.sleep_manager = SleepManager()
        if not touch_available and not self.mock_mode:
            self._disable_sleep_for_touch(
                self.evdev_touch.consume_failure_reason() or 'touchscreen unavailable at startup'
            )
        self._log_startup_health()
        self.carousel = SmoothCarousel()
        self.play_timer = PlayTimer()
        self.perf_monitor = PerformanceMonitor()
        self.anim_timer = AnimationTimer()
        self.volume = VolumeController(self.api, self.settings)
        # Usage analytics (only enabled if user opted in during install)
        analytics_key = POSTHOG_API_KEY if self.settings.share_usage_data else ''
        self.tracker = UsageTracker(
            api_key=analytics_key,
            host=POSTHOG_HOST,
            distinct_id=ANALYTICS_DISTINCT_ID,
            include_content=ANALYTICS_INCLUDE_CONTENT,
            use_machine_id=ANALYTICS_USE_MACHINE_ID,
            app_version=self.app_version_label,
            engine=SPOTIFY_ENGINE,
            ui='slint',
        )
        crash_log.on_thread_error = self.tracker.on_thread_crashed
        self._next_health_report = time.monotonic() + HEALTH_REPORT_EVERY
        
        self.auto_pause = AutoPauseManager(
            on_pause=lambda: (self.tracker.on_auto_pause(), run_async(self.api.pause)),
            get_volume=lambda: self.volume.speaker_level,
            get_timeout=lambda: self.settings.auto_pause_timeout,
        )
        
        # Playback controller (owns play/pause/resume, progress, navigation pause)
        self.playback = PlaybackController(
            api=self.api,
            catalog_manager=self.catalog_manager,
            volume=self.volume,
            mock_mode=self.mock_mode,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_resume=self.auto_pause.restore_volume_if_needed,
            is_request_current=self._is_play_request_current,
            on_play_committed=self._on_play_committed,
            on_play_failed=self._on_play_failed,
        )
        
        # State (with thread-safe now_playing and connected)
        self._now_playing = NowPlaying()
        self._now_playing_lock = threading.Lock()
        self._connected = self.mock_mode
        self._connected_lock = threading.Lock()
        self.selected_index = 0
        self._connection_fail_count = 0
        self._connection_grace_threshold = 3
        self._running = threading.Event()
        self._running.set()
        self._poll_wake_event = threading.Event()
        self._spotify_inactive = threading.Event()
        self._last_sleep_wait_log: float = 0.0
        
        # TempItem and delete mode (with lock for thread-safe access)
        self.temp_item: Optional[CatalogItem] = None
        self._temp_item_lock = threading.Lock()
        self.delete_mode_id: Optional[str] = None
        self._delete_button_rect: Optional[tuple] = None
        self._saving = False
        self._deleting = False
        
        # True while user is actively controlling playback (swipe/play).
        # While True, _sync_to_playing only accepts confirmation of our own play request.
        # While False, _sync_to_playing accepts anything (external Spotify control).
        self._user_driving = False
        self._focus_epoch: int = 0
        self._pending_focus_uri: Optional[str] = None
        self._pending_focus_since: float = 0.0
        self._requested_focus_epoch: Optional[int] = None
        self._requested_focus_uri: Optional[str] = None
        self._requested_focus_since: float = 0.0
        self._last_status_ok_at: float = 0.0
        # True when status is temporarily unknown (timeout/error). While unknown
        # we keep the last known now_playing snapshot and block auto-retrigger.
        self._status_unknown: bool = False
        self._last_status_unknown_log: float = 0.0
        self._status_failure_started_at: float = 0.0
        self._last_engine_restart_at: float = 0.0
        self._connection_lost_at: float = 0.0   # 0: connected, or never was
        self._last_play_commit_uri: Optional[str] = None
        self._last_play_commit_at: float = 0.0
        self._last_snap_pause_at: float = 0.0
        self._repeat_context_uri: Optional[str] = None
        self._repeat_context_last_attempt: float = 0.0
        # Blocks auto-play after an explicit user pause until user gives a
        # positive play intent (play tap or context switch).
        self._manual_pause_lock: bool = False
        self._manual_pause_context_uri: Optional[str] = None
        self._context_switch_stall_since: float = 0.0
        self._last_context_watchdog_log: float = 0.0
        
        # Interaction tracking
        self.user_interacting = False
        self._last_cover_collect_key: Optional[tuple] = None
        self._cover_collect_context: Optional[str] = None
        self._context_change_time: float = 0
        
        # Button debouncing and feedback
        self._last_action_time = 0
        self._pressed_button: Optional[str] = None
        self._pressed_time = 0
        
        # Toast messages (brief on-screen feedback)
        self._toast_message: Optional[str] = None
        self._toast_time: float = 0
        self._toast_duration: float = 3.0
        
        # Startup gate: blocks auto-play until _initial_connect completes
        self._startup_ready = False

        # Cached network status (avoid shelling out to nmcli every frame)
        self._cached_has_network: bool = True
        self._network_check_time: float = 0.0
        
        # Register signal handlers for graceful shutdown. SIGHUP too: the
        # service owns tty1 through a login session, and on reboot/shutdown
        # that session ends first and hangs up the tty. Unhandled, SIGHUP
        # killed Mello at once, before it saved where each album was.
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_signal)
        
        # Performance logging
        self._last_fps_log = time.time()
        self._fps_log_interval = 30  # Log FPS every 30 seconds
        
        # Bluetooth manager
        self.bluetooth = BluetoothManager(
            settings=self.settings,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_audio_changed=self._on_bt_audio_changed,
        )
        self._bt_audio_active: bool = False

        # Setup menu
        self.setup_menu = SetupMenu(
            catalog_manager=self.catalog_manager,
            settings=self.settings,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_library_cleared=self._on_library_cleared,
            bluetooth_manager=self.bluetooth,
            on_volume_preview=self._preview_volume,
            engine_setup=None if self.mock_mode else EngineSetup(
                engine=SPOTIFY_ENGINE,
                soloist_home=SOLOIST_HOME,
                port=ENGINE_SETUP_PORT,
                on_change=lambda: self.renderer.invalidate(),
            ),
        )
        # Rejoins a known WiFi network when NetworkManager has given up.
        self.wifi_keeper = WifiKeeper(
            is_online=self._has_network_connection,
            is_busy=lambda: self.setup_menu.wifi_setup_running,
            on_restored=self.tracker.on_wifi_restored,
        )
        self._pairing_known = True
        self._pairing_checked_at = 0.0
        # A Mello that starts with nothing saved was just set up: it gets the
        # "faster Mello" offer later, not as something new in an update.
        if not self.mock_mode and not self.catalog_manager.items:
            self.settings.mark_new_owner()
        # Volume button hold tracking (3s hold opens setup menu)
        self._volume_hold_start: Optional[float] = None
        self._menu_hold_triggered = False
        # Menu scroll tracking
        self._menu_touch_start: Optional[tuple] = None
        self._menu_touch_scrolled: bool = False
        
        # Initialize carousel
        self._update_carousel_max_index()
        self._focus_last_played()

    def _show_toast(self, message: str):
        """Show a brief toast message on screen."""
        self._toast_message = message
        self._toast_time = time.time()
        self.renderer.invalidate()

    def _preview_volume(self, level_idx: int, output_type: str, new_val: int):
        """Switch to the edited volume level and apply it immediately."""
        self.volume.index = level_idx
        if output_type == 'speaker':
            set_system_volume(new_val)
        elif output_type == 'bt' and self.bluetooth:
            self.bluetooth.set_volume(new_val)

    def _on_library_cleared(self):
        """Reset in-memory state after library clear (called by SetupMenu)."""
        self._bump_focus_epoch('library_cleared')
        self._user_driving = False
        self.playback.stop_all()
        self.catalog_manager.load()
        with self._temp_item_lock:
            self.temp_item = None
        self.selected_index = 0
        self.carousel.scroll_x = 0.0
        self.carousel.set_target(0)
        self._update_carousel_max_index()
    
    @property
    def _active_toast(self) -> Optional[str]:
        """Return toast message if still within display duration."""
        if self._toast_message and time.time() - self._toast_time < self._toast_duration:
            return self._toast_message
        self._toast_message = None
        return None
    
    def _on_ws_update(self):
        """Called when WebSocket receives an event."""
        logger.debug(f'WebSocket event, context: {self.events.context_uri}')
        # Soloist pushes its full state, so reading it is free: refresh right
        # away instead of waiting up to 3s for the next poll.
        if self.sleep_manager.is_sleeping or isinstance(self.api, SoloistAPI):
            self._poll_wake_event.set()
    
    def _on_ws_reconnect(self):
        """Called when WebSocket reconnects after disconnect."""
        logger.info('WebSocket reconnected - refreshing state')
        self._poll_wake_event.set()

    def _on_spotify_inactive(self):
        """Fence pending play requests as soon as Spotify leaves this device."""
        with self._now_playing_lock:
            self._spotify_inactive.set()
            self._now_playing = NowPlaying()
        self._poll_wake_event.set()

    def _apply_spotify_inactive(self):
        if not self._spotify_inactive.is_set():
            return
        self._bump_focus_epoch('spotify_inactive')
        self._user_driving = False
        self.playback.stop_all()
        self._clear_manual_pause_lock('spotify_inactive')
        self._spotify_inactive.clear()
        logger.info('SPOTIFY inactive | cancelled pending local playback')
    
    @property
    def display_items(self) -> List[CatalogItem]:
        """Return catalog items + tempItem if present."""
        items = self.catalog_manager.items
        if self.temp_item:
            return items + [self.temp_item]
        return items
    
    @property
    def now_playing(self) -> NowPlaying:
        """Thread-safe getter for now_playing state."""
        with self._now_playing_lock:
            return self._now_playing
    
    @now_playing.setter
    def now_playing(self, value: NowPlaying):
        """Thread-safe setter for now_playing state."""
        with self._now_playing_lock:
            self._now_playing = value
    
    @property
    def connected(self) -> bool:
        """Thread-safe getter for connected state."""
        with self._connected_lock:
            return self._connected
    
    @connected.setter
    def connected(self, value: bool):
        """Thread-safe setter for connected state."""
        with self._connected_lock:
            self._connected = value
    
    @property
    def running(self) -> bool:
        """Thread-safe running flag (backed by threading.Event)."""
        return self._running.is_set()
    
    @running.setter
    def running(self, value: bool):
        if value:
            self._running.set()
        else:
            self._running.clear()
    
    def _update_carousel_max_index(self):
        """Update carousel max index when items change."""
        self.carousel.max_index = max(0, len(self.display_items) - 1)
    
    def _on_bt_audio_changed(self, active: bool):
        """Called by BluetoothManager when audio routing changes."""
        self._bt_audio_active = active
        if active:
            # Set initial volume on BT sink
            self.bluetooth.set_volume(self.volume.bt_level)
        self.renderer.invalidate()

    def _on_fell_asleep(self):
        """The screen just went off after being left alone."""
        # Every touch sets the touchscreen's wake signal and only sleep clears
        # it, so a touch from before the screen went off was still pending:
        # the screen woke 0.2 s later with nobody touching it (9 of 18 touch
        # wakes on the daily speaker) and stayed on for 2 more minutes. Only a
        # touch while asleep may wake it.
        self.evdev_touch.wake_event.clear()
        self.bluetooth.pause_monitoring()
        idle = time.time() - self.sleep_manager.last_activity
        self.tracker.on_sleep(idle)

    def _handle_signal(self, signum, frame):
        """Handle SIGTERM/SIGINT/SIGHUP for graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info(f'Received {sig_name}, shutting down...')
        crash_log.stop_requested()
        self.running = False
    
    def start(self):
        """Start the application."""
        self._run_loop()
        if hasattr(self, 'tracker'):   # set up (not stopped during the splash)
            self._shutdown()

    def _start_services(self):
        """Everything that runs once before the first frame."""
        logger.info('Starting Mello...')
        self.tracker.on_app_started(
            catalog_size=len(self.catalog_manager.items),
            previous_run=crash_log.previous_run,
        )
        
        if not self.mock_mode:
            self.events.start()
            self.catalog_manager.cleanup_unused_images()
            
            # Set system volume at startup (also unmutes as safety reset)
            self.volume.init()
            
            # Status polling and the initial connect run for as long as the
            # app does; in the shared pool of 4 they would leave only two
            # workers for pause, skip and saving progress.
            threading.Thread(target=self._poll_status, daemon=True, name='status-poll').start()
            logger.info(f'Polling {SOLOIST_WS} ({SPOTIFY_ENGINE})')

            # Force initial connection check (don't wait for first poll interval)
            threading.Thread(target=self._initial_connect, daemon=True, name='initial-connect').start()

            # Start Bluetooth monitoring
            self.bluetooth.start_monitoring()
            self.wifi_keeper.start()
        else:
            logger.info('Running in MOCK MODE')
            self._startup_ready = True
        
        logger.info('Entering main loop...')

    def _shutdown(self):
        """Save progress and let go of everything (after the main loop ended)."""
        # Save progress before shutdown
        logger.info('Shutting down...')
        self._save_progress_on_shutdown()
        self.tracker.on_shutdown()
        self.bluetooth.stop()
        self.wifi_keeper.stop()
        
        # Restore display before exit so next boot doesn't start with black screen
        if self.sleep_manager.is_sleeping:
            self.sleep_manager.wake_up('shutdown')
        
        self.events.stop()
        self.evdev_touch.stop()
        crash_log.mark_clean_exit(LOG_DIR)
        logger.info('Mello stopped')
    
    def _run_loop(self):
        """The main loop. Slint's event loop calls tick() every ~8 ms; frames
        are paced by the target fps here, and Slint draws when something on
        screen changed.
        """
        display = self.slint_display
        state = {'last': time.monotonic(), 'sleep_check': 0.0, 'ready': False, 'shown': 0}

        def tick() -> bool:
            if not state['ready']:
                # Let Slint put the splash on screen first (a couple of ticks),
                # then set up the app; that takes a few seconds.
                state['shown'] += 1
                if state['shown'] < 3:
                    return True
                self._init_components()
                display.attach(self.renderer, self.image_cache)
                self._start_services()
                state['ready'] = True
                state['last'] = time.monotonic()
                return self.running
            if not self.running:
                return False
            now = time.monotonic()
            crash_log.heartbeat()
            self._apply_spotify_inactive()
            self._check_touch_health()
            if self.sleep_manager.is_sleeping:
                state['last'] = now  # time asleep is not a frame
                if now - state['sleep_check'] < 0.2:
                    return True
                state['sleep_check'] = now
                if self.evdev_touch.wake_event.is_set():
                    self.evdev_touch.wake_event.clear()
                    keys = [e.key for e in self.evdev_touch.take_events() if e.kind == 'key']
                    self._wake_from_sleep(f'key:{keys[0]}' if keys else 'evdev_touch')
                    # The touch that woke the screen is not a tap
                    self.evdev_touch.clear_events()
                    return True
                if self.setup_menu.wifi_setup_running:
                    # The hotspot keeps going with the screen off: someone may
                    # be filling in the WiFi page on a phone. When it finishes,
                    # light the screen so they see how it went.
                    self.setup_menu.update()
                    if not self.setup_menu.wifi_setup_running:
                        self._wake_from_sleep('wifi_setup_done')
                        return True
                self._log_sleep_wait_if_due()
                return True

            target_fps = self._target_fps()
            is_animating = not self.carousel.settled or self.touch.dragging
            interval = 0.2 if (target_fps <= 5 and not is_animating) else 1.0 / target_fps
            # A touch is handled at once, not at the next idle frame (up to
            # 0.2 s later): a press shows straight away.
            touched = not self.evdev_touch.events.empty()
            if now - state['last'] < interval - 0.004 and not touched:
                return True
            dt = now - state['last']
            state['last'] = now

            self.anim_timer.begin_frame()
            self._handle_events()
            self.anim_timer.mark('events')
            self._update(dt)
            self.anim_timer.mark('update')
            self._draw()
            self.anim_timer.mark('draw')
            display.present()
            self.anim_timer.mark('display')

            spike_threshold = max(0.1, 1.0 / target_fps * 1.2)
            if dt > spike_threshold and target_fps > 5 and not touched:
                logger.warning(f'Frame spike: {dt*1000:.0f}ms (target: {target_fps} FPS)')
            self.perf_monitor.update(dt)
            anim_summary = self.anim_timer.end_frame(is_animating, dt)
            if anim_summary:
                logger.info(anim_summary)
            self._log_fps_if_due(target_fps)
            return self.running

        display.run(tick)

    def _target_fps(self) -> int:
        """Calculate target FPS based on current activity.
        
        60 FPS for animations/loading, 10 for playback/menu, 5 for idle.
        """
        if self.setup_menu.is_open or self._volume_hold_start is not None:
            return 10
        is_animating = not self.carousel.settled or self.touch.dragging
        if is_animating or self.playback.play_state.is_loading:
            return 60
        elif self.now_playing.playing or self._active_toast:
            return 10
        return 5
    
    def _on_wake(self):
        """Resume monitoring and refresh Spotify status after sleep."""
        self.bluetooth.resume_monitoring()
        self.tracker.on_wake()
        logger.info('WAKE UP | refreshing Spotify status')
        self._poll_wake_event.set()

    def _wake_from_sleep(self, reason: str):
        """Wake from sleep and emit one high-signal diagnostic line."""
        if not self.sleep_manager.is_sleeping:
            return
        logger.info(
            f'Wake requested | reason={reason} | '
            f'touch_available={self.evdev_touch.is_available} | '
            f'touch_device={self.evdev_touch.device_name or "none"} | '
            f'touch_path={self.evdev_touch.device_path or "none"}'
        )
        self.sleep_manager.wake_up(reason)
        self._on_wake()
        # Someone touched the speaker awake: the moment to mention, a few
        # times at most, that it can switch to Spotify Soloist. Not when it
        # woke by itself because music started, and never over playing music.
        if 'touch' in reason and not self.now_playing.playing:
            self.setup_menu.show_spotify_prompt()

    def _spotify_needs_pairing(self) -> bool:
        """Whether the new player is on but Mello was never chosen in Spotify.

        Right after switching, Spotify's player knows no account yet: someone
        has to pick Mello once in the Spotify app. Until then nothing saved
        can play, so the screen says what to do instead of showing covers.
        (After that the login is kept; a Mello that is merely still logging
        in after a restart is not "unpaired".)
        """
        if SPOTIFY_ENGINE != 'soloist' or self.mock_mode:
            return False
        if getattr(self.api, 'logged_in', False):
            self._pairing_known = True
            return False
        now = time.time()
        if now - self._pairing_checked_at > 2.0:
            self._pairing_checked_at = now
            users = SOLOIST_DATA_DIR / 'cache' / 'Users'
            try:
                self._pairing_known = any(users.iterdir())
            except OSError:
                self._pairing_known = False
        return not self._pairing_known

    def _save_progress_on_shutdown(self):
        """Save progress synchronously before shutdown."""
        self.playback.save_progress_on_shutdown(self.now_playing)
    
    def _update(self, dt: float):
        """Update application state."""
        self._check_touch_health()
        items = self.display_items
        if items:
            self.selected_index = max(0, min(self.selected_index, len(items) - 1))
        
        # Update carousel
        was_animating = not self.carousel.settled
        self.carousel.update(dt)
        
        focused_item = items[self.selected_index] if self.selected_index < len(items) else None
        if self._manual_pause_lock and self._manual_pause_context_uri:
            active_ctx = self.now_playing.context_uri
            if active_ctx and active_ctx != self._manual_pause_context_uri:
                if self.playback.pause_intent_active:
                    logger.info(
                        'Manual pause lock retained (active_context_changed) | '
                        'reason=pause_intent_active'
                    )
                else:
                    self._clear_manual_pause_lock('active_context_changed')

        self._update_focus_playback(focused_item)
        
        # Check long press for delete mode
        if self.touch.check_long_press():
            self._trigger_delete_mode()
        
        # Update interaction state
        self.user_interacting = (
            self.touch.dragging or 
            not self.carousel.settled or 
            self._pending_focus_uri is not None
        )
        
        self._report_health_changes()

        if self.wifi_keeper.take_restored():
            self._cached_has_network = True
            self.setup_menu.network_restored()
        self.setup_menu.update()

        # Volume hold detection: open menu after MENU_HOLD_TIME seconds
        if self._volume_hold_start is not None and not self._menu_hold_triggered:
            if time.time() - self._volume_hold_start >= MENU_HOLD_TIME:
                self._menu_hold_triggered = True
                self._volume_hold_start = None
                self._pressed_button = None
                self.setup_menu.open()
        
        # Keep volume button visually pressed while holding
        if self._volume_hold_start is not None:
            self._pressed_button = 'volume'
            self._pressed_time = time.time()
        
        if self._pressed_button and not self._volume_hold_start and time.time() - self._pressed_time > BUTTON_PRESS_DURATION:
            self._pressed_button = None
            self.renderer.invalidate()
        
        self._sync_to_playing()
        
        self.playback.update_mock(dt, self.now_playing)
        self.playback.save_progress(self.now_playing)
        
        # Collect playlist covers in background (once per track change)
        # Guard: context_uri comes from WebSocket (instant) but track_cover comes
        # from HTTP /status (can lag). After a context switch, skip collection for
        # 2 seconds so we don't associate the old track's cover with the new playlist.
        np = self.now_playing
        if (np.playing and 'playlist' in (np.context_uri or '')):
            if np.context_uri != self._cover_collect_context:
                self._cover_collect_context = np.context_uri
                self._context_change_time = time.time()
                self._last_cover_collect_key = None
            elif time.time() - self._context_change_time > 2.0:
                track_key = (np.context_uri, np.track_cover)
                if track_key != self._last_cover_collect_key and np.track_cover:
                    self._last_cover_collect_key = track_key
                    run_async(self._collect_cover_async, np.context_uri, np.track_cover)
        else:
            self._cover_collect_context = None
        
        was_awake = not self.sleep_manager.is_sleeping
        # An open menu does not keep the screen on: touching it does. The
        # WiFi screen opens by itself when a speaker starts without WiFi, and
        # kept the backlight on all night on a speaker whose WiFi was gone.
        self.sleep_manager.check_sleep(self.now_playing.playing)
        if was_awake and self.sleep_manager.is_sleeping:
            self._on_fell_asleep()
        
        self.playback.update_loading_state(
            self.now_playing, self.carousel.settled,
            self._pending_focus_uri is not None or self._requested_focus_uri is not None,
        )
        self._check_context_switch_watchdog(focused_item)

        # Detect "should be loading but loader disappeared" condition.
        if focused_item is not None and not focused_item.is_temp:
            expected_loading = (
                not self._is_item_playing(focused_item)
                and (
                    self.playback.play_in_progress
                    or self._pending_focus_uri == focused_item.uri
                    or (
                        self._requested_focus_epoch == self._focus_epoch
                        and self._requested_focus_uri == focused_item.uri
                    )
                )
            )
            mismatch = expected_loading and not self.playback.play_state.should_show_loading
            # Runs every frame: report the start of a mismatch, not each frame of it.
            if mismatch and not self._loader_mismatch_logged:
                logger.warning(
                    'LOADER mismatch | play pending without loading state | '
                    f'focused_uri={focused_item.uri[:40]} | pending_uri={(self._pending_focus_uri or "none")[:40]} | '
                    f'requested_uri={(self._requested_focus_uri or "none")[:40]} | '
                    f'play_in_progress={self.playback.play_in_progress} | epoch={self._focus_epoch}'
                )
            self._loader_mismatch_logged = mismatch

    # ============================================
    # SETUP MENU
    # ============================================
    
    def _draw(self):
        """Draw the UI."""
        items = self.display_items
        np = self.now_playing
        focused_item = items[self.selected_index] if self.selected_index < len(items) else None
        focused_uri = focused_item.uri if focused_item else None
        focused_context_playing = bool(
            focused_item
            and np.playing
            and np.context_uri == focused_uri
        )
        recent_focus_commit = bool(
            focused_uri
            and self._last_play_commit_uri == focused_uri
            and (time.time() - self._last_play_commit_at) < 1.25
        )

        # Snapshot BT state once to avoid race with monitor thread
        bt_dev = self.bluetooth.connected_device

        # First: this starts the phone page the screen's code points at.
        home_connect_spotify = self.setup_menu.home_connect_spotify(
            self._get_cached_network_status(), not items)

        ctx = RenderContext(
            items=items,
            selected_index=self.selected_index,
            now_playing=np,
            scroll_x=self.carousel.scroll_x,
            drag_offset=self.touch.drag_offset,
            dragging=self.touch.dragging,
            is_sleeping=self.sleep_manager.is_sleeping,
            volume_index=self.volume.index,
            delete_mode_id=self.delete_mode_id,
            pressed_button=self._pressed_button,
            # Hide loader as soon as focused context audio is already playing.
            is_loading=self.playback.play_state.is_loading and not (focused_context_playing or recent_focus_commit),
            is_playing=self.playback.play_state.display_playing(np.playing),
            pending_focus_uri=self._pending_focus_uri,
            requested_focus_uri=self._requested_focus_uri,
            play_in_progress=self.playback.play_in_progress,
            toast_message=self._active_toast,
            menu_state=self.setup_menu.state,
            menu_known_networks=self.setup_menu.known_networks,
            menu_current_network=self.setup_menu.current_network,
            menu_wifi_join_failed=self.setup_menu.wifi_join_failed,
            menu_wifi_joining=self.setup_menu.wifi_joining,
            menu_wifi_join_wrong_password=self.setup_menu.wifi_join_wrong_password,
            menu_spotify_items=self.setup_menu.spotify_items,
            menu_spotify_prompt=self.setup_menu.spotify_prompt_active,
            menu_prompt_hold_progress=self.setup_menu.prompt_hold_progress,
            menu_prompt_title=self.setup_menu.prompt_title,
            menu_prompt_benefits=PROMPT_BENEFITS,
            menu_prompt_button=self.setup_menu.prompt_button,
            menu_prompt_hint=PROMPT_SCAN_HINT,
            menu_spotify_menu_items=self.setup_menu.spotify_menu_items,
            home_lines=self.setup_menu.home_lines,
            spotify_needs_pairing=self._spotify_needs_pairing(),
            home_connect_spotify=home_connect_spotify,
            menu_spotify_switching=self.setup_menu.spotify_switching,
            auto_pause_minutes=self.settings.auto_pause_minutes,
            progress_expiry_hours=self.settings.progress_expiry_hours,
            app_version_label=(f'{self.app_version_label} · {self.setup_menu.update_problem}'
                               if self.setup_menu.update_problem else self.app_version_label),
            bt_connected=bt_dev is not None,
            bt_audio_active=self._bt_audio_active,
            bt_connected_name=bt_dev.name if bt_dev else None,
            bt_paired_devices=self.bluetooth.paired_devices,
            bt_discovered_devices=self.bluetooth.discovered_devices,
            bt_scanning=self.bluetooth.scanning,
            bt_pairing_mac=self.bluetooth.pairing_mac,
            volume_levels=self.settings.get_volume_levels(),
            menu_scroll_offset=self.setup_menu.scroll_offset,
            update_label=self.setup_menu.update_button_label,
            reset_confirm_pending=self.setup_menu._reset_confirm_pending,
            has_network=self._get_cached_network_status(),
        )
        self.renderer.draw(ctx)
        if self.delete_mode_id and self.renderer.delete_button_rect:
            self._delete_button_rect = self.renderer.delete_button_rect
        elif not self.delete_mode_id:
            self._delete_button_rect = None

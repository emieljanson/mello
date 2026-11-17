"""
Mello Application - Main application class.
"""
import os
import time
import signal
import logging
import subprocess
import threading
from typing import Optional, List

import pygame

from . import crash_log
from .config import (
    LOG_DIR,
    SCREEN_WIDTH, SCREEN_HEIGHT,
    LIBRESPOT_URL, LIBRESPOT_WS, SPOTIFY_ENGINE, SOLOIST_WS, SPOTIFY_ENGINE_SERVICE, UI_ENGINE,
    SOLOIST_HOME, SOLOIST_DATA_DIR, ENGINE_SETUP_PORT,
    CATALOG_PATH, PROGRESS_PATH, IMAGES_DIR, ICONS_DIR,
    MOCK_MODE,
    COVER_SIZE, COVER_SIZE_SMALL, COVER_SPACING,
    CAROUSEL_X, CAROUSEL_CENTER_Y, CONTROLS_X, BTN_SIZE, PLAY_BTN_SIZE, BTN_SPACING,
    CAROUSEL_TOUCH_MARGIN, MAX_SWIPE_JUMP, VELOCITY_THRESHOLDS,
    ACTION_DEBOUNCE, BUTTON_PRESS_DURATION, MENU_HOLD_TIME,
    CONTEXT_SWITCH_WATCHDOG_TIMEOUT,
    POSTHOG_API_KEY, POSTHOG_HOST, ANALYTICS_DISTINCT_ID,
    ANALYTICS_INCLUDE_CONTENT, ANALYTICS_USE_MACHINE_ID,
)
from .models import CatalogItem, NowPlaying, LibrespotStatus, MenuState
from .api import LibrespotAPI, NullLibrespotAPI, SoloistAPI, CatalogManager
from .handlers import TouchHandler, EventListener, EvdevTouchHandler
from .managers import SleepManager, SmoothCarousel, PlayTimer, PerformanceMonitor, AnimationTimer, AutoPauseManager, SetupMenu, Settings, UsageTracker, BluetoothManager
from .managers.engine_setup import EngineSetup, PROMPT_BENEFITS, PROMPT_SCAN_HINT
from .managers.wifi_keeper import WifiKeeper
from .controllers import VolumeController, PlaybackController, is_repeatable_spotify_context
from .ui import ImageCache, Renderer, RenderContext
from .utils import run_async, get_runtime_version_label, set_system_volume

logger = logging.getLogger(__name__)

LIBRESPOT_RESTART_AFTER = 60.0
LIBRESPOT_RESTART_COOLDOWN = 300.0


class Mello:
    """Main Mello application."""

    # What _sync_to_playing last logged (it runs every frame, see there).
    _last_sync_situation: Optional[tuple] = None
    _last_sync_outcome: Optional[str] = None
    _loader_mismatch_logged = False
    
    def __init__(self, fullscreen: bool = False):
        # Restore display BEFORE pygame takes over DRM device.
        # Previous run may have been killed during sleep, leaving
        # backlight/DPMS off. Must happen before kmsdrm init.
        SleepManager.restore_display()
        self._ui = self._choose_ui()

        # Fast path: init display and show boot splash BEFORE the heavy
        # pygame.init() so the user sees the logo instead of a black screen.
        self._setup_video_driver()
        self._init_display_early(fullscreen)

        # Now init remaining subsystems (splash is already visible).
        # pygame.init() is idempotent for already-inited subsystems.
        pygame.init()
        pygame.mixer.quit()  # Release audio device for go-librespot
        pygame.display.set_caption('Mello')

        if self._ui == 'slint':
            # Slint shows the boot splash as soon as its event loop runs;
            # everything else is set up from inside that loop (start()).
            from .ui.slint_display import SlintDisplay
            self.slint_display = SlintDisplay()
            self.slint_display.show_surface(self.screen)
            return
        self._init_components()
    
    @staticmethod
    def _choose_ui() -> str:
        """Slint when asked for (MELLO_UI=slint) and it can load, else Pygame."""
        if UI_ENGINE != 'slint':
            return 'pygame'
        try:
            import slint  # noqa: F401  (also loads libinput etc.)
        except Exception as e:
            logger.error(f'MELLO_UI=slint but Slint cannot load ({e}); using Pygame')
            return 'pygame'
        logger.info('Screen: Slint')
        return 'slint'

    def _check_kms_available(self) -> bool:
        """Check if KMS/DRM is likely configured on the system."""
        # Check for DRI devices (KMS/DRM creates these)
        if os.path.exists('/dev/dri'):
            try:
                dri_devices = os.listdir('/dev/dri')
                # Should have at least card0 or renderD128
                if any(dev.startswith(('card', 'renderD')) for dev in dri_devices):
                    return True
            except OSError:
                pass
        
        # Check if GL driver is configured (check for vc4-kms-v3d overlay)
        try:
            if os.path.exists('/boot/config.txt'):
                with open('/boot/config.txt', 'r') as f:
                    config = f.read()
                    # Check for KMS-related overlays
                    if (
                        'dtoverlay=vc4-kms-v3d' in config
                        or 'dtoverlay=vc4-kms-dsi-7inch' in config
                        or 'dtoverlay=vc4-kms-dsi-ili9881-5inch' in config
                    ):
                        return True
        except (OSError, IOError):
            pass

        try:
            if os.path.exists('/boot/firmware/config.txt'):
                with open('/boot/firmware/config.txt', 'r') as f:
                    config = f.read()
                    if (
                        'dtoverlay=vc4-kms-v3d' in config
                        or 'dtoverlay=vc4-kms-dsi-7inch' in config
                        or 'dtoverlay=vc4-kms-dsi-ili9881-5inch' in config
                    ):
                        return True
        except (OSError, IOError):
            pass
        
        return False
    
    def _setup_video_driver(self):
        """Select optimal video driver (env var only, no init)."""
        if self._ui == 'slint':
            # Slint owns the display; Pygame only draws into off-screen surfaces.
            os.environ['SDL_VIDEODRIVER'] = 'dummy'
            return
        if os.environ.get('SDL_VIDEODRIVER'):
            return
        if not os.path.exists('/proc/device-tree/model'):
            return
        if self._check_kms_available():
            os.environ['SDL_VIDEODRIVER'] = 'kmsdrm'

    def _init_display_early(self, fullscreen: bool):
        """Init display and show boot splash as fast as possible.

        Called BEFORE pygame.init() so the logo appears immediately
        instead of a black screen while subsystems initialize.
        """
        _t0 = time.monotonic()
        # Init display subsystem only (not all of pygame)
        try:
            pygame.display.init()
            logger.info(f'Display driver: {os.environ.get("SDL_VIDEODRIVER", "default")} '
                        f'(display.init took {time.monotonic()-_t0:.2f}s)')
        except pygame.error as e:
            # kmsdrm failed — fall back to default driver
            driver = os.environ.pop('SDL_VIDEODRIVER', None)
            if driver:
                logger.warning(f'{driver} driver failed ({e}), using default')
            pygame.display.init()

        flags = pygame.DOUBLEBUF
        if fullscreen and self._ui != 'slint':
            # (With Slint the surface is off-screen: "full screen" would make
            # the dummy driver pick its own 1024x768 size.)
            flags |= pygame.FULLSCREEN

        _t1 = time.monotonic()
        try:
            self.screen = pygame.display.set_mode(
                (SCREEN_WIDTH, SCREEN_HEIGHT),
                flags | pygame.HWSURFACE
            )
        except pygame.error:
            self.screen = pygame.display.set_mode(
                (SCREEN_WIDTH, SCREEN_HEIGHT),
                flags
            )
        logger.info(f'set_mode took {time.monotonic()-_t1:.2f}s')

        # Show boot splash immediately — bridges Plymouth → app transition
        self._show_boot_splash()

        self.clock = pygame.time.Clock()
        pygame.mouse.set_visible(not fullscreen)

        self._log_video_info()

    def _show_boot_splash(self):
        """Show Mello logo on dark background with gradient.

        Plymouth shows plain black during early boot.  Once pygame takes
        over the DRM device we paint the logo + top gradient so the user
        sees something familiar while the rest of the app initialises.
        """
        try:
            logo_path = os.path.join(ICONS_DIR, 'mello-logo.png')
            if not os.path.exists(logo_path):
                return
            logo = pygame.image.load(logo_path).convert_alpha()
            # Scale to 320px wide (same as idle screen)
            logo_width = 320
            scale = logo_width / logo.get_width()
            logo = pygame.transform.smoothscale(
                logo, (logo_width, int(logo.get_height() * scale))
            )
            # Rotate for portrait display
            logo = pygame.transform.rotate(logo, -90)

            # Background with top gradient (matches carousel background)
            bg = (13, 13, 13)
            self.screen.fill(bg)
            for offset in range(150):
                x = SCREEN_WIDTH - 1 - offset
                alpha = int(30 * (1 - offset / 150))
                color = (
                    min(255, bg[0] + int(alpha * 0.75)),
                    min(255, bg[1] + int(alpha * 0.4)),
                    min(255, bg[2] + alpha),
                )
                pygame.draw.line(self.screen, color, (x, 0), (x, SCREEN_HEIGHT))

            x = (self.screen.get_width() - logo.get_width()) // 2
            y = (self.screen.get_height() - logo.get_height()) // 2
            self.screen.blit(logo, (x, y))
            pygame.display.flip()
            logger.info('Boot splash displayed')
        except Exception as e:
            logger.warning(f'Could not show boot splash: {e}')

    def _init_components(self):
        """Initialize all application components."""
        self.app_version_label = get_runtime_version_label()
        logger.info(f'App version: {self.app_version_label}')

        # Mock mode
        self.mock_mode = MOCK_MODE
        
        # API & Catalog (use NullAPI in mock mode)
        if self.mock_mode:
            self.api = NullLibrespotAPI()
        elif SPOTIFY_ENGINE == 'soloist':
            # Soloist has one socket for commands and events, so the same
            # object is both the API and the event listener.
            self.api = SoloistAPI(
                SOLOIST_WS, self._on_ws_update, self._on_ws_reconnect,
                on_inactive=self._on_spotify_inactive,
            )
        else:
            self.api = LibrespotAPI(LIBRESPOT_URL)
        self.settings = Settings()
        self.catalog_manager = CatalogManager(
            CATALOG_PATH, IMAGES_DIR, mock_mode=self.mock_mode,
            progress_path=PROGRESS_PATH,
            get_progress_expiry=lambda: self.settings.progress_expiry_hours,
        )
        self.catalog_manager.load()
        
        # UI Components
        self.image_cache = ImageCache(IMAGES_DIR)
        self.icons = self._load_icons()
        self.renderer = Renderer(self.screen, self.image_cache, self.icons)
        self.renderer.compositor = self._ui == 'slint'
        
        # Handlers
        self.touch = TouchHandler()
        if isinstance(self.api, SoloistAPI):
            self.events = self.api
        else:
            self.events = EventListener(
                LIBRESPOT_WS, self._on_ws_update, self._on_ws_reconnect,
                on_inactive=self._on_spotify_inactive,
            )
        
        # Evdev touch handler for KMSDRM mode (reads /dev/input directly)
        self.evdev_touch = EvdevTouchHandler(SCREEN_WIDTH, SCREEN_HEIGHT)
        touch_available = self.evdev_touch.start()  # Starts background thread if touchscreen found
        
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
        )
        
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
        self._last_librespot_restart_at: float = 0.0
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
    
    def _load_icons(self) -> dict:
        """Load icon images."""
        icons = {}
        icon_files = {
            'play': 'play-fill.png',
            'pause': 'pause-fill.png',
            'prev': 'skip-back-fill.png',
            'next': 'skip-forward-fill.png',
            'volume_none': 'speaker-none-fill.png',
            'volume_low': 'speaker-low-fill.png',
            'volume_high': 'speaker-high-fill.png',
            'plus': 'plus-circle-fill.png',
            'minus': 'minus-circle-fill.png',
            'headphone': 'headphone.png',
            'close': 'close.png',
            'back': 'back.png',
            'logo': 'mello-logo.png',
        }
        for name, filename in icon_files.items():
            try:
                icons[name] = pygame.image.load(ICONS_DIR / filename).convert_alpha()
            except Exception as e:
                logger.warning(f'Failed to load icon {filename}: {e}', exc_info=True)
        return icons
    
    def _show_toast(self, message: str):
        """Show a brief toast message on screen."""
        self._toast_message = message
        self._toast_time = time.time()
        self.renderer.invalidate()

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
        self.image_cache.cache.clear()
        self.image_cache._access_times.clear()
    
    @property
    def _active_toast(self) -> Optional[str]:
        """Return toast message if still within display duration."""
        if self._toast_message and time.time() - self._toast_time < self._toast_duration:
            return self._toast_message
        self._toast_message = None
        return None
    
    def _log_video_info(self):
        """Log video driver and display info."""
        video_driver = os.environ.get('SDL_VIDEODRIVER', 'default')
        actual_driver = pygame.display.get_driver()
        info = pygame.display.Info()
        
        logger.info(f'Display: {actual_driver} (requested: {video_driver})')
        logger.info(f'Resolution: {info.current_w}x{info.current_h}')
        
        # Check for Raspberry Pi
        if os.path.exists('/proc/device-tree/model'):
            try:
                with open('/proc/device-tree/model', 'r') as f:
                    pi_model = f.read().strip().replace('\x00', '')
                logger.info(f'Device: {pi_model}')
                
                # Only show warning if not using GPU acceleration
                if actual_driver not in ('kmsdrm', 'KMSDRM'):
                    kms_available = self._check_kms_available()
                    if not kms_available:
                        logger.debug('KMS/DRM not detected - GPU acceleration unavailable')
                    else:
                        logger.debug('KMS/DRM detected but not using kmsdrm driver')
            except Exception:
                pass

    def _read_text_file(self, path: str) -> Optional[str]:
        """Read a text file for diagnostics."""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception:
            return None

    def _boot_config_status(self) -> str:
        """Return compact boot config diagnostics for field support."""
        config_path = None
        for path in ('/boot/firmware/config.txt', '/boot/config.txt'):
            if os.path.exists(path):
                config_path = path
                break
        if not config_path:
            return 'missing'
        content = self._read_text_file(config_path) or ''
        explicit_overlay = 'dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90' in content
        display_auto_detect_active = any(
            line.strip() == 'display_auto_detect=1'
            for line in content.splitlines()
        )
        disable_splash = any(
            line.strip() == 'disable_splash=1'
            for line in content.splitlines()
        )
        return (
            f'path={config_path}, ili9881_5inch_overlay={explicit_overlay}, '
            f'display_auto_detect_active={display_auto_detect_active}, '
            f'disable_splash={disable_splash}'
        )

    def _log_startup_health(self):
        """Log display/touch health so black-screen diagnosis is evidence-based."""
        backlight_value = (
            self._read_text_file(self.sleep_manager.backlight_path)
            if self.sleep_manager.backlight_path else None
        )
        dsi_status = self._read_text_file('/sys/class/drm/card0-DSI-1/status')
        dsi_dpms = self._read_text_file('/sys/class/drm/card0-DSI-1/dpms')
        logger.info(f'HEALTH boot_config: {self._boot_config_status()}')
        logger.info(
            'HEALTH display: '
            f'backlight_path={self.sleep_manager.backlight_path or "none"}, '
            f'backlight_value={backlight_value or "unknown"}, '
            f'dsi_status={dsi_status or "unknown"}, dsi_dpms={dsi_dpms or "unknown"}'
        )
        logger.info(
            'HEALTH touch: '
            f'available={self.evdev_touch.is_available}, '
            f'device={self.evdev_touch.device_name or "none"}, '
            f'path={self.evdev_touch.device_path or "none"}, '
            f'sleep_enabled={self.sleep_manager.sleep_enabled}'
        )

    def _disable_sleep_for_touch(self, reason: str):
        """Disable sleep when touch wake is unavailable, waking display if needed."""
        was_sleeping = self.sleep_manager.is_sleeping
        self.sleep_manager.disable_sleep(f'touch wake unavailable: {reason}')
        if was_sleeping:
            self.bluetooth.resume_monitoring()
            self.tracker.on_wake()
        self.renderer.invalidate()

    def _check_touch_health(self):
        """React if the background touch reader fails after startup."""
        reason = self.evdev_touch.consume_failure_reason()
        if reason:
            self._disable_sleep_for_touch(reason)
    
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

    def _handle_signal(self, signum, frame):
        """Handle SIGTERM/SIGINT/SIGHUP for graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info(f'Received {sig_name}, shutting down...')
        crash_log.stop_requested()
        self.running = False
    
    def start(self):
        """Start the application."""
        if self._ui == 'slint':
            self._run_slint_loop()
            if hasattr(self, 'tracker'):   # set up (not stopped during the splash)
                self._shutdown()
            return
        self._start_services()
        self._run_pygame_loop()
        self._shutdown()

    def _start_services(self):
        """Everything that runs once before the first frame."""
        logger.info('Starting Mello...')
        self.tracker.on_app_started(catalog_size=len(self.catalog_manager.items))
        
        # Pre-load images
        self.image_cache.preload_catalog(self.catalog_manager.items)
        
        if not self.mock_mode:
            self.events.start()
            self.catalog_manager.cleanup_unused_images()
            
            # Set system volume at startup (also unmutes as safety reset)
            self.volume.init()
            
            # Status polling and the initial connect run for as long as the
            # app does; in the shared pool of 4 they would leave only two
            # workers for pause, skip and saving progress.
            threading.Thread(target=self._poll_status, daemon=True, name='status-poll').start()
            logger.info(f'Polling {SOLOIST_WS if SPOTIFY_ENGINE == "soloist" else LIBRESPOT_URL} ({SPOTIFY_ENGINE})')

            # Force initial connection check (don't wait for first poll interval)
            threading.Thread(target=self._initial_connect, daemon=True, name='initial-connect').start()

            # Start Bluetooth monitoring
            self.bluetooth.start_monitoring()
            self.wifi_keeper.start()
        else:
            logger.info('Running in MOCK MODE')
            self._startup_ready = True
        
        logger.info('Entering main loop...')

    def _run_pygame_loop(self):
        """The main loop when Pygame shows the screen."""
        dt = 1.0 / 60  # Initial delta time

        # Main loop
        while self.running:
            crash_log.heartbeat()
            self._apply_spotify_inactive()
            self._check_touch_health()
            # Sleep mode: wait for touch/key to wake up
            if self.sleep_manager.is_sleeping:
                # Time asleep is not a frame: without this the first frame
                # after waking is handed hours of dt and logged as a freeze.
                self.clock.tick()
                dt = 1.0 / 60
                # Primary wake: evdev threading.Event (reliable across threads)
                # Fallback: pygame.event.wait with timeout (catches KEYDOWN/QUIT)
                self.evdev_touch.wake_event.wait(0.2)
                if self.evdev_touch.wake_event.is_set():
                    self.evdev_touch.wake_event.clear()
                    self._wake_from_sleep('evdev_touch')
                    pygame.event.clear()  # Discard stale events from sleep
                    continue
                # Check for keyboard/quit events that bypass evdev
                for event in pygame.event.get():
                    if event.type == pygame.KEYDOWN:
                        self._wake_from_sleep(f'key:{event.key}')
                        pygame.event.clear()
                        break
                    elif event.type == pygame.QUIT:
                        self.running = False
                        break
                self._log_sleep_wait_if_due()
                continue
            
            self.anim_timer.begin_frame()
            self._handle_events()
            self.anim_timer.mark('events')
            self._update(dt)
            self.anim_timer.mark('update')
            dirty_rects = self._draw()
            self.anim_timer.mark('draw')
            
            if dirty_rects:
                pygame.display.update(dirty_rects)
            else:
                pygame.display.flip()
            self.anim_timer.mark('display')
            
            target_fps = self._target_fps()
            is_animating = not self.carousel.settled or self.touch.dragging
            
            if target_fps <= 5 and not is_animating:
                # Idle: true sleep instead of busy-wait, CPU can idle
                # Keep the frame clock running while idling: otherwise the
                # first frame after idle is handed the whole idle time as dt
                # and logged as a multi-second freeze.
                pygame.time.wait(200)
                dt = self.clock.tick() / 1000.0
            else:
                dt = self.clock.tick(target_fps) / 1000.0
            
            target_frame_time = 1.0 / target_fps
            spike_threshold = max(0.1, target_frame_time * 1.2)
            if dt > spike_threshold and target_fps > 5:
                logger.warning(f'Frame spike: {dt*1000:.0f}ms (target: {target_fps} FPS)')
            
            self.perf_monitor.update(dt)
            anim_summary = self.anim_timer.end_frame(is_animating, dt)
            if anim_summary:
                logger.info(anim_summary)
            self._log_fps_if_due(target_fps)

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
        pygame.quit()
        crash_log.mark_clean_exit(LOG_DIR)
        logger.info('Mello stopped')
    
    def _run_slint_loop(self):
        """The main loop when Slint shows the screen (MELLO_UI=slint).

        Same work per frame as the Pygame loop below, but Slint's event loop
        calls it (every ~8 ms) instead of a while loop that sleeps: frames are
        paced by the target fps here, and Slint draws when a property changed.
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
                    self._wake_from_sleep('evdev_touch')
                    pygame.event.clear()
                    return True
                for event in pygame.event.get():
                    if event.type == pygame.KEYDOWN:
                        self._wake_from_sleep(f'key:{event.key}')
                        pygame.event.clear()
                        break
                    elif event.type == pygame.QUIT:
                        self.running = False
                        return False
                self._log_sleep_wait_if_due()
                return True

            target_fps = self._target_fps()
            is_animating = not self.carousel.settled or self.touch.dragging
            interval = 0.2 if (target_fps <= 5 and not is_animating) else 1.0 / target_fps
            if now - state['last'] < interval - 0.004:
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
            display.present(self.screen)
            self.anim_timer.mark('display')

            spike_threshold = max(0.1, 1.0 / target_fps * 1.2)
            if dt > spike_threshold and target_fps > 5:
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
    
    def _log_fps_if_due(self, target_fps: int):
        """Log FPS stats periodically and warn on drops."""
        now = time.time()
        if now - self._last_fps_log < self._fps_log_interval:
            return
        
        self._last_fps_log = now
        avg_fps = self.perf_monitor.current_fps
        is_loading = self.playback.play_state.is_loading
        items = self.display_items
        focused = items[self.selected_index].name if items and self.selected_index < len(items) else '?'
        playing_ctx = self.now_playing.context_uri or 'none'
        playing_name = self.now_playing.track_name or 'none'
        api_metrics = self.api.metrics_snapshot() if hasattr(self.api, 'metrics_snapshot') else {}
        suppressed = api_metrics.get('suppressed', {})
        failures = api_metrics.get('failures', {})
        
        logger.info(
            f'STATE | focused="{focused}" | playing="{playing_name}" | ctx={playing_ctx[:40]} '
            f'| driving={self._user_driving} | loading={is_loading} | connected={self.connected} '
            f'| fps={avg_fps:.0f}/{target_fps} '
            f'| api_suppressed={suppressed} | api_failures={failures}'
        )

        focused_uri = items[self.selected_index].uri if items and self.selected_index < len(items) else None
        if (self.now_playing.playing and focused_uri and playing_ctx
                and focused_uri != playing_ctx and not is_loading and not self._user_driving):
            logger.warning(
                f'MISMATCH | screen="{focused}" | audio="{playing_name}" '
                f'| focused_uri={focused_uri[:40]} | playing_ctx={playing_ctx[:40]} '
                f'| interacting={self.user_interacting} '
                f'| settled={self.carousel.settled} | timer={self.play_timer.item is not None}'
            )
        
        if not self.sleep_manager.is_sleeping:
            if target_fps == 60 and avg_fps < 30:
                logger.warning(f'Low FPS during animation: {avg_fps:.1f} (target: 60 FPS)')
            elif target_fps == 10 and avg_fps < 8:
                logger.warning(f'Low FPS while playing: {avg_fps:.1f} (target: 10 FPS)')
            elif target_fps == 5 and avg_fps < 4:
                logger.warning(f'Low FPS while idle: {avg_fps:.1f} (target: 5 FPS)')
    
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
            else:
                logger.warning(f'CONNECTION LOST after {self._connection_fail_count} failures')
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
        except Exception as exc:
            logger.error(f'LIBRESPOT recovery | restart error: {exc}', exc_info=True)
    
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
    
    def _handle_events(self):
        """Handle pygame events."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            
            elif event.type == pygame.MOUSEBUTTONDOWN:
                logger.debug(f'Event: MOUSEBUTTONDOWN at {event.pos}')
                if self.sleep_manager.is_sleeping:
                    self._wake_from_sleep(f'pygame_touch:{event.pos}')
                    continue
                self.sleep_manager.reset_timer()
                self._handle_touch_down(event.pos)
            
            elif event.type == pygame.KEYDOWN:
                if self.sleep_manager.is_sleeping:
                    self._wake_from_sleep(f'pygame_key:{event.key}')
                    continue
                self.sleep_manager.reset_timer()
                self._handle_key(event.key)
            
            elif event.type == pygame.MOUSEMOTION:
                if self.setup_menu.is_open and self._menu_touch_start is not None:
                    # Menu scroll: track vertical drag (physical x-axis)
                    dx = event.pos[0] - self._menu_touch_start[0]
                    if abs(dx) > 10 and self.renderer.menu_content_overflow > 0:
                        self._menu_touch_scrolled = True
                        self.setup_menu.handle_scroll(
                            dx, self.renderer.menu_content_overflow)
                        self._menu_touch_start = event.pos
                elif self.touch.dragging:
                    self.sleep_manager.reset_timer()
                    self.touch.on_move(event.pos)

                    # Cancel delete mode when user starts swiping
                    if self.touch.is_swiping and self.delete_mode_id:
                        self.delete_mode_id = None
                        self.renderer.invalidate()

            elif event.type == pygame.MOUSEBUTTONUP:
                logger.debug(f'Event: MOUSEBUTTONUP at {event.pos}')
                if not self.sleep_manager.is_sleeping:
                    if self.setup_menu.is_open and self._menu_touch_start is not None:
                        self.setup_menu.handle_release()
                        if not self._menu_touch_scrolled:
                            # Flash pressed state on close/back button
                            close_rect = self.renderer.menu_button_rects.get('close')
                            if close_rect and close_rect.collidepoint(*event.pos):
                                self._pressed_button = 'menu_close'
                                self._pressed_time = time.time()
                            self.setup_menu.handle_tap(event.pos, self.renderer.menu_button_rects)
                        self._menu_touch_start = None
                        self._menu_touch_scrolled = False
                    else:
                        self._handle_touch_up(event.pos)
                    self._handle_button_up()
    
    def _handle_key(self, key):
        """Handle keyboard input."""
        if key == pygame.K_ESCAPE:
            self.running = False
        elif key == pygame.K_LEFT:
            self._navigate(-1)
        elif key == pygame.K_RIGHT:
            self._navigate(1)
        elif key == pygame.K_SPACE or key == pygame.K_RETURN:
            self._toggle_play()
        elif key == pygame.K_n:
            self._skip_track(self.api.next)
        elif key == pygame.K_p:
            self._skip_track(self.api.prev)
    
    def _handle_touch_down(self, pos):
        """Handle touch/mouse down."""
        # Menu intercept — track touch start for scroll vs tap detection
        if self.setup_menu.is_open:
            self._menu_touch_start = pos
            self._menu_touch_scrolled = False
            self.setup_menu.handle_press(pos, self.renderer.menu_button_rects)
            return
        
        x, y = pos
        
        carousel_x_min = CAROUSEL_X - CAROUSEL_TOUCH_MARGIN
        carousel_x_max = CAROUSEL_X + COVER_SIZE + CAROUSEL_TOUCH_MARGIN
        
        logger.debug(f'Touch down: pos={pos}, carousel_x_range={carousel_x_min}-{carousel_x_max}')

        # Delete mode is modal: the next tap confirms delete or cancels it.
        # It must never fall through to cover/play handling.
        if self.delete_mode_id:
            self._handle_delete_mode_tap(pos)
            return
        
        # Check button clicks
        if self._check_button_click(pos):
            logger.debug('Touch down: button click')
            return
        
        # Carousel swipes - within carousel X zone, full Y range
        if carousel_x_min <= x <= carousel_x_max:
            logger.debug('Touch down: carousel swipe start')
            self.touch.on_down(pos)
            self.user_interacting = True
        else:
            logger.debug('Touch down: outside carousel')
            self._handle_button_tap(pos)
    
    def _check_button_click(self, pos) -> bool:
        """Check if click is on add/delete button."""
        x, y = pos
        
        if self.renderer.add_button_rect:
            bx, by, bw, bh = self.renderer.add_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._save_temp_item()
                return True
        
        if self.renderer.delete_button_rect:
            bx, by, bw, bh = self.renderer.delete_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._delete_current_item()
                return True

        if self.renderer.settings_button_rect:
            bx, by, bw, bh = self.renderer.settings_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.setup_menu.open()
                return True

        return False

    @staticmethod
    def _point_in_rect(pos, rect: Optional[tuple]) -> bool:
        """Return True if pos is inside a tuple/pygame-style rect."""
        if not rect:
            return False
        x, y = pos
        bx, by, bw, bh = rect
        return bx <= x <= bx + bw and by <= y <= by + bh

    def _delete_fallback_rect(self) -> tuple:
        """Expected delete overlay hit rect for the centered cover.

        Mirrors Renderer._draw_overlay_button geometry. This is only a
        safety net for the rare case where the visual delete mode is active
        but the renderer hit rect was not captured yet.
        """
        btn_size, margin, touch_padding = 100, 16, 60
        cover_x = CAROUSEL_X
        cover_y = CAROUSEL_CENTER_Y - COVER_SIZE // 2
        btn_x = cover_x + margin
        btn_y = cover_y + COVER_SIZE - btn_size - margin
        hit_x = btn_x - touch_padding
        hit_y = btn_y - touch_padding
        hit_size = btn_size + touch_padding * 2
        return (hit_x, hit_y, hit_size, hit_size)

    def _focused_delete_item(self) -> Optional[CatalogItem]:
        """Return focused catalog item while delete mode is active."""
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return None
        item = items[self.selected_index]
        if item.is_temp or item.id != self.delete_mode_id:
            return None
        return item

    def _handle_delete_mode_tap(self, pos):
        """Confirm or cancel delete mode, consuming the tap either way."""
        item = self._focused_delete_item()
        renderer_rect = self.renderer.delete_button_rect
        remembered_rect = self._delete_button_rect
        fallback_rect = self._delete_fallback_rect()
        active_rect = renderer_rect or remembered_rect

        if item is None:
            logger.info(
                'Delete confirm hit-test | result=stale_item '
                f'| pos={pos} | rect={active_rect} | fallback={fallback_rect} '
                f'| delete_mode_id={self.delete_mode_id}'
            )
            self.delete_mode_id = None
            self._delete_button_rect = None
            self.renderer.invalidate()
            return

        if active_rect is None:
            result = 'confirm' if self._point_in_rect(pos, fallback_rect) else 'rect_missing'
            hit = result == 'confirm'
        else:
            hit = self._point_in_rect(pos, active_rect) or self._point_in_rect(pos, fallback_rect)
            result = 'confirm' if hit else 'miss_cancel'

        logger.info(
            'Delete confirm hit-test | '
            f'result={result} | pos={pos} | rect={active_rect} | fallback={fallback_rect} '
            f'| focused="{item.name}" | focused_id={item.id} | focused_uri={item.uri[:50]} '
            f'| delete_mode_id={self.delete_mode_id}'
        )

        if hit:
            self._delete_current_item()
            return

        self.delete_mode_id = None
        self._delete_button_rect = None
        self.renderer.invalidate()
    
    def _handle_touch_up(self, pos):
        """Handle touch/mouse up."""
        logger.debug(f'Touch up: pos={pos}, dragging={self.touch.dragging}')
        if not self.touch.dragging:
            logger.debug('Touch up: ignored (not dragging)')
            return
        
        drag_index_offset = -self.touch.drag_offset / (COVER_SIZE + COVER_SPACING)
        visual_position = self.selected_index + drag_index_offset
        
        action, velocity = self.touch.on_up(pos)
        self.carousel.scroll_x = visual_position
        
        x, y = pos
        
        if action in ('left', 'right'):
            abs_vel = abs(velocity)
            v_low, v_mid, v_high = VELOCITY_THRESHOLDS
            velocity_bonus = 0 if abs_vel < v_low else (1 if abs_vel < v_mid else (2 if abs_vel < v_high else 3))
            
            base_target = round(visual_position)
            target = base_target + velocity_bonus if velocity < 0 else base_target - velocity_bonus
            
            target = max(self.selected_index - MAX_SWIPE_JUMP, min(target, self.selected_index + MAX_SWIPE_JUMP))
            target = max(0, min(target, len(self.display_items) - 1))
            
            self._snap_to(target)
        elif action == 'tap':
            # Debounce tap actions
            now = time.time()
            if now - self._last_action_time < ACTION_DEBOUNCE:
                logger.debug('Carousel tap debounced')
                return
            
            # Carousel runs along Y axis - check Y position for tap target
            center_y = CAROUSEL_CENTER_Y  # 640
            if y < center_y - COVER_SIZE // 2:
                # Tap on previous item (lower Y)
                self._navigate(-1)
            elif y > center_y + COVER_SIZE // 2:
                # Tap on next item (higher Y)
                self._navigate(1)
            else:
                logger.debug('Carousel tap: play')
                self._last_action_time = now
                self._pressed_button = 'play'
                self._pressed_time = now
                self._toggle_play()
                self.renderer.invalidate()
    
    def _handle_button_tap(self, pos):
        """Handle direct tap on control buttons with debouncing.
        
        Portrait mode: buttons stacked vertically at X=CONTROLS_X, along Y axis.
        """
        now = time.time()
        if now - self._last_action_time < ACTION_DEBOUNCE:
            logger.debug(f'Button tap debounced at ({pos[0]}, {pos[1]})')
            return
        
        x, y = pos
        center_y = CAROUSEL_CENTER_Y
        btn_spacing = BTN_SPACING  # 155

        # Volume button Y position (matches renderer)
        vol_y = center_y + (COVER_SIZE + COVER_SPACING) + COVER_SIZE_SMALL // 2 - BTN_SIZE // 2

        # Headphone button Y position (matches renderer constant)
        hp_y = center_y - (COVER_SIZE + COVER_SPACING) - COVER_SIZE_SMALL // 2 + BTN_SIZE // 2

        # Portrait mode: check if X is in button column
        if CONTROLS_X - PLAY_BTN_SIZE <= x <= CONTROLS_X + PLAY_BTN_SIZE:
            button_pressed = None

            # Headphone: Y = hp_y (~107) — only active when BT device connected
            if hp_y - BTN_SIZE <= y <= hp_y + BTN_SIZE and self.bluetooth.connected_device:
                button_pressed = 'headphone'
                self.bluetooth.toggle_audio()
            # Prev: Y = center_y - btn_spacing (485)
            elif center_y - btn_spacing - BTN_SIZE <= y <= center_y - btn_spacing + BTN_SIZE:
                button_pressed = 'prev'
                self._skip_track(self.api.prev)
            # Play: Y = center_y (640)
            elif center_y - PLAY_BTN_SIZE <= y <= center_y + PLAY_BTN_SIZE:
                button_pressed = 'play'
                self._toggle_play()
            # Next: Y = center_y + btn_spacing (795)
            elif center_y + btn_spacing - BTN_SIZE <= y <= center_y + btn_spacing + BTN_SIZE:
                button_pressed = 'next'
                self._skip_track(self.api.next)
            # Volume: Y = vol_y (~1173) — start hold timer; action fires on release
            elif vol_y - BTN_SIZE <= y <= vol_y + BTN_SIZE:
                button_pressed = 'volume'
                self._volume_hold_start = now
                self._menu_hold_triggered = False
                # Don't toggle volume here — wait for button up (short tap) or hold (menu)
            
            if button_pressed:
                logger.debug(f'Button press: {button_pressed}')
                self._last_action_time = now
                self._pressed_button = button_pressed
                self._pressed_time = now
                self.renderer.invalidate()
    
    def _snap_to(self, target_index: int):
        """Snap carousel to a specific index.

        When the index changes this is the single point where playback
        is interrupted: timer cancelled, running play-thread invalidated,
        and instant silence sent to librespot.
        """
        items = self.display_items
        if not items:
            return

        target_index = max(0, min(target_index, len(items) - 1))

        if target_index != self.selected_index:
            old_index = self.selected_index
            self.selected_index = target_index
            self.carousel.set_target(target_index)
            self._bump_focus_epoch(f'snap {old_index}->{target_index}')
            self._clear_manual_pause_lock('focus_changed')

            self.play_timer.cancel()
            was_playing_or_loading = self.now_playing.playing or self.playback.play_in_progress
            self.playback.stop_all()
            self.volume.mute()
            now = time.time()
            should_pause_remote = (
                now - self._last_snap_pause_at > 0.4 and
                was_playing_or_loading
            )
            if should_pause_remote:
                self._last_snap_pause_at = now
                run_async(self.api.pause)
            item = items[target_index]
            self._user_driving = not item.is_temp and not self._is_item_playing(item)
            if self._user_driving:
                self._pending_focus_uri = item.uri
                self.playback.play_state.start_loading()
            logger.info(f'Snap: {old_index} -> {target_index}, item={item.name}, play_pending={self._user_driving}')
        else:
            self.carousel.set_target(target_index)
    
    def _navigate(self, direction: int):
        """Navigate carousel."""
        items = self.display_items
        if not items:
            return
        
        new_index = max(0, min(self.selected_index + direction, len(items) - 1))
        self._snap_to(new_index)
    
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

    def _log_sleep_wait_if_due(self):
        """Log periodic sleeping heartbeat so black-screen reports have context."""
        now = time.time()
        if now - self._last_sleep_wait_log < 60:
            return
        self._last_sleep_wait_log = now
        logger.info(
            f'Sleep wait | touch_available={self.evdev_touch.is_available} | '
            f'touch_device={self.evdev_touch.device_name or "none"} | '
            f'wake_event_set={self.evdev_touch.wake_event.is_set()}'
        )
        
    
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
                self.image_cache.preload_catalog(self.catalog_manager.items)
                with self._temp_item_lock:
                    if self.temp_item and self.temp_item.uri == temp.uri:
                        self.temp_item = None
                self.renderer.invalidate()
        finally:
            self._saving = False
    
    def _delete_current_item(self):
        """Delete the current item from catalog."""
        logger.info(
            f'Delete requested | delete_mode_id={self.delete_mode_id} '
            f'| deleting={self._deleting}'
        )
        if not self.delete_mode_id:
            logger.warning('Delete ignored | reason=no_delete_mode_id')
            return
        if self._deleting:
            logger.warning('Delete ignored | reason=already_deleting')
            return
        self._deleting = True
        
        try:
            item_id = self.delete_mode_id
            old_index = self.selected_index
            
            item = next((i for i in self.catalog_manager.items if i.id == item_id), None)
            if item:
                logger.info(f'Deleting: {item.name} | id={item.id} | uri={item.uri[:50]}')
            else:
                logger.warning(f'Delete target not found in catalog | id={item_id}')
            
            success = self.catalog_manager.delete_item(item_id)
            
            if success:
                self.catalog_manager.load()
                self._update_carousel_max_index()
                
                new_index = max(0, old_index - 1)
                if self.display_items:
                    new_index = min(new_index, len(self.display_items) - 1)
                    self.selected_index = new_index
                    self._bump_focus_epoch(f'delete select -> {new_index}')
                    self.carousel.scroll_x = float(new_index)
                    self.carousel.set_target(new_index)
                    
                    new_item = self.display_items[new_index]
                    logger.info(
                        f'Delete success | removed_id={item_id} | '
                        f'new_index={new_index} | new_item="{new_item.name}" | '
                        f'new_uri={new_item.uri[:50]}'
                    )
                    if not new_item.is_temp:
                        self._play_item(new_item.uri)
                else:
                    logger.info(f'Delete success | removed_id={item_id} | catalog empty')
            else:
                logger.warning(f'Delete failed | id={item_id}')
            
            self.delete_mode_id = None
            self._delete_button_rect = None
            self.renderer.invalidate()
        finally:
            self._deleting = False
    
    def _trigger_delete_mode(self):
        """Trigger delete mode for the currently selected item."""
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return
        
        item = items[self.selected_index]
        if item.is_temp:
            return
        
        logger.info(f'Delete mode: {item.name}')
        self.delete_mode_id = item.id
        self._delete_button_rect = None
        self.renderer.invalidate()
    
    def _save_progress_on_shutdown(self):
        """Save progress synchronously before shutdown."""
        self.playback.save_progress_on_shutdown(self.now_playing)
    
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
        elif now - self._pending_focus_since >= 1.0:
            self._play_item(item.uri)

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
        # Don't sleep while the setup menu is open (e.g. WiFi AP mode)
        menu_open = self.setup_menu.state != MenuState.CLOSED
        self.sleep_manager.check_sleep(self.now_playing.playing or menu_open)
        if was_awake and self.sleep_manager.is_sleeping:
            self.bluetooth.pause_monitoring()
            idle = time.time() - self.sleep_manager.last_activity
            self.tracker.on_sleep(idle)
        
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
    
    def _handle_button_up(self):
        """Handle MOUSEBUTTONUP for volume hold: short tap → toggle, long hold → already opened menu."""
        if self._volume_hold_start is None:
            return
        if self._menu_hold_triggered:
            self._volume_hold_start = None
            self._menu_hold_triggered = False
            return
        # Short tap: toggle volume
        self.volume.toggle()
        # Also set BT sink volume when BT audio is active
        if self._bt_audio_active:
            self.bluetooth.set_volume(self.volume.bt_level)
        self._last_action_time = time.time()
        self._volume_hold_start = None
    
    
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
            app_version_label=self.app_version_label,
            bt_connected=bt_dev is not None,
            bt_audio_active=self._bt_audio_active,
            bt_connected_name=bt_dev.name if bt_dev else None,
            bt_paired_devices=self.bluetooth.paired_devices,
            bt_discovered_devices=self.bluetooth.discovered_devices,
            bt_scanning=self.bluetooth.scanning,
            bt_pairing_mac=self.bluetooth.pairing_mac,
            volume_levels=self.settings.get_volume_levels(),
            menu_scroll_offset=self.setup_menu.scroll_offset,
            update_checking=self.setup_menu._update_checking,
            update_available=self.setup_menu._update_available,
            update_running=self.setup_menu._update_running,
            reset_confirm_pending=self.setup_menu._reset_confirm_pending,
            has_network=self._get_cached_network_status(),
        )
        dirty_rects = self.renderer.draw(ctx)
        if self.delete_mode_id and self.renderer.delete_button_rect:
            self._delete_button_rect = self.renderer.delete_button_rect
        elif not self.delete_mode_id:
            self._delete_button_rect = None
        return dirty_rects

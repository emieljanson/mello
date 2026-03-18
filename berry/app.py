"""
Berry Application - Main application class.
"""
import os
import time
import signal
import logging
import subprocess
import threading
from typing import Optional, List

import pygame

from .config import (
    SCREEN_WIDTH, SCREEN_HEIGHT,
    LIBRESPOT_URL, LIBRESPOT_WS,
    CATALOG_PATH, PROGRESS_PATH, IMAGES_DIR, ICONS_DIR,
    MOCK_MODE,
    COVER_SIZE, COVER_SIZE_SMALL, COVER_SPACING,
    CAROUSEL_X, CAROUSEL_CENTER_Y, CONTROLS_X, BTN_SIZE, PLAY_BTN_SIZE, BTN_SPACING,
    CAROUSEL_TOUCH_MARGIN, MAX_SWIPE_JUMP, VELOCITY_THRESHOLDS,
    ACTION_DEBOUNCE, BUTTON_PRESS_DURATION, MENU_HOLD_TIME,
)
from .models import CatalogItem, NowPlaying, LibrespotStatus
from .api import LibrespotAPI, NullLibrespotAPI, CatalogManager
from .handlers import TouchHandler, EventListener, EvdevTouchHandler
from .managers import SleepManager, SmoothCarousel, PlayTimer, PerformanceMonitor, AutoPauseManager, SetupMenu, Settings
from .controllers import VolumeController, PlaybackController
from .ui import ImageCache, Renderer, RenderContext
from .utils import run_async

logger = logging.getLogger(__name__)


class Berry:
    """Main Berry application."""
    
    def __init__(self, fullscreen: bool = False):
        # Restore display BEFORE pygame takes over DRM device.
        # Previous run may have been killed during sleep, leaving
        # backlight/DPMS off. Must happen before kmsdrm init.
        SleepManager.restore_display()
        
        # Try GPU-accelerated driver first on Raspberry Pi
        self._setup_video_driver()
        
        pygame.init()
        pygame.mixer.quit()  # Release audio device for go-librespot
        pygame.display.set_caption('Berry')
        
        # Initialize display and components
        self._init_display(fullscreen)
        self._init_components()
    
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
                    if 'dtoverlay=vc4-kms-v3d' in config or 'dtoverlay=vc4-kms-dsi-7inch' in config:
                        return True
        except (OSError, IOError):
            pass
        
        return False
    
    def _setup_video_driver(self):
        """Configure optimal video driver for the platform."""
        # Skip if already set
        if os.environ.get('SDL_VIDEODRIVER'):
            return
        
        # Only try kmsdrm on Raspberry Pi
        if not os.path.exists('/proc/device-tree/model'):
            return
        
        # Check if KMS/DRM is likely configured
        kms_configured = self._check_kms_available()
        
        # Try kmsdrm for GPU acceleration
        os.environ['SDL_VIDEODRIVER'] = 'kmsdrm'
        try:
            pygame.display.init()
            pygame.display.quit()  # Success - will reinit in pygame.init()
            logger.info('Using kmsdrm (GPU accelerated)')
        except pygame.error as e:
            # Fall back to default driver
            del os.environ['SDL_VIDEODRIVER']
            error_msg = str(e) if e else 'unknown error'
            logger.warning(f'kmsdrm driver failed: {error_msg}')
            
            if not kms_configured:
                logger.warning('=' * 60)
                logger.warning('GPU ACCELERATION NOT AVAILABLE')
                logger.warning('=' * 60)
                logger.warning('To enable GPU acceleration on Raspberry Pi:')
                logger.warning('  1. Run: sudo raspi-config')
                logger.warning('  2. Navigate to: Advanced Options > GL Driver')
                logger.warning('  3. Select: G1 GL (Full KMS)')
                logger.warning('  4. Reboot the Pi')
                logger.warning('=' * 60)
            else:
                logger.warning('KMS/DRM appears configured but kmsdrm driver failed.')
                logger.warning('This may indicate a driver compatibility issue.')
            logger.info('Falling back to default driver (software rendering)')
    
    def _init_display(self, fullscreen: bool):
        """Initialize the display with optimal settings."""
        flags = pygame.DOUBLEBUF
        if fullscreen:
            flags |= pygame.FULLSCREEN
        
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
        
        self.clock = pygame.time.Clock()
        pygame.mouse.set_visible(not fullscreen)
        
        self._log_video_info()
    
    def _init_components(self):
        """Initialize all application components."""
        # Mock mode
        self.mock_mode = MOCK_MODE
        
        # API & Catalog (use NullAPI in mock mode)
        self.api = NullLibrespotAPI() if self.mock_mode else LibrespotAPI(LIBRESPOT_URL)
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
        
        # Handlers
        self.touch = TouchHandler()
        self.events = EventListener(LIBRESPOT_WS, self._on_ws_update, self._on_ws_reconnect)
        
        # Evdev touch handler for KMSDRM mode (reads /dev/input directly)
        self.evdev_touch = EvdevTouchHandler(SCREEN_WIDTH, SCREEN_HEIGHT)
        self.evdev_touch.start()  # Starts background thread if touchscreen found
        
        # Managers
        self.sleep_manager = SleepManager()
        self.carousel = SmoothCarousel()
        self.play_timer = PlayTimer()
        self.perf_monitor = PerformanceMonitor()
        self.volume = VolumeController(self.api)
        self.auto_pause = AutoPauseManager(
            on_pause=lambda: run_async(self.api.pause),
            get_volume=lambda: (self.volume.speaker_level, self.volume.headphone_level),
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
        
        # TempItem and delete mode (with lock for thread-safe access)
        self.temp_item: Optional[CatalogItem] = None
        self._temp_item_lock = threading.Lock()
        self.delete_mode_id: Optional[str] = None
        self._saving = False
        self._deleting = False
        
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
        
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        
        # Performance logging
        self._last_fps_log = time.time()
        self._fps_log_interval = 30  # Log FPS every 30 seconds
        
        # Setup menu
        self.setup_menu = SetupMenu(
            catalog_manager=self.catalog_manager,
            settings=self.settings,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_library_cleared=self._on_library_cleared,
        )
        # Volume button hold tracking (3s hold opens setup menu)
        self._volume_hold_start: Optional[float] = None
        self._menu_hold_triggered = False
        
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

    def _on_library_cleared(self):
        """Reset in-memory state after library clear (called by SetupMenu)."""
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
    
    def _on_ws_update(self):
        """Called when WebSocket receives an event."""
        logger.debug(f'WebSocket event, context: {self.events.context_uri}')
    
    def _on_ws_reconnect(self):
        """Called when WebSocket reconnects after disconnect."""
        logger.info('WebSocket reconnected - refreshing state')
        self._connection_fail_count = 0
        self.connected = True
        run_async(self._refresh_status)
    
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
    
    def _handle_signal(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        sig_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
        logger.info(f'Received {sig_name}, shutting down...')
        self.running = False
    
    def start(self):
        """Start the application."""
        logger.info('Starting Berry...')
        
        # Pre-load images
        self.image_cache.preload_catalog(self.catalog_manager.items)
        
        if not self.mock_mode:
            self.events.start()
            self.catalog_manager.cleanup_unused_images()
            
            # Set system volume at startup
            self.volume.init()
            
            # Start status polling
            run_async(self._poll_status)
            logger.info(f'Polling {LIBRESPOT_URL}')
            
            # Force initial connection check (don't wait for first poll interval)
            run_async(self._initial_connect)
        else:
            logger.info('Running in MOCK MODE')
            self._startup_ready = True
        
        logger.info('Entering main loop...')
        dt = 1.0 / 60  # Initial delta time
        
        # Main loop
        while self.running:
            # Sleep mode: block until event arrives (0% CPU, instant wake)
            if self.sleep_manager.is_sleeping:
                event = pygame.event.wait()  # Blocks until event
                if event.type in (pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN):
                    self.sleep_manager.wake_up()
                    self._on_wake()
                elif event.type == pygame.QUIT:
                    self.running = False
                continue
            
            self._handle_events()
            self._update(dt)
            dirty_rects = self._draw()
            
            if dirty_rects:
                pygame.display.update(dirty_rects)
            else:
                pygame.display.flip()
            
            target_fps = self._target_fps()
            is_animating = not self.carousel.settled or self.touch.dragging
            
            dt = self.clock.tick(target_fps) / 1000.0
            
            target_frame_time = 1.0 / target_fps
            spike_threshold = max(0.1, target_frame_time * 1.2)
            if dt > spike_threshold:
                logger.warning(f'Frame spike: {dt*1000:.0f}ms (target: {target_fps} FPS)')
            
            self.perf_monitor.update(dt, is_animating)
            self._log_fps_if_due(target_fps)
        
        # Save progress before shutdown
        logger.info('Shutting down...')
        self._save_progress_on_shutdown()
        
        # Restore display before exit so next boot doesn't start with black screen
        if self.sleep_manager.is_sleeping:
            self.sleep_manager.wake_up()
        
        self.events.stop()
        self.evdev_touch.stop()
        pygame.quit()
        logger.info('Berry stopped')
    
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
        
        logger.info(f'FPS: {avg_fps:.1f} | connected={self.connected} | playing={self.now_playing.playing} | loading={is_loading} | target={target_fps}')
        
        if not self.sleep_manager.is_sleeping:
            if target_fps == 60 and avg_fps < 30:
                logger.warning(f'Low FPS during animation: {avg_fps:.1f} (target: 60 FPS)')
            elif target_fps == 10 and avg_fps < 8:
                logger.warning(f'Low FPS while playing: {avg_fps:.1f} (target: 10 FPS)')
            elif target_fps == 5 and avg_fps < 4:
                logger.warning(f'Low FPS while idle: {avg_fps:.1f} (target: 5 FPS)')
    
    def _poll_status(self):
        """Poll librespot status in background."""
        was_fast_polling = False
        while self.running:
            try:
                self._refresh_status()
            except Exception as e:
                # Handle connection errors with grace period
                self._connection_fail_count += 1
                if self._connection_fail_count >= self._connection_grace_threshold:
                    if self.connected:  # Only log once
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
            
            poll_interval = 0.5 if is_fast_polling else 1.0
            time.sleep(poll_interval)
    
    def _refresh_status(self):
        """Refresh playback status from librespot."""
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
                logger.info(f'CONNECTION RESTORED (was disconnected)')
            else:
                logger.warning(f'CONNECTION LOST after {self._connection_fail_count} failures')
            logger.info(f'  fail_count={self._connection_fail_count}, status={raw is not None}')
        
        if raw and isinstance(raw, dict):
            status = LibrespotStatus.from_dict(raw, context_uri=self.events.context_uri)
            
            self.now_playing = NowPlaying(
                playing=status.playing,
                paused=status.paused,
                stopped=status.stopped,
                context_uri=status.context_uri,
                track_name=status.track_name,
                track_artist=status.track_artist,
                track_album=status.track_album,
                track_cover=status.track_cover,
                position=status.position,
                duration=status.duration,
            )
            
            self.playback.play_state.pending_action = None
            
            
            self._update_temp_item()
            self._check_autoplay()
            
            if status.playing and self.sleep_manager.is_sleeping:
                self.sleep_manager.wake_up()
                self._on_wake()
            
            if status.playing:
                self.auto_pause.on_play(status.context_uri)
                self.auto_pause.check(is_playing=True)
            elif status.paused or status.stopped:
                self.auto_pause.on_stop()
        else:
            self.now_playing = NowPlaying()
            self.auto_pause.on_stop()
    
    def _check_autoplay(self):
        """Detect autoplay and clear progress when context finishes."""
        self.playback.check_autoplay(self.now_playing)
    
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
                    self.sleep_manager.wake_up()
                    self._on_wake()
                    continue
                self.sleep_manager.reset_timer()
                self._handle_touch_down(event.pos)
            
            elif event.type == pygame.KEYDOWN:
                if self.sleep_manager.is_sleeping:
                    self.sleep_manager.wake_up()
                    self._on_wake()
                    continue
                self.sleep_manager.reset_timer()
                self._handle_key(event.key)
            
            elif event.type == pygame.MOUSEMOTION:
                if self.touch.dragging:
                    self.sleep_manager.reset_timer()
                    self.touch.on_move(event.pos)
                    
                    # Cancel delete mode when user starts swiping
                    if self.touch.is_swiping and self.delete_mode_id:
                        self.delete_mode_id = None
                        self.renderer.invalidate()
            
            elif event.type == pygame.MOUSEBUTTONUP:
                logger.debug(f'Event: MOUSEBUTTONUP at {event.pos}')
                if not self.sleep_manager.is_sleeping:
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
        # Menu intercept — all taps handled by menu when open
        if self.setup_menu.is_open:
            self.setup_menu.handle_tap(pos, self.renderer.menu_button_rects)
            return
        
        x, y = pos
        
        carousel_x_min = CAROUSEL_X - CAROUSEL_TOUCH_MARGIN
        carousel_x_max = CAROUSEL_X + COVER_SIZE + CAROUSEL_TOUCH_MARGIN
        
        logger.debug(f'Touch down: pos={pos}, carousel_x_range={carousel_x_min}-{carousel_x_max}')
        
        # Check button clicks
        if self._check_button_click(pos):
            logger.debug('Touch down: button click')
            return
        
        # Cancel delete mode
        if self.delete_mode_id:
            self.delete_mode_id = None
            self.renderer.invalidate()
        
        # Carousel swipes - within carousel X zone, full Y range
        if carousel_x_min <= x <= carousel_x_max:
            logger.debug('Touch down: carousel swipe start')
            self.touch.on_down(pos)
            self.user_interacting = True
            self.play_timer.cancel()
            self.playback.cancel_pending()
            
            # Pause immediately when scrolling starts
            if self.now_playing.playing and not self.mock_mode:
                self.playback.pause_for_navigation(self.now_playing.context_uri)
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
        
        return False
    
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
        
        # Portrait mode: check if X is in button column
        if CONTROLS_X - PLAY_BTN_SIZE <= x <= CONTROLS_X + PLAY_BTN_SIZE:
            button_pressed = None
            
            # Prev: Y = center_y - btn_spacing (485)
            if center_y - btn_spacing - BTN_SIZE <= y <= center_y - btn_spacing + BTN_SIZE:
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
        
        Only handles carousel movement. Playback decisions (play timer,
        resume, pause) happen when the carousel settles in _update().
        This prevents race conditions during rapid swiping.
        """
        items = self.display_items
        if not items:
            return
        
        target_index = max(0, min(target_index, len(items) - 1))
        
        if target_index != self.selected_index:
            old_index = self.selected_index
            self.selected_index = target_index
            self.carousel.set_target(target_index)
            
            # Pause if navigating away from playing item (handles keyboard nav)
            if self.now_playing.playing and not self.mock_mode:
                old_item = items[old_index] if old_index < len(items) else None
                if old_item and self._is_item_playing(old_item):
                    self.playback.pause_for_navigation(self.now_playing.context_uri)
            
            item = items[target_index]
            logger.debug(f'Snap: {old_index} -> {target_index}, item={item.name}')
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
    
    def _skip_track(self, api_fn):
        """Save progress, mark as user action, then skip prev/next."""
        self.playback.last_user_play_time = time.time()
        self.playback.save_progress(self.now_playing, force=True)
        run_async(api_fn)

    def _toggle_play(self):
        """Toggle play/pause."""
        if self.mock_mode:
            self._toggle_mock_play()
            return
        self.playback.toggle_play(self.display_items, self.selected_index, self.now_playing)
    
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
                track_name=ct.get('name', item.name) if ct else item.name,
                track_artist=ct.get('artist', item.artist) if ct else item.artist,
                position=self.playback.mock_position,
                duration=self.playback.mock_duration,
            )
        else:
            self.now_playing = NowPlaying(paused=True, context_uri=self.now_playing.context_uri)
    
    def _play_item(self, uri: str, from_beginning: bool = False):
        """Queue a play request via the playback controller."""
        if self.now_playing.context_uri and self.now_playing.context_uri != uri:
            self.playback.save_progress(self.now_playing, force=True)
        self.playback.play_item(uri, from_beginning)
    
    def _on_wake(self):
        """Called when waking from sleep - reconnect and reset state."""
        logger.info('=' * 40)
        logger.info('WAKE UP START')
        logger.info(f'  Connection state: {self.connected}')
        logger.info(f'  Fail count: {self._connection_fail_count}')
        logger.info(f'  Playing: {self.now_playing.playing}')
        logger.info(f'  Volume mode: {self.volume.mode}')
        
        # Reset connection state for immediate reconnect
        self._connection_fail_count = 0
        self.connected = True  # Optimistic - will correct on next poll if wrong
        
        # Force immediate status refresh in background
        def wake_refresh():
            try:
                self._refresh_status()
                logger.info(f'  Post-refresh connected: {self.connected}')
                logger.info(f'  Post-refresh playing: {self.now_playing.playing}')
                logger.info('WAKE UP COMPLETE')
                logger.info('=' * 40)
            except Exception as e:
                logger.error(f'  Wake refresh failed: {e}')
                logger.info('WAKE UP FAILED')
                logger.info('=' * 40)
        
        run_async(wake_refresh)
        
    
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
        if not self.delete_mode_id or self._deleting:
            return
        self._deleting = True
        
        try:
            item_id = self.delete_mode_id
            old_index = self.selected_index
            
            item = next((i for i in self.catalog_manager.items if i.id == item_id), None)
            if item:
                logger.info(f'Deleting: {item.name}')
            
            success = self.catalog_manager.delete_item(item_id)
            
            if success:
                self.catalog_manager.load()
                self._update_carousel_max_index()
                
                new_index = max(0, old_index - 1)
                if self.display_items:
                    new_index = min(new_index, len(self.display_items) - 1)
                    self.selected_index = new_index
                    self.carousel.scroll_x = float(new_index)
                    self.carousel.set_target(new_index)
                    
                    new_item = self.display_items[new_index]
                    if not new_item.is_temp:
                        self._play_item(new_item.uri)
            
            self.delete_mode_id = None
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
        """Sync carousel to currently playing item."""
        items = self.display_items
        if not items or self.user_interacting:
            return
        
        if self.play_timer.item or not self.carousel.settled:
            return
        
        if self.play_timer.is_in_cooldown():
            return
        
        context_uri = self.now_playing.context_uri
        if not context_uri or context_uri == self.playback.last_context_uri:
            return
        
        if context_uri == self.play_timer.last_played_uri:
            self.play_timer.last_played_uri = None
            self.playback.last_context_uri = context_uri
            return
        
        playing_index = next((i for i, item in enumerate(items) if item.uri == context_uri), None)
        if playing_index is None:
            return

        if playing_index >= len(items):
            logger.debug(f'Sync index out of bounds: {playing_index} >= {len(items)}')
            return

        if playing_index != self.selected_index:
            logger.info(f'Syncing to: {items[playing_index].name}')
            self.selected_index = playing_index
            self.carousel.set_target(playing_index)

        self.playback.last_context_uri = context_uri
    
    def _update(self, dt: float):
        """Update application state."""
        items = self.display_items
        if items:
            self.selected_index = max(0, min(self.selected_index, len(items) - 1))
        
        # Update carousel
        was_animating = not self.carousel.settled
        self.carousel.update(dt)
        
        # When carousel settles: decide what to play (single source of truth)
        # Skip until startup is complete — librespot needs time after boot
        if was_animating and self.carousel.settled and not self.touch.dragging and self._startup_ready:
            item = items[self.selected_index] if self.selected_index < len(items) else None
            if item and not item.is_temp:
                if (self.playback.paused_for_navigation and
                    self.playback.paused_context_uri == item.uri):
                    self.playback.resume_from_navigation()
                elif not self._is_item_playing(item):
                    logger.info(f'PlayTimer starting for: {item.name} (index={self.selected_index})')
                    self.play_timer.start(item)
        
        # Check long press for delete mode
        if self.touch.check_long_press():
            self._trigger_delete_mode()
        
        # Update interaction state
        self.user_interacting = (
            self.touch.dragging or 
            not self.carousel.settled or 
            self.play_timer.item is not None
        )
        
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
        
        item_to_play = self.play_timer.check()
        if item_to_play:
            # Safety: verify the timer item is still the focused item
            focused_item = items[self.selected_index] if self.selected_index < len(items) else None
            if not focused_item or focused_item.uri != item_to_play.uri:
                logger.warning(f'PlayTimer fired for stale item: timer={item_to_play.name}, '
                             f'focused={focused_item.name if focused_item else "none"}')
            else:
                logger.info(f'PlayTimer fired: item={item_to_play.name}, uri={item_to_play.uri[:50]}..., '
                           f'selected_index={self.selected_index}, carousel_pos={self.carousel.scroll_x:.2f}')
                if (self.playback.paused_for_navigation and
                    self.playback.paused_context_uri == item_to_play.uri):
                    logger.info('  -> Resuming (paused for nav)')
                    self.playback.resume_from_navigation()
                else:
                    logger.info('  -> Auto-playing NEW')
                    self.playback.clear_navigation_pause()
                    self._play_item(item_to_play.uri)
        
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
        
        self.sleep_manager.check_sleep(self.now_playing.playing)
        
        self.playback.update_loading_state(
            self.now_playing, self.carousel.settled, self.play_timer.item is not None
        )
    
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
        self._last_action_time = time.time()
        self._volume_hold_start = None
    
    
    def _draw(self):
        """Draw the UI."""
        ctx = RenderContext(
            items=self.display_items,
            selected_index=self.selected_index,
            now_playing=self.now_playing,
            scroll_x=self.carousel.scroll_x,
            drag_offset=self.touch.drag_offset,
            dragging=self.touch.dragging,
            is_sleeping=self.sleep_manager.is_sleeping,
            volume_index=self.volume.index,
            delete_mode_id=self.delete_mode_id,
            pressed_button=self._pressed_button,
            is_loading=self.playback.play_state.is_loading,
            is_playing=self.playback.play_state.display_playing(self.now_playing.playing),
            toast_message=self._active_toast,
            menu_state=self.setup_menu.state,
            menu_known_networks=self.setup_menu.known_networks,
            menu_current_network=self.setup_menu.current_network,
            auto_pause_minutes=self.settings.auto_pause_minutes,
            progress_expiry_hours=self.settings.progress_expiry_hours,
        )
        return self.renderer.draw(ctx)


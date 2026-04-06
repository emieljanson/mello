"""
Evdev Touch Handler - Direct touch input for KMSDRM mode.

When running with KMSDRM driver (without Wayland), SDL2 doesn't automatically
pick up touch input from evdev devices. This module reads touch events directly
and converts them to pygame mouse events.
"""
import threading
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Only import evdev if available (not needed on desktop)
try:
    import evdev
    from evdev import ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    logger.debug('evdev not available - touch input via SDL only')


class EvdevTouchHandler:
    """Reads touch input directly from evdev and posts pygame events."""
    
    def __init__(self, screen_width: int, screen_height: int):
        self.screen_width = screen_width
        self.screen_height = screen_height
        
        self._device: Optional['evdev.InputDevice'] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # Wake signal for sleep mode (pygame.event.post from thread
        # doesn't reliably wake pygame.event.wait in KMSDRM mode)
        self.wake_event = threading.Event()

        # Touch state (written from reader thread, read from main thread)
        self._touch_lock = threading.Lock()
        self._touch_x = 0
        self._touch_y = 0
        self._touching = False
        
        # Calibration (touch panel dimensions, detected at start)
        self._touch_max_x = 1279
        self._touch_max_y = 719
    
    def start(self) -> bool:
        """Start reading touch events. Returns True if successful."""
        if not EVDEV_AVAILABLE:
            logger.debug('evdev not available, skipping touch handler')
            return False
        
        # Find touchscreen device
        self._device = self._find_touchscreen()
        if not self._device:
            logger.warning('No touchscreen found')
            return False
        
        logger.info(f'Touch input: {self._device.name} ({self._device.path})')
        
        # Get touch panel dimensions from device
        caps = self._device.capabilities()
        if ecodes.EV_ABS in caps:
            for code, absinfo in caps[ecodes.EV_ABS]:
                if code == ecodes.ABS_X:
                    self._touch_max_x = absinfo.max
                elif code == ecodes.ABS_Y:
                    self._touch_max_y = absinfo.max
        
        logger.info(f'Touch calibration: {self._touch_max_x}x{self._touch_max_y} -> {self.screen_width}x{self.screen_height}')
        
        # Start reader thread
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True
    
    def stop(self):
        """Stop reading touch events."""
        self._running = False
        if self._device:
            try:
                self._device.close()
            except Exception as e:
                logger.debug(f'Error closing touch device: {e}')
    
    def _find_touchscreen(self) -> Optional['evdev.InputDevice']:
        """Find the touchscreen device."""
        try:
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            for device in devices:
                # Look for touchscreen by name or capabilities
                name_lower = device.name.lower()
                if 'touch' in name_lower or 'goodix' in name_lower:
                    return device
                # Check for BTN_TOUCH capability
                caps = device.capabilities()
                if ecodes.EV_KEY in caps:
                    if ecodes.BTN_TOUCH in caps[ecodes.EV_KEY]:
                        return device
        except Exception as e:
            logger.warning(f'Error finding touchscreen: {e}')
        return None
    
    def _scale_coordinates(self, touch_x: int, touch_y: int) -> Tuple[int, int]:
        """Scale touch coordinates to screen coordinates.
        
        Direct mapping from touch panel to screen coordinates.
        """
        screen_x = int(touch_x * self.screen_width / self._touch_max_x)
        screen_y = int(touch_y * self.screen_height / self._touch_max_y)
        
        # Clamp to screen bounds
        screen_x = max(0, min(self.screen_width - 1, screen_x))
        screen_y = max(0, min(self.screen_height - 1, screen_y))
        
        return screen_x, screen_y
    
    def _read_loop(self):
        """Read touch events in background thread."""
        import pygame
        
        try:
            for event in self._device.read_loop():
                if not self._running:
                    break
                
                # Handle touch position
                if event.type == ecodes.EV_ABS:
                    with self._touch_lock:
                        if event.code == ecodes.ABS_X or event.code == ecodes.ABS_MT_POSITION_X:
                            self._touch_x = event.value
                        elif event.code == ecodes.ABS_Y or event.code == ecodes.ABS_MT_POSITION_Y:
                            self._touch_y = event.value

                # Handle touch down/up
                elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                    with self._touch_lock:
                        pos = self._scale_coordinates(self._touch_x, self._touch_y)

                    if event.value == 1:  # Touch down
                        with self._touch_lock:
                            self._touching = True
                        self.wake_event.set()
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONDOWN,
                            {'pos': pos, 'button': 1}
                        ))
                        logger.debug(f'Touch DOWN at {pos}')

                    elif event.value == 0:  # Touch up
                        with self._touch_lock:
                            self._touching = False
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONUP,
                            {'pos': pos, 'button': 1}
                        ))
                        logger.debug(f'Touch UP at {pos}')

                # Handle touch move (SYN_REPORT indicates end of event batch)
                elif event.type == ecodes.EV_SYN:
                    with self._touch_lock:
                        touching = self._touching
                        if touching:
                            pos = self._scale_coordinates(self._touch_x, self._touch_y)
                    if touching:
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEMOTION,
                            {'pos': pos, 'rel': (0, 0), 'buttons': (1, 0, 0)}
                        ))
        
        except Exception as e:
            if self._running:
                logger.error(f'Touch read error: {e}')
        
        logger.debug('Touch handler stopped')

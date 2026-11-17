"""
Evdev Touch Handler - reads the touchscreen directly (/dev/input).

Touches become InputEvents in a queue that the main loop empties every frame.
On a computer without a touchscreen, the Slint window posts mouse and key
presses into the same queue (see SlintDisplay.forward_input).
"""
import queue
import threading
import time
import logging
from typing import NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

# Only import evdev if available (not needed on desktop)
try:
    import evdev
    from evdev import ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    logger.debug('evdev not available - touch input via SDL only')


class InputEvent(NamedTuple):
    kind: str                         # 'down', 'move', 'up' or 'key'
    pos: Tuple[int, int] = (0, 0)     # panel pixels
    touched_at: Optional[float] = None
    key: str = ''                     # for 'key': 'left', 'right', 'escape', 'return', ' ', 'n', ...


class EvdevTouchHandler:
    """Reads touch input directly from evdev into a queue of InputEvents."""
    
    def __init__(self, screen_width: int, screen_height: int):
        self.screen_width = screen_width
        self.screen_height = screen_height
        
        self._device: Optional['evdev.InputDevice'] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._healthy = False
        self._failure_reason: Optional[str] = None
        self._failure_lock = threading.Lock()

        # Touches (and mouse/keys on a computer) for the main loop
        self.events: 'queue.SimpleQueue[InputEvent]' = queue.SimpleQueue()

        # Wake signal for sleep mode: set by every touch down
        self.wake_event = threading.Event()

        # Touch state (written from reader thread, read from main thread)
        self._touch_lock = threading.Lock()
        self._touch_x = 0
        self._touch_y = 0
        self._touching = False
        
        # Calibration (touch panel dimensions, detected at start)
        self._touch_max_x = 1279
        self._touch_max_y = 719
    
    def post(self, event: InputEvent):
        """Add an input event (also used for mouse and keys on a computer)."""
        self.events.put(event)
        if event.kind in ('down', 'key'):
            self.wake_event.set()

    def take_events(self) -> list:
        """All input events since the last call, oldest first."""
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def clear_events(self):
        """Forget pending input (touches that woke the screen are not taps)."""
        self.take_events()

    def start(self) -> bool:
        """Start reading touch events. Returns True if successful."""
        if not EVDEV_AVAILABLE:
            self._mark_failed('evdev not available')
            logger.debug('evdev not available, skipping touch handler')
            return False
        
        # Find touchscreen device
        self._device = self._find_touchscreen()
        if not self._device:
            self._mark_failed('no touchscreen found')
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
        self._healthy = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    @property
    def is_available(self) -> bool:
        """True when a touchscreen device was found and the reader is running."""
        return self._healthy and self._device is not None

    @property
    def device_name(self) -> Optional[str]:
        """Human-readable touchscreen name, if detected."""
        return self._device.name if self._device else None

    @property
    def device_path(self) -> Optional[str]:
        """evdev path for the touchscreen, if detected."""
        return self._device.path if self._device else None

    def consume_failure_reason(self) -> Optional[str]:
        """Return and clear the latest touch failure reason."""
        with self._failure_lock:
            reason = self._failure_reason
            self._failure_reason = None
            return reason

    def _mark_failed(self, reason: str):
        """Mark touch wake as unavailable and expose the reason to the app."""
        self._healthy = False
        with self._failure_lock:
            self._failure_reason = reason
    
    def stop(self):
        """Stop reading touch events."""
        self._running = False
        self._healthy = False
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

                    # 'touched_at': when it happened. The main loop handles
                    # events once per frame, up to 0.2 s later when idle, and
                    # swipe speed is measured from touch down to touch up.
                    if event.value == 1:  # Touch down
                        with self._touch_lock:
                            self._touching = True
                        self.post(InputEvent('down', pos, time.time()))
                        logger.debug(f'Touch DOWN at {pos}')

                    elif event.value == 0:  # Touch up
                        with self._touch_lock:
                            self._touching = False
                        self.post(InputEvent('up', pos, time.time()))
                        logger.debug(f'Touch UP at {pos}')

                # Handle touch move (SYN_REPORT indicates end of event batch)
                elif event.type == ecodes.EV_SYN:
                    with self._touch_lock:
                        touching = self._touching
                        if touching:
                            pos = self._scale_coordinates(self._touch_x, self._touch_y)
                    if touching:
                        self.post(InputEvent('move', pos))
        
        except Exception as e:
            if self._running:
                self._mark_failed(f'touch read error: {e}')
                logger.error(f'Touch read error: {e}')
        else:
            if self._running:
                self._mark_failed('touch read loop exited')
                logger.error('Touch read loop exited unexpectedly')
        
        logger.debug('Touch handler stopped')

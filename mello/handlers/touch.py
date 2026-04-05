"""
Touch Handler - Swipe gestures for carousel navigation.
"""
import time
import logging
from typing import Tuple, Optional

from ..config import SWIPE_THRESHOLD, SWIPE_VELOCITY, LONG_PRESS_TIME

logger = logging.getLogger(__name__)


class TouchHandler:
    """Handle swipe gestures for carousel navigation."""

    # Movement threshold to consider it a swipe (not a long press)
    SWIPE_MOVEMENT_THRESHOLD = 15

    def __init__(self, long_press_time: float = None):
        self.start_x = 0
        self.start_y = 0
        self.start_time = 0
        self.dragging = False
        self.drag_offset = 0  # Current drag offset in pixels
        self.long_press_fired = False  # Track if long press was triggered
        self.is_swiping = False  # Track if user started swiping (moved beyond threshold)
        self.long_press_time = long_press_time if long_press_time is not None else LONG_PRESS_TIME
    
    def on_down(self, pos: Tuple[int, int]):
        """Called on touch/mouse down."""
        self.start_x = pos[0]
        self.start_y = pos[1]
        self.start_time = time.time()
        self.dragging = True
        self.drag_offset = 0
        self.long_press_fired = False
        self.is_swiping = False
        logger.debug(f'Touch down at ({pos[0]}, {pos[1]})')
    
    def on_move(self, pos: Tuple[int, int]) -> float:
        """Called on touch/mouse move. Returns drag offset.
        
        Portrait mode: drag along Y axis (user's horizontal).
        """
        if not self.dragging:
            return 0
        # Portrait mode: use Y axis for carousel swipe (user's horizontal)
        self.drag_offset = pos[1] - self.start_y
        
        # Once user moves beyond threshold, mark as swiping (prevents long press)
        if not self.is_swiping and abs(self.drag_offset) > self.SWIPE_MOVEMENT_THRESHOLD:
            self.is_swiping = True
            logger.debug(f'Swipe started, offset={self.drag_offset}px')
        
        return self.drag_offset
    
    def check_long_press(self) -> bool:
        """Check if long press threshold reached. Returns True once."""
        if not self.dragging or self.long_press_fired:
            return False
        
        # Never trigger long press if user has started swiping
        if self.is_swiping:
            return False
        
        if time.time() - self.start_time >= self.long_press_time:
            self.long_press_fired = True
            logger.debug(f'Long press triggered at ({self.start_x}, {self.start_y})')
            return True
        
        return False
    
    def on_up(self, pos: Tuple[int, int]) -> Tuple[Optional[str], float]:
        """
        Called on touch/mouse up.
        
        Portrait mode: swipes along Y axis (user's horizontal).
        
        Returns:
            (action, velocity) where action is 'left', 'right', 'tap', or None.
            Velocity is in pixels/ms (positive = right, negative = left).
        """
        if not self.dragging:
            return ('tap', 0)
        
        self.dragging = False
        dx = pos[0] - self.start_x
        dy = pos[1] - self.start_y
        dt = (time.time() - self.start_time) * 1000  # ms

        # If long-press already fired, suppress tap/swipe action on release.
        if self.long_press_fired:
            self.drag_offset = 0
            logger.debug('Touch up: long press release, suppress tap action')
            return (None, 0)
        
        # Portrait mode: ignore if mostly perpendicular to carousel direction
        # Carousel is along Y, so ignore swipes that are mostly along X
        if abs(dx) > abs(dy) * 1.5:
            self.drag_offset = 0
            logger.debug(f'Touch up: perpendicular swipe ignored, dx={dx}')
            return ('tap', 0)
        
        # Use minimum dt of 50ms to prevent extreme velocity on instant release
        dt_clamped = max(50, dt)
        # Portrait mode: velocity along Y axis
        velocity = dy / dt_clamped if dt_clamped > 0 else 0
        
        # Cap velocity to reasonable range (-5 to 5 px/ms)
        velocity = max(-5.0, min(5.0, velocity))
        
        # Check for swipe (using Y axis distance)
        if abs(dy) >= SWIPE_THRESHOLD or abs(velocity) >= SWIPE_VELOCITY:
            self.drag_offset = 0
            # Note: 'left'/'right' refer to user's view direction
            action = 'right' if dy > 0 else 'left'
            logger.debug(f'Touch up: swipe {action}, dy={dy}px, velocity={velocity:.2f}px/ms, dt={dt:.0f}ms')
            return (action, velocity)
        
        self.drag_offset = 0
        logger.debug(f'Touch up: tap at ({pos[0]}, {pos[1]}), dt={dt:.0f}ms')
        return ('tap', 0)

"""
Carousel - Smooth scrolling and auto-play timer.
"""
import math
import time
from typing import Optional

from ..models import CatalogItem
from ..config import PLAY_TIMER_DELAY, SYNC_COOLDOWN


class SmoothCarousel:
    """Smooth scrolling carousel - items follow finger, then lerp to target."""
    
    # Decay rate for exponential smoothing (higher = faster animation)
    # At 15, animation reaches ~95% in ~200ms regardless of framerate
    DECAY_RATE = 15.0
    SNAP_THRESHOLD = 0.01      # When to finish animation
    
    def __init__(self):
        self.scroll_x = 0.0         # Current scroll position (float index)
        self.target_index = 0       # Target index to animate to
        self.settled = True         # True when not animating
        self.max_index = 0          # Will be set by app
    
    def set_target(self, index: int):
        """Set target index to animate to."""
        self.target_index = max(0, min(index, self.max_index))
        self.settled = False
    
    def update(self, dt: float) -> bool:
        """Update scroll position. Returns True if position changed."""
        if self.settled:
            return False
        
        # Time-based exponential decay (frame-rate independent)
        # This ensures consistent animation speed regardless of FPS
        target = float(self.target_index)
        diff = target - self.scroll_x
        
        # Exponential decay: move by a fraction that depends on elapsed time
        # Factor approaches 1 as dt increases, ensuring we always move toward target
        decay_factor = 1 - math.exp(-self.DECAY_RATE * dt)
        self.scroll_x += diff * decay_factor
        
        # Check if settled
        if abs(diff) < self.SNAP_THRESHOLD:
            self.scroll_x = target
            self.settled = True
        
        return True


class PlayTimer:
    """Auto-play after settling on a cover for N seconds."""
    
    def __init__(self):
        self.item: Optional[CatalogItem] = None
        self.start_time = 0
        self.last_played_uri: Optional[str] = None  # Track what we just played
        self.last_fired_time = 0  # Track when we last fired (for sync cooldown)
    
    def start(self, item: CatalogItem):
        """Start timer for an item."""
        if item is None:
            self.cancel()
            return
        
        # Don't restart if same item
        if self.item and self.item.uri == item.uri:
            return
        
        self.item = item
        self.start_time = time.time()
    
    def cancel(self):
        """Cancel the timer."""
        self.item = None
        self.start_time = 0
    
    def check(self) -> Optional[CatalogItem]:
        """Check if timer expired. Returns item to play or None."""
        if not self.item:
            return None
        
        if time.time() - self.start_time >= PLAY_TIMER_DELAY:
            result = self.item
            self.last_played_uri = result.uri
            self.last_fired_time = time.time()
            self.item = None
            self.start_time = 0
            return result
        
        return None
    
    def is_in_cooldown(self) -> bool:
        """Check if still in sync cooldown after firing."""
        return time.time() - self.last_fired_time < SYNC_COOLDOWN


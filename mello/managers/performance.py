"""
Performance Monitor - FPS and frame time tracking.
"""
from collections import deque

from ..config import PERF_SAMPLE_SIZE


class PerformanceMonitor:
    """Tracks frame times; app.py handles all FPS logging."""
    
    def __init__(self):
        self.frame_times: deque = deque(maxlen=PERF_SAMPLE_SIZE)
    
    def update(self, dt: float, is_animating: bool):
        """Record frame delta time."""
        self.frame_times.append(dt)
    
    @property
    def current_fps(self) -> float:
        """Get current average FPS."""
        if not self.frame_times:
            return 0
        avg_dt = sum(self.frame_times) / len(self.frame_times)
        return 1.0 / avg_dt if avg_dt > 0 else 0

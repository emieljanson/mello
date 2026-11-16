"""
Performance Monitor - FPS and frame time tracking.
"""
import time
from collections import deque

from ..config import PERF_SAMPLE_SIZE


class PerformanceMonitor:
    """Tracks frame times; app.py handles all FPS logging."""
    
    def __init__(self):
        self.frame_times: deque = deque(maxlen=PERF_SAMPLE_SIZE)
    
    def update(self, dt: float):
        """Record frame delta time."""
        self.frame_times.append(dt)
    
    @property
    def current_fps(self) -> float:
        """Get current average FPS."""
        if not self.frame_times:
            return 0
        avg_dt = sum(self.frame_times) / len(self.frame_times)
        return 1.0 / avg_dt if avg_dt > 0 else 0


class AnimationTimer:
    """Measures one animation (a swipe and its settle) frame by frame.

    Each frame is split into phases (events, update, draw, display) so the
    summary shows where the time goes: drawing, or work done on the main
    loop. Call begin_frame()/mark(phase) every frame, end_frame(animating)
    after the clock tick; when the animation stops, end_frame() returns
    the summary line once.
    """

    PHASES = ('events', 'update', 'draw', 'display')

    def __init__(self, clock=time.perf_counter):
        self._clock = clock
        self._reset()
        self._frame_start = None
        self._last_mark = None
        self._current: dict = {}

    def _reset(self):
        self.frames = 0
        self.total = 0.0
        self.worst = 0.0
        self.worst_phases: dict = {}
        self.sums = {p: 0.0 for p in self.PHASES}

    def begin_frame(self):
        self._frame_start = self._last_mark = self._clock()
        self._current = {}

    def mark(self, phase: str):
        if self._last_mark is None:
            return
        now = self._clock()
        self._current[phase] = now - self._last_mark
        self._last_mark = now

    def end_frame(self, animating: bool, dt: float):
        """Record the frame; returns a summary when an animation just ended."""
        if animating:
            self.frames += 1
            self.total += dt
            for p in self.PHASES:
                self.sums[p] += self._current.get(p, 0.0)
            if dt > self.worst:
                self.worst = dt
                self.worst_phases = dict(self._current)
            return None
        if self.frames == 0:
            return None
        summary = self.summary()
        self._reset()
        return summary

    def summary(self) -> str:
        n = self.frames
        fps = n / self.total if self.total > 0 else 0
        avg = ' '.join(f'{p}={self.sums[p] / n * 1000:.1f}' for p in self.PHASES)
        worst = ' '.join(f'{p}={self.worst_phases.get(p, 0) * 1000:.0f}' for p in self.PHASES)
        return (f'ANIM | frames={n} | {self.total:.2f}s | fps={fps:.0f} '
                f'| worst={self.worst * 1000:.0f}ms | avg ms: {avg} | worst frame ms: {worst}')

"""
Auto-pause Manager - Pauses playback after extended listening.

Prevents music from playing indefinitely when a child forgets to stop it.
After 30 minutes of continuous play in the same context, fades out and pauses.
"""
import time
import logging
import threading
from typing import Optional, Callable

from ..config import AUTO_PAUSE_FADE_DURATION
from ..utils import set_system_volume

logger = logging.getLogger(__name__)


class AutoPauseManager:
    """Manages automatic pause after extended playback."""
    
    def __init__(self, on_pause: Callable[[], None], get_volume: Callable[[], int],
                 get_timeout: Callable[[], int] = None):
        """
        Args:
            on_pause: Callback to pause playback
            get_volume: Returns speaker_level (int)
            get_timeout: Returns auto-pause timeout in seconds (default 30 min)
        """
        self._on_pause = on_pause
        self._get_volume = get_volume
        self._get_timeout = get_timeout or (lambda: 30 * 60)

        self._context_uri: Optional[str] = None
        self._play_start_time: Optional[float] = None
        self._is_fading = False
        self._fade_thread: Optional[threading.Thread] = None
        self._original_volume: int = 100
        self._should_restore_volume = False
    
    def on_play(self, context_uri: Optional[str]):
        """Called when playback starts or context changes."""
        if not context_uri:
            self._reset()
            return
        
        if context_uri != self._context_uri:
            timeout = self._get_timeout()
            logger.info(f'Auto-pause: new context, timer reset ({timeout // 60}min)')
            self._context_uri = context_uri
            self._play_start_time = time.time()
            self._is_fading = False
    
    def on_stop(self):
        """Called when playback stops or pauses."""
        self._reset()
    
    def check(self, is_playing: bool) -> bool:
        """Check if auto-pause should trigger. Returns True if triggered."""
        if not is_playing or not self._play_start_time or self._is_fading:
            return False
        
        elapsed = time.time() - self._play_start_time
        timeout = self._get_timeout()
        if elapsed >= timeout:
            logger.info(f'Auto-pause: {timeout // 60} minutes reached, fading out...')
            self._trigger_fade_out()
            return True
        
        return False
    
    def restore_volume_if_needed(self):
        """Restore volume after auto-pause (call when user resumes)."""
        if self._should_restore_volume:
            logger.info(f'Auto-pause: restoring volume to speaker={self._original_volume}%')
            set_system_volume(self._original_volume)
            self._should_restore_volume = False
    
    def _reset(self):
        """Reset timer state."""
        self._context_uri = None
        self._play_start_time = None
        self._is_fading = False
    
    def _trigger_fade_out(self):
        """Start fade-out in background thread."""
        self._is_fading = True
        self._original_volume = self._get_volume()
        self._should_restore_volume = True
        
        self._fade_thread = threading.Thread(target=self._fade_out_and_pause, daemon=True)
        self._fade_thread.start()
    
    def _fade_out_and_pause(self):
        """Fade out volume over FADE_DURATION seconds, then pause."""
        steps = 20
        step_duration = AUTO_PAUSE_FADE_DURATION / steps
        speaker = self._original_volume

        for i in range(steps):
            progress = (i + 1) / steps
            fade = 1 - progress
            set_system_volume(max(0, int(speaker * fade)))
            time.sleep(step_duration)

        logger.info('Auto-pause: pausing playback')
        self._on_pause()

        time.sleep(0.5)
        set_system_volume(speaker)
        self._should_restore_volume = False
        
        self._reset()
        logger.info('Auto-pause: complete, volume restored')

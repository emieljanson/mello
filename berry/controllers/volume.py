"""
Volume Controller - Manages volume via ALSA on the Pi.

Berry always owns volume: Spotify stays at 100%, Pi controls via ALSA.
"""
import logging

from ..config import VOLUME_LEVELS
from ..api.librespot import LibrespotAPIProtocol
from ..utils import run_async, set_system_volume, mute_speakers, unmute_speakers

logger = logging.getLogger(__name__)


class VolumeController:
    """Manages volume state via ALSA. Spotify is kept at 100%."""
    
    def __init__(self, api: LibrespotAPIProtocol):
        self.api = api
        self.index = 1
        self._spotify_initialized = False
        self._muted = False
    
    @property
    def speaker_level(self) -> int:
        """Current speaker volume level (0-100)."""
        return VOLUME_LEVELS[self.index]['speaker']
    
    @property
    def headphone_level(self) -> int:
        """Current headphone volume level (0-100)."""
        return VOLUME_LEVELS[self.index]['headphone']

    @property
    def bt_level(self) -> int:
        """Current Bluetooth volume level (0-100) for pactl."""
        return VOLUME_LEVELS[self.index]['bt']
    
    @property
    def icon(self) -> str:
        """Current volume icon name."""
        return VOLUME_LEVELS[self.index]['icon']
    
    def init(self):
        """Initialize system volume at startup."""
        set_system_volume(self.speaker_level, self.headphone_level)
        unmute_speakers(self.speaker_level, self.headphone_level)
        self._muted = False
    
    def toggle(self):
        """Cycle through volume levels."""
        self.index = (self.index + 1) % len(VOLUME_LEVELS)
        logger.info(f'Volume: speaker={self.speaker_level}%, headphone={self.headphone_level}%')
        run_async(set_system_volume, self.speaker_level, self.headphone_level)
    
    def mute(self):
        """Mute audio output instantly via ALSA hardware. No-op if already muted."""
        if self._muted:
            return
        self._muted = True
        mute_speakers()
        logger.debug('Speaker muted')

    def unmute(self):
        """Restore audio output via ALSA hardware. No-op if not muted."""
        if not self._muted:
            return
        self._muted = False
        unmute_speakers(self.speaker_level, self.headphone_level)
        logger.debug('Speaker unmuted')

    def ensure_spotify_at_100(self) -> bool:
        """Ensure Spotify volume is at 100% (call on first play). Returns True if set."""
        if not self._spotify_initialized:
            self._spotify_initialized = True
            if self.api.set_volume(100):
                logger.info('Spotify volume set to 100%')
                return True
        return False

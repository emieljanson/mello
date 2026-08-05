"""
Volume Controller - Manages volume via ALSA on the Pi.

Mello always owns volume: Spotify stays at 100%, Pi controls via ALSA.
"""
import logging

from ..api.librespot import LibrespotAPIProtocol
from ..config import VOLUME_FLOOR, VOLUME_ICONS, VOLUME_STEP_PCT
from ..utils import run_async, set_system_volume, mute_speakers, unmute_speakers

logger = logging.getLogger(__name__)


class VolumeController:
    """One continuous 0-100% level, mapped onto each output's usable band.

    The percentage is what the user sets and sees. It is not an ALSA value:
    0% is VOLUME_FLOOR (the quietest still-audible output on this hardware) and
    100% is the ceiling from settings. That way the slider always spans its full
    travel and the parental cap stays invisible to whoever's turning it up.
    """

    def __init__(self, api: LibrespotAPIProtocol, settings):
        self.api = api
        self.settings = settings
        self._spotify_initialized = False
        self._muted = False

    @property
    def pct(self) -> int:
        """Where the slider sits, 0-100."""
        return self.settings.volume_pct

    def _level_for(self, output_type: str) -> int:
        """Map the current percentage onto this output's floor..ceiling band."""
        floor = VOLUME_FLOOR.get(output_type, 0)
        ceiling = max(floor, self.settings.get_max_volume(output_type))
        return round(floor + (ceiling - floor) * self.pct / 100)

    @property
    def speaker_level(self) -> int:
        """Current speaker volume as ALSA wants it (0-100)."""
        return self._level_for('speaker')

    @property
    def bt_level(self) -> int:
        """Current Bluetooth volume for pactl (0-100)."""
        return self._level_for('bt')

    @property
    def icon(self) -> str:
        """Volume icon for the current level, quietest first."""
        if self.pct <= 0:
            return VOLUME_ICONS[0]
        # Even thirds, so the icon tracks the slider rather than a preset index.
        bucket = min(len(VOLUME_ICONS) - 1, self.pct * len(VOLUME_ICONS) // 100)
        return VOLUME_ICONS[bucket]

    def init(self):
        """Initialize system volume at startup."""
        set_system_volume(self.speaker_level)
        unmute_speakers(self.speaker_level)
        self._muted = False

    def set_pct(self, pct: float) -> int:
        """Set the level from the slider. Snaps to VOLUME_STEP_PCT. Returns it."""
        snapped = int(round(max(0.0, min(100.0, pct)) / VOLUME_STEP_PCT) * VOLUME_STEP_PCT)
        if snapped == self.pct:
            return snapped
        self.settings.set_volume_pct(snapped)
        self.apply()
        return snapped

    def nudge(self, delta_pct: float) -> int:
        """Move the level by a signed amount, for keyboard/rotary input."""
        return self.set_pct(self.pct + delta_pct)

    def apply(self):
        """Push the current level to whichever output is live."""
        logger.info(f'Volume: {self.pct}% (speaker={self.speaker_level}, bt={self.bt_level})')
        run_async(set_system_volume, self.speaker_level)

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
        unmute_speakers(self.speaker_level)
        logger.debug('Speaker unmuted')

    def ensure_spotify_at_100(self) -> bool:
        """Ensure Spotify volume is at 100% (call on first play). Returns True if set."""
        if not self._spotify_initialized:
            self._spotify_initialized = True
            if self.api.set_volume(100):
                logger.info('Spotify volume set to 100%')
                return True
        return False

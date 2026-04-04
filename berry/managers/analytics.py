"""
Usage Analytics - Tracks listening sessions via PostHog.

Events fired:
  - session_start    Music starts playing (album, artist)
  - session_end      Music stops (duration, reason: pause/auto_pause/shutdown/switch)
  - track_changed    New track within same session (track name, artist)
  - app_started      Berry boots up
  - device_sleep     Screen goes to sleep (idle_seconds)
  - device_wake      Screen wakes up
"""
import time
import socket
import logging
from datetime import datetime
from typing import Optional

from ..models import NowPlaying

logger = logging.getLogger(__name__)

try:
    from posthog import Posthog
    HAS_POSTHOG = True
except ImportError:
    HAS_POSTHOG = False


class UsageTracker:
    """Tracks active listening sessions and reports to PostHog.

    Call ``update(now_playing)`` on every status refresh. The tracker
    detects playing/not-playing transitions and fires events automatically.
    Gracefully does nothing when PostHog is unavailable or unconfigured.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        host: str = 'https://us.i.posthog.com',
        distinct_id: str = '',
        include_content: bool = False,
        use_machine_id: bool = False,
    ):
        self._enabled = False
        self._posthog = None

        if not api_key:
            logger.info('Analytics: no API key, tracking disabled')
            return

        if not HAS_POSTHOG:
            logger.warning('Analytics: posthog package not installed')
            return

        try:
            self._posthog = Posthog(api_key, host=host)
            self._enabled = True
            logger.info('Analytics: PostHog enabled')
        except Exception as e:
            logger.warning(f'Analytics: PostHog init failed: {e}')
            return

        self._include_content = include_content
        self._use_machine_id = use_machine_id
        self._distinct_id = distinct_id.strip() or self._get_device_id(self._use_machine_id)
        logger.info(f'Analytics: distinct_id={self._distinct_id}')

        # Session state
        self._was_playing = False
        self._session_start: Optional[float] = None
        self._session_context: Optional[str] = None
        self._session_album: Optional[str] = None
        self._session_artist: Optional[str] = None

        # Track change detection
        self._last_track_name: Optional[str] = None

        # Accumulated listening time today (survives pause/resume)
        self._daily_seconds: float = 0
        self._daily_date: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, now_playing: NowPlaying):
        """Call every status refresh. Detects transitions and fires events."""
        if not self._enabled:
            return

        is_playing = now_playing.playing

        # State transition: started playing
        if is_playing and not self._was_playing:
            self._start_session(now_playing)

        # State transition: stopped playing
        elif not is_playing and self._was_playing:
            self._end_session('pause')

        # Context changed while playing (switched album/playlist)
        elif is_playing and self._session_context != now_playing.context_uri:
            self._end_session('switch')
            self._start_session(now_playing)

        # Track changed within same session
        elif is_playing and now_playing.track_name and now_playing.track_name != self._last_track_name:
            self._on_track_changed(now_playing)

        self._was_playing = is_playing

    def on_app_started(self, catalog_size: int = 0):
        """Call once at startup."""
        if not self._enabled:
            return
        self._capture('app_started', {
            'catalog_size': catalog_size,
        })

    def on_shutdown(self):
        """Call before app exits to close any open session and flush."""
        if not self._enabled:
            return
        if self._was_playing:
            self._end_session('shutdown')
        try:
            self._posthog.shutdown()
        except Exception:
            pass

    def on_auto_pause(self):
        """Call when auto-pause triggers."""
        if not self._enabled:
            return
        if self._was_playing:
            self._end_session('auto_pause')
            self._was_playing = False

    def on_sleep(self, idle_seconds: float):
        """Call when device enters sleep mode."""
        if not self._enabled:
            return
        self._capture('device_sleep', {
            'idle_seconds': round(idle_seconds),
        })

    def on_wake(self):
        """Call when device wakes from sleep."""
        if not self._enabled:
            return
        self._capture('device_wake', {})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_session(self, np: NowPlaying):
        self._session_start = time.time()
        self._session_context = np.context_uri
        self._session_album = np.track_album
        self._session_artist = np.track_artist
        self._last_track_name = np.track_name

        content_type = 'playlist' if 'playlist' in (np.context_uri or '') else 'album'

        properties = {
            'content_type': content_type,
        }
        if self._include_content:
            properties.update({
                'album': np.track_album or '',
                'artist': np.track_artist or '',
                'track': np.track_name or '',
            })
        self._capture('session_start', properties)

    def _end_session(self, reason: str):
        duration = 0
        if self._session_start:
            duration = round(time.time() - self._session_start)
        self._add_daily_seconds(duration)

        properties = {
            'duration_seconds': duration,
            'duration_minutes': round(duration / 60, 1),
            'reason': reason,
            'daily_total_minutes': round(self._daily_seconds / 60, 1),
        }
        if self._include_content:
            properties.update({
                'album': self._session_album or '',
                'artist': self._session_artist or '',
            })
        self._capture('session_end', properties)

        self._session_start = None
        self._session_context = None
        self._session_album = None
        self._session_artist = None
        self._last_track_name = None

    def _on_track_changed(self, np: NowPlaying):
        self._last_track_name = np.track_name
        if not self._include_content:
            return
        self._capture('track_changed', {
            'track': np.track_name or '',
            'artist': np.track_artist or '',
            'album': np.track_album or '',
        })

    def _add_daily_seconds(self, seconds: float):
        """Accumulate listening time per calendar day."""
        today = datetime.now().strftime('%Y-%m-%d')
        if self._daily_date != today:
            self._daily_seconds = 0
            self._daily_date = today
        self._daily_seconds += seconds

    def _capture(self, event: str, properties: dict):
        now = datetime.now()
        properties['hour_of_day'] = now.hour
        properties['day_of_week'] = now.strftime('%A')

        try:
            self._posthog.capture(event, distinct_id=self._distinct_id, properties=properties)
            logger.info(f'Analytics: {event} {properties}')
        except Exception as e:
            logger.warning(f'Analytics: capture failed: {e}')

    @staticmethod
    def _get_device_id(use_machine_id: bool = False) -> str:
        """Device identifier for analytics (privacy-safe defaults)."""
        hostname = 'berry'
        try:
            hostname = socket.gethostname()
        except Exception:
            pass
        if not use_machine_id:
            return hostname
        try:
            with open('/etc/machine-id') as f:
                machine_id = f.read().strip()[:8]
            return f'{hostname}-{machine_id}'
        except Exception:
            return hostname

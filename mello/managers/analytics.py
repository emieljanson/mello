"""
Usage Analytics - Tracks listening sessions via PostHog.

Events fired:
  - session_start    Music starts playing (album, artist)
  - session_end      Music stops (duration, reason: pause/auto_pause/shutdown/switch)
  - track_changed    New track within same session (track name, artist)
  - app_started      Mello boots up (and how the previous run ended)
  - app_stopped      Mello stops on purpose (uptime)
  - device_sleep     Screen goes to sleep (idle_seconds)
  - device_wake      Screen wakes up

Speaker health, so a release can be compared with the one before it:
  - unclean_shutdown       Previous run did not stop on purpose (cause: app_crashed / power_lost)
  - main_loop_froze        Previous run froze for 30 s or died hard (crash.log has a dump)
  - thread_crashed         An uncaught error stopped a background thread
  - watchdog_tripped       Loading an album hung and was cancelled
  - engine_restarted       Spotify engine stopped answering and was restarted
  - engine_reconnected     Connection to the Spotify engine came back (offline_seconds)
  - wifi_restored          WiFi came back after a drop (offline_seconds, rejoin rounds)
  - bluetooth_connected    Headphone connected
  - bluetooth_disconnected Headphone went away (connected_minutes, while_playing, from_menu)
  - device_health          Every 6 hours: uptime, free memory, CPU temperature

Every event carries app_version (branch@commit), engine and ui.
"""
import time
import socket
import logging
from pathlib import Path
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
        app_version: str = 'unknown',
        engine: str = '',
        ui: str = '',
    ):
        self._enabled = False
        self._posthog = None
        self._started_at = time.monotonic()

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

        # Sent with every event, so PostHog can split any chart by release.
        self._common = {
            'app_version': app_version,
            'engine': engine,
            'ui': ui,
        }

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

    def on_app_started(self, catalog_size: int = 0, previous_run: Optional[dict] = None):
        """Call once at startup.

        ``previous_run`` is what crash_log found about the run before this
        one: {'ended': 'clean' | 'app_crashed' | 'power_lost', 'froze': bool}.
        """
        if not self._enabled:
            return
        previous_run = previous_run or {}
        ended = previous_run.get('ended', 'clean')
        froze = bool(previous_run.get('froze'))
        self._capture('app_started', {
            'catalog_size': catalog_size,
            'previous_run': ended,
            'previous_run_froze': froze,
            '$set': {k: self._common[k] for k in ('app_version', 'engine', 'ui')},
        })
        if ended != 'clean':
            self._capture('unclean_shutdown', {'cause': ended})
        if froze:
            self._capture('main_loop_froze', {
                'cause': previous_run.get('dump', 'hang'),
                'previous_run': ended,
            })

    def on_shutdown(self):
        """Call before app exits to close any open session and flush."""
        if not self._enabled:
            return
        if self._was_playing:
            self._end_session('shutdown')
        self._capture('app_stopped', {'uptime_hours': self._uptime_hours()})
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
    # Speaker health
    # ------------------------------------------------------------------

    def on_thread_crashed(self, thread_name: str, error_type: str):
        """An uncaught error stopped a background thread."""
        if not self._enabled:
            return
        self._capture('thread_crashed', {
            'thread': thread_name,
            'error_type': error_type,
        })

    def on_watchdog_tripped(self, stall_seconds: float):
        """Loading an album hung so long that the watchdog cancelled it."""
        if not self._enabled:
            return
        self._capture('watchdog_tripped', {'stall_seconds': round(stall_seconds)})

    def on_engine_restarted(self, outage_seconds: float, ok: bool):
        """The Spotify engine stopped answering and Mello restarted it."""
        if not self._enabled:
            return
        self._capture('engine_restarted', {
            'outage_seconds': round(outage_seconds),
            'ok': ok,
        })

    def on_engine_reconnected(self, offline_seconds: float):
        """The connection to the Spotify engine came back after being lost."""
        if not self._enabled:
            return
        self._capture('engine_reconnected', {'offline_seconds': round(offline_seconds)})

    def on_wifi_restored(self, offline_seconds: float, rejoin_rounds: int,
                         signal_dbm: Optional[int] = None):
        """WiFi came back. Sent afterwards: while offline nothing gets out."""
        if not self._enabled:
            return
        properties = {
            'offline_seconds': round(offline_seconds),
            'rejoin_rounds': rejoin_rounds,
            'rejoined_by_mello': rejoin_rounds > 0,
        }
        if signal_dbm is not None:
            properties['signal_dbm'] = signal_dbm
        self._capture('wifi_restored', properties)

    def on_bluetooth_connected(self):
        if not self._enabled:
            return
        self._capture('bluetooth_connected', {})

    def on_bluetooth_disconnected(self, connected_seconds: float, while_playing: bool,
                                  from_menu: bool):
        """A headphone went away. ``from_menu`` means the owner most likely
        chose it; otherwise it switched off, ran out of range or dropped."""
        if not self._enabled:
            return
        self._capture('bluetooth_disconnected', {
            'connected_minutes': round(connected_seconds / 60, 1),
            'while_playing': while_playing,
            'from_menu': from_menu,
        })

    def on_health(self, health: dict):
        """Periodic snapshot (see ``device_health``), so crash counts can be
        set against how many hours speakers ran."""
        if not self._enabled:
            return
        properties = {'app_uptime_hours': self._uptime_hours()}
        properties.update(health)
        self._capture('device_health', properties)

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

    def _uptime_hours(self) -> float:
        return round((time.monotonic() - self._started_at) / 3600, 2)

    def _capture(self, event: str, properties: dict):
        properties = {**self._common, **properties}
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
        hostname = 'mello'
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


def device_health() -> dict:
    """System uptime, free memory and CPU temperature (Linux / Pi only)."""
    health = {}
    try:
        health['system_uptime_hours'] = round(
            float(Path('/proc/uptime').read_text().split()[0]) / 3600, 1)
    except Exception:
        pass
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                health['mem_available_mb'] = int(line.split()[1]) // 1024
    except Exception:
        pass
    try:
        temp = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip())
        health['cpu_temp_c'] = round(temp / 1000, 1)
    except Exception:
        pass
    return health

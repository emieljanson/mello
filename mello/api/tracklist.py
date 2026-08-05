"""
Track lists - what songs are in an album, playlist or show.

go-librespot's /status only reports the track playing right now, so the only
way to know what comes next is Spotify's Web API. The daemon will mint an
access token for its own session (POST /token), which means no developer app
registration and no credentials of our own.

That borrowed token authenticates as go-librespot's client ID, which every
librespot/spotifyd/go-librespot install on earth shares — and the Web API rate
limit is per client ID. In practice that quota is permanently exhausted: every
request comes back 429 on the first try, so borrowing is a fallback, not a
plan.

The fix is your own Spotify app (client ID + secret in .env), which gets its
own quota. See docs/spotify-api.md. Client credentials are enough — album,
playlist and show track lists are public catalog data.

Requests go straight to api.spotify.com rather than through the daemon's
/web-api proxy, because that proxy discards Spotify's Retry-After header.

Every list is cached on disk forever. An album only has to succeed once.
"""
import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = 'https://api.spotify.com/v1'
ACCOUNTS_TOKEN_URL = 'https://accounts.spotify.com/api/token'

# Spotify caps page size at 50 for album tracks, 100 for playlist items.
PAGE_SIZE = 50

# ponytail: hard cap so a 5000-track playlist can't eat the SD card or the
# rate limit. Raise it if anyone actually hits this on a kids' speaker.
MAX_TRACKS = 300

# A 429 carries a Retry-After in seconds. Short ones are slept out inline;
# longer ones become a cooldown so no worker thread is parked waiting.
MAX_ATTEMPTS = 3
INLINE_RETRY_WAIT = 30

# Never trust a Retry-After beyond this — a bogus header must not disable track
# lists for the rest of the day.
MAX_COOLDOWN = 15 * 60

# Assumed cooldown when a 429 arrives with no usable Retry-After.
DEFAULT_COOLDOWN = 60

# Refresh an app token this many seconds before it actually expires.
TOKEN_EXPIRY_MARGIN = 60


@dataclass
class Track:
    """One entry in a context's track list."""
    uri: str
    name: str
    artist: str = ''


def parse_context(context_uri: str) -> Optional[tuple]:
    """Split 'spotify:album:xyz' into ('album', 'xyz'). None if unsupported."""
    match = re.match(r'^spotify:(album|playlist|show):([A-Za-z0-9]+)$', context_uri or '')
    if not match:
        return None
    return match.group(1), match.group(2)


def _endpoint(kind: str, spotify_id: str) -> str:
    return {
        'album': f'{API_BASE}/albums/{spotify_id}/tracks',
        'playlist': f'{API_BASE}/playlists/{spotify_id}/tracks',
        'show': f'{API_BASE}/shows/{spotify_id}/episodes',
    }[kind]


def _parse_items(kind: str, items: list) -> List[Track]:
    """Normalise the three response shapes into Tracks, skipping dead entries."""
    tracks = []
    for raw in items:
        # Playlist items wrap the track; albums and shows don't.
        entry = raw.get('track') if kind == 'playlist' else raw
        if not isinstance(entry, dict) or not entry.get('uri'):
            continue  # local files and removed tracks come back as null
        artists = entry.get('artists') or []
        artist = ', '.join(a['name'] for a in artists if a.get('name'))
        tracks.append(Track(
            uri=entry['uri'],
            name=entry.get('name') or 'Unknown',
            artist=artist,
        ))
    return tracks


class TrackListStore:
    """Fetches and caches the track list for each saved context."""

    def __init__(self, cache_dir: Path, token_url: str, mock_mode: bool = False,
                 client_id: str = '', client_secret: str = ''):
        self.cache_dir = Path(cache_dir)
        self.token_url = token_url
        self.mock_mode = mock_mode
        self.client_id = client_id
        self.client_secret = client_secret

        self._lock = threading.Lock()
        self._lists: Dict[str, List[Track]] = {}
        self._in_flight: set = set()
        self._failed: set = set()   # don't hammer a context that can't be fetched
        self._unavailable: set = set()  # Spotify refuses these (404) — never retry
        # Spotify throttles per client ID, and go-librespot's is shared by every
        # install — so a 429 applies to every context, not just this one.
        self._blocked_until: float = 0.0
        self._app_token: Optional[str] = None
        self._app_token_expires: float = 0.0

        if not mock_mode:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(f'Track cache unavailable: {e}')

    # --- Reading ---

    def get(self, context_uri: str) -> Optional[List[Track]]:
        """Cached track list for a context, or None if we don't have it yet."""
        if not context_uri:
            return None
        with self._lock:
            cached = self._lists.get(context_uri)
        if cached is not None:
            return cached
        loaded = self._load_from_disk(context_uri)
        if loaded is not None:
            with self._lock:
                self._lists[context_uri] = loaded
        return loaded

    def index_of(self, context_uri: str, track_uri: Optional[str]) -> Optional[int]:
        """Position of a track within its context, or None if unknown."""
        if not track_uri:
            return None
        tracks = self.get(context_uri)
        if not tracks:
            return None
        return next((i for i, t in enumerate(tracks) if t.uri == track_uri), None)

    def neighbours(self, context_uri: str, track_uri: Optional[str]) -> tuple:
        """(previous, next) Track around the current one — the peek under the cover."""
        tracks = self.get(context_uri)
        idx = self.index_of(context_uri, track_uri)
        if not tracks or idx is None:
            return None, None
        prev = tracks[idx - 1] if idx > 0 else None
        nxt = tracks[idx + 1] if idx + 1 < len(tracks) else None
        return prev, nxt

    # --- Fetching ---

    def wants_fetch(self, context_uri: str) -> bool:
        """True when this context has no list and none is being fetched."""
        if self.mock_mode or not parse_context(context_uri):
            return False
        if self.get(context_uri) is not None:
            return False
        if self.cooldown_remaining() > 0:
            return False
        with self._lock:
            return (context_uri not in self._in_flight
                    and context_uri not in self._failed
                    and context_uri not in self._unavailable)

    def is_unavailable(self, context_uri: str) -> bool:
        """True when Spotify has refused to share this list (404)."""
        with self._lock:
            return context_uri in self._unavailable

    def uses_own_credentials(self) -> bool:
        """True when we have our own Spotify app, not go-librespot's shared one."""
        return bool(self.client_id and self.client_secret)

    def fetch(self, context_uri: str) -> Optional[List[Track]]:
        """Fetch and cache a context's tracks. Blocking — call from a worker."""
        parsed = parse_context(context_uri)
        if not parsed:
            return None

        with self._lock:
            if context_uri in self._in_flight:
                return None
            self._in_flight.add(context_uri)

        logger.info(f'Track list: fetching {context_uri[:45]}')
        try:
            tracks = self._fetch_all_pages(*parsed)
            if tracks is None:
                with self._lock:
                    self._failed.add(context_uri)
                return None
            with self._lock:
                self._lists[context_uri] = tracks
                self._failed.discard(context_uri)
            self._save_to_disk(context_uri, tracks)
            logger.info(f'Track list cached: {len(tracks)} tracks for {context_uri[:45]}')
            return tracks
        finally:
            with self._lock:
                self._in_flight.discard(context_uri)

    def cooldown_remaining(self) -> float:
        """Seconds until Spotify's rate limit is expected to lift. 0 when clear."""
        with self._lock:
            return max(0.0, self._blocked_until - time.time())

    def retry_failed(self):
        """Forget past failures so a throttled context can be tried again."""
        with self._lock:
            if self._failed:
                logger.info(f'Track list: clearing {len(self._failed)} failed fetch(es) for retry')
            self._failed.clear()

    def _access_token(self) -> Optional[str]:
        """Our own app token when configured, else one borrowed from the daemon."""
        if self.uses_own_credentials():
            token = self._client_credentials_token()
            if token:
                return token
            # Fall through: a bad secret shouldn't be worse than no secret.
        return self._borrowed_token()

    def _client_credentials_token(self) -> Optional[str]:
        """Token for our own Spotify app. Cached until it nearly expires.

        Client credentials give no user context, which is all we need: album,
        playlist and show track lists are public catalog data.
        """
        with self._lock:
            if self._app_token and time.time() < self._app_token_expires:
                return self._app_token

        basic = base64.b64encode(
            f'{self.client_id}:{self.client_secret}'.encode()).decode()
        try:
            resp = requests.post(
                ACCOUNTS_TOKEN_URL,
                data={'grant_type': 'client_credentials'},
                headers={'Authorization': f'Basic {basic}'},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(
                    f'Spotify app token rejected ({resp.status_code}) — '
                    f'check SPOTIFY_CLIENT_ID/SECRET in .env'
                )
                return None
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f'Spotify app token request failed: {e}')
            return None

        token = data.get('access_token')
        if not isinstance(token, str) or not token:
            return None
        try:
            lifetime = float(data.get('expires_in') or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        with self._lock:
            self._app_token = token
            self._app_token_expires = time.time() + max(30.0, lifetime - TOKEN_EXPIRY_MARGIN)
        logger.info('Spotify app token obtained (own client ID, own rate limit)')
        return token

    def _borrowed_token(self) -> Optional[str]:
        """Borrow an access token from the daemon's own Spotify session."""
        try:
            resp = requests.post(self.token_url, timeout=5)
            if resp.status_code == 204:
                logger.info('Track list: no active Spotify session yet, cannot get a token')
                return None
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f'Token request failed: {e}')
            return None

        if not isinstance(data, dict):
            return None
        # Field name has varied across go-librespot versions; take the one that
        # looks like a bearer token rather than guessing a key.
        for key in ('token', 'access_token', 'accessToken'):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return next((v for v in data.values() if isinstance(v, str) and len(v) > 40), None)

    def _fetch_all_pages(self, kind: str, spotify_id: str) -> Optional[List[Track]]:
        token = self._access_token()
        if not token:
            return None

        headers = {'Authorization': f'Bearer {token}'}
        url = _endpoint(kind, spotify_id)
        params = {'limit': PAGE_SIZE, 'offset': 0}
        tracks: List[Track] = []

        context_uri = f'spotify:{kind}:{spotify_id}'
        while url and len(tracks) < MAX_TRACKS:
            payload = self._get_json(url, headers, params, context_uri)
            if payload is None:
                return None  # give up on this context for now; retried later
            tracks.extend(_parse_items(kind, payload.get('items') or []))
            url = payload.get('next')
            params = None  # 'next' already carries limit/offset
            if url and len(tracks) >= MAX_TRACKS:
                logger.info(f'Track list truncated at {MAX_TRACKS} for {kind}:{spotify_id}')

        return tracks[:MAX_TRACKS]

    def _get_json(self, url: str, headers: dict, params: Optional[dict],
                  context_uri: str = '') -> Optional[dict]:
        """GET with bounded retries that honour Spotify's Retry-After."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=8)
            except requests.RequestException as e:
                logger.warning(f'Track list request failed: {e}')
                return None

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    logger.warning('Track list response was not JSON')
                    return None

            if resp.status_code == 429:
                wait = self._retry_after(resp)
                # Short cooldown: sleep it out here, we're on a worker thread.
                if wait <= INLINE_RETRY_WAIT and attempt < MAX_ATTEMPTS - 1:
                    logger.info(f'Track list throttled, waiting {wait:.0f}s (attempt {attempt + 1})')
                    threading.Event().wait(wait)
                    continue
                # Long one: record it and back off. Retrying sooner than Spotify
                # asked adds load to the quota we're already being limited on.
                self._start_cooldown(wait)
                return None

            if resp.status_code == 401:
                logger.info('Track list token rejected (401), will refetch a token next time')
                with self._lock:
                    self._app_token = None   # force a fresh one next attempt
                return None

            if resp.status_code == 404:
                # Spotify's own algorithmic and editorial playlists (the
                # 37i9dQZF1D... ones) are not readable by third-party apps.
                # No amount of retrying changes that, so stop asking.
                if context_uri:
                    with self._lock:
                        self._unavailable.add(context_uri)
                logger.info(
                    f'Track list unavailable: Spotify returned 404 for '
                    f'{context_uri[:45] or url} (its own editorial playlists '
                    f'are closed to third-party apps)'
                )
                return None

            logger.warning(f'Track list request returned {resp.status_code}')
            return None
        return None

    @staticmethod
    def _retry_after(resp) -> float:
        """Seconds Spotify asked us to wait. Clamped, never None.

        The previous version returned None for anything over the inline limit,
        which threw away the only number that explains a throttle — and left
        the caller retrying on its own schedule instead of Spotify's.
        """
        raw = resp.headers.get('Retry-After')
        try:
            wait = float(raw) if raw is not None else DEFAULT_COOLDOWN
        except (TypeError, ValueError):
            wait = DEFAULT_COOLDOWN
        return max(1.0, min(wait, MAX_COOLDOWN))

    def _start_cooldown(self, seconds: float):
        """Stop trying any context until Spotify's cooldown has passed."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.time() + seconds)
        logger.info(
            f'Track list throttled (429): backing off {seconds:.0f}s '
            f'(Spotify rate-limits go-librespot\'s shared client ID)'
        )

    # --- Disk cache ---

    def _cache_path(self, context_uri: str) -> Path:
        parsed = parse_context(context_uri)
        kind, spotify_id = parsed if parsed else ('other', context_uri.replace(':', '_'))
        return self.cache_dir / f'{kind}_{spotify_id}.json'

    def _load_from_disk(self, context_uri: str) -> Optional[List[Track]]:
        if self.mock_mode:
            return None
        path = self._cache_path(context_uri)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            return [Track(**entry) for entry in raw['tracks']]
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            logger.warning(f'Discarding unreadable track cache {path.name}: {e}')
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def _save_to_disk(self, context_uri: str, tracks: List[Track]):
        if self.mock_mode:
            return
        path = self._cache_path(context_uri)
        temp = path.with_suffix('.tmp')
        try:
            # Write-then-rename: a power cut mid-write must not leave a
            # half-written file that poisons the cache on next boot.
            temp.write_text(json.dumps(
                {'uri': context_uri, 'tracks': [asdict(t) for t in tracks]}))
            temp.replace(path)
        except OSError as e:
            logger.warning(f'Could not cache track list: {e}')
            try:
                temp.unlink()
            except OSError:
                pass

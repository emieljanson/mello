"""
Track lists - what songs are in an album, playlist or show.

go-librespot's /status only reports the track playing right now, so the only
way to know what comes next is Spotify's Web API. The daemon will mint an
access token for its own session (POST /token), which means no developer app
registration and no credentials of our own.

Requests go straight to api.spotify.com rather than through the daemon's
/web-api proxy, because that proxy discards Spotify's Retry-After header and
we need it: go-librespot ships a client ID shared by every installation, so
throttling is frequent — though the cooldown is seconds, not hours.

Every list is cached on disk forever. An album only has to succeed once.
"""
import json
import logging
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = 'https://api.spotify.com/v1'

# Spotify caps page size at 50 for album tracks, 100 for playlist items.
PAGE_SIZE = 50

# ponytail: hard cap so a 5000-track playlist can't eat the SD card or the
# rate limit. Raise it if anyone actually hits this on a kids' speaker.
MAX_TRACKS = 300

# A 429 here carries a short Retry-After (seconds). Bounded retries keep a
# throttled fetch from hanging a worker thread all day.
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30


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

    def __init__(self, cache_dir: Path, token_url: str, mock_mode: bool = False):
        self.cache_dir = Path(cache_dir)
        self.token_url = token_url
        self.mock_mode = mock_mode

        self._lock = threading.Lock()
        self._lists: Dict[str, List[Track]] = {}
        self._in_flight: set = set()
        self._failed: set = set()   # don't hammer a context that can't be fetched

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
        with self._lock:
            return context_uri not in self._in_flight and context_uri not in self._failed

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

    def retry_failed(self):
        """Forget past failures so a throttled context can be tried again."""
        with self._lock:
            if self._failed:
                logger.info(f'Track list: clearing {len(self._failed)} failed fetch(es) for retry')
            self._failed.clear()

    def _access_token(self) -> Optional[str]:
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

        while url and len(tracks) < MAX_TRACKS:
            payload = self._get_json(url, headers, params)
            if payload is None:
                return None  # give up on this context for now; retried later
            tracks.extend(_parse_items(kind, payload.get('items') or []))
            url = payload.get('next')
            params = None  # 'next' already carries limit/offset
            if url and len(tracks) >= MAX_TRACKS:
                logger.info(f'Track list truncated at {MAX_TRACKS} for {kind}:{spotify_id}')

        return tracks[:MAX_TRACKS]

    def _get_json(self, url: str, headers: dict, params: Optional[dict]) -> Optional[dict]:
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
                if wait is None or attempt == MAX_ATTEMPTS - 1:
                    logger.info(f'Track list throttled (429), giving up for now (retry_after={wait})')
                    return None
                logger.info(f'Track list throttled, waiting {wait}s (attempt {attempt + 1})')
                threading.Event().wait(wait)
                continue

            if resp.status_code == 401:
                logger.info('Track list token rejected (401), will refetch a token next time')
                return None

            logger.warning(f'Track list request returned {resp.status_code}')
            return None
        return None

    @staticmethod
    def _retry_after(resp) -> Optional[float]:
        """Seconds to wait per Retry-After, or None if it's unusably long."""
        raw = resp.headers.get('Retry-After')
        try:
            wait = float(raw) if raw is not None else 5.0
        except (TypeError, ValueError):
            wait = 5.0
        if wait > MAX_RETRY_WAIT:
            return None
        return max(1.0, wait)

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

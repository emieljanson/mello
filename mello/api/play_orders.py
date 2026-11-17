"""
Play orders: the track order of each album, playlist or show, as Spotify plays it.

Soloist can only start an album at its first track (it ignores any "start at"
option; a show starts wherever Spotify decides). Reaching a saved track meant
skipping there, one track at a time: 41 s for episode 13 of a podcast on the
daily speaker. With the order known, Mello plays the saved track directly
(~0.3 s) and starts each next track itself.

The order is learned the first time a context plays the normal way (from
Soloist's queue), and kept in data/play_orders.json so it survives restarts.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Playlists get edited and shows get new episodes, and the queue is the only
# way to see that (by playing the context the normal, slow way). Learn again
# when the order is this old, or when the saved track is not in it.
MAX_AGE = 3 * 24 * 3600


class PlayOrders:
    """Track orders per context URI, saved to a JSON file."""

    def __init__(self, path: Optional[Path] = None, max_age: float = MAX_AGE, clock=time.time):
        self.path = Path(path) if path else None
        self.max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._orders: dict = {}
        self._load()

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._orders = {k: v for k, v in data.items()
                                if isinstance(v, dict) and isinstance(v.get('items'), list)}
        except (OSError, ValueError) as e:
            logger.warning(f'Play orders unreadable, starting empty: {e}')

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix('.json.tmp')
        try:
            tmp.write_text(json.dumps(self._orders))
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f'Could not save play orders: {e}')

    def get(self, context_uri: str) -> Optional[List[str]]:
        """The known order, or None when unknown or too old to trust."""
        with self._lock:
            entry = self._orders.get(context_uri)
            if not entry or not entry.get('items'):
                return None
            if self._clock() - entry.get('learned_at', 0) > self.max_age:
                return None
            return list(entry['items'])

    def put(self, context_uri: str, items: List[str]):
        if not items:
            return
        with self._lock:
            self._orders[context_uri] = {'items': list(items), 'learned_at': self._clock()}
            self._save()
        logger.info(f'Play order learned: {context_uri[:50]} ({len(items)} items)')

    def forget(self, context_uri: str):
        with self._lock:
            if self._orders.pop(context_uri, None) is not None:
                self._save()


def one_round(current: str, upcoming: List[str]) -> Optional[List[str]]:
    """The full order from the queue of a context that repeats.

    With context repeat on, Soloist's queue runs on into the next round, so
    the current track comes round again after one full round. Without that
    the list is cut off at the end and the tracks before `current` are
    missing: then there is no complete order to keep.
    """
    if current in upcoming:
        return [current] + upcoming[:upcoming.index(current)]
    return None

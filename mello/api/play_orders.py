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
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# An order is kept until it turns out to be out of date: an album never
# changes, and for playlists and shows (edited, new episodes) direct play
# notices it by itself: the saved track is not in the order, or the order
# runs out. Then that one play goes the normal way and learns again.
MAX_AGE = None


class PlayOrders:
    """Track orders per context URI, saved to a JSON file."""

    def __init__(self, path: Optional[Path] = None, max_age: Optional[float] = MAX_AGE, clock=time.time):
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
        entry = self.entry(context_uri)
        return entry[0] if entry else None

    def entry(self, context_uri: str) -> Optional[Tuple[List[str], bool]]:
        """(items, complete): complete means the whole album, looping round."""
        with self._lock:
            entry = self._orders.get(context_uri)
            if not entry or not entry.get('items'):
                return None
            if self.max_age is not None and self._clock() - entry.get('learned_at', 0) > self.max_age:
                return None
            return list(entry['items']), bool(entry.get('complete'))

    def put(self, context_uri: str, items: List[str], complete: bool = True):
        """Remember an order; a partial one is joined to what is known."""
        if not items:
            return
        with self._lock:
            old = self._orders.get(context_uri)
            if not complete and old and not old.get('complete'):
                items = _join(old['items'], list(items))
            self._orders[context_uri] = {'items': list(items), 'complete': complete,
                                         'learned_at': self._clock()}
            self._save()
        logger.info(f'Play order learned: {context_uri[:50]} ({len(items)} items'
                    f'{"" if complete else ", part of it"})')

    def find(self, track_uri: str) -> Optional[Tuple[str, List[str], bool]]:
        """The album a track belongs to: (context, items, complete), newest first."""
        with self._lock:
            best = None
            for context, entry in self._orders.items():
                if track_uri in (entry.get('items') or []):
                    if best is None or entry.get('learned_at', 0) > best[1].get('learned_at', 0):
                        best = (context, entry)
            if not best:
                return None
            return best[0], list(best[1]['items']), bool(best[1].get('complete'))

    def forget(self, context_uri: str):
        with self._lock:
            if self._orders.pop(context_uri, None) is not None:
                self._save()


def one_round(current: str, upcoming: List[str]) -> Tuple[List[str], bool]:
    """The order seen in the queue: (items, complete).

    With context repeat on, Soloist's queue runs on into the next round, so
    the current track comes round again after one full round: that is the
    whole album. The queue holds at most 80 tracks though (seen on the
    device), so for a long audiobook it is only the next part.
    """
    if current in upcoming:
        return [current] + upcoming[:upcoming.index(current)], True
    return [current] + list(upcoming), False


def _join(old: List[str], new: List[str]) -> List[str]:
    """Join two overlapping parts of one album's order."""
    if new[0] in old:
        start = old.index(new[0])
        joined = old[:start] + new
        if new[-1] in old:
            joined += old[old.index(new[-1]) + 1:]
        return joined
    if old[0] in new:
        return new[:new.index(old[0])] + old if old[-1] not in new else new
    return new

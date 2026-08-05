"""
Tests for TrackListStore — fetching, rate-limit handling, and the disk cache.

The rate limit is the whole reason this class exists in this shape:
go-librespot ships a client ID shared by every install, so 429 is common and
carries a short Retry-After. Getting that wrong means either hammering
Spotify or never showing a track list.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.tracklist import (
    MAX_TRACKS, Track, TrackListStore, parse_context, _parse_items,
)

ALBUM = 'spotify:album:0ETFjACtuP2ADo6LFhL6HN'
PLAYLIST = 'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M'


@pytest.fixture
def store(tmp_path):
    return TrackListStore(cache_dir=tmp_path / 'tracks',
                          token_url='http://localhost:3678/token')


def _resp(status=200, payload=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload if payload is not None else {}
    return r


# --- URI parsing ---

@pytest.mark.parametrize('uri, expected', [
    (ALBUM, ('album', '0ETFjACtuP2ADo6LFhL6HN')),
    (PLAYLIST, ('playlist', '37i9dQZF1DXcBWIGoYBM5M')),
    ('spotify:show:abc123', ('show', 'abc123')),
    ('spotify:track:abc123', None),      # a track is not a context
    ('spotify:artist:abc123', None),
    ('', None),
    (None, None),
])
def test_parse_context(uri, expected):
    assert parse_context(uri) == expected


# --- Response shapes ---

def test_album_items_parsed():
    items = [{'uri': 'spotify:track:1', 'name': 'Come Together',
              'artists': [{'name': 'The Beatles'}]}]
    assert _parse_items('album', items) == [
        Track(uri='spotify:track:1', name='Come Together', artist='The Beatles')]


def test_playlist_items_are_wrapped():
    """Playlist responses nest the track one level deeper than albums."""
    items = [{'added_at': 'x', 'track': {'uri': 'spotify:track:2', 'name': 'Dreams',
                                         'artists': [{'name': 'Fleetwood Mac'}]}}]
    assert _parse_items('playlist', items)[0].name == 'Dreams'


def test_null_and_local_entries_skipped():
    """Removed tracks and local files come back as null and must not crash."""
    items = [{'track': None}, {'track': {'name': 'No URI'}},
             {'track': {'uri': 'spotify:track:3', 'name': 'Real'}}]
    parsed = _parse_items('playlist', items)
    assert [t.name for t in parsed] == ['Real']


def test_multiple_artists_joined():
    items = [{'uri': 'u', 'name': 'n', 'artists': [{'name': 'A'}, {'name': 'B'}]}]
    assert _parse_items('album', items)[0].artist == 'A, B'


def test_missing_name_falls_back():
    assert _parse_items('album', [{'uri': 'u'}])[0].name == 'Unknown'


# --- Fetching ---

def test_fetch_caches_to_memory_and_disk(store):
    payload = {'items': [{'uri': 'spotify:track:1', 'name': 'One', 'artists': []}],
               'next': None}
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)):
        tracks = store.fetch(ALBUM)

    assert [t.name for t in tracks] == ['One']
    assert store.get(ALBUM) is not None
    # A fresh store must read it back without any network at all
    reread = TrackListStore(cache_dir=store.cache_dir, token_url=store.token_url)
    assert [t.name for t in reread.get(ALBUM)] == ['One']


def test_pagination_follows_next(store):
    page1 = {'items': [{'uri': 'spotify:track:1', 'name': 'One', 'artists': []}],
             'next': 'https://api.spotify.com/v1/albums/x/tracks?offset=50'}
    page2 = {'items': [{'uri': 'spotify:track:2', 'name': 'Two', 'artists': []}],
             'next': None}
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', side_effect=[_resp(200, page1), _resp(200, page2)]):
        tracks = store.fetch(ALBUM)
    assert [t.name for t in tracks] == ['One', 'Two']


def test_truncates_absurd_playlists(store):
    """A huge playlist must not fill the SD card or the rate limit."""
    items = [{'uri': f'spotify:track:{i}', 'name': str(i), 'artists': []}
             for i in range(MAX_TRACKS + 50)]
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(200, {'items': items, 'next': None})):
        assert len(store.fetch(ALBUM)) == MAX_TRACKS


# --- Rate limiting: the reason this class is shaped this way ---

def test_short_retry_after_is_honoured_then_succeeds(store):
    payload = {'items': [{'uri': 'spotify:track:1', 'name': 'One', 'artists': []}], 'next': None}
    throttled = _resp(429, headers={'Retry-After': '2'})
    waits = []
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', side_effect=[throttled, _resp(200, payload)]), \
         patch('mello.api.tracklist.threading.Event') as ev:
        ev.return_value.wait.side_effect = lambda w: waits.append(w)
        tracks = store.fetch(ALBUM)

    assert waits == [2.0]                    # waited exactly what Spotify asked
    assert [t.name for t in tracks] == ['One']


def test_absurd_retry_after_gives_up_without_waiting(store):
    """An hour-long cooldown must not park a worker thread for an hour."""
    waits = []
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '3600'})), \
         patch('mello.api.tracklist.threading.Event') as ev:
        ev.return_value.wait.side_effect = lambda w: waits.append(w)
        assert store.fetch(ALBUM) is None
    assert waits == []


def test_persistent_throttle_stops_retrying(store):
    """A failed context is remembered, so we don't hammer Spotify every frame."""
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '3600'})):
        store.fetch(ALBUM)
    assert store.wants_fetch(ALBUM) is False
    store.retry_failed()
    assert store.wants_fetch(ALBUM) is True


def test_no_session_yields_no_list(store):
    """204 from /token means nothing has played yet — not an error."""
    with patch('mello.api.tracklist.requests.post', return_value=_resp(204)):
        assert store.fetch(ALBUM) is None


def test_token_field_name_variations(store):
    payload = {'items': [], 'next': None}
    for body in ({'token': 'a' * 50}, {'access_token': 'b' * 50}, {'weird_key': 'c' * 50}):
        s = TrackListStore(cache_dir=store.cache_dir / str(id(body)), token_url=store.token_url)
        with patch('mello.api.tracklist.requests.post', return_value=_resp(200, body)), \
             patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)) as get:
            s.fetch(ALBUM)
        assert get.called, f'no request made for token body {body}'


# --- Neighbours: what the peek under the cover shows ---

def _seed(store, uris):
    store._lists[ALBUM] = [Track(uri=u, name=u.split(':')[-1]) for u in uris]


def test_neighbours_in_the_middle(store):
    _seed(store, ['spotify:track:a', 'spotify:track:b', 'spotify:track:c'])
    prev, nxt = store.neighbours(ALBUM, 'spotify:track:b')
    assert (prev.name, nxt.name) == ('a', 'c')


def test_no_previous_on_first_track(store):
    _seed(store, ['spotify:track:a', 'spotify:track:b'])
    prev, nxt = store.neighbours(ALBUM, 'spotify:track:a')
    assert prev is None and nxt.name == 'b'


def test_no_next_on_last_track(store):
    _seed(store, ['spotify:track:a', 'spotify:track:b'])
    prev, nxt = store.neighbours(ALBUM, 'spotify:track:b')
    assert prev.name == 'a' and nxt is None


def test_neighbours_unknown_without_a_list(store):
    assert store.neighbours(ALBUM, 'spotify:track:a') == (None, None)


def test_neighbours_unknown_for_untracked_track(store):
    """Playing something not in the cached list must not guess."""
    _seed(store, ['spotify:track:a'])
    assert store.neighbours(ALBUM, 'spotify:track:zzz') == (None, None)


def test_index_of(store):
    _seed(store, ['spotify:track:a', 'spotify:track:b'])
    assert store.index_of(ALBUM, 'spotify:track:b') == 1
    assert store.index_of(ALBUM, None) is None


# --- Cache robustness ---

def test_corrupt_cache_file_is_discarded(store):
    store.cache_dir.mkdir(parents=True, exist_ok=True)
    path = store._cache_path(ALBUM)
    path.write_text('{ truncated')
    assert store.get(ALBUM) is None
    assert not path.exists()   # removed so it can't poison every boot


def test_cache_written_atomically(store):
    """Write-then-rename: a power cut must not leave a half-written file."""
    payload = {'items': [{'uri': 'spotify:track:1', 'name': 'One', 'artists': []}], 'next': None}
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)):
        store.fetch(ALBUM)
    assert json.loads(store._cache_path(ALBUM).read_text())['uri'] == ALBUM
    assert not list(store.cache_dir.glob('*.tmp'))


def test_unsupported_context_never_fetched(store):
    assert store.wants_fetch('spotify:track:abc') is False
    assert store.fetch('spotify:track:abc') is None


# --- App-level guards around playing a picked track ---

class TestPlayTrackAtIndex:
    """Tapping a row must never play the wrong thing, or crash on a stale list."""

    def _app(self, tracks, context_uri=ALBUM):
        from types import SimpleNamespace
        import threading
        from mello.app import Mello
        from mello.models import NowPlaying

        app = Mello.__new__(Mello)
        app._now_playing_lock = threading.Lock()
        app._now_playing = NowPlaying(playing=True, stopped=False,
                                      context_uri=context_uri,
                                      track_uri=tracks[0].uri if tracks else None)
        store = TrackListStore(cache_dir=Path('/nonexistent'), token_url='x', mock_mode=True)
        store._lists[context_uri] = tracks
        app.track_lists = store
        app.playback = SimpleNamespace(play_item=MagicMock())
        app.volume = SimpleNamespace(unmute=MagicMock())
        app._user_activated_playback = False
        return app

    def test_plays_the_tapped_track(self):
        tracks = [Track(uri='spotify:track:a', name='A'), Track(uri='spotify:track:b', name='B')]
        app = self._app(tracks)
        app._play_track_at_index(1)
        app.playback.play_item.assert_called_once_with(ALBUM, skip_to_uri='spotify:track:b')

    def test_marks_playback_user_activated(self):
        """Otherwise the focus gate treats it as machine-initiated."""
        app = self._app([Track(uri='spotify:track:a', name='A')])
        app._play_track_at_index(0)
        assert app._user_activated_playback is True

    def test_out_of_range_index_is_ignored(self):
        """The list on screen can go stale while a tap is in flight."""
        app = self._app([Track(uri='spotify:track:a', name='A')])
        app._play_track_at_index(7)
        app.playback.play_item.assert_not_called()

    def test_negative_index_is_ignored(self):
        app = self._app([Track(uri='spotify:track:a', name='A')])
        app._play_track_at_index(-1)
        app.playback.play_item.assert_not_called()

    def test_no_list_means_no_play(self):
        app = self._app([])
        app._play_track_at_index(0)
        app.playback.play_item.assert_not_called()

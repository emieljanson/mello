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


def test_long_cooldown_never_parks_a_thread(store):
    """An hour-long cooldown must become a deferral, not a sleeping worker."""
    waits = []
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '3600'})), \
         patch('mello.api.tracklist.threading.Event') as ev:
        ev.return_value.wait.side_effect = lambda w: waits.append(w)
        assert store.fetch(ALBUM) is None
    assert waits == []                          # nothing slept
    assert store.cooldown_remaining() > 0       # deferred instead


def test_long_cooldown_outranks_retry_failed(store):
    """Spotify's cooldown must win over our own retry timer.

    Retrying sooner than asked adds load to the very quota we're limited on.
    """
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '600'})):
        store.fetch(ALBUM)

    assert store.wants_fetch(ALBUM) is False
    store.retry_failed()                       # clears our own failure memory
    assert store.wants_fetch(ALBUM) is False   # but the cooldown still holds
    assert 500 < store.cooldown_remaining() <= 600


def test_cooldown_is_global_not_per_album(store):
    """The limit is on the shared client ID, so it applies to every album."""
    other = 'spotify:album:1AAAAAAAAAAAAAAAAAAAAA'
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '600'})):
        store.fetch(ALBUM)
    assert store.wants_fetch(other) is False


def test_fetching_resumes_once_the_cooldown_expires(store):
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '600'})):
        store.fetch(ALBUM)
    store._blocked_until = 0.0                 # pretend it elapsed
    store.retry_failed()
    assert store.wants_fetch(ALBUM) is True


def test_absurd_retry_after_is_clamped(store):
    """A bogus header must not disable track lists for the rest of the day."""
    from mello.api.tracklist import MAX_COOLDOWN
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429, headers={'Retry-After': '99999'})):
        store.fetch(ALBUM)
    assert store.cooldown_remaining() <= MAX_COOLDOWN


def test_missing_retry_after_still_backs_off(store):
    """go-librespot's proxy strips the header; a 429 must still cause a wait."""
    from mello.api.tracklist import DEFAULT_COOLDOWN
    with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
         patch('mello.api.tracklist.requests.get', return_value=_resp(429)):
        store.fetch(ALBUM)
    assert 0 < store.cooldown_remaining() <= DEFAULT_COOLDOWN


class TestOwnCredentials:
    """Our own client ID, so we aren't sharing go-librespot's spent quota."""

    @pytest.fixture
    def keyed(self, tmp_path):
        return TrackListStore(cache_dir=tmp_path / 'tracks',
                              token_url='http://localhost:3678/token',
                              client_id='id123', client_secret='secret456')

    def test_borrowed_token_when_unconfigured(self, store):
        assert store.uses_own_credentials() is False

    def test_app_token_used_instead_of_borrowing(self, keyed):
        payload = {'items': [{'uri': 'spotify:track:1', 'name': 'A'}], 'next': None}
        token_resp = _resp(200, {'access_token': 'app-token', 'expires_in': 3600})
        with patch('mello.api.tracklist.requests.post', return_value=token_resp) as post, \
             patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)) as get:
            assert len(keyed.fetch(ALBUM)) == 1

        # The daemon must not be asked for a token at all.
        urls = [c.args[0] if c.args else c.kwargs.get('url') for c in post.call_args_list]
        assert 'https://accounts.spotify.com/api/token' in urls
        assert keyed.token_url not in urls
        assert get.call_args.kwargs['headers']['Authorization'] == 'Bearer app-token'

    def test_app_token_is_cached_between_fetches(self, keyed):
        payload = {'items': [], 'next': None}
        token_resp = _resp(200, {'access_token': 'app-token', 'expires_in': 3600})
        with patch('mello.api.tracklist.requests.post', return_value=token_resp) as post, \
             patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)):
            keyed.fetch(ALBUM)
            keyed.fetch(PLAYLIST)
        assert post.call_count == 1, 'a token request per album wastes the quota we just fixed'

    def test_short_lived_token_is_refetched(self, keyed):
        """A token expiring sooner than the safety margin must not be cached."""
        payload = {'items': [], 'next': None}
        token_resp = _resp(200, {'access_token': 'app-token', 'expires_in': 1})
        with patch('mello.api.tracklist.requests.post', return_value=token_resp) as post, \
             patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)):
            keyed.fetch(ALBUM)
            keyed._app_token_expires = 0.0   # as if the second fetch came later
            keyed.fetch(PLAYLIST)
        assert post.call_count == 2

    def test_bad_secret_falls_back_to_borrowing(self, keyed):
        """A typo'd key must not be worse than having configured none at all."""
        payload = {'items': [], 'next': None}
        responses = [_resp(400, {'error': 'invalid_client'}),   # accounts.spotify.com
                     _resp(200, {'token': 'a' * 50})]           # the daemon
        with patch('mello.api.tracklist.requests.post', side_effect=responses), \
             patch('mello.api.tracklist.requests.get', return_value=_resp(200, payload)) as get:
            keyed.fetch(ALBUM)
        assert get.call_args.kwargs['headers']['Authorization'] == 'Bearer ' + 'a' * 50

    def test_401_drops_the_cached_token(self, keyed):
        """An expired token must not be reused forever."""
        token_resp = _resp(200, {'access_token': 'app-token', 'expires_in': 3600})
        with patch('mello.api.tracklist.requests.post', return_value=token_resp), \
             patch('mello.api.tracklist.requests.get', return_value=_resp(401)):
            keyed.fetch(ALBUM)
        assert keyed._app_token is None


class TestUnavailableContext:
    """Spotify's own editorial playlists 404 for every third-party app."""

    def test_404_marks_the_context_unavailable(self, store):
        with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
             patch('mello.api.tracklist.requests.get', return_value=_resp(404)):
            assert store.fetch(PLAYLIST) is None
        assert store.is_unavailable(PLAYLIST)
        assert not store.is_unavailable(ALBUM), 'one refusal must not condemn every album'

    def test_unavailable_context_is_never_retried(self, store):
        """Not even after retry_failed() — the answer will not change."""
        with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
             patch('mello.api.tracklist.requests.get', return_value=_resp(404)):
            store.fetch(PLAYLIST)
        store.retry_failed()
        assert store.wants_fetch(PLAYLIST) is False
        assert store.wants_fetch(ALBUM) is True

    def test_404_does_not_start_a_cooldown(self, store):
        """A refused playlist must not block every other album's list."""
        with patch('mello.api.tracklist.requests.post', return_value=_resp(200, {'token': 'a' * 50})), \
             patch('mello.api.tracklist.requests.get', return_value=_resp(404)):
            store.fetch(PLAYLIST)
        assert store.cooldown_remaining() == 0


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


# --- What the list describes: the cover on screen, not the speaker ---

def _focused_app(catalog_items, selected=0, playing_uri=None, playing_track=None,
                 progress=None, seeded=None, temp_item=None):
    """Minimal Mello exercising the focus-following track list."""
    import threading
    from types import SimpleNamespace
    from mello.app import Mello
    from mello.models import NowPlaying

    app = Mello.__new__(Mello)
    app._now_playing_lock = threading.Lock()
    app._now_playing = NowPlaying(
        playing=playing_uri is not None, stopped=playing_uri is None,
        context_uri=playing_uri, track_uri=playing_track)
    app.catalog_manager = SimpleNamespace(
        items=catalog_items,
        get_progress=lambda uri: progress,
    )
    app.temp_item = temp_item
    app.selected_index = selected
    app.quiet_hours = SimpleNamespace(active=False)
    app.settings = SimpleNamespace(bedtime_uri=None)

    store = TrackListStore(cache_dir=Path('/nonexistent'), token_url='x', mock_mode=True)
    for uri, tracks in (seeded or {}).items():
        store._lists[uri] = tracks
    app.track_lists = store
    app.playback = SimpleNamespace(play_item=MagicMock())
    app.volume = SimpleNamespace(unmute=MagicMock())
    app._user_activated_playback = False
    app._resume_cache_key = ()          # get_progress is cached per focused album
    app._resume_cache_uri = None
    return app


def _item(uri, name='Album'):
    from mello.models import CatalogItem
    return CatalogItem(id=uri[-1], uri=uri, name=name, type='album')


TRACKS = [Track(uri='spotify:track:a', name='A'),
          Track(uri='spotify:track:b', name='B'),
          Track(uri='spotify:track:c', name='C')]


class TestFocusedContext:
    def test_playing_album_uses_the_live_track(self):
        app = _focused_app([_item(ALBUM)], playing_uri=ALBUM,
                           playing_track='spotify:track:b', seeded={ALBUM: TRACKS})
        assert app._focused_context() == (ALBUM, 'spotify:track:b')

    def test_browsing_uses_the_resume_point(self):
        """Not playing: the reference is whatever pressing play would start."""
        app = _focused_app([_item(ALBUM)], progress={'uri': 'spotify:track:c'},
                           seeded={ALBUM: TRACKS})
        assert app._focused_context() == (ALBUM, 'spotify:track:c')

    def test_browsing_without_progress_uses_the_first_track(self):
        app = _focused_app([_item(ALBUM)], seeded={ALBUM: TRACKS})
        assert app._focused_context() == (ALBUM, 'spotify:track:a')

    def test_other_album_playing_still_describes_the_focused_one(self):
        """Play A, browse to B: the list must be B's, not A's."""
        other = 'spotify:album:other'
        app = _focused_app([_item(ALBUM), _item(other)], selected=1,
                           playing_uri=ALBUM, playing_track='spotify:track:b',
                           seeded={ALBUM: TRACKS})
        assert app._focused_context()[0] == other

    def test_temp_item_has_no_list(self):
        """A cast-but-unsaved album isn't in the catalog to list."""
        from mello.models import CatalogItem
        temp = CatalogItem(id='temp', uri='spotify:album:new', name='New', is_temp=True)
        app = _focused_app([], temp_item=temp)
        assert app._focused_context() == (None, None)

    def test_empty_catalog(self):
        assert _focused_app([])._focused_context() == (None, None)


class TestTrackListView:
    def test_index_points_at_the_reference_track(self):
        app = _focused_app([_item(ALBUM)], progress={'uri': 'spotify:track:c'},
                           seeded={ALBUM: TRACKS})
        tracks, index = app._track_list_view()
        assert [t.name for t in tracks] == ['A', 'B', 'C']
        assert index == 2

    def test_no_cached_list_yields_nothing(self):
        app = _focused_app([_item(ALBUM)])
        assert app._track_list_view() == ([], None)


class TestPlayTrackAtIndex:
    """Tapping a row must never play the wrong album or crash on a stale list."""

    def test_plays_the_tapped_track_of_the_focused_album(self):
        other = 'spotify:album:other'
        app = _focused_app([_item(ALBUM), _item(other)], selected=1,
                           playing_uri=ALBUM, playing_track='spotify:track:a',
                           seeded={other: TRACKS})
        app._play_track_at_index(1)
        # The focused album, not the one currently on the speaker
        app.playback.play_item.assert_called_once_with(other, skip_to_uri='spotify:track:b')

    def test_marks_playback_user_activated(self):
        """Otherwise the focus gate would treat it as machine-initiated."""
        app = _focused_app([_item(ALBUM)], seeded={ALBUM: TRACKS})
        app._play_track_at_index(0)
        assert app._user_activated_playback is True

    def test_out_of_range_index_is_ignored(self):
        """The list on screen can go stale while a tap is in flight."""
        app = _focused_app([_item(ALBUM)], seeded={ALBUM: TRACKS})
        app._play_track_at_index(7)
        app.playback.play_item.assert_not_called()

    def test_negative_index_is_ignored(self):
        app = _focused_app([_item(ALBUM)], seeded={ALBUM: TRACKS})
        app._play_track_at_index(-1)
        app.playback.play_item.assert_not_called()

    def test_no_list_means_no_play(self):
        app = _focused_app([_item(ALBUM)])
        app._play_track_at_index(0)
        app.playback.play_item.assert_not_called()


class TestFetchDwell:
    """Swiping past covers must not fire a request per cover."""

    def _app(self, tmp_path, settled=True):
        from types import SimpleNamespace
        app = _focused_app([_item(ALBUM)])
        # A real (non-mock) store: mock_mode disables fetching by design.
        app.track_lists = TrackListStore(cache_dir=tmp_path / 'tracks',
                                         token_url='http://localhost:3678/token')
        app.carousel = SimpleNamespace(settled=settled)
        app.touch = SimpleNamespace(dragging=False)
        app._track_focus_uri = None
        app._track_focus_since = 0.0
        app._track_retry_at = 0.0
        app._track_gate_log_at = 0.0
        return app

    def test_first_sight_only_starts_the_dwell_timer(self, tmp_path):
        app = self._app(tmp_path)
        with patch('mello.app.run_async') as run:
            app._maybe_fetch_track_list()
        run.assert_not_called()
        assert app._track_focus_uri == ALBUM

    def test_fetches_after_dwelling(self, tmp_path):
        app = self._app(tmp_path)
        with patch('mello.app.run_async') as run:
            app._maybe_fetch_track_list()          # starts the timer
            app._track_focus_since -= 5            # pretend the dwell elapsed
            app._maybe_fetch_track_list()
        assert run.called

    def test_no_fetch_while_the_carousel_moves(self, tmp_path):
        app = self._app(tmp_path, settled=False)
        with patch('mello.app.run_async') as run:
            app._maybe_fetch_track_list()
            app._track_focus_since -= 5
            app._maybe_fetch_track_list()
        run.assert_not_called()

    def test_no_fetch_when_already_cached(self, tmp_path):
        app = self._app(tmp_path)
        app.track_lists._lists[ALBUM] = TRACKS
        with patch('mello.app.run_async') as run:
            app._maybe_fetch_track_list()
            app._track_focus_since -= 5
            app._maybe_fetch_track_list()
        run.assert_not_called()


# --- The button must be reachable even when the list isn't loaded ---

class TestListableWithoutAList:
    """Hiding the button when the fetch hadn't landed left no way to find out why."""

    def test_saved_album_is_listable_before_any_fetch(self):
        app = _focused_app([_item(ALBUM)])
        context_uri, _ = app._focused_context()
        assert app._track_list_view() == ([], None)      # nothing cached yet
        assert parse_context(context_uri) is not None    # but the button still shows

    def test_listable_once_cached_too(self):
        app = _focused_app([_item(ALBUM)], seeded={ALBUM: TRACKS})
        context_uri, _ = app._focused_context()
        assert parse_context(context_uri) is not None

    def test_nothing_focused_is_not_listable(self):
        app = _focused_app([])
        context_uri, _ = app._focused_context()
        assert parse_context(context_uri) is None

    def test_unsupported_context_is_not_listable(self):
        """A URI Spotify has no track endpoint for must not offer a button."""
        app = _focused_app([_item('spotify:artist:xyz')])
        context_uri, _ = app._focused_context()
        assert parse_context(context_uri) is None


# --- The list button must open the list, never start playback ---

class TestTrackListButtonHitTest:
    """A missed hit test falls through to the carousel, which plays on a tap."""

    def _app(self, listable_uri=ALBUM, delete_mode=None, temp=None, renderer_rect=None):
        from types import SimpleNamespace
        items = [_item(listable_uri)] if listable_uri else []
        app = _focused_app(items, temp_item=temp)
        app.delete_mode_id = delete_mode
        app.renderer = SimpleNamespace(
            add_button_rect=None, delete_button_rect=None,
            settings_button_rect=None, track_list_button_rect=renderer_rect,
            invalidate=MagicMock())
        app.setup_menu = SimpleNamespace(show_track_list=MagicMock(), open=MagicMock())
        app._save_temp_item = MagicMock()
        app._delete_current_item = MagicMock()
        return app

    def _centre_of_fallback(self, app):
        x, y, w, h = app._track_list_fallback_rect()
        return (x + w // 2, y + h // 2)

    def test_opens_the_list_when_the_renderer_rect_is_missing(self):
        """The exact reported bug: rect None at tap time, so play started."""
        app = self._app(renderer_rect=None)
        assert app._check_button_click(self._centre_of_fallback(app)) is True
        app.setup_menu.show_track_list.assert_called_once()

    def test_opens_the_list_from_the_renderer_rect(self):
        app = self._app(renderer_rect=(0, 0, 50, 50))
        assert app._check_button_click((10, 10)) is True
        app.setup_menu.show_track_list.assert_called_once()

    def test_a_tap_elsewhere_still_falls_through_to_the_carousel(self):
        """Returning True everywhere would break play-on-tap entirely."""
        app = self._app()
        assert app._check_button_click((300, 640)) is False
        app.setup_menu.show_track_list.assert_not_called()

    def test_no_button_means_no_fallback_hit(self):
        """Nothing focused: that corner must not open an empty list."""
        app = self._app(listable_uri=None)
        assert app._check_button_click(self._centre_of_fallback(app)) is False

    def test_delete_mode_suppresses_the_button(self):
        app = self._app(delete_mode='1')
        assert app._track_list_button_active() is False

    def test_unsupported_uri_suppresses_the_button(self):
        app = self._app(listable_uri='spotify:artist:xyz')
        assert app._check_button_click(self._centre_of_fallback(app)) is False

    def test_fallback_matches_the_renderer_geometry(self):
        """If these drift apart the fallback silently covers the wrong corner."""
        from mello.config import CAROUSEL_X, CAROUSEL_CENTER_Y, COVER_SIZE
        app = self._app()
        x, y, w, h = app._track_list_fallback_rect()
        # far side on physical x, near side on physical y — the top corner
        assert x + w // 2 > CAROUSEL_X + COVER_SIZE // 2
        assert y + h // 2 < CAROUSEL_CENTER_Y

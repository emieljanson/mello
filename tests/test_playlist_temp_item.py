"""
The playlist temp-item tile shows the real playlist name/cover, not a guess.

go-librespot's /status only ever reports the currently PLAYING TRACK's album
and cover, never the playlist's own — so before this, the temp tile froze on
whichever track happened to play first. Fixed by fetching playlist metadata
(name, cover) separately, since that endpoint carries no scope requirement and
works even when the track list itself is refused.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.app import Mello
from mello.models import CatalogItem, NowPlaying

PLAYLIST = 'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M'
ALBUM = 'spotify:album:0ETFjACtuP2ADo6LFhL6HN'


def _app(context_uri, track_album='Some Album', track_cover='https://x/cover.jpg'):
    app = Mello.__new__(Mello)
    app._now_playing_lock = threading.Lock()
    app._now_playing = NowPlaying(
        playing=True, stopped=False, context_uri=context_uri,
        track_album=track_album, track_artist='Artist', track_cover=track_cover)
    app.temp_item = None
    app._temp_item_lock = threading.Lock()
    app.catalog_manager = SimpleNamespace(
        items=[],
        get_collected_covers=lambda uri: None,
        download_temp_image=lambda url: f'/images/temp_{hash(url) & 0xff:x}.png',
    )
    app.track_lists = SimpleNamespace(
        known_show_for=lambda uri: None,
        playlist_info=lambda uri: None,
    )
    app._update_carousel_max_index = lambda: None
    app.renderer = SimpleNamespace(invalidate=lambda: None)
    return app


class TestFetchIsDispatchedOncePerPlaylist:

    def test_dispatched_for_a_new_playlist(self):
        app = _app(PLAYLIST)
        with patch('mello.app.run_async') as run:
            app._update_temp_item()
        assert any(call.args[0] == app._fetch_playlist_info_async for call in run.call_args_list)

    def test_not_dispatched_for_an_album(self):
        """Albums already get their real name/cover from now_playing — no gap to fill."""
        app = _app(ALBUM)
        with patch('mello.app.run_async') as run:
            app._update_temp_item()
        assert not any(call.args[0] == app._fetch_playlist_info_async for call in run.call_args_list)

    def test_not_redispatched_on_the_next_frame_for_the_same_playlist(self):
        app = _app(PLAYLIST)
        with patch('mello.app.run_async') as run:
            app._update_temp_item()   # uri just changed: dispatches
            run.reset_mock()
            app._update_temp_item()   # same uri, nothing new: must not dispatch again
        assert not any(call.args[0] == app._fetch_playlist_info_async for call in run.call_args_list)

    def test_redispatched_after_switching_to_a_different_playlist(self):
        other = 'spotify:playlist:1AAAAAAAAAAAAAAAAAAAAA'
        app = _app(PLAYLIST)
        with patch('mello.app.run_async'):
            app._update_temp_item()
        with app._now_playing_lock:
            app._now_playing = NowPlaying(
                playing=True, stopped=False, context_uri=other,
                track_album='Other', track_artist='Artist', track_cover='https://x/y.jpg')
        with patch('mello.app.run_async') as run:
            app._update_temp_item()
        assert any(call.args[0] == app._fetch_playlist_info_async for call in run.call_args_list)


class TestInitialGuessWhileWaiting:
    """Instant feedback matters: don't leave a blank tile during the fetch."""

    def test_uses_the_current_track_as_a_placeholder(self):
        app = _app(PLAYLIST, track_album='Placeholder Album')
        with patch('mello.app.run_async'):
            app._update_temp_item()
        assert app.temp_item.name == 'Placeholder Album'
        assert app.temp_item.image == 'https://x/cover.jpg'


class TestFetchPlaylistInfoAsync:

    def test_replaces_name_and_cover_once_resolved(self):
        app = _app(PLAYLIST, track_album='Wrong Name')
        with patch('mello.app.run_async'):
            app._update_temp_item()   # seeds temp_item with the placeholder guess

        app.track_lists.playlist_info = lambda uri: {'name': 'Gims', 'image': 'https://mosaic/x'}
        app._fetch_playlist_info_async(PLAYLIST)

        assert app.temp_item.name == 'Gims'
        assert app.temp_item.image.startswith('/images/')

    def test_a_context_switch_mid_fetch_is_not_applied(self):
        """The fetch is async; by the time it resolves the user may have moved on."""
        app = _app(PLAYLIST)
        with patch('mello.app.run_async'):
            app._update_temp_item()

        app.temp_item = CatalogItem(id='temp', uri=ALBUM, name='Different item now',
                                    type='album', artist='A', image=None, is_temp=True)
        app.track_lists.playlist_info = lambda uri: {'name': 'Gims', 'image': 'https://mosaic/x'}
        app._fetch_playlist_info_async(PLAYLIST)

        assert app.temp_item.name == 'Different item now'

    def test_no_info_available_leaves_the_guess_alone(self):
        app = _app(PLAYLIST, track_album='Placeholder Album')
        with patch('mello.app.run_async'):
            app._update_temp_item()

        app.track_lists.playlist_info = lambda uri: None
        app._fetch_playlist_info_async(PLAYLIST)

        assert app.temp_item.name == 'Placeholder Album'

    def test_an_exception_does_not_propagate(self):
        """A background thread's own exception must not crash the app."""
        app = _app(PLAYLIST)
        with patch('mello.app.run_async'):
            app._update_temp_item()

        def boom(uri):
            raise RuntimeError('network is down')
        app.track_lists.playlist_info = boom

        app._fetch_playlist_info_async(PLAYLIST)   # must not raise


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))

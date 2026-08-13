"""
Tests for title selection logic in Renderer.
"""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip('pygame')

from mello.models import CatalogItem, NowPlaying
from mello.ui.renderer import Renderer


def _item(uri='spotify:album:test', name='Album'):
    return CatalogItem(id='1', uri=uri, name=name, type='album')


def test_track_key_visible_while_paused_on_focused_context():
    item = _item()
    now = NowPlaying(
        playing=False,
        paused=True,
        context_uri='spotify:album:test',
        track_name='Chapter 2',
        track_artist='Author',
    )

    key = Renderer._get_track_key(
        item=item,
        now_playing=now,
        is_loading=False,
        pending_focus_uri=None,
        requested_focus_uri=None,
        play_in_progress=False,
    )
    assert key == ('Chapter 2', 'Author')


def test_track_key_hidden_when_context_mismatch():
    item = _item(uri='spotify:album:focused')
    now = NowPlaying(
        playing=True,
        context_uri='spotify:album:other',
        track_name='Wrong Track',
    )

    key = Renderer._get_track_key(
        item=item,
        now_playing=now,
        is_loading=True,
        pending_focus_uri='spotify:album:focused',
        requested_focus_uri='spotify:album:focused',
        play_in_progress=True,
    )
    assert key is None


def test_live_cover_used_only_for_focused_playing_context():
    item = _item(uri='spotify:playlist:focused')
    now = NowPlaying(playing=True, context_uri=item.uri)

    path = Renderer._cover_path_for_item(
        item, now, '/images/playlist.png', '/images/live.png',
        is_center=True, item_index=1, selected_index=1,
    )

    assert path == '/images/live.png'


def test_non_current_carousel_item_keeps_catalog_image():
    item = _item(uri='spotify:album:not-playing')
    now = NowPlaying(playing=True, context_uri='spotify:playlist:playing')

    path = Renderer._cover_path_for_item(
        item, now, '/images/catalog.png', '/images/live.png',
        is_center=False, item_index=0, selected_index=1,
    )

    assert path == '/images/catalog.png'

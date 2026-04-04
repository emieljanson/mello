"""
Tests for title selection logic in Renderer.
"""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip('pygame')

from berry.models import CatalogItem, NowPlaying
from berry.ui.renderer import Renderer


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

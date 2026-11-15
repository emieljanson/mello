"""
What the cover shows while an album is still loading: the saved track's
progress, so the child sees where they were right away.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.catalog import CatalogManager
from mello.models import CatalogItem, NowPlaying
from mello.ui.renderer import Renderer

ALBUM = 'spotify:album:a1'


def item(saved=None):
    return CatalogItem(id='1', uri=ALBUM, name='Audiobook', current_track=saved)


def test_saved_place_is_shown_while_the_album_is_still_loading():
    saved = {'uri': 'spotify:track:c12', 'name': 'Chapter 12', 'position': 90000, 'duration': 180000}
    loading = NowPlaying(playing=False, stopped=False, context_uri=ALBUM)      # no track, no position yet

    assert Renderer._cover_progress(item(saved), loading) == 0.5
    assert Renderer._get_track_key(item(saved), loading, True, None, None, True) == ('Chapter 12', '')


def test_live_position_wins_once_spotify_reports_it():
    saved = {'uri': 'spotify:track:c12', 'name': 'Chapter 12', 'position': 90000, 'duration': 180000}
    playing = NowPlaying(playing=True, stopped=False, context_uri=ALBUM, position=120000, duration=180000)

    assert round(Renderer._cover_progress(item(saved), playing), 3) == 0.667


def test_no_bar_without_a_saved_length_or_when_another_album_plays():
    old_entry = {'uri': 'spotify:track:c12', 'name': 'Chapter 12', 'position': 90000}   # saved before lengths were kept
    other = NowPlaying(playing=True, stopped=False, context_uri='spotify:album:other', position=5000, duration=10000)

    assert Renderer._cover_progress(item(old_entry), NowPlaying()) == 0.0
    assert Renderer._cover_progress(item(None), other) == 0.0


def test_track_length_is_saved_with_the_place(tmp_path):
    catalog = CatalogManager(
        tmp_path / 'catalog.json', tmp_path / 'images',
        progress_path=tmp_path / 'progress.json', get_progress_expiry=lambda: 96,
    )

    catalog.save_progress(ALBUM, 'spotify:track:c12', 90000, 'Chapter 12', 'Author', 180000)
    catalog.save_progress('spotify:album:a2', 'spotify:track:x', 1000, 'X', 'Y')            # length unknown

    saved = json.loads((tmp_path / 'progress.json').read_text())
    assert saved[ALBUM]['duration'] == 180000
    assert 'duration' not in saved['spotify:album:a2']

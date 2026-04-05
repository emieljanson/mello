"""
Tests for data models - LibrespotStatus, CatalogItem, etc.
"""
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.models import LibrespotStatus, CatalogItem


class TestLibrespotStatus:
    """Tests for LibrespotStatus.from_dict parsing."""

    def test_parses_playing_state(self):
        raw = {
            'stopped': False,
            'paused': False,
            'volume': 100,
            'track': {
                'name': 'Come Together',
                'artist_names': ['The Beatles'],
                'album_name': 'Abbey Road',
                'album_cover_url': 'https://example.com/cover.jpg',
                'uri': 'spotify:track:123',
                'position': 30000,
                'duration': 260000,
            }
        }
        s = LibrespotStatus.from_dict(raw, context_uri='spotify:album:abbey')
        assert s.playing is True
        assert s.paused is False
        assert s.stopped is False
        assert s.volume == 100
        assert s.track_name == 'Come Together'
        assert s.track_artist == 'The Beatles'
        assert s.track_album == 'Abbey Road'
        assert s.track_uri == 'spotify:track:123'
        assert s.position == 30000
        assert s.duration == 260000
        assert s.context_uri == 'spotify:album:abbey'

    def test_parses_paused_state(self):
        raw = {'stopped': False, 'paused': True, 'track': {}}
        s = LibrespotStatus.from_dict(raw)
        assert s.playing is False
        assert s.paused is True

    def test_parses_stopped_state(self):
        raw = {'stopped': True, 'paused': False, 'track': {}}
        s = LibrespotStatus.from_dict(raw)
        assert s.playing is False
        assert s.stopped is True

    def test_handles_missing_track(self):
        raw = {'stopped': True}
        s = LibrespotStatus.from_dict(raw)
        assert s.track_name is None
        assert s.position == 0

    def test_handles_none_track(self):
        raw = {'stopped': True, 'track': None}
        s = LibrespotStatus.from_dict(raw)
        assert s.track_name is None

    def test_multiple_artists_joined(self):
        raw = {'stopped': False, 'paused': False, 'track': {'artist_names': ['A', 'B', 'C']}}
        s = LibrespotStatus.from_dict(raw)
        assert s.track_artist == 'A, B, C'

    def test_no_artists(self):
        raw = {'stopped': False, 'paused': False, 'track': {}}
        s = LibrespotStatus.from_dict(raw)
        assert s.track_artist is None

    def test_defaults(self):
        s = LibrespotStatus()
        assert s.playing is False
        assert s.stopped is True
        assert s.volume is None


class TestCatalogItemIsTemp:
    """Tests for CatalogItem.is_temp property."""

    def test_regular_item_not_temp(self):
        item = CatalogItem(id='1', uri='spotify:album:x', name='X', type='album')
        assert item.is_temp is False

    def test_item_marked_as_temp(self):
        item = CatalogItem(id='1', uri='spotify:album:x', name='X', type='album', is_temp=True)
        assert item.is_temp is True

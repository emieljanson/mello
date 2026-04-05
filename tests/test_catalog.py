"""
Tests for CatalogManager - save/load, atomic writes, deduplication.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.catalog import CatalogManager


class TestCatalogLoadSave:
    """Tests for catalog load/save operations."""

    def test_load_empty_catalog(self, catalog_path, images_path):
        """Loading non-existent catalog returns empty list."""
        manager = CatalogManager(catalog_path, images_path)
        items = manager.load()
        assert items == []

    def test_load_existing_catalog(self, catalog_with_file, images_path, sample_catalog_data):
        """Loading existing catalog returns all items."""
        manager = CatalogManager(catalog_with_file, images_path)
        items = manager.load()
        assert len(items) == 2
        assert items[0].name == 'Test Album 1'
        assert items[1].name == 'Test Playlist'

    def test_save_and_reload(self, catalog_path, images_path):
        """Saving and reloading preserves item data."""
        manager = CatalogManager(catalog_path, images_path)
        manager.load()

        # Save a new item
        item_data = {
            'type': 'album',
            'uri': 'spotify:album:new',
            'name': 'New Album',
            'artist': 'New Artist',
            'image': None,
        }
        success = manager.save_item(item_data)
        assert success

        # Reload and verify
        manager2 = CatalogManager(catalog_path, images_path)
        items = manager2.load()
        assert len(items) == 1
        assert items[0].name == 'New Album'
        assert items[0].uri == 'spotify:album:new'

    def test_delete_item(self, catalog_with_file, images_path):
        """Deleting item removes it from catalog."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()
        assert len(manager.items) == 2

        # Delete first item
        success = manager.delete_item('1')
        assert success

        # Reload and verify
        manager2 = CatalogManager(catalog_with_file, images_path)
        items = manager2.load()
        assert len(items) == 1
        assert items[0].id == '2'

    def test_duplicate_uri_rejected(self, catalog_with_file, images_path):
        """Saving duplicate URI is rejected."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        item_data = {
            'type': 'album',
            'uri': 'spotify:album:test1',  # Already exists
            'name': 'Duplicate Album',
            'artist': 'Artist',
            'image': None,
        }
        success = manager.save_item(item_data)
        assert not success


class TestAtomicWrites:
    """Tests for atomic file write functionality."""

    def test_atomic_write_creates_file(self, catalog_path, images_path):
        """Atomic write creates catalog file correctly."""
        manager = CatalogManager(catalog_path, images_path)
        manager.load()

        item_data = {
            'type': 'album',
            'uri': 'spotify:album:atomic',
            'name': 'Atomic Album',
            'artist': 'Artist',
            'image': None,
        }
        manager.save_item(item_data)

        # Verify file exists and is valid JSON
        assert catalog_path.exists()
        data = json.loads(catalog_path.read_text())
        assert 'items' in data
        assert len(data['items']) == 1

    def test_no_temp_file_after_save(self, catalog_path, images_path):
        """Temp file is cleaned up after successful save."""
        manager = CatalogManager(catalog_path, images_path)
        manager.load()

        item_data = {
            'type': 'album',
            'uri': 'spotify:album:test',
            'name': 'Test',
            'artist': 'Artist',
            'image': None,
        }
        manager.save_item(item_data)

        # Verify no temp file left behind
        temp_path = catalog_path.with_suffix('.json.tmp')
        assert not temp_path.exists()

    def test_recovery_from_temp_file(self, catalog_path, images_path):
        """Recovery from leftover temp file on startup."""
        # Create a temp file simulating crashed save
        temp_path = catalog_path.with_suffix('.json.tmp')
        temp_data = {
            'items': [
                {'id': 'recovered', 'uri': 'spotify:album:recovered',
                 'name': 'Recovered Album', 'type': 'album'}
            ]
        }
        temp_path.write_text(json.dumps(temp_data))

        # Load should recover from temp file
        manager = CatalogManager(catalog_path, images_path)
        items = manager.load()

        # Should have recovered the item
        assert len(items) == 1
        assert items[0].name == 'Recovered Album'

        # Temp file should be gone
        assert not temp_path.exists()
        # Main file should exist
        assert catalog_path.exists()


class TestProgressTracking:
    """Tests for playback progress tracking."""

    def test_save_and_get_progress(self, catalog_with_file, images_path):
        """Progress is saved and retrieved correctly."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        # Save progress
        manager.save_progress(
            context_uri='spotify:album:test1',
            track_uri='spotify:track:123',
            position=60000,
            track_name='Test Track',
            artist='Test Artist'
        )

        # Get progress
        progress = manager.get_progress('spotify:album:test1')
        assert progress is not None
        assert progress['uri'] == 'spotify:track:123'
        assert progress['position'] == 60000
        assert progress['name'] == 'Test Track'

    def test_clear_progress(self, catalog_with_file, images_path):
        """Progress can be cleared."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        # Save and then clear
        manager.save_progress('spotify:album:test1', 'spotify:track:123', 60000)
        manager.clear_progress('spotify:album:test1')

        progress = manager.get_progress('spotify:album:test1')
        assert progress is None

    def test_clear_progress_clears_in_memory_current_track(self, catalog_with_file, images_path):
        """Clearing progress also resets the in-memory track metadata."""
        manager = CatalogManager(catalog_with_file, images_path)
        items = manager.load()
        item = next(i for i in items if i.uri == 'spotify:album:test1')
        assert item.current_track is None

        manager.save_progress('spotify:album:test1', 'spotify:track:123', 60000, 'Track', 'Artist')
        assert item.current_track is not None

        manager.clear_progress('spotify:album:test1')
        assert item.current_track is None

    def test_progress_for_unknown_context(self, catalog_with_file, images_path):
        """Getting progress for unknown context returns None."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        progress = manager.get_progress('spotify:album:unknown')
        assert progress is None

    def test_clear_all_progress(self, catalog_with_file, images_path):
        """clear_all_progress deletes the progress file entirely."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        manager.save_progress('spotify:album:test1', 'spotify:track:1', 1000)
        manager.save_progress('spotify:album:test2', 'spotify:track:2', 2000)
        assert manager.progress_path.exists()

        manager.clear_all_progress()
        assert not manager.progress_path.exists()
        assert manager.get_progress('spotify:album:test1') is None
        assert manager.get_progress('spotify:album:test2') is None

    def test_progress_stored_in_separate_file(self, catalog_with_file, images_path):
        """Progress is stored in progress.json, not catalog.json."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        manager.save_progress('spotify:album:test1', 'spotify:track:1', 5000, 'Song')

        catalog_data = json.loads(catalog_with_file.read_text())
        for item in catalog_data['items']:
            assert 'currentTrack' not in item

        progress_data = json.loads(manager.progress_path.read_text())
        assert 'spotify:album:test1' in progress_data
        assert progress_data['spotify:album:test1']['position'] == 5000

    def test_progress_populates_current_track_on_load(self, catalog_with_file, images_path):
        """Loading catalog populates current_track from progress.json."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        manager.save_progress('spotify:album:test1', 'spotify:track:1', 5000, 'Song', 'Artist')

        manager2 = CatalogManager(catalog_with_file, images_path)
        items = manager2.load()
        item = next(i for i in items if i.uri == 'spotify:album:test1')
        assert item.current_track is not None
        assert item.current_track['name'] == 'Song'

    def test_same_track_position_regression_is_rejected(self, catalog_with_file, images_path):
        """Older/stale lower position should not overwrite same-track progress."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        manager.save_progress('spotify:album:test1', 'spotify:track:1', 90000, 'Song', 'Artist')
        manager.save_progress('spotify:album:test1', 'spotify:track:1', 0, 'Song', 'Artist')

        progress = manager.get_progress('spotify:album:test1')
        assert progress is not None
        assert progress['position'] == 90000

    def test_track_change_allows_low_position(self, catalog_with_file, images_path):
        """When track URI changes, lower position is valid and should be persisted."""
        manager = CatalogManager(catalog_with_file, images_path)
        manager.load()

        manager.save_progress('spotify:album:test1', 'spotify:track:old', 90000, 'Old', 'Artist')
        manager.save_progress('spotify:album:test1', 'spotify:track:new', 0, 'New', 'Artist')

        progress = manager.get_progress('spotify:album:test1')
        assert progress is not None
        assert progress['uri'] == 'spotify:track:new'
        assert progress['position'] == 0


class TestMockMode:
    """Tests for mock mode behavior."""

    def test_mock_mode_returns_mock_data(self, catalog_path, images_path):
        """Mock mode returns predefined test data."""
        manager = CatalogManager(catalog_path, images_path, mock_mode=True)
        items = manager.load()

        assert len(items) > 0
        assert items[0].name == 'Abbey Road'

    def test_mock_mode_save_returns_true(self, catalog_path, images_path):
        """Save in mock mode always succeeds but does nothing."""
        manager = CatalogManager(catalog_path, images_path, mock_mode=True)
        manager.load()

        result = manager.save_item({'uri': 'test', 'name': 'Test'})
        assert result is True

        # File should not be created
        assert not catalog_path.exists()


class TestCoverUrlGuards:
    """Tests for defensive URL handling in image/cover helpers."""

    def test_download_temp_image_rejects_non_http_urls(self, catalog_path, images_path):
        manager = CatalogManager(catalog_path, images_path)
        assert manager.download_temp_image('file:///etc/passwd') is None
        assert manager.download_temp_image('ftp://example.com/a.png') is None
        assert manager.download_temp_image('') is None

    def test_collect_cover_for_playlist_rejects_empty_inputs(self, catalog_path, images_path):
        manager = CatalogManager(catalog_path, images_path)
        assert manager.collect_cover_for_playlist('', 'https://example.com/a.png') is False
        assert manager.collect_cover_for_playlist('spotify:playlist:test', '') is False
        assert manager.collect_cover_for_playlist('spotify:album:test', 'https://example.com/a.png') is False

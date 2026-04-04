"""
Pytest configuration and shared fixtures for Berry tests.
"""
import json
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that's cleaned up after each test."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def catalog_path(temp_dir):
    """Provide path for a temporary catalog.json file."""
    return temp_dir / 'catalog.json'


@pytest.fixture
def images_path(temp_dir):
    """Provide path for a temporary images directory."""
    images_dir = temp_dir / 'images'
    images_dir.mkdir()
    return images_dir


@pytest.fixture
def sample_catalog_data():
    """Provide sample catalog data for testing."""
    return {
        'items': [
            {
                'id': '1',
                'uri': 'spotify:album:test1',
                'name': 'Test Album 1',
                'type': 'album',
                'artist': 'Test Artist 1',
                'image': '/images/abc12345.png',
            },
            {
                'id': '2',
                'uri': 'spotify:playlist:test2',
                'name': 'Test Playlist',
                'type': 'playlist',
                'artist': None,
                'image': '/images/def67890.png',
            },
        ]
    }


@pytest.fixture
def catalog_with_file(catalog_path, images_path, sample_catalog_data):
    """Create a catalog file with sample data."""
    catalog_path.write_text(json.dumps(sample_catalog_data, indent=2))
    return catalog_path

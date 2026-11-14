"""
The saved place per album (progress.json) must survive concurrent writers
and a damaged file.
"""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.catalog import CatalogManager


def manager(tmp_path):
    return CatalogManager(
        tmp_path / 'catalog.json', tmp_path / 'images',
        progress_path=tmp_path / 'progress.json',
        get_progress_expiry=lambda: 96,
    )


def test_simultaneous_saves_for_different_albums_all_survive(tmp_path):
    catalog = manager(tmp_path)
    albums = [f'spotify:album:{n}' for n in range(12)]
    start = threading.Barrier(len(albums))

    def save(uri):
        start.wait()
        for position in range(0, 20000, 1000):
            catalog.save_progress(uri, 'spotify:track:t', position, 'Track', 'Artist')

    threads = [threading.Thread(target=save, args=(uri,)) for uri in albums]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = json.loads((tmp_path / 'progress.json').read_text())
    assert sorted(saved) == sorted(albums)
    assert all(entry['position'] == 19000 for entry in saved.values())


def test_corrupt_progress_file_is_kept_aside_not_silently_replaced(tmp_path):
    catalog = manager(tmp_path)
    (tmp_path / 'progress.json').write_text('{"spotify:album:a": {"uri": "spotify:tr')

    catalog.save_progress('spotify:album:b', 'spotify:track:t', 5000, 'Track', 'Artist')

    assert 'spotify:album:b' in json.loads((tmp_path / 'progress.json').read_text())
    assert (tmp_path / 'progress.json.corrupt').exists()

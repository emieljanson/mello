"""Saved items never keep pointing at a temporary cover.

Seen on the daily speaker (2026-10-30): a single was saved with the cover
its temp item used (temp_2cebd1bf); seconds later its album, same cover, was
saved and renamed temp_2cebd1bf to 2cebd1bf. The single lost its cover for
good: a grey square, and playing it made a duplicate temp item.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.catalog import CatalogManager

VARIANTS = ['', '_small', '_dim', '_small_dim']


def _covers(images, base):
    for suffix in VARIANTS:
        (images / f'{base}{suffix}.png').write_bytes(b'png')


def _catalog(path, items):
    path.write_text(json.dumps({'items': items}))


def test_saving_an_item_with_a_cached_temp_cover_makes_it_permanent(tmp_path):
    images = tmp_path / 'images'; images.mkdir()
    _covers(images, 'temp_2cebd1bf')
    catalog = tmp_path / 'catalog.json'
    _catalog(catalog, [])
    manager = CatalogManager(catalog, images)
    manager.load()

    # The cover came back from the hash cache as the temp path.
    manager.save_item({'uri': 'spotify:track:single', 'name': 'Single', 'type': 'album',
                       'image': '/images/temp_2cebd1bf.png'})
    manager.save_item({'uri': 'spotify:album:album', 'name': 'Album', 'type': 'album',
                       'image': '/images/temp_2cebd1bf.png'})

    saved = {i['uri']: i['image'] for i in json.loads(catalog.read_text())['items']}
    assert saved == {'spotify:track:single': '/images/2cebd1bf.png',
                     'spotify:album:album': '/images/2cebd1bf.png'}
    assert all((images / f'2cebd1bf{s}.png').exists() for s in VARIANTS)


def test_a_lost_temp_cover_is_repaired_at_start(tmp_path):
    images = tmp_path / 'images'; images.mkdir()
    _covers(images, '2cebd1bf')            # renamed by the album's save
    _covers(images, 'temp_aeb8cfeb')       # still temp, still there
    catalog = tmp_path / 'catalog.json'
    _catalog(catalog, [
        {'id': '1', 'uri': 'spotify:track:single', 'name': 'Single', 'type': 'album',
         'image': '/images/temp_2cebd1bf.png'},
        {'id': '2', 'uri': 'spotify:album:a', 'name': 'Wiebel', 'type': 'album',
         'image': '/images/temp_aeb8cfeb.png'},
        {'id': '3', 'uri': 'spotify:album:gone', 'name': 'Gone', 'type': 'album',
         'image': '/images/temp_deadbeef.png'},
    ])

    items = {i.uri: i.image for i in CatalogManager(catalog, images).load()}

    assert items['spotify:track:single'] == '/images/2cebd1bf.png'
    assert items['spotify:album:a'] == '/images/aeb8cfeb.png'
    assert items['spotify:album:gone'] is None      # nothing to repair it with
    assert (images / 'aeb8cfeb_small_dim.png').exists()
    assert not (images / 'temp_aeb8cfeb.png').exists()
    saved = {i['uri']: i['image'] for i in json.loads(catalog.read_text())['items']}
    assert saved['spotify:track:single'] == '/images/2cebd1bf.png'

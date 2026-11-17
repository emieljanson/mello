"""After a restart the carousel opens on the album that played last."""
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.app import Mello
from mello.managers.carousel import SmoothCarousel
from mello.models import CatalogItem


def _item(n, played_at=None):
    item = CatalogItem(id=str(n), uri=f'spotify:album:{n}', name=f'Album {n}', type='album')
    if played_at:
        item.current_track = {'name': 'x', 'updatedAt': played_at}
    return item


def _app(items):
    app = Mello.__new__(Mello)
    app.catalog_manager = SimpleNamespace(items=items)
    app.temp_item = None
    app.selected_index = 0
    app.carousel = SmoothCarousel()
    app.carousel.max_index = len(items) - 1
    return app


def test_opens_on_most_recently_played_album():
    app = _app([
        _item(0, '2026-11-17T08:00:00'),
        _item(1),
        _item(2, '2026-11-17T09:21:34'),
        _item(3, '2026-11-16T19:00:00'),
    ])
    app._focus_last_played()
    assert app.selected_index == 2
    assert app.carousel.scroll_x == 2.0
    assert app.carousel.target_index == 2
    assert app.carousel.settled  # no slide animation at start, and nothing starts playing


def test_stays_on_first_album_without_history():
    app = _app([_item(0), _item(1)])
    app._focus_last_played()
    assert app.selected_index == 0
    assert app.carousel.scroll_x == 0.0


def test_empty_library():
    app = _app([])
    app._focus_last_played()
    assert app.selected_index == 0

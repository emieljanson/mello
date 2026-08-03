"""
Tests for delete-mode tap routing.
"""
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.app import Mello
from mello.models import CatalogItem


def _item(item_id='1') -> CatalogItem:
    return CatalogItem(
        id=item_id,
        uri='spotify:album:test',
        name='Test Album',
        type='album',
    )


def _make_app(rect=(10, 10, 50, 50)) -> Mello:
    item = _item()
    app = Mello.__new__(Mello)
    app.catalog_manager = SimpleNamespace(items=[item])
    app.temp_item = None
    app.quiet_hours = SimpleNamespace(active=False)
    app.settings = SimpleNamespace(bedtime_uri=None)
    app.selected_index = 0
    app.delete_mode_id = item.id
    app._delete_button_rect = rect
    app._deleting = False
    app.setup_menu = SimpleNamespace(is_open=False)
    app.renderer = SimpleNamespace(
        delete_button_rect=rect,
        add_button_rect=None,
        settings_button_rect=None,
        invalidate=MagicMock(),
    )
    app.touch = SimpleNamespace(on_down=MagicMock())
    app.user_interacting = False
    app._user_activated_playback = False
    app._handle_button_tap = MagicMock()
    app._delete_current_item = MagicMock()
    return app


def test_delete_mode_confirm_calls_delete():
    app = _make_app(rect=(10, 10, 50, 50))

    app._handle_touch_down((20, 20))

    app._delete_current_item.assert_called_once()
    app._handle_button_tap.assert_not_called()
    app.touch.on_down.assert_not_called()


def test_delete_mode_miss_cancels_without_play_or_swipe():
    app = _make_app(rect=(10, 10, 50, 50))

    app._handle_touch_down((200, 200))

    app._delete_current_item.assert_not_called()
    app._handle_button_tap.assert_not_called()
    app.touch.on_down.assert_not_called()
    assert app.delete_mode_id is None
    app.renderer.invalidate.assert_called_once()


def test_delete_mode_missing_renderer_rect_uses_fallback_without_fallthrough():
    app = _make_app(rect=None)
    app.renderer.delete_button_rect = None
    app._delete_button_rect = None
    fallback = app._delete_fallback_rect()
    x, y, w, h = fallback

    app._handle_touch_down((x + w // 2, y + h // 2))

    app._delete_current_item.assert_called_once()
    app._handle_button_tap.assert_not_called()
    app.touch.on_down.assert_not_called()


def test_delete_mode_missing_rect_miss_cancels_without_play_or_swipe():
    app = _make_app(rect=None)
    app.renderer.delete_button_rect = None
    app._delete_button_rect = None

    app._handle_touch_down((0, 0))

    app._delete_current_item.assert_not_called()
    app._handle_button_tap.assert_not_called()
    app.touch.on_down.assert_not_called()
    assert app.delete_mode_id is None

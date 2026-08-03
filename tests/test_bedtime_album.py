"""
Tests for the bedtime album — the one record still reachable during quiet hours.

The carousel is the only thing a child can touch, so what it contains at
bedtime *is* the access control. These tests pin that down.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.app import Mello
from mello.models import CatalogItem

LULLABY = 'spotify:album:lullaby'
LOUD = 'spotify:album:loud'


def _items():
    return [
        CatalogItem(id='1', uri=LOUD, name='Drum Solos', type='album'),
        CatalogItem(id='2', uri=LULLABY, name='Lullabies', type='album'),
    ]


def _app(quiet_active: bool, bedtime_uri, temp_item=None) -> Mello:
    """Minimal Mello exposing just what the bedtime properties touch."""
    app = Mello.__new__(Mello)
    app.catalog_manager = SimpleNamespace(items=_items())
    app.temp_item = temp_item
    app.quiet_hours = SimpleNamespace(active=quiet_active)
    app.settings = SimpleNamespace(bedtime_uri=bedtime_uri)
    return app


# --- What the carousel holds ---

def test_normal_hours_show_everything():
    app = _app(quiet_active=False, bedtime_uri=LULLABY)
    assert [i.uri for i in app.display_items] == [LOUD, LULLABY]


def test_bedtime_narrows_to_the_chosen_album():
    app = _app(quiet_active=True, bedtime_uri=LULLABY)
    assert [i.uri for i in app.display_items] == [LULLABY]


def test_bedtime_without_a_chosen_album_leaves_the_list_alone():
    """No bedtime album means the lock is total, handled by bedtime_locked."""
    app = _app(quiet_active=True, bedtime_uri=None)
    assert [i.uri for i in app.display_items] == [LOUD, LULLABY]


def test_deleted_bedtime_album_does_not_break_the_carousel():
    app = _app(quiet_active=True, bedtime_uri='spotify:album:gone')
    assert app.bedtime_item is None
    assert [i.uri for i in app.display_items] == [LOUD, LULLABY]


def test_temp_item_hidden_at_bedtime():
    """A freshly cast album must not sneak past the bedtime filter."""
    temp = CatalogItem(id='temp', uri='spotify:album:new', name='New', is_temp=True)
    app = _app(quiet_active=True, bedtime_uri=LULLABY, temp_item=temp)
    assert [i.uri for i in app.display_items] == [LULLABY]


def test_temp_item_visible_outside_bedtime():
    temp = CatalogItem(id='temp', uri='spotify:album:new', name='New', is_temp=True)
    app = _app(quiet_active=False, bedtime_uri=LULLABY, temp_item=temp)
    assert [i.uri for i in app.display_items] == [LOUD, LULLABY, 'spotify:album:new']


# --- Whether taps wake the screen at all ---

def test_locked_when_no_bedtime_album():
    assert _app(quiet_active=True, bedtime_uri=None).bedtime_locked is True


def test_not_locked_when_a_bedtime_album_exists():
    """Taps should wake — to a carousel holding only the allowed album."""
    assert _app(quiet_active=True, bedtime_uri=LULLABY).bedtime_locked is False


def test_locked_when_chosen_album_was_deleted():
    """A dangling URI must fall back to the strict lock, not unlock the device."""
    assert _app(quiet_active=True, bedtime_uri='spotify:album:gone').bedtime_locked is True


def test_never_locked_outside_quiet_hours():
    assert _app(quiet_active=False, bedtime_uri=None).bedtime_locked is False


# --- Focus reset when the list changes size under us ---

def test_filter_change_resets_focus():
    app = _app(quiet_active=True, bedtime_uri=LULLABY)
    app.selected_index = 1          # was pointing at the second album
    app.carousel = SimpleNamespace(scroll_x=1.0)
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._update_carousel_max_index = MagicMock()
    app._bedtime_filtered = False

    app._sync_bedtime_filter()

    assert app._bedtime_filtered is True
    assert app.selected_index == 0   # else it would index past a 1-item list
    assert app.carousel.scroll_x == 0
    app._update_carousel_max_index.assert_called_once()


def test_no_reset_when_nothing_changed():
    app = _app(quiet_active=True, bedtime_uri=LULLABY)
    app.selected_index = 0
    app.carousel = SimpleNamespace(scroll_x=0)
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._update_carousel_max_index = MagicMock()
    app._bedtime_filtered = True     # already filtered

    app._sync_bedtime_filter()

    app._update_carousel_max_index.assert_not_called()
    app.renderer.invalidate.assert_not_called()

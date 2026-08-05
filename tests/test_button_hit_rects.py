"""
Button hit rects must survive frames that redraw nothing.

Most frames take the steady-state path in render() and draw nothing at all.
The rects used to be cleared at the top of every frame regardless, so an
on-cover button stopped being tappable one frame after it appeared — and the
tap fell through to the carousel, which reads a centre tap as play. That was
reported three times (list button, delete confirm, then +) before the shared
cause was found here rather than in three separate fallbacks.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

pygame = pytest.importorskip('pygame')

from mello.models import CatalogItem, MenuState, NowPlaying
from mello.ui.context import RenderContext
from mello.ui.renderer import Renderer


def _renderer():
    pygame.init()
    pygame.font.init()
    # A real display surface: draw() calls convert(), which needs a video mode.
    screen = pygame.display.set_mode((720, 1280))
    return Renderer(screen, image_cache=_FakeCovers(), icons={})


class _FakeCovers:
    """Enough of ImageCache to let a frame draw."""

    def get(self, path, size):
        return pygame.Surface((size, size))

    def get_dimmed(self, path, size):
        return self.get(path, size)


def _item(uri, is_temp=False, item_id='i1'):
    return CatalogItem(id=item_id, uri=uri, name='Thing', type='album',
                       artist='A', image='/images/x.png', is_temp=is_temp)


def _ctx(items, **kwargs):
    # Paused, with a title resolved for the focused cover. That's what reaches
    # the steady state: the title area only settles once _get_track_key returns
    # something, and the carousel is only skipped while playback is stopped.
    focused = items[0] if items else None
    paused_on = NowPlaying(
        paused=True, stopped=False,
        context_uri=focused.uri if focused else None,
        track_name='Song', track_artist='A')
    ctx = RenderContext(
        items=items, selected_index=0, now_playing=paused_on,
        scroll_x=0.0, drag_offset=0.0, dragging=False, is_sleeping=False,
        volume_pct=60, volume_icon='volume_low', volume_slider_open=False,
        delete_mode_id=None, pressed_button=None,
        is_loading=False, is_playing=False)
    for key, value in kwargs.items():
        setattr(ctx, key, value)
    return ctx


def _settle(renderer, ctx):
    """Render until we reach the steady state that redraws nothing."""
    for _ in range(6):
        dirty = renderer.draw(ctx)
        if dirty == []:
            return True
    return False


class TestRectsSurviveIdleFrames:

    def test_add_button_stays_tappable(self):
        """The reported bug: + fell through to play while idle."""
        r = _renderer()
        ctx = _ctx([_item('spotify:album:x', is_temp=True)])
        assert _settle(r, ctx), 'never reached the steady state'
        assert r.add_button_rect is not None

    def test_track_list_button_stays_tappable(self):
        r = _renderer()
        ctx = _ctx([_item('spotify:album:x')], track_listable=True)
        assert _settle(r, ctx)
        assert r.track_list_button_rect is not None

    def test_delete_button_stays_tappable(self):
        r = _renderer()
        ctx = _ctx([_item('spotify:album:x')], delete_mode_id='i1')
        assert _settle(r, ctx)
        assert r.delete_button_rect is not None

    def test_settings_button_appears_only_with_no_network(self):
        """It lives in the empty state, not the controls row."""
        r = _renderer()
        r.draw(_ctx([], has_network=False))
        assert r.settings_button_rect is not None
        r.draw(_ctx([], has_network=True))
        assert r.settings_button_rect is None


class TestRectsDoNotOutliveTheirButton:
    """The other half: a kept rect must not be a stale rect."""

    def test_add_rect_goes_when_the_item_is_no_longer_temp(self):
        r = _renderer()
        r.draw(_ctx([_item('spotify:album:x', is_temp=True)]))
        assert r.add_button_rect is not None
        r.draw(_ctx([_item('spotify:album:x')], selected_index=0))
        assert r.add_button_rect is None, 'tapping the corner would still save'

    def test_track_list_rect_goes_when_not_listable(self):
        r = _renderer()
        r.draw(_ctx([_item('spotify:album:x')], track_listable=True))
        assert r.track_list_button_rect is not None
        r.draw(_ctx([_item('spotify:album:x')], track_listable=False))
        assert r.track_list_button_rect is None

    def test_delete_rect_goes_when_delete_mode_ends(self):
        r = _renderer()
        r.draw(_ctx([_item('spotify:album:x')], delete_mode_id='i1'))
        assert r.delete_button_rect is not None
        r.draw(_ctx([_item('spotify:album:x')], delete_mode_id=None))
        assert r.delete_button_rect is None

    def test_everything_goes_while_asleep(self):
        r = _renderer()
        r.draw(_ctx([_item('spotify:album:x', is_temp=True)]))
        assert r.add_button_rect is not None
        r.draw(_ctx([_item('spotify:album:x', is_temp=True)], is_sleeping=True))
        assert r.add_button_rect is None

    def test_everything_goes_behind_a_menu(self):
        r = _renderer()
        r.draw(_ctx([_item('spotify:album:x', is_temp=True)]))
        r.draw(_ctx([_item('spotify:album:x', is_temp=True)],
                      menu_state=MenuState.MAIN))
        assert r.add_button_rect is None

    def test_everything_goes_when_the_catalog_is_empty(self):
        r = _renderer()
        r.draw(_ctx([_item('spotify:album:x', is_temp=True)]))
        r.draw(_ctx([]))
        assert r.add_button_rect is None


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))

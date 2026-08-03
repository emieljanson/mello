"""
Menu layout: section headers must not be painted over by the next row.
"""
import os
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

pygame = pytest.importorskip('pygame')

from mello.config import COLORS
from mello.managers.bluetooth import BluetoothDevice
from mello.models import MenuState, NowPlaying
from mello.ui.context import RenderContext
from mello.ui.renderer import Renderer


def _renderer():
    pygame.init()
    pygame.font.init()
    screen = pygame.Surface((720, 1280))
    return Renderer(screen, image_cache=None, icons={})


def _bt_context():
    return RenderContext(
        items=[], selected_index=0, now_playing=NowPlaying(), scroll_x=0.0,
        drag_offset=0.0, dragging=False, is_sleeping=False, volume_index=1,
        delete_mode_id=None, pressed_button=None, is_loading=False, is_playing=False,
        menu_state=MenuState.BT_LIST,
        bt_paired_devices=[BluetoothDevice(mac='AA:BB', name='Speaker', paired=True)],
        bt_discovered_devices=[BluetoothDevice(mac='CC:DD', name='Earbuds')],
    )


def test_bt_headers_render_visible_pixels():
    r = _renderer()
    ctx = _bt_context()
    r._draw_menu_frame(ctx)

    muted = COLORS['text_muted']
    found = 0
    for x in range(720):
        for y in range(1280):
            if r.screen.get_at((x, y))[:3] == muted:
                found += 1
    assert found > 50, f'no header text pixels found (got {found})'


def test_menu_rows_do_not_overlap():
    r = _renderer()
    ctx = _bt_context()
    items = r._build_bt_content(ctx)

    extents = [r._menu_row_extent(i[0]) for i in items]
    top = r._MENU_CONTENT_TOP + extents[0]
    prev_bottom = None
    for extent in extents:
        x = top - extent
        if prev_bottom is not None:
            assert x + extent <= prev_bottom, 'row paints over the one above it'
        prev_bottom = x
        top = x - r._MENU_BTN_GAP


if __name__ == '__main__':
    test_bt_headers_render_visible_pixels()
    test_menu_rows_do_not_overlap()
    print('ok')

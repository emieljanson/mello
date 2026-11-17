"""
What the renderer puts on screen, and where a touch lands.

The renderer describes each screen as boxes, pictures and labels that Slint
draws; the same rectangles are used to find what a touch hit. These checks
make sure the two can not drift apart, and that a frame where nothing
changed hands Slint nothing new.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.config import (
    COLORS, ICONS_DIR, CAROUSEL_X, CAROUSEL_CENTER_Y, COVER_SIZE, CONTROLS_X, BTN_SPACING,
)
from mello.handlers.evdev_touch import EvdevTouchHandler, InputEvent
from mello.managers.bluetooth import BluetoothDevice
from mello.models import CatalogItem, MenuState, NowPlaying
from mello.ui import Bitmaps, Renderer, RenderContext
from mello.ui.bitmaps import QR_SIZE, text_size, FONT_LARGE
from mello.ui.geometry import Rect
from mello.ui.renderer import Box, lighten

ITEMS = [CatalogItem(id=str(i), uri=f'spotify:album:{i}', name=f'Album {i}', image=None) for i in range(6)]
ITEMS[2].current_track = {'name': 'Liedje', 'artist': 'Iemand', 'position': 50000, 'duration': 100000}


def ctx(**kw):
    base = dict(items=ITEMS, selected_index=2, now_playing=NowPlaying(), scroll_x=2.0, drag_offset=0,
                dragging=False, is_sleeping=False, volume_index=1, delete_mode_id=None, pressed_button=None,
                is_loading=False, is_playing=False, volume_levels=[{'speaker': 70, 'bt': 20}] * 3)
    base.update(kw)
    return RenderContext(**base)


@pytest.fixture
def renderer():
    return Renderer(Bitmaps(ICONS_DIR))


def test_rect_touch_area():
    r = Rect(10, 20, 30, 40)
    assert r.collidepoint(10, 20) and r.collidepoint((39, 59))
    assert not r.collidepoint(40, 20) and not r.collidepoint(10, 60)   # right/bottom edge is outside
    assert r.center == (25, 40) and tuple(r) == (10, 20, 30, 40)


def test_main_screen_buttons_are_where_taps_are_handled(renderer):
    renderer.draw(ctx())
    centers = {(b.x + b.w // 2, b.y + b.h // 2) for b in renderer.scene.boxes}
    # app._handle_button_tap: prev / play / next along y at x=CONTROLS_X
    for y in (CAROUSEL_CENTER_Y - BTN_SPACING, CAROUSEL_CENTER_Y, CAROUSEL_CENTER_Y + BTN_SPACING):
        assert (CONTROLS_X, y) in centers
    assert renderer.scene.labels[0].text == 'Liedje'


def test_pressed_button_is_lighter_and_shown_at_once(renderer):
    renderer.draw(ctx())
    before = renderer.scene
    renderer.draw(ctx(pressed_button='play'))
    play = next(b for b in renderer.scene.boxes if b.w == 121)
    assert play.color == lighten(COLORS['accent'])
    assert renderer.scene is not before


def test_nothing_new_for_slint_while_swiping(renderer):
    renderer.draw(ctx())
    scene = renderer.scene
    renderer.draw(ctx(dragging=True, drag_offset=-120))
    assert renderer.scene is scene                       # buttons and title untouched
    assert renderer.carousel_frame['tops'][2] != 640 - COVER_SIZE // 2


def test_progress_bar_from_saved_place(renderer):
    renderer.draw(ctx())
    assert renderer.carousel_frame['progress_fill'] == COVER_SIZE // 2


def test_delete_button_touch_area_matches_the_apps_fallback(renderer):
    from mello.app import Mello
    renderer.draw(ctx(delete_mode_id='2'))
    assert renderer.carousel_frame['button'] == 'delete'
    assert renderer.delete_button_rect == Mello._delete_fallback_rect(None)


def test_add_button_on_a_new_album(renderer):
    temp = CatalogItem(id='t', uri='spotify:album:t', name='New', is_temp=True)
    items = ITEMS + [temp]
    renderer.draw(ctx(items=items, selected_index=6, scroll_x=6.0))
    assert renderer.carousel_frame['button'] == 'add'
    x, y, w, h = renderer.add_button_rect
    assert x < CAROUSEL_X + 66 < x + w                   # around the + on the cover


def test_long_title_is_shortened_to_fit(renderer):
    np = NowPlaying(playing=True, paused=False, stopped=False, context_uri=ITEMS[2].uri,
                    track_name='Hoofdstuk 12 ' * 20, track_artist='Iemand')
    renderer.draw(ctx(now_playing=np))
    title = renderer.scene.labels[0].text
    assert title.endswith('...') and text_size(title, FONT_LARGE)[0] <= 1280 - 100 - 30


def test_menu_buttons_are_drawn_where_they_are_tapped(renderer):
    renderer.draw(ctx(menu_state=MenuState.MAIN, app_version_label='v1'))
    rects = renderer.menu_button_rects
    boxes = {tuple(b[:4]) for b in renderer.scene.boxes}
    for name in ('wifi', 'bluetooth', 'volume', 'spotify'):
        assert tuple(rects[name]) in boxes, name
    close = rects['close']
    assert any(tuple(b[:4]) == (close.x, close.y, close.width + 1, close.height + 1)
               for b in renderer.scene.top_boxes)
    assert renderer.scene.header_fade and renderer.scene.clip_left == 20


def test_scrolled_menu_only_taps_what_is_visible(renderer):
    renderer.draw(ctx(menu_state=MenuState.MAIN))
    overflow = renderer.menu_content_overflow
    assert overflow > 0
    renderer.draw(ctx(menu_state=MenuState.MAIN, menu_scroll_offset=overflow))
    assert 'wifi' not in renderer.menu_button_rects        # scrolled under the title
    assert 'reset' in renderer.menu_button_rects


def test_volume_rows_have_plus_and_minus(renderer):
    renderer.draw(ctx(menu_state=MenuState.VOLUME_LEVELS))
    assert {'vol_minus_0_speaker', 'vol_plus_2_bt'} <= set(renderer.menu_button_rects)
    assert any(l.text == 'Speaker low: 70%' for l in renderer.scene.labels)


def test_bluetooth_lists_devices(renderer):
    devices = [BluetoothDevice('AA', 'Koptelefoon', True, True)]
    renderer.draw(ctx(menu_state=MenuState.BT_LIST, bt_paired_devices=devices))
    assert any(l.text == 'Koptelefoon' for l in renderer.scene.labels)
    assert any(b.color == COLORS['accent'] for b in renderer.scene.boxes)   # the connected one


def test_no_internet_settings_button_is_tappable(renderer):
    renderer.draw(ctx(items=[], has_network=False))
    x, y, w, h = renderer.settings_button_rect
    assert Box(x, y, w, h, COLORS['accent'], 12) in renderer.scene.boxes
    assert renderer.carousel_frame is None


def test_soloist_prompt_button_fills_while_held(renderer):
    qr = [[True, False] * 10] * 20
    kw = dict(menu_state=MenuState.SPOTIFY, menu_spotify_prompt=True, menu_prompt_title='New: a faster Mello',
              menu_prompt_benefits=('Starts sooner',), menu_prompt_button='Hold to do it later',
              menu_prompt_hint='Scan', menu_spotify_items=[('qr', qr)])
    renderer.draw(ctx(menu_prompt_hold_progress=0.0, **kw))
    pill = renderer.menu_button_rects['prompt_close']
    assert len(renderer.scene.boxes) == 1
    renderer.draw(ctx(menu_prompt_hold_progress=0.5, **kw))
    fill = renderer.scene.boxes[1]
    assert fill.color == COLORS['accent'] and fill.h == pill.height // 2
    assert renderer.scene.strokes                                # the tick before the benefit
    assert any(p.key[0] == 'qr' and p.w == QR_SIZE for p in renderer.scene.pictures)


def test_sleeping_screen_is_black(renderer):
    renderer.draw(ctx(is_sleeping=True))
    assert renderer.scene.background == 'black' and not renderer.scene.boxes
    assert renderer.carousel_frame is None


def test_pictures_are_prepared(renderer):
    bitmaps = renderer.bitmaps
    w, h = bitmaps.size(('logo', 320))
    assert h == 320 and w < 320                                  # turned for the sideways panel
    assert bitmaps.size(('qr', ((True, False),) * 2)) == (QR_SIZE, QR_SIZE)
    plus = bitmaps.image(('icon', 'plus', 72, COLORS['accent']))
    assert plus.size == (72, 72)
    opaque = [plus.getpixel((x, y)) for x in range(72) for y in range(72) if plus.getpixel((x, y))[3] == 255]
    # the (nearly) white icon turned purple
    assert opaque and all(abs(c - t) <= 4 for px in opaque for c, t in zip(px[:3], COLORS['accent']))


def test_touch_events_queue_and_wake():
    touch = EvdevTouchHandler(720, 1280)
    touch.post(InputEvent('down', (10, 20), 1.0))
    touch.post(InputEvent('up', (10, 20), 1.1))
    assert touch.wake_event.is_set()
    assert [e.kind for e in touch.take_events()] == ['down', 'up']
    assert touch.take_events() == []


def test_screen_error_message_fits_the_framebuffer():
    from mello.ui import screen_error
    image = screen_error.message_image(['This speaker needs a newer Raspberry Pi OS', 'Reinstall it'])
    assert image.size == (720, 1280)
    data = screen_error._to_framebuffer_bytes(image, 16, 1440)
    assert len(data) == 1440 * 1280


def test_slint_file_compiles():
    slint = pytest.importorskip('slint')
    from mello.ui.slint_display import SLINT_FILE
    window = slint.load_file(str(SLINT_FILE)).MelloWindow
    assert window is not None

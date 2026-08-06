"""
The volume slider: opening, closing, and setting the level by touch.

Three preset values cycled by tapping gave no real control. This replaces them
with one continuous level, so the tests that matter are about the gestures —
especially that a tap outside is *consumed* by closing the widget. If it fell
through, closing the slider would also toggle playback, which is the exact bug
class that bit the + and list buttons.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.config import (CAROUSEL_X, COVER_SIZE, VOLUME_SLIDER_TIMEOUT,
                          VOLUME_STEP_PCT, CAROUSEL_TOUCH_MARGIN,
                          ACTION_DEBOUNCE, CONTROLS_X, PLAY_BTN_SIZE)
from mello.ui.renderer import Renderer


class FakeSettings:
    def __init__(self, pct=50):
        self.volume_pct = pct

    def get_max_volume(self, output_type):
        return {'speaker': 98, 'bt': 65}[output_type]

    def set_volume_pct(self, pct):
        self.volume_pct = pct


def _app(pct=50, slider_open=False):
    """Minimal Mello wired for slider gestures only."""
    from mello.app import Mello
    from mello.controllers.volume import VolumeController

    app = Mello.__new__(Mello)
    app.volume = VolumeController(SimpleNamespace(), FakeSettings(pct))
    app.volume_slider_open = slider_open
    app._volume_dragging = False
    app._volume_slider_touched = time.time()
    app._volume_hold_start = None
    app._menu_hold_triggered = False
    app._bt_audio_active = False
    app.bluetooth = None
    app._last_action_time = 0.0
    app.applied = []
    app.renderer = SimpleNamespace(invalidate=lambda: None)
    # Don't touch real ALSA.
    app.volume.apply = lambda: app.applied.append(app.volume.pct)
    return app


def _on_track(fraction):
    """A point at `fraction` along the slider track."""
    x0, x1, center_y = Renderer.volume_slider_geometry()
    return (int(x0 + (x1 - x0) * fraction), center_y)


class TestGeometry:

    def test_ends_map_to_zero_and_full(self):
        assert Renderer.volume_pct_from_touch(_on_track(0.0)) == 0
        assert Renderer.volume_pct_from_touch(_on_track(1.0)) == 100

    def test_midpoint_is_fifty(self):
        assert round(Renderer.volume_pct_from_touch(_on_track(0.5))) == 50

    def test_beyond_the_ends_is_clamped(self):
        x0, x1, y = Renderer.volume_slider_geometry()
        assert Renderer.volume_pct_from_touch((x0 - 400, y)) == 0
        assert Renderer.volume_pct_from_touch((x1 + 400, y)) == 100

    def test_travels_along_the_users_vertical(self):
        """The display is pre-rotated: 'vertical' to the user is physical x."""
        x0, x1, _ = Renderer.volume_slider_geometry()
        assert x1 > x0, 'louder must be further up the physical x axis'

    def test_hit_rect_needs_no_drawn_frame(self):
        """A classmethod, so hit-testing can't depend on the last frame.

        This is the #13 lesson: rects that only exist after a draw made every
        on-cover button untappable while paused.
        """
        x, y, w, h = Renderer.volume_slider_hit_rect()
        assert w > 0 and h > 0

    def test_hit_rect_covers_the_whole_track(self):
        hx, hy, hw, hh = Renderer.volume_slider_hit_rect()
        x0, x1, center_y = Renderer.volume_slider_geometry()
        assert hx <= x0 and hx + hw >= x1
        assert hy <= center_y <= hy + hh

    def test_widget_sits_inside_the_carousel_dirty_rect(self):
        """Partial updates only repaint the carousel; the slider must be in it."""
        hx, hy, hw, hh = Renderer.volume_slider_hit_rect()
        assert hx >= CAROUSEL_X - 50
        assert hx + hw <= CAROUSEL_X - 50 + COVER_SIZE + 100

    def test_track_does_not_reach_the_volume_button(self):
        """Or a tap meant for the button would land on the track instead."""
        x0, _, _ = Renderer.volume_slider_geometry()
        assert x0 > CAROUSEL_X - CAROUSEL_TOUCH_MARGIN - 60


class TestOpeningAndClosing:

    def test_button_tap_opens_it(self):
        app = _app()
        app._volume_hold_start = time.time()
        app._handle_button_up()
        assert app.volume_slider_open is True

    def test_button_tap_closes_it_again(self):
        app = _app(slider_open=True)
        app._volume_hold_start = time.time()
        app._handle_button_up()
        assert app.volume_slider_open is False

    def test_a_long_hold_opens_the_menu_instead(self):
        """The 3s hold still belongs to the settings menu, not the slider."""
        app = _app()
        app._volume_hold_start = time.time()
        app._menu_hold_triggered = True
        app._handle_button_up()
        assert app.volume_slider_open is False

    def test_tap_outside_closes_and_is_consumed(self):
        """Consumed matters: otherwise the same tap would also toggle play."""
        app = _app(slider_open=True)
        handled = app._handle_volume_slider_touch((700, 300))
        assert handled is True, 'the tap must not reach the carousel'
        assert app.volume_slider_open is False

    def test_touches_are_ignored_while_closed(self):
        app = _app()
        assert app._handle_volume_slider_touch(_on_track(0.5)) is False

    def test_the_volume_button_is_not_outside(self):
        """Tapping the button must reach its own handler, not the close path."""
        _, _, vol_y = Renderer.volume_slider_geometry()
        app = _app(slider_open=True)
        assert app._touch_on_volume_button((85, vol_y)) is True

    def test_a_cover_tap_is_outside(self):
        app = _app(slider_open=True)
        assert app._touch_on_volume_button((CAROUSEL_X + 100, 640)) is False


class TestSettingTheLevel:

    def test_tap_on_the_track_jumps_to_that_level(self):
        """Tap-to-jump, not drag-only: a 22px bar is not a child-sized target."""
        app = _app(pct=10, slider_open=True)
        app._handle_volume_slider_touch(_on_track(1.0))
        assert app.volume.pct == 100

    def test_tap_starts_a_drag(self):
        app = _app(slider_open=True)
        app._handle_volume_slider_touch(_on_track(0.5))
        assert app._volume_dragging is True

    def test_dragging_follows_the_finger(self):
        app = _app(pct=0, slider_open=True)
        app._handle_volume_slider_touch(_on_track(0.2))
        for fraction in (0.4, 0.6, 0.8):
            app._set_volume_from_touch(_on_track(fraction))
        assert app.volume.pct == 80

    def test_levels_snap_to_the_step(self):
        app = _app(slider_open=True)
        for fraction in (i / 40 for i in range(41)):
            app._set_volume_from_touch(_on_track(fraction))
            assert app.volume.pct % VOLUME_STEP_PCT == 0

    def test_touch_up_ends_the_drag(self):
        app = _app(slider_open=True)
        app._handle_volume_slider_touch(_on_track(0.5))
        app._handle_touch_up(_on_track(0.5))
        assert app._volume_dragging is False
        assert app.volume_slider_open is True, 'releasing must not close it'

    def test_bt_volume_follows_when_bt_is_live(self):
        app = _app(pct=0, slider_open=True)
        app._bt_audio_active = True
        sent = []
        app.bluetooth = SimpleNamespace(set_volume=sent.append)
        app._set_volume_from_touch(_on_track(1.0))
        assert sent == [app.volume.bt_level]


class TestTapThenHoldIsNotDebounced:
    """The regression: 'press volume, settings won't open, and it skips track'.

    Root cause: a fast tap-then-hold on the volume button landed inside the
    300ms ACTION_DEBOUNCE window shared by every button. The debounce silently
    dropped the second touch-down, so no hold timer ever started and holding
    afterward did nothing — read by the user as "the button stopped
    responding". This is much easier to trigger now than before: a short tap
    used to just cycle a preset, so nobody immediately pressed again; now it
    opens the slider, which invites exactly that "tap, then hold" gesture.
    """

    def _button_app(self):
        app = _app()
        app._last_action_time = 0.0
        app.renderer.invalidate = lambda: None
        return app

    def _volume_pos(self):
        _, _, vol_y = Renderer.volume_slider_geometry()
        return (CONTROLS_X, vol_y)

    def test_a_lone_press_starts_the_hold_timer(self):
        app = self._button_app()
        app._handle_button_tap(self._volume_pos())
        assert app._volume_hold_start is not None

    def test_second_press_inside_the_debounce_window_still_starts_it(self):
        """This is the exact failure: it must NOT be swallowed like prev/next are."""
        app = self._button_app()
        app._handle_button_tap(self._volume_pos())
        app._handle_button_up()   # short tap: opens the slider, clears hold_start
        assert app._volume_hold_start is None

        app._last_action_time = time.time()   # as if the tap just registered
        app._handle_button_tap(self._volume_pos())
        assert app._volume_hold_start is not None, \
            'a fast tap-then-hold must still arm the hold timer'

    def test_other_buttons_are_still_debounced(self):
        """The fix must be volume-specific, not a debounce bypass for everyone."""
        app = self._button_app()
        skipped = []
        app._skip_track = lambda fn: skipped.append(fn)
        app.api = SimpleNamespace(next=lambda: True, prev=lambda: True)
        app.bluetooth = SimpleNamespace(connected_device=False)

        from mello.config import CAROUSEL_CENTER_Y, BTN_SPACING
        next_pos = (CONTROLS_X, CAROUSEL_CENTER_Y + BTN_SPACING)

        app._handle_button_tap(next_pos)
        app._handle_button_tap(next_pos)   # within ACTION_DEBOUNCE
        assert len(skipped) == 1, 'prev/next must still collapse a bouncy double-tap'

    def test_hit_test_agrees_with_the_sliders_own_check(self):
        """The two must never disagree — that gap is what let a slightly
        off-centre tap fall between "on the button" and "on the slider"."""
        app = self._button_app()
        pos = self._volume_pos()
        assert app._volume_button_hit(pos) == app._touch_on_volume_button(pos) == True


class TestIdleClose:

    def test_closes_once_left_alone(self):
        app = _app(slider_open=True)
        app._volume_slider_touched = time.time() - VOLUME_SLIDER_TIMEOUT - 1
        app._close_volume_slider_if_idle()
        assert app.volume_slider_open is False

    def test_stays_open_while_recently_touched(self):
        app = _app(slider_open=True)
        app._close_volume_slider_if_idle()
        assert app.volume_slider_open is True

    def test_never_closes_mid_drag(self):
        """A slow, deliberate drag must not have the widget vanish under it."""
        app = _app(slider_open=True)
        app._volume_dragging = True
        app._volume_slider_touched = time.time() - VOLUME_SLIDER_TIMEOUT - 1
        app._close_volume_slider_if_idle()
        assert app.volume_slider_open is True

    def test_setting_the_level_resets_the_timer(self):
        app = _app(slider_open=True)
        app._volume_slider_touched = time.time() - VOLUME_SLIDER_TIMEOUT - 1
        app._set_volume_from_touch(_on_track(0.5))
        app._close_volume_slider_if_idle()
        assert app.volume_slider_open is True

    def test_idle_check_is_harmless_when_closed(self):
        app = _app()
        app._close_volume_slider_if_idle()
        assert app.volume_slider_open is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))

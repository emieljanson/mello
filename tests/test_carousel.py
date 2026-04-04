"""
Tests for SmoothCarousel and PlayTimer.
"""
import time
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from berry.managers.carousel import SmoothCarousel, PlayTimer
from berry.models import CatalogItem


# ============================================
# SmoothCarousel
# ============================================

class TestSmoothCarousel:
    """Tests for SmoothCarousel scroll animation."""

    def test_initial_state(self):
        c = SmoothCarousel()
        assert c.scroll_x == 0.0
        assert c.target_index == 0
        assert c.settled is True

    def test_set_target_starts_animation(self):
        c = SmoothCarousel()
        c.max_index = 5
        c.set_target(3)
        assert c.target_index == 3
        assert c.settled is False

    def test_update_moves_toward_target(self):
        c = SmoothCarousel()
        c.max_index = 5
        c.set_target(3)
        c.update(0.016)  # ~60 FPS frame
        assert c.scroll_x > 0.0
        assert c.scroll_x < 3.0

    def test_settles_after_enough_updates(self):
        c = SmoothCarousel()
        c.max_index = 5
        c.set_target(2)
        for _ in range(100):
            c.update(0.016)
        assert c.settled is True
        assert c.scroll_x == 2.0

    def test_clamps_to_max_index(self):
        c = SmoothCarousel()
        c.max_index = 3
        c.set_target(10)
        assert c.target_index == 3

    def test_clamps_to_zero(self):
        c = SmoothCarousel()
        c.max_index = 3
        c.set_target(-5)
        assert c.target_index == 0

    def test_no_update_when_settled(self):
        c = SmoothCarousel()
        changed = c.update(0.016)
        assert changed is False

    def test_animates_backward(self):
        c = SmoothCarousel()
        c.max_index = 5
        c.scroll_x = 4.0
        c.set_target(1)
        c.update(0.016)
        assert c.scroll_x < 4.0


# ============================================
# PlayTimer
# ============================================

def _make_item(uri: str = 'spotify:album:test', name: str = 'Test') -> CatalogItem:
    return CatalogItem(id='1', uri=uri, name=name, type='album')


class TestPlayTimer:
    """Tests for PlayTimer auto-play behavior."""

    def test_initial_state(self):
        t = PlayTimer()
        assert t.item is None
        assert t.check() is None

    def test_start_stores_item(self):
        t = PlayTimer()
        item = _make_item()
        t.start(item)
        assert t.item is item

    def test_does_not_fire_before_delay(self):
        t = PlayTimer()
        t.start(_make_item())
        assert t.check() is None

    def test_fires_after_delay(self):
        t = PlayTimer()
        item = _make_item()
        t.start(item)
        # Monkey-patch start_time to simulate elapsed time
        t.start_time = time.time() - 10
        result = t.check()
        assert result is item

    def test_cancel_clears_timer(self):
        t = PlayTimer()
        t.start(_make_item())
        t.cancel()
        assert t.item is None
        assert t.check() is None

    def test_start_none_cancels(self):
        t = PlayTimer()
        t.start(_make_item())
        t.start(None)
        assert t.item is None

    def test_same_item_no_restart(self):
        t = PlayTimer()
        item = _make_item()
        t.start(item)
        original_start = t.start_time
        t.start_time = original_start - 0.01
        t.start(item)  # Same URI
        assert t.start_time == original_start - 0.01

    def test_different_item_restarts(self):
        t = PlayTimer()
        t.start(_make_item(uri='spotify:album:a'))
        original_start = t.start_time
        t.start_time = original_start - 0.01
        t.start(_make_item(uri='spotify:album:b'))
        assert t.start_time > (original_start - 0.01)

    def test_fire_sets_last_played_uri(self):
        t = PlayTimer()
        item = _make_item(uri='spotify:album:x')
        t.start(item)
        t.start_time = time.time() - 10
        t.check()
        assert t.last_played_uri == 'spotify:album:x'

    def test_cooldown_after_fire(self):
        t = PlayTimer()
        t.start(_make_item())
        t.start_time = time.time() - 10
        t.check()
        assert t.is_in_cooldown() is True

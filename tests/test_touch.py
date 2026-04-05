"""
Tests for TouchHandler - swipe detection, long press, gestures.
"""
import time
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.handlers.touch import TouchHandler


class TestSwipeDetection:
    """Tests for swipe gesture detection."""

    def test_left_swipe_detected(self):
        """Left swipe is detected correctly."""
        handler = TouchHandler()

        # Simulate left swipe (decreasing Y in portrait = left)
        handler.on_down((400, 600))
        time.sleep(0.05)
        handler.on_move((400, 400))
        action, velocity = handler.on_up((400, 300))

        assert action == 'left'
        assert velocity < 0  # Negative velocity for left

    def test_right_swipe_detected(self):
        """Right swipe is detected correctly."""
        handler = TouchHandler()

        # Simulate right swipe (increasing Y in portrait = right)
        handler.on_down((400, 300))
        time.sleep(0.05)
        handler.on_move((400, 500))
        action, velocity = handler.on_up((400, 600))

        assert action == 'right'
        assert velocity > 0  # Positive velocity for right

    def test_tap_detected(self):
        """Tap (no significant movement) is detected."""
        handler = TouchHandler()

        handler.on_down((400, 400))
        time.sleep(0.05)
        action, velocity = handler.on_up((400, 410))  # Small movement

        assert action == 'tap'

    def test_velocity_calculation(self):
        """Swipe velocity is calculated correctly."""
        handler = TouchHandler()

        handler.on_down((400, 200))
        time.sleep(0.1)  # 100ms
        handler.on_move((400, 600))
        action, velocity = handler.on_up((400, 600))

        # 400px in 100ms = 4 px/ms
        # Allow some tolerance for timing
        assert abs(velocity) > 1.0  # Should have significant velocity


class TestLongPress:
    """Tests for long press detection."""

    def test_long_press_not_triggered_early(self):
        """Long press doesn't trigger before threshold."""
        handler = TouchHandler()

        handler.on_down((400, 400))
        time.sleep(0.1)  # Only 100ms

        result = handler.check_long_press()
        assert result is False

    def test_long_press_triggered_after_threshold(self):
        """Long press triggers after 1 second hold."""
        handler = TouchHandler(long_press_time=0.2)  # Shorter for testing

        handler.on_down((400, 400))
        time.sleep(0.25)  # Wait longer than threshold

        result = handler.check_long_press()
        assert result is True

    def test_long_press_only_triggers_once(self):
        """Long press only triggers once per press."""
        handler = TouchHandler(long_press_time=0.1)

        handler.on_down((400, 400))
        time.sleep(0.15)

        # First check should trigger
        result1 = handler.check_long_press()
        assert result1 is True

        # Second check should not trigger again
        result2 = handler.check_long_press()
        assert result2 is False

    def test_long_press_cancelled_by_movement(self):
        """Long press is cancelled if finger moves too much."""
        handler = TouchHandler(long_press_time=0.1)

        handler.on_down((400, 400))
        time.sleep(0.05)
        handler.on_move((400, 500))  # Move significantly
        time.sleep(0.1)

        # Should be swiping, not long pressing
        result = handler.check_long_press()
        assert result is False

    def test_long_press_release_does_not_emit_tap(self):
        """Releasing after long press should not trigger tap action."""
        handler = TouchHandler(long_press_time=0.1)

        handler.on_down((400, 400))
        time.sleep(0.15)
        assert handler.check_long_press() is True

        action, velocity = handler.on_up((400, 400))
        assert action is None
        assert velocity == 0


class TestDragState:
    """Tests for drag/swipe state tracking."""

    def test_dragging_state(self):
        """Dragging state is tracked correctly."""
        handler = TouchHandler()

        assert handler.dragging is False

        handler.on_down((400, 400))
        assert handler.dragging is True

        handler.on_up((400, 500))
        assert handler.dragging is False

    def test_drag_offset_calculated(self):
        """Drag offset tracks finger movement."""
        handler = TouchHandler()

        handler.on_down((400, 300))
        handler.on_move((400, 500))

        # Offset should reflect the Y movement (carousel direction)
        assert handler.drag_offset != 0

    def test_is_swiping_after_threshold(self):
        """is_swiping becomes True after movement threshold."""
        handler = TouchHandler()

        handler.on_down((400, 400))
        assert handler.is_swiping is False

        handler.on_move((400, 500))  # Move past threshold
        assert handler.is_swiping is True


class TestEdgeCases:
    """Edge case tests."""

    def test_on_up_without_down(self):
        """on_up without prior on_down handles gracefully."""
        handler = TouchHandler()

        # Should not crash
        action, velocity = handler.on_up((400, 400))
        assert action == 'tap'

    def test_multiple_rapid_touches(self):
        """Multiple rapid touch sequences work correctly."""
        handler = TouchHandler()

        for _ in range(5):
            handler.on_down((400, 400))
            handler.on_move((400, 450))
            handler.on_up((400, 500))

        # Should still be in clean state
        assert handler.dragging is False

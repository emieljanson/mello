"""
Tests for AutoPauseManager - verifies the variable timeout actually works.

Uses time.time() monkeypatching so tests run instantly (no real waiting).
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from berry.managers.auto_pause import AutoPauseManager


@pytest.fixture
def mock_deps():
    """Shared mock dependencies for AutoPauseManager."""
    return {
        'on_pause': MagicMock(),
        'get_volume': MagicMock(return_value=(100, 80)),
        'timeout_seconds': 30 * 60,
    }


@pytest.fixture
def make_manager(mock_deps):
    """Factory that creates an AutoPauseManager with a configurable timeout."""
    def _make(timeout_seconds=None):
        t = timeout_seconds or mock_deps['timeout_seconds']
        return AutoPauseManager(
            on_pause=mock_deps['on_pause'],
            get_volume=mock_deps['get_volume'],
            get_timeout=lambda: t,
        )
    return _make


class TestTimerStartsAndResets:
    def test_no_trigger_before_timeout(self, make_manager):
        mgr = make_manager(timeout_seconds=1800)
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 1799):
            assert mgr.check(is_playing=True) is False

    def test_triggers_at_timeout(self, make_manager):
        mgr = make_manager(timeout_seconds=1800)
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 1800):
            with patch.object(mgr, '_trigger_fade_out'):
                assert mgr.check(is_playing=True) is True

    def test_timer_resets_on_context_change(self, make_manager):
        mgr = make_manager(timeout_seconds=600)
        mgr.on_play('spotify:album:abc')
        first_start = mgr._play_start_time

        with patch('time.time', return_value=first_start + 500):
            mgr.on_play('spotify:album:xyz')

        assert mgr._play_start_time != first_start
        assert mgr._context_uri == 'spotify:album:xyz'

    def test_timer_resets_on_stop(self, make_manager):
        mgr = make_manager(timeout_seconds=600)
        mgr.on_play('spotify:album:abc')
        mgr.on_stop()

        assert mgr._play_start_time is None
        assert mgr._context_uri is None

    def test_same_context_does_not_reset_timer(self, make_manager):
        mgr = make_manager(timeout_seconds=600)
        mgr.on_play('spotify:album:abc')
        original_start = mgr._play_start_time

        mgr.on_play('spotify:album:abc')
        assert mgr._play_start_time == original_start


class TestVariableTimeout:
    """The core question: does changing the timeout setting actually affect when it triggers?"""

    def test_15_min_timeout_triggers_at_15_min(self, make_manager):
        mgr = make_manager(timeout_seconds=15 * 60)
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 14 * 60):
            assert mgr.check(is_playing=True) is False

        with patch('time.time', return_value=mgr._play_start_time + 15 * 60):
            with patch.object(mgr, '_trigger_fade_out'):
                assert mgr.check(is_playing=True) is True

    def test_60_min_timeout_does_not_trigger_at_30_min(self, make_manager):
        mgr = make_manager(timeout_seconds=60 * 60)
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 30 * 60):
            assert mgr.check(is_playing=True) is False

    def test_120_min_timeout_triggers_at_120_min(self, make_manager):
        mgr = make_manager(timeout_seconds=120 * 60)
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 120 * 60):
            with patch.object(mgr, '_trigger_fade_out'):
                assert mgr.check(is_playing=True) is True

    def test_dynamic_timeout_change_mid_playback(self):
        """Simulates user changing the setting while music is playing."""
        timeout_box = [30 * 60]

        mgr = AutoPauseManager(
            on_pause=MagicMock(),
            get_volume=MagicMock(return_value=(100, 80)),
            get_timeout=lambda: timeout_box[0],
        )
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 25 * 60):
            assert mgr.check(is_playing=True) is False

        timeout_box[0] = 15 * 60

        with patch('time.time', return_value=mgr._play_start_time + 25 * 60):
            with patch.object(mgr, '_trigger_fade_out'):
                assert mgr.check(is_playing=True) is True


class TestCheckGuards:
    def test_no_trigger_when_not_playing(self, make_manager):
        mgr = make_manager()
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 9999):
            assert mgr.check(is_playing=False) is False

    def test_no_trigger_when_no_context(self, make_manager):
        mgr = make_manager()
        assert mgr.check(is_playing=True) is False

    def test_no_trigger_when_already_fading(self, make_manager):
        mgr = make_manager(timeout_seconds=600)
        mgr.on_play('spotify:album:abc')
        mgr._is_fading = True

        with patch('time.time', return_value=mgr._play_start_time + 9999):
            assert mgr.check(is_playing=True) is False

    def test_null_context_resets(self, make_manager):
        mgr = make_manager()
        mgr.on_play('spotify:album:abc')
        mgr.on_play(None)
        assert mgr._play_start_time is None


class TestFadeOutAndRestore:
    @patch('berry.managers.auto_pause.set_system_volume')
    @patch('berry.managers.auto_pause.time.sleep')
    def test_fade_calls_pause_and_restores_volume(self, mock_sleep, mock_vol):
        on_pause = MagicMock()
        mgr = AutoPauseManager(
            on_pause=on_pause,
            get_volume=MagicMock(return_value=(100, 80)),
        )
        mgr._original_volume = (100, 80)
        mgr._is_fading = True

        mgr._fade_out_and_pause()

        on_pause.assert_called_once()
        last_restore_call = mock_vol.call_args_list[-1]
        assert last_restore_call == ((100, 80),)

    @patch('berry.managers.auto_pause.set_system_volume')
    def test_restore_volume_if_needed(self, mock_vol):
        mgr = AutoPauseManager(
            on_pause=MagicMock(),
            get_volume=MagicMock(return_value=(100, 80)),
        )
        mgr._original_volume = (90, 70)
        mgr._should_restore_volume = True

        mgr.restore_volume_if_needed()

        mock_vol.assert_called_once_with(90, 70)
        assert mgr._should_restore_volume is False

    @patch('berry.managers.auto_pause.set_system_volume')
    def test_restore_does_nothing_when_not_needed(self, mock_vol):
        mgr = AutoPauseManager(
            on_pause=MagicMock(),
            get_volume=MagicMock(return_value=(100, 80)),
        )
        mgr.restore_volume_if_needed()
        mock_vol.assert_not_called()


class TestSettingsIntegration:
    """Verify the full Settings → AutoPauseManager chain."""

    def test_settings_timeout_feeds_into_manager(self, tmp_path):
        import json
        settings_path = tmp_path / 'settings.json'
        settings_path.write_text(json.dumps({'auto_pause_minutes': 60}))

        from berry.managers.settings import Settings
        settings = Settings(path=settings_path)

        mgr = AutoPauseManager(
            on_pause=MagicMock(),
            get_volume=MagicMock(return_value=(100, 80)),
            get_timeout=lambda: settings.auto_pause_timeout,
        )
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 59 * 60):
            assert mgr.check(is_playing=True) is False

        with patch('time.time', return_value=mgr._play_start_time + 60 * 60):
            with patch.object(mgr, '_trigger_fade_out'):
                assert mgr.check(is_playing=True) is True

    def test_cycle_setting_changes_effective_timeout(self, tmp_path):
        import json
        settings_path = tmp_path / 'settings.json'

        from berry.managers.settings import Settings
        settings = Settings(path=settings_path)

        assert settings.auto_pause_timeout == 30 * 60

        settings.cycle_auto_pause()
        assert settings.auto_pause_timeout == 60 * 60

        mgr = AutoPauseManager(
            on_pause=MagicMock(),
            get_volume=MagicMock(return_value=(100, 80)),
            get_timeout=lambda: settings.auto_pause_timeout,
        )
        mgr.on_play('spotify:album:abc')

        with patch('time.time', return_value=mgr._play_start_time + 45 * 60):
            assert mgr.check(is_playing=True) is False

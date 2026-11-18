"""
Tests for VolumeController - always Mello mode (ALSA-controlled).
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.controllers.volume import VolumeController
from mello.utils import set_system_volume
from mello.config import DEFAULT_VOLUME_LEVELS


class FakeSettings:
    def get_volume_levels(self):
        return [dict(level) for level in DEFAULT_VOLUME_LEVELS]


class FakeAPI:
    """Minimal fake that satisfies PlayerAPIProtocol."""

    def __init__(self):
        self.volume_calls = []

    def status(self):
        return None

    def play(self, uri, skip_to_uri=None):
        return True

    def pause(self):
        return True

    def resume(self):
        return True

    def next(self):
        return True

    def prev(self):
        return True

    def seek(self, position):
        return True

    def set_volume(self, level):
        self.volume_calls.append(level)
        return True

    def set_repeat_context(self, enabled):
        return True

    def is_connected(self):
        return True


def _make_controller(api=None):
    return VolumeController(api or FakeAPI(), FakeSettings())


class TestVolumeInit:
    """Tests for initial state and setup."""

    def test_starts_at_index_1(self):
        vc = _make_controller()
        assert vc.index == 1

    def test_speaker_and_headphone_levels(self):
        vc = _make_controller()
        assert vc.speaker_level == DEFAULT_VOLUME_LEVELS[1]['speaker']
        assert vc.bt_level == DEFAULT_VOLUME_LEVELS[1]['bt']

    @patch('mello.controllers.volume.set_system_volume')
    @patch('mello.controllers.volume.unmute_speakers')
    def test_init_sets_system_volume(self, mock_unmute, mock_set_vol):
        vc = _make_controller()
        vc.init()
        mock_set_vol.assert_called_once_with(vc.speaker_level)
        mock_unmute.assert_called_once_with(vc.speaker_level)


class TestVolumeToggle:
    """Tests for cycling volume levels."""

    @patch('mello.controllers.volume.set_system_volume')
    @patch('mello.controllers.volume.run_async')
    def test_toggle_cycles_through_levels(self, mock_run_async, mock_set_vol):
        vc = _make_controller()
        initial = vc.index
        vc.toggle()
        assert vc.index == (initial + 1) % len(DEFAULT_VOLUME_LEVELS)

    @patch('mello.controllers.volume.set_system_volume')
    @patch('mello.controllers.volume.run_async')
    def test_toggle_wraps_around(self, mock_run_async, mock_set_vol):
        vc = _make_controller()
        for _ in range(len(DEFAULT_VOLUME_LEVELS)):
            vc.toggle()
        assert vc.index == 1  # Back to start

    @patch('mello.controllers.volume.run_async')
    def test_toggle_calls_set_system_volume(self, mock_run_async):
        vc = _make_controller()
        vc.toggle()
        mock_run_async.assert_called()
        args = mock_run_async.call_args[0]
        assert args[0] == set_system_volume


class TestEnsureSpotifyAt100:
    """Tests for first-play volume initialization."""

    def test_sets_volume_on_first_call(self):
        api = FakeAPI()
        vc = _make_controller(api)
        result = vc.ensure_spotify_at_100()
        assert result is True
        assert api.volume_calls == [100]

    def test_noop_on_second_call(self):
        api = FakeAPI()
        vc = _make_controller(api)
        vc.ensure_spotify_at_100()
        result = vc.ensure_spotify_at_100()
        assert result is False
        assert len(api.volume_calls) == 1

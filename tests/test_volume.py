"""
Tests for VolumeController - always Berry mode (ALSA-controlled).
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from berry.controllers.volume import VolumeController
from berry.utils import set_system_volume
from berry.config import VOLUME_LEVELS


class FakeAPI:
    """Minimal fake that satisfies LibrespotAPIProtocol."""

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

    def is_connected(self):
        return True


class TestVolumeInit:
    """Tests for initial state and setup."""

    def test_starts_at_index_1(self):
        api = FakeAPI()
        vc = VolumeController(api)
        assert vc.index == 1

    def test_speaker_and_headphone_levels(self):
        api = FakeAPI()
        vc = VolumeController(api)
        assert vc.speaker_level == VOLUME_LEVELS[1]['speaker']
        assert vc.headphone_level == VOLUME_LEVELS[1]['headphone']

    @patch('berry.controllers.volume.set_system_volume')
    def test_init_sets_system_volume(self, mock_set_vol):
        api = FakeAPI()
        vc = VolumeController(api)
        vc.init()
        mock_set_vol.assert_called_once_with(vc.speaker_level, vc.headphone_level)


class TestVolumeToggle:
    """Tests for cycling volume levels."""

    @patch('berry.controllers.volume.set_system_volume')
    @patch('berry.controllers.volume.run_async')
    def test_toggle_cycles_through_levels(self, mock_run_async, mock_set_vol):
        api = FakeAPI()
        vc = VolumeController(api)
        initial = vc.index
        vc.toggle()
        assert vc.index == (initial + 1) % len(VOLUME_LEVELS)

    @patch('berry.controllers.volume.set_system_volume')
    @patch('berry.controllers.volume.run_async')
    def test_toggle_wraps_around(self, mock_run_async, mock_set_vol):
        api = FakeAPI()
        vc = VolumeController(api)
        for _ in range(len(VOLUME_LEVELS)):
            vc.toggle()
        assert vc.index == 1  # Back to start

    @patch('berry.controllers.volume.run_async')
    def test_toggle_calls_set_system_volume(self, mock_run_async):
        api = FakeAPI()
        vc = VolumeController(api)
        vc.toggle()
        mock_run_async.assert_called()
        args = mock_run_async.call_args[0]
        assert args[0] == set_system_volume


class TestEnsureSpotifyAt100:
    """Tests for first-play volume initialization."""

    def test_sets_volume_on_first_call(self):
        api = FakeAPI()
        vc = VolumeController(api)
        result = vc.ensure_spotify_at_100()
        assert result is True
        assert api.volume_calls == [100]

    def test_noop_on_second_call(self):
        api = FakeAPI()
        vc = VolumeController(api)
        vc.ensure_spotify_at_100()
        result = vc.ensure_spotify_at_100()
        assert result is False
        assert len(api.volume_calls) == 1

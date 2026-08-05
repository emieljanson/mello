"""
Tests for VolumeController — one continuous level, always ALSA-controlled.

The percentage is what the user sets; it is deliberately NOT an ALSA value.
0% maps to VOLUME_FLOOR (the quietest still-audible output on this hardware)
and 100% to the ceiling from settings, so the slider always spans its whole
travel and the parental cap stays invisible to whoever's turning it up.
"""
import pytest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.controllers.volume import VolumeController
from mello.utils import set_system_volume
from mello.config import VOLUME_FLOOR, VOLUME_ICONS, VOLUME_STEP_PCT


class FakeSettings:
    def __init__(self, pct=60, maxima=None):
        self.volume_pct = pct
        self._maxima = maxima or {'speaker': 98, 'bt': 65}
        self.saved = []

    def get_max_volume(self, output_type):
        return self._maxima[output_type]

    def set_volume_pct(self, pct):
        self.volume_pct = pct
        self.saved.append(pct)


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

    def set_repeat_context(self, enabled):
        return True

    def is_connected(self):
        return True


def _make_controller(api=None, settings=None):
    return VolumeController(api or FakeAPI(), settings or FakeSettings())


class TestLevelMapping:
    """The whole point: percentage -> the output's usable band."""

    def test_zero_percent_is_the_floor_not_silence(self):
        """Silence would look like a broken speaker; the floor is audible."""
        vc = _make_controller(settings=FakeSettings(pct=0))
        assert vc.speaker_level == VOLUME_FLOOR['speaker']
        assert vc.bt_level == VOLUME_FLOOR['bt']

    def test_hundred_percent_is_the_ceiling(self):
        vc = _make_controller(settings=FakeSettings(pct=100))
        assert vc.speaker_level == 98
        assert vc.bt_level == 65

    def test_fifty_percent_is_the_midpoint(self):
        vc = _make_controller(settings=FakeSettings(pct=50))
        expected = round(VOLUME_FLOOR['speaker'] + (98 - VOLUME_FLOOR['speaker']) * 0.5)
        assert vc.speaker_level == expected

    def test_a_lower_ceiling_compresses_the_whole_range(self):
        """The cap is invisible: 100% still means 100%, just quieter."""
        quiet = _make_controller(settings=FakeSettings(pct=100, maxima={'speaker': 92, 'bt': 30}))
        assert quiet.speaker_level == 92
        assert quiet.bt_level == 30

    def test_never_exceeds_the_ceiling_at_any_percentage(self):
        for pct in range(0, 101):
            vc = _make_controller(settings=FakeSettings(pct=pct, maxima={'speaker': 90, 'bt': 40}))
            assert VOLUME_FLOOR['speaker'] <= vc.speaker_level <= 90
            assert VOLUME_FLOOR['bt'] <= vc.bt_level <= 40

    def test_ceiling_below_the_floor_does_not_invert(self):
        """A ceiling under the floor must not make 0% louder than 100%."""
        vc = _make_controller(settings=FakeSettings(pct=100, maxima={'speaker': 10, 'bt': 1}))
        assert vc.speaker_level == VOLUME_FLOOR['speaker']

    def test_level_rises_monotonically(self):
        settings = FakeSettings(pct=0)
        vc = _make_controller(settings=settings)
        levels = []
        for pct in range(0, 101, VOLUME_STEP_PCT):
            settings.volume_pct = pct
            levels.append(vc.speaker_level)
        assert levels == sorted(levels)


class TestSetPct:
    """Setting the level from the slider."""

    @patch('mello.controllers.volume.run_async')
    def test_snaps_to_the_step(self, _run_async):
        vc = _make_controller(settings=FakeSettings(pct=0))
        assert vc.set_pct(42) == 40
        assert vc.set_pct(43) == 45

    @patch('mello.controllers.volume.run_async')
    def test_clamps_out_of_range_touches(self, _run_async):
        """A drag past either end of the track must not wrap or crash."""
        vc = _make_controller(settings=FakeSettings(pct=50))
        assert vc.set_pct(-30) == 0
        assert vc.set_pct(180) == 100

    @patch('mello.controllers.volume.run_async')
    def test_persists_so_it_survives_a_restart(self, _run_async):
        settings = FakeSettings(pct=50)
        vc = _make_controller(settings=settings)
        vc.set_pct(75)
        assert settings.saved == [75]
        assert settings.volume_pct == 75

    @patch('mello.controllers.volume.run_async')
    def test_same_step_does_no_work(self, run_async):
        """Dragging within one step must not spam ALSA on every motion event."""
        settings = FakeSettings(pct=60)
        vc = _make_controller(settings=settings)
        assert vc.set_pct(61) == 60
        assert settings.saved == []
        run_async.assert_not_called()

    @patch('mello.controllers.volume.run_async')
    def test_applies_through_set_system_volume(self, run_async):
        vc = _make_controller(settings=FakeSettings(pct=0))
        vc.set_pct(80)
        assert run_async.call_args[0][0] == set_system_volume

    @patch('mello.controllers.volume.run_async')
    def test_nudge_moves_relative(self, _run_async):
        vc = _make_controller(settings=FakeSettings(pct=50))
        assert vc.nudge(10) == 60
        assert vc.nudge(-25) == 35


class TestIcon:
    """The button icon follows the level, not a preset index."""

    @pytest.mark.parametrize('pct, expected', [
        (0, VOLUME_ICONS[0]),
        (10, VOLUME_ICONS[0]),
        (50, VOLUME_ICONS[1]),
        (100, VOLUME_ICONS[-1]),
    ])
    def test_icon_tracks_the_level(self, pct, expected):
        vc = _make_controller(settings=FakeSettings(pct=pct))
        assert vc.icon == expected

    def test_every_level_has_an_icon(self):
        for pct in range(0, 101):
            vc = _make_controller(settings=FakeSettings(pct=pct))
            assert vc.icon in VOLUME_ICONS


class TestVolumeInit:

    @patch('mello.controllers.volume.set_system_volume')
    @patch('mello.controllers.volume.unmute_speakers')
    def test_init_sets_system_volume(self, mock_unmute, mock_set_vol):
        vc = _make_controller()
        vc.init()
        mock_set_vol.assert_called_once_with(vc.speaker_level)
        mock_unmute.assert_called_once_with(vc.speaker_level)


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

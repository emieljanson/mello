"""
Tests for Settings - persistent user-configurable values.
"""
import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.managers.settings import (Settings, DEFAULT_AUTO_PAUSE_MINUTES,
                                     DEFAULT_PROGRESS_EXPIRY_HOURS,
                                     QUIET_START_OPTIONS, QUIET_END_OPTIONS)
from mello.config import (VOLUME_ADJUST_STEP, VOLUME_RANGE, VOLUME_FLOOR,
                          DEFAULT_MAX_VOLUME)


@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / 'settings.json'


class TestSettingsDefaults:
    def test_defaults_when_no_file(self, settings_path):
        s = Settings(path=settings_path)
        assert s.auto_pause_minutes == DEFAULT_AUTO_PAUSE_MINUTES
        assert s.progress_expiry_hours == DEFAULT_PROGRESS_EXPIRY_HOURS

    def test_auto_pause_timeout_in_seconds(self, settings_path):
        s = Settings(path=settings_path)
        assert s.auto_pause_timeout == DEFAULT_AUTO_PAUSE_MINUTES * 60


class TestSettingsPersistence:
    def test_cycle_auto_pause_persists(self, settings_path):
        s = Settings(path=settings_path)
        new_val = s.cycle_auto_pause()
        assert new_val != DEFAULT_AUTO_PAUSE_MINUTES

        s2 = Settings(path=settings_path)
        assert s2.auto_pause_minutes == new_val

    def test_cycle_progress_expiry_persists(self, settings_path):
        s = Settings(path=settings_path)
        new_val = s.cycle_progress_expiry()
        assert new_val != DEFAULT_PROGRESS_EXPIRY_HOURS

        s2 = Settings(path=settings_path)
        assert s2.progress_expiry_hours == new_val

    def test_full_cycle_wraps_around(self, settings_path):
        s = Settings(path=settings_path)
        first = s.auto_pause_minutes
        from mello.managers.settings import AUTO_PAUSE_OPTIONS
        for _ in range(len(AUTO_PAUSE_OPTIONS)):
            s.cycle_auto_pause()
        assert s.auto_pause_minutes == first


class TestShareUsageData:
    def test_default_is_true(self, settings_path):
        s = Settings(path=settings_path)
        assert s.share_usage_data is True

    def test_loads_false_from_file(self, settings_path):
        settings_path.write_text(json.dumps({'share_usage_data': False}))
        s = Settings(path=settings_path)
        assert s.share_usage_data is False

    def test_loads_true_from_file(self, settings_path):
        settings_path.write_text(json.dumps({'share_usage_data': True}))
        s = Settings(path=settings_path)
        assert s.share_usage_data is True

    def test_persisted_on_save(self, settings_path):
        settings_path.write_text(json.dumps({'share_usage_data': False}))
        s = Settings(path=settings_path)
        # Trigger a save via another setting change
        s.cycle_auto_pause()
        data = json.loads(settings_path.read_text())
        assert data['share_usage_data'] is False

    def test_missing_key_defaults_true(self, settings_path):
        settings_path.write_text(json.dumps({'auto_pause_minutes': 60}))
        s = Settings(path=settings_path)
        assert s.share_usage_data is True


class TestSettingsCorruption:
    def test_corrupted_file_uses_defaults(self, settings_path):
        settings_path.write_text('not json')
        s = Settings(path=settings_path)
        assert s.auto_pause_minutes == DEFAULT_AUTO_PAUSE_MINUTES

    def test_partial_file_uses_available(self, settings_path):
        settings_path.write_text(json.dumps({'auto_pause_minutes': 60}))
        s = Settings(path=settings_path)
        assert s.auto_pause_minutes == 60
        assert s.progress_expiry_hours == DEFAULT_PROGRESS_EXPIRY_HOURS


class TestQuietHoursSettings:
    def test_defaults_to_off(self, settings_path):
        s = Settings(path=settings_path)
        assert s.quiet_hours == (None, 7 * 60)
        assert s.quiet_start_label == 'Off'
        assert s.quiet_end_label == '07:00'

    def test_cycle_start_wraps_through_off(self, settings_path):
        s = Settings(path=settings_path)
        seen = [s.cycle_quiet_start() for _ in range(len(QUIET_START_OPTIONS))]
        assert seen[-1] is None            # cycles back to Off
        assert 19 * 60 + 30 in seen

    def test_cycle_end_wraps(self, settings_path):
        s = Settings(path=settings_path)
        start = s.quiet_hours[1]
        seen = [s.cycle_quiet_end() for _ in range(len(QUIET_END_OPTIONS))]
        assert seen[-1] == start                    # a full cycle returns home
        assert sorted(seen) == sorted(QUIET_END_OPTIONS)  # and visits every option

    def test_persisted_across_reload(self, settings_path):
        s = Settings(path=settings_path)
        s.cycle_quiet_start()
        s.cycle_quiet_end()
        expected = s.quiet_hours
        assert Settings(path=settings_path).quiet_hours == expected

    def test_unknown_stored_value_recovers(self, settings_path):
        settings_path.write_text(json.dumps({'quiet_hours_start': 12345}))
        s = Settings(path=settings_path)
        assert s.cycle_quiet_start() == QUIET_START_OPTIONS[1]


class TestMaxVolume:
    def test_one_tap_moves_a_full_step(self, settings_path):
        s = Settings(path=settings_path)
        before = s.get_max_volume('speaker')
        assert s.adjust_max_volume('speaker', -1) == before - VOLUME_ADJUST_STEP

    def test_clamped_between_floor_and_range_top(self, settings_path):
        """The ceiling can never sink below the floor, or 0% would beat 100%."""
        s = Settings(path=settings_path)
        _, hi = VOLUME_RANGE['speaker']
        for _ in range(50):
            s.adjust_max_volume('speaker', -1)
        assert s.get_max_volume('speaker') == VOLUME_FLOOR['speaker']
        for _ in range(50):
            s.adjust_max_volume('speaker', 1)
        assert s.get_max_volume('speaker') == hi

    def test_bt_and_speaker_are_independent(self, settings_path):
        s = Settings(path=settings_path)
        bt_before = s.get_max_volume('bt')
        s.adjust_max_volume('speaker', -1)
        assert s.get_max_volume('bt') == bt_before

    def test_unknown_output_is_ignored(self, settings_path):
        assert Settings(path=settings_path).adjust_max_volume('kazoo', 1) == 0

    def test_survives_a_reload(self, settings_path):
        s = Settings(path=settings_path)
        s.adjust_max_volume('speaker', -1)
        expected = s.get_max_volume('speaker')
        assert Settings(path=settings_path).get_max_volume('speaker') == expected

    def test_reset_restores_defaults(self, settings_path):
        s = Settings(path=settings_path)
        s.adjust_max_volume('speaker', -1)
        s.reset_volume_levels()
        assert s.get_max_volume('speaker') == DEFAULT_MAX_VOLUME['speaker']


class TestVolumePct:
    def test_defaults_to_a_sane_level(self, settings_path):
        assert 0 < Settings(path=settings_path).volume_pct < 100

    def test_persists(self, settings_path):
        Settings(path=settings_path).set_volume_pct(35)
        assert Settings(path=settings_path).volume_pct == 35

    @pytest.mark.parametrize('given, expected', [(-10, 0), (0, 0), (100, 100), (150, 100)])
    def test_clamped(self, settings_path, given, expected):
        s = Settings(path=settings_path)
        s.set_volume_pct(given)
        assert s.volume_pct == expected

    def test_garbage_on_disk_recovers(self, settings_path):
        settings_path.write_text(json.dumps({'volume_pct': 900}))
        assert Settings(path=settings_path).volume_pct == 100


class TestVolumeMigration:
    """The old three presets become one ceiling.

    Someone who tuned their presets down for a bedroom must not have the device
    get louder just because it updated.
    """

    def test_loudest_preset_becomes_the_ceiling(self, settings_path):
        settings_path.write_text(json.dumps({'volume_levels': [
            {'speaker': 70, 'bt': 10},
            {'speaker': 80, 'bt': 20},
            {'speaker': 90, 'bt': 30},
        ]}))
        s = Settings(path=settings_path)
        assert s.get_max_volume('speaker') == 90
        assert s.get_max_volume('bt') == 30

    def test_new_format_wins_over_the_old_one(self, settings_path):
        settings_path.write_text(json.dumps({
            'volume_levels': [{'speaker': 70, 'bt': 10}],
            'max_volume': {'speaker': 95, 'bt': 50},
        }))
        assert Settings(path=settings_path).get_max_volume('speaker') == 95

    def test_no_stored_volume_uses_defaults(self, settings_path):
        settings_path.write_text(json.dumps({'auto_pause_minutes': 30}))
        s = Settings(path=settings_path)
        assert s.get_max_volume('speaker') == DEFAULT_MAX_VOLUME['speaker']

    @pytest.mark.parametrize('legacy', [
        [], 'nonsense', {'speaker': 90}, [{'speaker': None}], [{}], [None],
    ])
    def test_malformed_legacy_data_falls_back(self, settings_path, legacy):
        settings_path.write_text(json.dumps({'volume_levels': legacy}))
        s = Settings(path=settings_path)
        assert s.get_max_volume('speaker') == DEFAULT_MAX_VOLUME['speaker']

    def test_junk_keys_in_new_format_are_dropped(self, settings_path):
        settings_path.write_text(json.dumps(
            {'max_volume': {'speaker': 92, 'kazoo': 11, 'bt': 'loud'}}))
        s = Settings(path=settings_path)
        assert s.get_max_volume('speaker') == 92
        assert s.get_max_volume('bt') == DEFAULT_MAX_VOLUME['bt']

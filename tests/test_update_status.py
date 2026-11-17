"""The settings menu says so when a speaker has not updated for a while."""
from mello.update_status import FAILING_FOR, SILENT_FOR, update_problem

NOW = 1_800_000_000.0


def _write(tmp_path, **fields):
    path = tmp_path / 'status'
    path.write_text(''.join(f'{k}={v}\n' for k, v in fields.items()))
    return path


def test_nothing_written_yet(tmp_path):
    assert update_problem(tmp_path / 'missing', NOW) is None


def test_last_night_went_fine(tmp_path):
    assert update_problem(_write(tmp_path, time=NOW - 3600, result='ok', message='', last_ok=NOW - 3600), NOW) is None


def test_one_bad_night_is_not_mentioned(tmp_path):
    path = _write(tmp_path, time=NOW, result='no-internet', message='x', last_ok=NOW - 3600 * 24)
    assert update_problem(path, NOW) is None


def test_no_internet_for_days(tmp_path):
    path = _write(tmp_path, time=NOW, result='no-internet', message='x', last_ok=NOW - FAILING_FOR - 1)
    assert update_problem(path, NOW) == 'Update failed: no internet'


def test_never_worked(tmp_path):
    path = _write(tmp_path, time=NOW, result='failed', message='rolled back', last_ok='')
    assert update_problem(path, NOW) == 'Update failed'


def test_nightly_run_stopped_running(tmp_path):
    path = _write(tmp_path, time=NOW - SILENT_FOR - 1, result='ok', message='', last_ok=NOW - SILENT_FOR - 1)
    assert update_problem(path, NOW) == 'Updates not running'

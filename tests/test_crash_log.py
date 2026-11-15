"""A crash, a hang or a pulled plug leaves a trace in the log."""
import logging
import threading
import time

import pytest

from mello import crash_log


@pytest.fixture(autouse=True)
def restore_hooks():
    import sys
    saved = sys.excepthook, threading.excepthook
    yield
    sys.excepthook, threading.excepthook = saved
    import faulthandler
    faulthandler.cancel_dump_traceback_later()
    if crash_log._crash_stream is not None:
        faulthandler.disable()
        crash_log._crash_stream.close()
        crash_log._crash_stream = None
    crash_log._last_beat = 0.0
    crash_log._stopping = False


def messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_clean_stop_leaves_nothing_to_report(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    crash_log.report_previous_run(tmp_path)
    crash_log.mark_clean_exit(tmp_path)
    caplog.clear()
    crash_log.report_previous_run(tmp_path)
    assert not any('did not shut down' in m for m in messages(caplog))


def test_app_that_died_is_reported(tmp_path, caplog):
    crash_log.report_previous_run(tmp_path, boot_time=lambda: 0.0)
    caplog.clear()
    crash_log.report_previous_run(tmp_path, boot_time=lambda: 0.0)
    assert any('the app crashed or was killed' in m for m in messages(caplog))


def test_pulled_plug_is_reported_as_power_loss(tmp_path, caplog):
    crash_log.report_previous_run(tmp_path)
    caplog.clear()
    crash_log.report_previous_run(tmp_path, boot_time=lambda: time.time() + 5)
    assert any('lost power or rebooted' in m for m in messages(caplog))


def test_hard_crash_dump_is_copied_into_the_log(tmp_path, caplog):
    (tmp_path / 'crash.log').write_text(
        '--- Mello started 2026-11-14 08:00:00\n'
        '--- Mello started 2026-11-15 08:00:00\n'
        'Fatal Python error: Segmentation fault\n'
        '  File "mello/app.py", line 920 in start\n')
    crash_log.report_previous_run(tmp_path)
    text = '\n'.join(messages(caplog))
    assert 'Segmentation fault' in text and 'line 920' in text


def test_nothing_reported_when_crash_log_has_only_start_lines(tmp_path, caplog):
    (tmp_path / 'crash.log').write_text('--- Mello started 2026-11-15 08:00:00\n')
    crash_log.report_previous_run(tmp_path)
    assert not any('crash.log' in m for m in messages(caplog))


def test_uncaught_error_in_a_thread_goes_to_the_log(tmp_path, caplog):
    crash_log.install(tmp_path)

    def boom():
        raise ValueError('kapot')

    t = threading.Thread(target=boom, name='status-poll')
    t.start()
    t.join()
    rec = [r for r in caplog.records if 'thread status-poll' in r.getMessage()]
    assert rec and rec[0].exc_info[0] is ValueError


def test_frozen_main_loop_dumps_stacks_to_crash_log(tmp_path, monkeypatch):
    monkeypatch.setattr(crash_log, 'HANG_AFTER', 0.2)
    crash_log.install(tmp_path)
    crash_log.heartbeat()
    time.sleep(0.6)       # no heartbeat: the "screen" is frozen
    text = (tmp_path / 'crash.log').read_text()
    assert 'Thread' in text or 'Stack' in text


def test_stop_that_hangs_dumps_stacks_before_systemd_kills_it(tmp_path, monkeypatch):
    monkeypatch.setattr(crash_log, 'STOP_STUCK_AFTER', 0.2)
    crash_log.install(tmp_path)
    crash_log.heartbeat()
    crash_log.stop_requested()
    crash_log.heartbeat(now=10_000)   # must not push the dump further away
    time.sleep(0.6)
    text = (tmp_path / 'crash.log').read_text()
    assert 'Thread' in text or 'Stack' in text

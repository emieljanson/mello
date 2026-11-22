"""When Slint cannot start: what the screen says, and the repair the app starts."""
import logging

from mello import main as m

logger = logging.getLogger('test')


def test_only_an_old_system_is_told_to_reinstall():
    assert 'newer Raspberry Pi OS' in m.screen_error_lines(too_old=True)[0]
    current = ' '.join(m.screen_error_lines(too_old=False))
    assert 'Raspberry Pi OS' not in current and 'Reinstall' not in current
    assert 'repairing' in current


def test_repair_starts_once_and_not_again_right_away(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'SCREEN_REPAIR_FILE', tmp_path / '.mello-screen-repair')
    started = []
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda cmd, **kw: started.append(cmd))
    assert m._start_screen_repair(logger) is True
    assert any('auto-update.sh' in part for part in started[0])
    assert m._start_screen_repair(logger) is False      # restarted app: not again
    assert len(started) == 1
    (tmp_path / '.mello-screen-repair').write_text('0\n')   # hours later: again
    assert m._start_screen_repair(logger) is True


def _wait(monkeypatch, tmp_path, too_old):
    shown, repairs = [], []
    monkeypatch.setattr(m, 'SCREEN_ERROR_FILE', tmp_path / '.mello-screen-error')
    monkeypatch.setattr(m, '_system_too_old', lambda: too_old)
    monkeypatch.setattr(m, '_report_screen_error', lambda *a: None)
    monkeypatch.setattr(m, '_start_screen_repair', lambda lg: repairs.append(1))
    from mello.ui import screen_error
    monkeypatch.setattr(screen_error, 'show', lambda lines: shown.append(lines) or True)
    import signal
    # Stop the wait loop at once: the first sleep raises.
    import time

    def stop(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(time, 'sleep', stop)
    monkeypatch.setattr(signal, 'signal', lambda *a: None)
    try:
        m._wait_with_screen_error(logger, RuntimeError('libinput.so.10: cannot open shared object file'))
    except KeyboardInterrupt:
        pass
    return shown, repairs


def test_current_system_says_repairing_and_starts_the_repair(tmp_path, monkeypatch):
    shown, repairs = _wait(monkeypatch, tmp_path, too_old=False)
    assert 'repairing' in shown[0][0]
    assert repairs == [1]
    assert 'libinput.so.10' in (tmp_path / '.mello-screen-error').read_text()


def test_old_system_asks_for_a_reinstall_and_starts_no_repair(tmp_path, monkeypatch):
    shown, repairs = _wait(monkeypatch, tmp_path, too_old=True)
    assert 'newer Raspberry Pi OS' in shown[0][0]
    assert repairs == []

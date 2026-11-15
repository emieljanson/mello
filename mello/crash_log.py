"""
Crash log - makes sure a crash or an unclean stop leaves a trace in the log.

Without this an error nobody caught (in the main loop or in one of the
background threads) only went to systemd's stdout file, without a time and
outside logs/mello.log, and a hard crash (segfault in SDL) or the speaker
being unplugged left nothing at all. Then "Mello did not work" could not be
traced back afterwards.

- Uncaught errors in the main thread and in threads go to the normal log.
- A hard crash, or a main loop that hangs for 30 s, dumps every thread's stack into logs/crash.log.
- At startup the log says whether the previous run stopped cleanly, and if
  not, whether the speaker lost power / rebooted or the app itself died.
"""
import faulthandler
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_MARKER = 'running'
_START_LINE = '--- Mello started'
_CRASH_FILE = 'crash.log'
_CRASH_TAIL = 40   # lines of crash.log copied into the main log
_CRASH_MAX_BYTES = 1_000_000
HANG_AFTER = 30.0   # main loop silent this long: dump every thread's stack
_REARM_EVERY = 5.0
_crash_stream = None
_last_beat = 0.0


def _boot_time() -> Optional[float]:
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
    except Exception:
        return None
    return time.time() - uptime


def report_previous_run(log_dir: Path, boot_time: Callable[[], Optional[float]] = _boot_time):
    """Log how the previous run ended, then mark this run as running."""
    marker = log_dir / _MARKER
    crash = log_dir / _CRASH_FILE
    if marker.exists():
        started = marker.stat().st_mtime
        started_text = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))
        booted = boot_time()
        if booted is not None and booted > started:
            logger.warning(f'Previous run (started {started_text}) did not shut down: '
                           f'the speaker lost power or rebooted while Mello ran')
        else:
            logger.warning(f'Previous run (started {started_text}) did not shut down: '
                           f'the app crashed or was killed')
    # Anything after the last start line is what faulthandler wrote during
    # the previous run: a hard crash, or a hang (even one it recovered from).
    if crash.exists():
        lines = crash.read_text(errors='replace').splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith(_START_LINE)]
        dump = lines[starts[-1] + 1:] if starts else lines
        if dump:
            logger.warning('Previous run wrote to crash.log:\n' + '\n'.join(dump[-_CRASH_TAIL:]))
    try:
        marker.write_text(f'{time.time():.0f}\n')
    except OSError as e:
        logger.debug(f'Could not write run marker: {e}')


def heartbeat(now: Optional[float] = None):
    """Call once per main-loop pass. If it stops coming for HANG_AFTER
    seconds (a frozen screen), faulthandler writes where every thread is
    stuck into crash.log, and the next start copies that into the log."""
    global _last_beat
    now = time.monotonic() if now is None else now
    if _crash_stream is None or now - _last_beat < _REARM_EVERY:
        return
    _last_beat = now
    faulthandler.dump_traceback_later(HANG_AFTER, repeat=False, file=_crash_stream)


def mark_clean_exit(log_dir: Path):
    """The app stopped on purpose: the next start has nothing to report."""
    if _crash_stream is not None:
        faulthandler.cancel_dump_traceback_later()
    try:
        (log_dir / _MARKER).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug(f'Could not remove run marker: {e}')


def _log_uncaught(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logger.critical('Uncaught error, Mello stops', exc_info=(exc_type, exc, tb))


def _log_uncaught_in_thread(args: threading.ExceptHookArgs):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else '?'
    logger.error(f'Uncaught error in thread {name}, the thread stopped',
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def install(log_dir: Path):
    """Route crashes into the logs. Call once, after logging is set up."""
    global _crash_stream
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_in_thread
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    report_previous_run(log_dir)    # before crash.log is written to again
    crash = log_dir / _CRASH_FILE
    try:
        if crash.exists() and crash.stat().st_size > _CRASH_MAX_BYTES:
            crash.replace(log_dir / (_CRASH_FILE + '.1'))
        _crash_stream = open(crash, 'a', buffering=1)
        _crash_stream.write(f'{_START_LINE} {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        faulthandler.enable(file=_crash_stream, all_threads=True)
    except OSError as e:
        faulthandler.enable(all_threads=True)    # stderr: systemd's mello.log
        logger.debug(f'crash.log not available, hard crashes go to stderr: {e}')

#!/usr/bin/env python3
"""
Mello - music player for Raspberry Pi (screen drawn with Slint)

Usage:
    python -m mello              # Windowed (development)
    python -m mello --fullscreen # Fullscreen (Pi)
    python -m mello --mock       # Mock mode (UI testing)
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import (
    SCREEN_WIDTH, SCREEN_HEIGHT, 
    SOLOIST_WS, SPOTIFY_ENGINE, MOCK_MODE, FULLSCREEN,
    LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT,
)
from .app import Mello, ScreenUnavailable
from . import crash_log


def setup_logging():
    """Configure logging with console and rotating file handler."""
    # Determine log level from environment or default to INFO
    level_name = os.environ.get('MELLO_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    
    # Create formatters
    console_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    file_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    root = logging.getLogger()
    root.setLevel(level)

    # File handler with rotation (only on Pi / when LOG_DIR is writable)
    file_handler = None
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)
    except (OSError, PermissionError) as e:
        print(f'Could not create log file: {e}', file=sys.stderr)

    # Console output only for a person at a terminal. As a service, stdout is
    # appended to ~/mello/mello.log by systemd: logging there as well wrote
    # every line to the SD card twice, into a file that is never trimmed
    # while the app runs. Crashes still reach that file through stderr.
    if file_handler is None or sys.stdout.isatty():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(console_formatter)
        console.setLevel(level)
        root.addHandler(console)

    if file_handler is not None:
        root.info(f'Logging to: {LOG_FILE}')

    # Quiet down noisy libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('websocket').setLevel(logging.WARNING)


def log_system_info(logger: logging.Logger):
    """Log system information at startup."""
    logger.info('=' * 50)
    logger.info('MELLO STARTUP')
    logger.info('=' * 50)
    
    # Python version
    logger.info(f'Python: {sys.version.split()[0]}')
    
    # Platform
    import platform
    logger.info(f'Platform: {platform.system()} {platform.release()}')
    
    # Raspberry Pi model
    pi_model_path = Path('/proc/device-tree/model')
    if pi_model_path.exists():
        try:
            pi_model = pi_model_path.read_text().strip().replace('\x00', '')
            logger.info(f'Device: {pi_model}')
        except Exception:
            pass
    
    # Memory info
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = f.read()
            for line in meminfo.split('\n'):
                if line.startswith('MemTotal:'):
                    total = int(line.split()[1]) // 1024  # MB
                    logger.info(f'Memory: {total} MB total')
                elif line.startswith('MemAvailable:'):
                    available = int(line.split()[1]) // 1024  # MB
                    logger.info(f'Memory: {available} MB available')
    except Exception:
        pass
    
    # CPU temperature (Raspberry Pi)
    temp_path = Path('/sys/class/thermal/thermal_zone0/temp')
    if temp_path.exists():
        try:
            temp = int(temp_path.read_text().strip()) / 1000
            logger.info(f'CPU temp: {temp:.1f}°C')
        except Exception:
            pass
    
    # Disk space
    try:
        import shutil
        total, used, free = shutil.disk_usage('/')
        logger.info(f'Disk: {free // (1024**3)} GB free of {total // (1024**3)} GB')
    except Exception:
        pass
    
    # Uptime
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            logger.info(f'Uptime: {hours}h {minutes}m')
    except Exception:
        pass
    
    logger.info('=' * 50)


def main():
    """Entry point for Mello application."""
    setup_logging()
    
    logger = logging.getLogger(__name__)
    crash_log.install(LOG_DIR)
    
    # Log system info first
    log_system_info(logger)
    
    logger.info('Mello')
    if MOCK_MODE:
        logger.info('Mode: MOCK (UI testing)')
    else:
        logger.info(f'Spotify engine: {SPOTIFY_ENGINE} ({SOLOIST_WS})')
    logger.info(f'Screen: {SCREEN_WIDTH}x{SCREEN_HEIGHT}')
    logger.info(f'Fullscreen: {FULLSCREEN}')
    
    print()
    print('Controls:')
    print('   ← →     Navigate carousel')
    print('   Space   Play/Pause')
    print('   N       Next track')
    print('   P       Previous track')
    print('   D       Toggle frame profiler')
    print('   Esc     Quit')
    print()
    
    try:
        app = Mello(fullscreen=FULLSCREEN)
    except ScreenUnavailable as e:
        _wait_with_screen_error(logger, e)
        return
    SCREEN_ERROR_FILE.unlink(missing_ok=True)
    app.start()


# While Slint cannot start, this file says why. The update script reads it:
# its health check fails on it, and a run with no new version repairs the
# speaker (pi/auto-update.sh catch_up, pi/migrate.sh ensure_screen_libs).
SCREEN_ERROR_FILE = Path.home() / '.mello-screen-error'
# When the app last started such a repair itself, so it is not started again
# on every restart.
SCREEN_REPAIR_FILE = Path.home() / '.mello-screen-repair'
SCREEN_REPAIR_EVERY = 6 * 3600


def _system_too_old() -> bool:
    """Slint's wheels need Python 3.12+ (Raspberry Pi OS Trixie or newer)."""
    return sys.version_info < (3, 12)


def screen_error_lines(too_old: bool) -> list:
    """What the screen says. Only an old system needs a reinstall: on a
    current one the cause is something that can be fixed in place (missing
    screen libraries, an update that stopped halfway), and Mello does that."""
    if too_old:
        return [
            'This speaker needs a newer Raspberry Pi OS',
            'Reinstall it with the current Raspberry Pi OS Lite',
            'and set up Mello again',
        ]
    return [
        'Mello is repairing its screen',
        'This takes a few minutes',
        'Keep the speaker on and connected to WiFi',
    ]


def _start_screen_repair(logger: logging.Logger) -> bool:
    """Run the update script now: with SCREEN_ERROR_FILE there it installs
    what is missing and restarts the app. At most once every few hours."""
    import time
    try:
        last = float(SCREEN_REPAIR_FILE.read_text().strip())
    except (OSError, ValueError):
        last = 0.0
    if time.time() - last < SCREEN_REPAIR_EVERY:
        logger.info('Screen repair started recently, not starting it again')
        return False
    try:
        import subprocess
        from .managers.setup_menu import _update_command
        subprocess.Popen(_update_command(Path.home() / 'mello-update.log'),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        SCREEN_REPAIR_FILE.write_text(f'{time.time():.0f}\n')
        logger.info('Screen repair started (update script)')
        return True
    except Exception as e:
        logger.error(f'Could not start the screen repair: {e}')
        return False


def _report_screen_error(logger: logging.Logger, error: Exception):
    """Count speakers whose screen cannot start (when usage data is shared)."""
    try:
        from .config import (POSTHOG_API_KEY, POSTHOG_HOST, ANALYTICS_DISTINCT_ID,
                             ANALYTICS_INCLUDE_CONTENT, ANALYTICS_USE_MACHINE_ID)
        from .managers import Settings, UsageTracker
        from .utils import get_runtime_version_label
        tracker = UsageTracker(
            api_key=POSTHOG_API_KEY if Settings().share_usage_data else '',
            host=POSTHOG_HOST,
            distinct_id=ANALYTICS_DISTINCT_ID,
            include_content=ANALYTICS_INCLUDE_CONTENT,
            use_machine_id=ANALYTICS_USE_MACHINE_ID,
            app_version=get_runtime_version_label(),
            engine=SPOTIFY_ENGINE,
            ui='none',
        )
        tracker.on_slint_fallback(f'{type(error.__cause__ or error).__name__}: {error}'[:200])
        tracker.flush()
    except Exception as e:
        logger.warning(f'Could not report the screen error: {e}')


def _wait_with_screen_error(logger: logging.Logger, error: Exception):
    """Slint cannot run here: say so on the screen and in the log, then wait.

    Exiting would only have systemd start Mello again every few seconds and
    fill the log with the same error.
    """
    import platform
    import signal
    import time
    from .ui import screen_error
    too_old = _system_too_old()
    system = f'Python {platform.python_version()}, {platform.machine()}, glibc {platform.libc_ver()[1]}'
    if too_old:
        advice = ('Slint needs Raspberry Pi OS Trixie or newer (Python 3.12+, glibc 2.35+). '
                  'Reinstall the speaker with the current Raspberry Pi OS.')
    else:
        advice = ('The system is new enough, so something is missing (for example the screen '
                  'library libinput10): the update script repairs it.')
    logger.error(f'SCREEN UNAVAILABLE | Slint cannot start: {error} | {system} | {advice}')
    try:
        SCREEN_ERROR_FILE.write_text(f'{error} ({system})\n'[:500])
    except OSError as e:
        logger.warning(f'Could not write {SCREEN_ERROR_FILE}: {e}')
    shown = screen_error.show(screen_error_lines(too_old))
    logger.info(f'Screen error message shown: {shown}')
    _report_screen_error(logger, error)
    if not too_old:
        _start_screen_repair(logger)
    stop = {'now': False}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.update(now=True))
    while not stop['now']:
        time.sleep(1)
    crash_log.stop_requested()


if __name__ == '__main__':
    main()

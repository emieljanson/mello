#!/usr/bin/env python3
"""
Mello - Pygame UI for Raspberry Pi

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
    LIBRESPOT_URL, SOLOIST_WS, SPOTIFY_ENGINE, MOCK_MODE, FULLSCREEN,
    LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT,
)
from .app import Mello


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
    
    # Log system info first
    log_system_info(logger)
    
    logger.info('Mello')
    if MOCK_MODE:
        logger.info('Mode: MOCK (UI testing)')
    else:
        engine_url = SOLOIST_WS if SPOTIFY_ENGINE == 'soloist' else LIBRESPOT_URL
        logger.info(f'Spotify engine: {SPOTIFY_ENGINE} ({engine_url})')
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
    
    app = Mello(fullscreen=FULLSCREEN)
    app.start()


if __name__ == '__main__':
    main()

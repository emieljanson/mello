"""
Berry Utilities - Shared helper functions.
"""
import sys
import subprocess
import threading
import logging

logger = logging.getLogger(__name__)


def run_async(fn, *args):
    """Fire-and-forget async execution in daemon thread.

    Wraps function to catch and log exceptions.
    """
    def wrapper():
        try:
            fn(*args)
        except Exception as e:
            logger.warning(f'Async task {fn.__name__} failed: {e}', exc_info=True)

    threading.Thread(target=wrapper, daemon=True).start()


def get_version() -> str:
    """Get current git commit SHA, or 'dev' if not in a git repo."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            cwd=str(__import__('pathlib').Path(__file__).parent.parent)
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return 'dev'


def set_system_volume(speaker_level: int, headphone_level: int):
    """Set the Pi's ALSA system volume for speaker and headphone separately."""
    if sys.platform != 'linux':
        return
    try:
        # WM8960 Audio HAT on card 2
        # Playback = master DAC volume, keep at 100%
        # Speaker/Headphone = amplifier volumes, set independently
        subprocess.run(
            ['amixer', '-c', '2', 'set', 'Playback', '100%'],
            capture_output=True,
            check=True
        )
        subprocess.run(
            ['amixer', '-c', '2', 'set', 'Speaker', f'{speaker_level}%'],
            capture_output=True,
            check=True
        )
        subprocess.run(
            ['amixer', '-c', '2', 'set', 'Headphone', f'{headphone_level}%'],
            capture_output=True,
            check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not set system volume: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error setting system volume: {e}', exc_info=True)


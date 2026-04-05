"""
Mello Utilities - Shared helper functions.
"""
import sys
import atexit
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)
atexit.register(_executor.shutdown, wait=False)


def run_async(fn, *args):
    """Fire-and-forget async execution in a bounded thread pool.

    Wraps function to catch and log exceptions.
    Max 4 concurrent background tasks — safe for Raspberry Pi.
    """
    def wrapper():
        try:
            fn(*args)
        except Exception as e:
            logger.warning(f'Async task {fn.__name__} failed: {e}', exc_info=True)

    _executor.submit(wrapper)


def get_runtime_version_label() -> str:
    """Return a short runtime version label from git metadata.

    Format: "<branch>@<short-hash>" (example: "main@1818997").
    Falls back to "unknown" when git metadata is unavailable.
    """
    try:
        branch_result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, check=True, timeout=2,
        )
        branch = branch_result.stdout.strip() or 'unknown'
    except Exception:
        branch = 'unknown'

    try:
        hash_result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, check=True, timeout=2,
        )
        short_hash = hash_result.stdout.strip() or 'unknown'
    except Exception:
        short_hash = 'unknown'

    return f'{branch}@{short_hash}'


_wm8960_card: str | None = None


def _find_wm8960_card() -> str:
    """Find the ALSA card number for the WM8960 Audio HAT."""
    global _wm8960_card
    if _wm8960_card is not None:
        return _wm8960_card
    try:
        result = subprocess.run(
            ['aplay', '-l'], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'wm8960' in line.lower():
                _wm8960_card = line.split(':')[0].split()[-1]
                logger.info(f'WM8960 Audio HAT found on card {_wm8960_card}')
                return _wm8960_card
    except Exception:
        pass
    _wm8960_card = '2'
    logger.warning('WM8960 not found in aplay -l, falling back to card 2')
    return _wm8960_card


def set_system_volume(speaker_level: int):
    """Set the Pi's ALSA system volume for the speaker."""
    if sys.platform != 'linux':
        return
    try:
        card = _find_wm8960_card()
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Playback', '100%'],
            capture_output=True, check=True
        )
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Speaker', f'{speaker_level}%'],
            capture_output=True, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not set system volume: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error setting system volume: {e}', exc_info=True)


def mute_speakers():
    """Silence speaker by setting volume to 0% (WM8960 has no mute switch)."""
    if sys.platform != 'linux':
        return
    try:
        card = _find_wm8960_card()
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Speaker', '0%'],
            capture_output=True, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not mute speakers: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error muting speakers: {e}', exc_info=True)


def unmute_speakers(speaker_level: int):
    """Restore speaker to given volume level."""
    if sys.platform != 'linux':
        return
    try:
        card = _find_wm8960_card()
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Speaker', f'{speaker_level}%'],
            capture_output=True, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not unmute speakers: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error unmuting speakers: {e}', exc_info=True)


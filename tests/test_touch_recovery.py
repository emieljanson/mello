import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_touch_fix_wakes_display_and_rebinds_goodix(tmp_path):
    driver = tmp_path / 'Goodix-TS'
    device = driver / '10-005d'
    device.mkdir(parents=True)
    bind = driver / 'bind'
    unbind = driver / 'unbind'
    bind.touch()
    unbind.touch()
    backlight = tmp_path / 'bl_power'
    backlight.write_text('1')

    env = os.environ.copy()
    env.update({
        'MELLO_GOODIX_DEVICE': str(device),
        'MELLO_GOODIX_BIND': str(bind),
        'MELLO_GOODIX_UNBIND': str(unbind),
        'MELLO_BACKLIGHT_POWER_PATH': str(backlight),
        'MELLO_I2C_DEVICE': str(tmp_path / 'missing-i2c'),
        'MELLO_RECOVERY_SLEEP_SECONDS': '0',
    })

    result = subprocess.run(
        ['sh', str(ROOT / 'pi/touch-fix.sh')],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert backlight.read_text() == '0\n'
    assert unbind.read_text() == '10-005d\n'
    assert bind.read_text() == '10-005d\n'
    assert 'Goodix touchscreen bound' in result.stdout


def test_watchdog_recovery_wakes_screen_before_restarting_ui(tmp_path):
    command_log = tmp_path / 'commands.log'
    fake_systemctl = tmp_path / 'systemctl'
    fake_systemctl.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$*" >> "{command_log}"\n'
    )
    fake_systemctl.chmod(0o755)
    recovery = tmp_path / 'touch-fix.sh'
    recovery.write_text(
        '#!/bin/sh\n'
        f'printf "touch-fix\\n" >> "{command_log}"\n'
    )
    recovery.chmod(0o755)
    fake_vcgencmd = tmp_path / 'vcgencmd'
    fake_vcgencmd.write_text('#!/bin/sh\nprintf "throttled=0x50005\\n"\n')
    fake_vcgencmd.chmod(0o755)

    env = os.environ.copy()
    env.update({
        'MELLO_SYSTEMCTL': str(fake_systemctl),
        'MELLO_TOUCH_FIX': str(recovery),
        'MELLO_VCGENCMD': str(fake_vcgencmd),
    })

    result = subprocess.run(
        ['sh', str(ROOT / 'pi/touch-watchdog.sh'), '--recover-once'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        'stop mello-native',
        'touch-fix',
        'start mello-native',
    ]
    assert 'throttled=0x50005' in result.stdout


def test_watchdog_recovers_only_after_a_burst_of_goodix_errors(tmp_path):
    command_log = tmp_path / 'commands.log'
    fake_systemctl = tmp_path / 'systemctl'
    fake_systemctl.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$*" >> "{command_log}"\n'
    )
    fake_systemctl.chmod(0o755)
    recovery = tmp_path / 'touch-fix.sh'
    recovery.write_text('#!/bin/sh\nexit 0\n')
    recovery.chmod(0o755)
    fake_vcgencmd = tmp_path / 'vcgencmd'
    fake_vcgencmd.write_text('#!/bin/sh\nprintf "throttled=0x0\\n"\n')
    fake_vcgencmd.chmod(0o755)
    fake_journalctl = tmp_path / 'journalctl'
    fake_journalctl.write_text(
        '#!/bin/sh\n'
        'echo "Goodix-TS 10-005d: Error reading 10 bytes from 0x814e: -5"\n'
        'echo "Goodix-TS 10-005d: Error writing 1 bytes to 0x814e: -5"\n'
        'echo "Goodix-TS 10-005d: Error reading 10 bytes from 0x814e: -5"\n'
    )
    fake_journalctl.chmod(0o755)

    env = os.environ.copy()
    env.update({
        'MELLO_SYSTEMCTL': str(fake_systemctl),
        'MELLO_TOUCH_FIX': str(recovery),
        'MELLO_VCGENCMD': str(fake_vcgencmd),
        'MELLO_JOURNALCTL': str(fake_journalctl),
    })

    result = subprocess.run(
        ['sh', str(ROOT / 'pi/touch-watchdog.sh')],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        'stop mello-native',
        'start mello-native',
    ]

"""
Sleep Manager - Power saving and screen burn-in prevention.
"""
import os
import subprocess
import time
import logging
from typing import Optional

from ..config import SLEEP_TIMEOUT, SLEEP_CLOCK_BRIGHTNESS

logger = logging.getLogger(__name__)


class SleepManager:
    """Manages deep sleep mode for power saving and screen burn-in prevention.

    Sleep saves power by:
    - Dimming the DSI backlight to a clock-readable level (or off if
      brightness control is unavailable)
    - Dropping CPU to minimum frequency (600MHz)
    - Turning off activity LED
    """

    BACKLIGHT_DIR = '/sys/class/backlight'
    DRM_DIR = '/sys/class/drm'
    CPU_GOVERNOR_PATH = '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'
    LED_TRIGGER_PATH = '/sys/class/leds/ACT/trigger'
    LED_BRIGHTNESS_PATH = '/sys/class/leds/ACT/brightness'
    
    def __init__(self):
        self.is_sleeping = False
        self.sleep_enabled = True
        self.sleep_disabled_reason: Optional[str] = None
        self.last_activity = time.time()
        self.backlight_path = self._detect_backlight()
        self.brightness_path, self.max_brightness = self._detect_brightness()
        self.drm_dpms_path = self._detect_drm_connector()
        self._saved_governor: Optional[str] = None
        self._saved_led_trigger: Optional[str] = None
        self._sleep_started_at: Optional[float] = None
        self._saved_brightness: Optional[str] = None

        if self.backlight_path:
            logger.info(f'Backlight: {self.backlight_path}')
        if self.brightness_path:
            logger.info(f'Brightness: {self.brightness_path} (max={self.max_brightness})')
        else:
            logger.info('No brightness control: sleep will blank the screen instead of dimming')
        if self.drm_dpms_path:
            logger.info(f'DRM DPMS: {self.drm_dpms_path}')
        if not self.backlight_path and not self.drm_dpms_path:
            logger.info('No display control found (not on Pi?)')
        
        # Restore CPU/LED (safe to do anytime). Display restore happens
        # in ensure_display_on() which must be called BEFORE pygame init
        # to avoid conflicting with kmsdrm's DRM master.
        self._set_low_power_cpu(False)
        self._set_led(True)
    
    @staticmethod
    def restore_display():
        """Restore display power at startup. Call BEFORE pygame init.
        
        Uses a temporary instance to detect and restore display,
        avoiding conflicts with kmsdrm's DRM master.
        """
        try:
            tmp = SleepManager.__new__(SleepManager)
            tmp.backlight_path = tmp._detect_backlight()
            tmp.drm_dpms_path = tmp._detect_drm_connector()
            if tmp.backlight_path or tmp.drm_dpms_path:
                tmp._set_display(True)
                logger.info(f'Display restored at startup (bl={tmp.backlight_path is not None}, dpms={tmp.drm_dpms_path is not None})')

            # A crash while dimmed for the sleep clock would otherwise leave the
            # panel dim forever, since the saved level died with the process.
            brightness_path, max_brightness = tmp._detect_brightness()
            if brightness_path and max_brightness > 0:
                tmp.brightness_path = brightness_path
                tmp._write_sysfs(brightness_path, str(max_brightness))
                logger.info(f'Brightness restored to max ({max_brightness}) at startup')
        except Exception as e:
            logger.warning(f'Display restore failed: {e}')
    
    def _detect_backlight(self) -> Optional[str]:
        """Detect the correct backlight path for any Pi touchscreen."""
        try:
            backlights = os.listdir(self.BACKLIGHT_DIR)
            if backlights:
                return f'{self.BACKLIGHT_DIR}/{backlights[0]}/bl_power'
        except Exception:
            pass
        return None
    
    def _detect_brightness(self) -> tuple:
        """Detect backlight brightness control. Returns (path, max_value)."""
        try:
            backlights = os.listdir(self.BACKLIGHT_DIR)
            if not backlights:
                return None, 0
            base = f'{self.BACKLIGHT_DIR}/{backlights[0]}'
            path = f'{base}/brightness'
            if not os.path.exists(path):
                return None, 0
            raw_max = self._read_sysfs(f'{base}/max_brightness')
            return path, int(raw_max) if raw_max else 0
        except Exception:
            return None, 0

    def _detect_drm_connector(self) -> Optional[str]:
        """Detect the active DRM connector for DPMS control (KMS-compatible)."""
        try:
            for entry in sorted(os.listdir(self.DRM_DIR)):
                dpms_path = f'{self.DRM_DIR}/{entry}/dpms'
                status_path = f'{self.DRM_DIR}/{entry}/status'
                if not os.path.exists(dpms_path):
                    continue
                try:
                    with open(status_path, 'r') as f:
                        if f.read().strip() == 'connected':
                            return dpms_path
                except Exception:
                    continue
        except Exception:
            pass
        return None
    
    def reset_timer(self):
        """Reset the sleep timer (called on user activity or playback)."""
        self.last_activity = time.time()
        if self.is_sleeping:
            self.wake_up()

    def disable_sleep(self, reason: str):
        """Disable automatic sleep for this app session and ensure display is on."""
        if self.sleep_enabled:
            logger.warning(f'Sleep disabled: {reason}')
        else:
            logger.debug(f'Sleep already disabled: {self.sleep_disabled_reason}')
        self.sleep_enabled = False
        self.sleep_disabled_reason = reason
        self.reset_timer()
        if not self.is_sleeping:
            self._set_display(True)

    def enable_sleep(self):
        """Re-enable automatic sleep for this app session."""
        if not self.sleep_enabled:
            logger.info('Sleep enabled')
        self.sleep_enabled = True
        self.sleep_disabled_reason = None
        self.reset_timer()
    
    def check_sleep(self, is_playing: bool) -> bool:
        """Check if should enter sleep mode. Returns True if sleeping."""
        if not self.sleep_enabled:
            if self.is_sleeping:
                self.wake_up()
            return False

        if self.is_sleeping:
            return True
        
        if is_playing:
            self.last_activity = time.time()
            return False
        
        if time.time() - self.last_activity >= SLEEP_TIMEOUT:
            self.enter_sleep()
            return True
        
        return False
    
    def enter_sleep(self):
        """Enter deep sleep mode - minimize power consumption."""
        if self.is_sleeping:
            return
        
        logger.info(f'Entering sleep mode... diag_before={self._display_diag()}')
        self.is_sleeping = True
        self._sleep_started_at = time.time()
        dimmed = self._dim_backlight()
        if not dimmed:
            # No brightness control: fall back to blanking (the clock won't show)
            self._set_display(False)
        self._set_low_power_cpu(True)
        self._set_led(False)
        logger.info(
            f'Sleep mode active (display={"dim" if dimmed else "off"}, CPU low, '
            f'LED off, WiFi kept awake) diag_after={self._display_diag()}'
        )
    
    def wake_up(self, reason: str = 'activity'):
        """Wake from sleep mode - restore full power."""
        if not self.is_sleeping:
            return
        
        slept_for = time.time() - self._sleep_started_at if self._sleep_started_at else None
        slept_text = f'{slept_for:.1f}s' if slept_for is not None else 'unknown'
        logger.info(f'Waking up... reason={reason}, slept_for={slept_text}, diag_before={self._display_diag()}')
        self.is_sleeping = False
        self.last_activity = time.time()
        self._set_led(True)
        self._set_low_power_cpu(False)
        self._restore_brightness()
        self._set_display(True)
        logger.info(f'Awake (display on, CPU normal, LED on) diag_after={self._display_diag()}')

    def _dim_backlight(self) -> bool:
        """Dim the backlight for the sleep clock. Returns True if dimming worked.

        Keeps bl_power on — blanking the panel would hide the clock entirely.
        """
        if not self.brightness_path or self.max_brightness <= 0:
            return False

        target = max(1, int(self.max_brightness * SLEEP_CLOCK_BRIGHTNESS))
        try:
            self._saved_brightness = self._read_sysfs(self.brightness_path)
            self._set_display(True)  # ensure the panel is powered before dimming
            self._write_sysfs(self.brightness_path, str(target))
            actual = self._read_sysfs(self.brightness_path)
            if actual is None or actual.strip() != str(target):
                logger.warning(f'Dim failed: wanted={target}, actual={actual}')
                return False
            logger.info(f'Backlight dimmed to {target}/{self.max_brightness}')
            return True
        except (IOError, OSError, PermissionError) as e:
            logger.warning(f'Dim failed: {e}')
            return False

    def _restore_brightness(self):
        """Restore the pre-sleep brightness level."""
        if not self.brightness_path or self._saved_brightness is None:
            return
        try:
            self._write_sysfs(self.brightness_path, self._saved_brightness)
            logger.info(f'Backlight restored to {self._saved_brightness}')
        except (IOError, OSError, PermissionError) as e:
            logger.warning(f'Brightness restore failed: {e}')
        finally:
            self._saved_brightness = None

    def _set_display(self, on: bool):
        """Turn display on/off via backlight only.

        DRM DPMS is NOT used because it powers down the DSI pipeline,
        which kills the I2C bus and disables the Goodix touch controller.
        Backlight-only keeps touch alive for wake-from-sleep.
        """
        state = 'on' if on else 'off'

        if self.backlight_path:
            try:
                value = '0' if on else '1'
                with open(self.backlight_path, 'w') as f:
                    f.write(value)
                actual = self._read_sysfs(self.backlight_path)
                logger.info(f'Backlight {state}: wrote={value}, actual={actual}, path={self.backlight_path}')
            except (IOError, OSError, PermissionError) as e:
                logger.warning(f'Backlight {state} failed: {e}')
        else:
            logger.warning(f'Backlight {state} skipped: no backlight path detected')
    
    def _set_low_power_cpu(self, low_power: bool):
        """Switch CPU governor: 'powersave' locks at 600MHz, 'ondemand' scales up."""
        if not os.path.exists(self.CPU_GOVERNOR_PATH):
            return
        try:
            if low_power:
                self._saved_governor = self._read_sysfs(self.CPU_GOVERNOR_PATH)
                self._write_sysfs(self.CPU_GOVERNOR_PATH, 'powersave')
            else:
                governor = self._saved_governor or 'ondemand'
                self._write_sysfs(self.CPU_GOVERNOR_PATH, governor)
        except Exception as e:
            logger.debug(f'Could not set CPU governor: {e}')
    
    def _set_led(self, on: bool):
        """Turn activity LED on/off to save a tiny bit + reduce visual noise."""
        try:
            if on:
                trigger = self._saved_led_trigger or 'mmc0'
                self._write_sysfs(self.LED_TRIGGER_PATH, trigger)
            else:
                self._saved_led_trigger = self._read_sysfs_bracket(self.LED_TRIGGER_PATH)
                self._write_sysfs(self.LED_TRIGGER_PATH, 'none')
                self._write_sysfs(self.LED_BRIGHTNESS_PATH, '0')
        except Exception as e:
            logger.debug(f'Could not control LED: {e}')
    
    def _set_wifi_power_save(self, on: bool):
        """Enable/disable WiFi power save. Lets the chip sleep between beacons."""
        state = 'on' if on else 'off'
        try:
            subprocess.run(
                ['sudo', 'iw', 'wlan0', 'set', 'power_save', state],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            logger.debug(f'Could not set WiFi power save: {e}')
    
    def _write_sysfs(self, path: str, value: str):
        """Write to a sysfs file, trying direct first then sudo."""
        try:
            with open(path, 'w') as f:
                f.write(value)
        except PermissionError:
            subprocess.run(
                ['sudo', 'tee', path],
                input=value.encode(), capture_output=True, timeout=5
            )
    
    def _read_sysfs(self, path: str) -> Optional[str]:
        """Read a sysfs file."""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception:
            return None
    
    def _read_sysfs_bracket(self, path: str) -> Optional[str]:
        """Read active value from sysfs trigger file (format: 'opt1 [active] opt2')."""
        content = self._read_sysfs(path)
        if content and '[' in content:
            start = content.index('[') + 1
            end = content.index(']')
            return content[start:end]
        return content

    def _display_diag(self) -> dict:
        """Small display power snapshot for diagnosing black-screen wake issues."""
        diag = {
            'backlight_path': self.backlight_path,
            'backlight': self._read_sysfs(self.backlight_path) if self.backlight_path else None,
            'dpms_path': self.drm_dpms_path,
            'dpms': self._read_sysfs(self.drm_dpms_path) if self.drm_dpms_path else None,
        }
        if self.drm_dpms_path:
            status_path = self.drm_dpms_path.rsplit('/', 1)[0] + '/status'
            diag['drm_status'] = self._read_sysfs(status_path)
        return diag

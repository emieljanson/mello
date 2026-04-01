"""
Sleep Manager - Power saving and screen burn-in prevention.
"""
import os
import subprocess
import time
import logging
from typing import Optional

from ..config import SLEEP_TIMEOUT

logger = logging.getLogger(__name__)


class SleepManager:
    """Manages deep sleep mode for power saving and screen burn-in prevention.
    
    Sleep saves power by:
    - Turning off DSI backlight
    - Turning off HDMI/DSI via DRM DPMS
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
        self.last_activity = time.time()
        self.backlight_path = self._detect_backlight()
        self.drm_dpms_path = self._detect_drm_connector()
        self._saved_governor: Optional[str] = None
        self._saved_led_trigger: Optional[str] = None
        
        if self.backlight_path:
            logger.info(f'Backlight: {self.backlight_path}')
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
    
    def check_sleep(self, is_playing: bool) -> bool:
        """Check if should enter sleep mode. Returns True if sleeping."""
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
        
        logger.info('Entering sleep mode...')
        self.is_sleeping = True
        self._set_display(False)
        self._set_low_power_cpu(True)
        self._set_led(False)
        self._set_wifi_power_save(True)
        logger.info('Sleep mode active (display off, CPU low, LED off, WiFi ps)')
    
    def wake_up(self):
        """Wake from sleep mode - restore full power."""
        if not self.is_sleeping:
            return
        
        logger.info('Waking up...')
        self.is_sleeping = False
        self.last_activity = time.time()
        self._set_wifi_power_save(False)
        self._set_led(True)
        self._set_low_power_cpu(False)
        self._set_display(True)
        logger.info('Awake (display on, CPU normal, LED on)')
    
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
            except (IOError, OSError, PermissionError) as e:
                logger.warning(f'Backlight {state} failed: {e}')
    
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

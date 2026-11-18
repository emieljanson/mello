"""
Diagnostics for the Mello app: startup health, touch health, FPS/state logs
and the periodic health report to analytics.
"""
import logging
import os
import time
from typing import Optional

from ..managers.analytics import device_health
from ..utils import run_async

logger = logging.getLogger('mello.app')

HEALTH_REPORT_EVERY = 6 * 3600   # seconds between device_health events


class DiagnosticsMixin:
    """Startup/touch health checks, periodic state logs and health reports."""

    def _log_device_info(self):
        """Log which Raspberry Pi this is."""
        model = self._read_text_file('/proc/device-tree/model')
        if model:
            logger.info(f'Device: {model.replace(chr(0), "")}')

    def _read_text_file(self, path: str) -> Optional[str]:
        """Read a text file for diagnostics."""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception:
            return None

    def _boot_config_status(self) -> str:
        """Return compact boot config diagnostics for field support."""
        config_path = None
        for path in ('/boot/firmware/config.txt', '/boot/config.txt'):
            if os.path.exists(path):
                config_path = path
                break
        if not config_path:
            return 'missing'
        content = self._read_text_file(config_path) or ''
        explicit_overlay = 'dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90' in content
        display_auto_detect_active = any(
            line.strip() == 'display_auto_detect=1'
            for line in content.splitlines()
        )
        disable_splash = any(
            line.strip() == 'disable_splash=1'
            for line in content.splitlines()
        )
        return (
            f'path={config_path}, ili9881_5inch_overlay={explicit_overlay}, '
            f'display_auto_detect_active={display_auto_detect_active}, '
            f'disable_splash={disable_splash}'
        )

    def _log_startup_health(self):
        """Log display/touch health so black-screen diagnosis is evidence-based."""
        self._log_device_info()
        backlight_value = (
            self._read_text_file(self.sleep_manager.backlight_path)
            if self.sleep_manager.backlight_path else None
        )
        dsi_status = self._read_text_file('/sys/class/drm/card0-DSI-1/status')
        dsi_dpms = self._read_text_file('/sys/class/drm/card0-DSI-1/dpms')
        logger.info(f'HEALTH boot_config: {self._boot_config_status()}')
        logger.info(
            'HEALTH display: '
            f'backlight_path={self.sleep_manager.backlight_path or "none"}, '
            f'backlight_value={backlight_value or "unknown"}, '
            f'dsi_status={dsi_status or "unknown"}, dsi_dpms={dsi_dpms or "unknown"}'
        )
        logger.info(
            'HEALTH touch: '
            f'available={self.evdev_touch.is_available}, '
            f'device={self.evdev_touch.device_name or "none"}, '
            f'path={self.evdev_touch.device_path or "none"}, '
            f'sleep_enabled={self.sleep_manager.sleep_enabled}'
        )

    def _disable_sleep_for_touch(self, reason: str):
        """Disable sleep when touch wake is unavailable, waking display if needed."""
        was_sleeping = self.sleep_manager.is_sleeping
        self.sleep_manager.disable_sleep(f'touch wake unavailable: {reason}')
        if was_sleeping:
            self.bluetooth.resume_monitoring()
            self.tracker.on_wake()
        self.renderer.invalidate()

    def _check_touch_health(self):
        """React if the background touch reader fails after startup."""
        reason = self.evdev_touch.consume_failure_reason()
        if reason:
            self._disable_sleep_for_touch(reason)
    
    def _report_health_changes(self):
        """Once per frame: tell analytics about headphones coming and going,
        and send a health snapshot every few hours."""
        dev = self.bluetooth.connected_device
        mac = dev.mac if dev else None
        if mac != self._bt_seen:
            now = time.monotonic()
            if self._bt_seen is not None:
                self.tracker.on_bluetooth_disconnected(
                    now - self._bt_seen_since,
                    while_playing=self.now_playing.playing,
                    from_menu=self.setup_menu.is_open,
                )
            if mac is not None:
                self.tracker.on_bluetooth_connected()
            self._bt_seen = mac
            self._bt_seen_since = now
        if time.monotonic() >= self._next_health_report:
            self._next_health_report = time.monotonic() + HEALTH_REPORT_EVERY
            run_async(lambda: self.tracker.on_health(device_health()))

    def _log_fps_if_due(self, target_fps: int):
        """Log FPS stats periodically and warn on drops."""
        now = time.time()
        if now - self._last_fps_log < self._fps_log_interval:
            return
        
        self._last_fps_log = now
        avg_fps = self.perf_monitor.current_fps
        is_loading = self.playback.play_state.is_loading
        items = self.display_items
        focused = items[self.selected_index].name if items and self.selected_index < len(items) else '?'
        playing_ctx = self.now_playing.context_uri or 'none'
        playing_name = self.now_playing.track_name or 'none'
        api_metrics = self.api.metrics_snapshot() if hasattr(self.api, 'metrics_snapshot') else {}
        suppressed = api_metrics.get('suppressed', {})
        failures = api_metrics.get('failures', {})
        
        logger.info(
            f'STATE | focused="{focused}" | playing="{playing_name}" | ctx={playing_ctx[:40]} '
            f'| driving={self._user_driving} | loading={is_loading} | connected={self.connected} '
            f'| fps={avg_fps:.0f}/{target_fps} '
            f'| api_suppressed={suppressed} | api_failures={failures}'
        )

        focused_uri = items[self.selected_index].uri if items and self.selected_index < len(items) else None
        if (self.now_playing.playing and focused_uri and playing_ctx
                and focused_uri != playing_ctx and not is_loading and not self._user_driving):
            logger.warning(
                f'MISMATCH | screen="{focused}" | audio="{playing_name}" '
                f'| focused_uri={focused_uri[:40]} | playing_ctx={playing_ctx[:40]} '
                f'| interacting={self.user_interacting} '
                f'| settled={self.carousel.settled} | timer={self.play_timer.item is not None}'
            )
        
        if not self.sleep_manager.is_sleeping:
            if target_fps == 60 and avg_fps < 30:
                logger.warning(f'Low FPS during animation: {avg_fps:.1f} (target: 60 FPS)')
            elif target_fps == 10 and avg_fps < 8:
                logger.warning(f'Low FPS while playing: {avg_fps:.1f} (target: 10 FPS)')
            elif target_fps == 5 and avg_fps < 4:
                logger.warning(f'Low FPS while idle: {avg_fps:.1f} (target: 5 FPS)')
    
    def _log_sleep_wait_if_due(self):
        """Log periodic sleeping heartbeat so black-screen reports have context."""
        now = time.time()
        if now - self._last_sleep_wait_log < 60:
            return
        self._last_sleep_wait_log = now
        logger.info(
            f'Sleep wait | touch_available={self.evdev_touch.is_available} | '
            f'touch_device={self.evdev_touch.device_name or "none"} | '
            f'wake_event_set={self.evdev_touch.wake_event.is_set()}'
        )

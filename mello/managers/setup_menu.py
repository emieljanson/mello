"""
Setup Menu - WiFi management and library reset.

Extracted from app.py to keep system-admin concerns separate from the player.
"""
import json
import time
import logging
import subprocess
import threading
from typing import Optional, Callable

import shutil

from pathlib import Path

from ..config import CATALOG_PATH, IMAGES_DIR, LIBRESPOT_STATE_PATH, SETTINGS_PATH
from ..models import MenuState

_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)

# SSIDs to exclude when scanning or forgetting WiFi networks
_WIFI_SKIP_SSIDS = {'Mello-Setup', 'mello-ap', 'mello-setup'}

logger = logging.getLogger(__name__)


class SetupMenu:
    """Manages the setup menu overlay (WiFi, library clear, settings)."""

    def __init__(
        self,
        catalog_manager,
        settings,
        on_toast: Callable[[str], None],
        on_invalidate: Callable[[], None],
        on_library_cleared: Callable[[], None],
        bluetooth_manager=None,
        on_volume_preview: Optional[Callable[[int, str, int], None]] = None,
    ):
        self.catalog_manager = catalog_manager
        self.settings = settings
        self._on_toast = on_toast
        self._on_invalidate = on_invalidate
        self._on_library_cleared = on_library_cleared
        self.bluetooth = bluetooth_manager
        self._on_volume_preview = on_volume_preview

        self.state = MenuState.CLOSED
        self.scroll_offset: int = 0  # pixels scrolled in current menu screen
        self.known_networks: list = []
        self.current_network: Optional[str] = None
        self._ssid_to_con_name: dict = {}
        self._wifi_process: Optional[subprocess.Popen] = None

        # Reset confirmation state
        self._reset_confirm_pending: bool = False
        self._reset_confirm_time: float = 0.0

        # Manual update state
        self._update_available: bool = False
        self._update_checking: bool = False
        self._update_running: bool = False
        self._update_process: Optional[subprocess.Popen] = None

    @property
    def is_open(self) -> bool:
        return self.state != MenuState.CLOSED

    def open(self):
        """Open the setup menu overlay."""
        logger.info('Setup menu opened')
        self.state = MenuState.MAIN
        self.scroll_offset = 0
        self.current_network = None
        self._on_invalidate()

    def show_wifi(self):
        """Open directly to the WiFi screen (skipping main menu)."""
        if self.state == MenuState.WIFI_AP:
            logger.info('show_wifi() ignored — AP mode active')
            return
        self._show_wifi_screen()

    def close(self):
        """Close the setup menu, stopping wifi-connect and BT scan if running."""
        logger.info('Setup menu closed')
        if self._wifi_process:
            self._kill_wifi_processes()
            self._wifi_process = None
        if self.bluetooth and self.state == MenuState.BT_LIST:
            self.bluetooth.stop_scan()
        self._reset_confirm_pending = False
        self.state = MenuState.CLOSED
        self.current_network = None
        self._on_invalidate()

    def handle_tap(self, pos, button_rects: dict):
        """Handle a tap while the menu is open."""
        x, y = pos

        if 'close' in button_rects and button_rects['close'].collidepoint(x, y):
            if self.state == MenuState.MAIN:
                self.close()
            elif self.state == MenuState.WIFI_AP:
                self._restore_wifi_autoconnect()
                if self._wifi_process:
                    self._kill_wifi_processes()
                    self._wifi_process = None
                    self._reconnect_to_known_network()
                self.state = MenuState.WIFI_LIST
                self.scroll_offset = 0
                self._on_invalidate()
            else:
                # All other submenus → back to main
                if self.state == MenuState.BT_LIST and self.bluetooth:
                    self.bluetooth.stop_scan()
                self.state = MenuState.MAIN
                self.scroll_offset = 0
                self._on_invalidate()
            return

        if self.state == MenuState.VOLUME_LEVELS:
            self._handle_volume_tap(button_rects, x, y)
        elif self.state == MenuState.BT_LIST:
            self._handle_bt_tap(button_rects, x, y)
        elif self.state == MenuState.WIFI_LIST:
            if 'new_network' in button_rects and button_rects['new_network'].collidepoint(x, y):
                self._start_wifi_ap()
            else:
                self._check_reconnect_tap(button_rects, x, y)
        elif self.state == MenuState.WIFI_AP:
            self._check_reconnect_tap(button_rects, x, y)
        else:
            if 'reset' in button_rects and button_rects['reset'].collidepoint(x, y):
                if self._reset_confirm_pending:
                    self._reset_confirm_pending = False
                    self._factory_reset()
                else:
                    self._reset_confirm_pending = True
                    self._reset_confirm_time = time.time()
                    self._on_invalidate()
                return
            # Any other tap in main menu clears reset confirmation
            if self._reset_confirm_pending:
                self._reset_confirm_pending = False
                self._on_invalidate()
            if 'wifi' in button_rects and button_rects['wifi'].collidepoint(x, y):
                self._show_wifi_screen()
            elif 'bluetooth' in button_rects and button_rects['bluetooth'].collidepoint(x, y):
                self._show_bt_screen()
            elif 'auto_pause' in button_rects and button_rects['auto_pause'].collidepoint(x, y):
                mins = self.settings.cycle_auto_pause()
                self._on_toast(f'Auto-pause: {mins} min')
                self._on_invalidate()
            elif 'progress_expiry' in button_rects and button_rects['progress_expiry'].collidepoint(x, y):
                hours = self.settings.cycle_progress_expiry()
                self._on_toast(f'Remember progress: {hours} hrs')
                self._on_invalidate()
            elif 'volume' in button_rects and button_rects['volume'].collidepoint(x, y):
                self.state = MenuState.VOLUME_LEVELS
                self.scroll_offset = 0
                self._on_invalidate()
            elif 'check_update' in button_rects and button_rects['check_update'].collidepoint(x, y):
                if self._update_running or self._update_checking:
                    return
                if self._update_available:
                    self._run_update()
                else:
                    self._check_for_update()

    def handle_scroll(self, delta: int, max_overflow: int):
        """Adjust scroll offset by delta, clamped to valid range."""
        self.scroll_offset = max(0, min(max_overflow, self.scroll_offset + delta))
        self._on_invalidate()

    def update(self):
        """Called each frame to detect wifi-connect / update exit."""
        # Auto-clear reset confirmation after 4 seconds
        if self._reset_confirm_pending and time.time() - self._reset_confirm_time > 4:
            self._reset_confirm_pending = False
            self._on_invalidate()

        # Monitor manual update process
        if self._update_process is not None:
            ret = self._update_process.poll()
            if ret is not None:
                self._update_process = None
                self._update_running = False
                if ret != 0:
                    self._on_toast('Update failed')
                self._update_available = False
                self._on_invalidate()

        if self.state == MenuState.WIFI_AP and self._wifi_process:
            ret = self._wifi_process.poll()
            if ret is not None:
                self._wifi_process = None
                self._restore_wifi_autoconnect()
                if ret == 0:
                    logger.info('wifi-connect exited (code=0)')
                    self._on_toast('WiFi connected!')
                    self.close()
                else:
                    logger.info(f'wifi-connect exited (code={ret})')
                    self._reconnect_to_known_network()
                    self._show_wifi_screen()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_for_update(self):
        """Check if a newer version is available on the remote branch."""
        self._update_checking = True
        self._update_available = False
        self._on_invalidate()

        def _check():
            try:
                # Verify git repo is healthy before checking
                git_dir_check = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', '--git-dir'],
                    capture_output=True, timeout=5,
                )
                if git_dir_check.returncode != 0:
                    logger.warning('Git repo broken — triggering auto-update to re-clone')
                    self._on_toast('Repairing install...')
                    self._update_checking = False
                    self._on_invalidate()
                    self._run_update()
                    return

                branch = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', '--abbrev-ref', 'HEAD'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip() or 'main'
                result = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'fetch', 'origin', branch],
                    capture_output=True, timeout=15,
                )
                if result.returncode != 0:
                    self._on_toast('No internet?')
                    self._update_checking = False
                    self._on_invalidate()
                    return
                local = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', 'HEAD'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                remote = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', f'origin/{branch}'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                self._update_available = local != remote
                if self._update_available:
                    self._on_toast('Update available!')
                else:
                    self._on_toast('Up to date')
            except Exception as e:
                logger.error(f'Update check failed: {e}')
                self._on_toast('Check failed')
            finally:
                self._update_checking = False
                self._on_invalidate()

        threading.Thread(target=_check, daemon=True).start()

    def _run_update(self):
        """Trigger the auto-update script (will restart the app)."""
        self._update_running = True
        self._on_invalidate()
        self._on_toast("Updating... don't unplug")
        try:
            self._update_process = subprocess.Popen(
                ['bash', f'{_REPO_DIR}/pi/auto-update.sh'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            logger.info('Manual update triggered')
        except Exception as e:
            logger.error(f'Failed to start update: {e}')
            self._update_running = False
            self._on_toast('Update failed')
            self._on_invalidate()

    def _handle_volume_tap(self, button_rects: dict, x: int, y: int):
        """Handle taps on the volume settings screen (+/- buttons)."""
        for key, rect in button_rects.items():
            if not rect.collidepoint(x, y):
                continue
            # Keys are like "vol_plus_0_speaker", "vol_minus_1_bt"
            if key.startswith('vol_'):
                parts = key.split('_')  # ['vol', 'plus'/'minus', index, type]
                if len(parts) == 4:
                    delta = 1 if parts[1] == 'plus' else -1
                    level_idx = int(parts[2])
                    output_type = parts[3]
                    new_val = self.settings.adjust_volume(level_idx, output_type, delta)
                    if self._on_volume_preview:
                        self._on_volume_preview(level_idx, output_type, new_val)
                    self._on_invalidate()
                break

    def _show_bt_screen(self):
        logger.info('Setup menu: Bluetooth screen')
        self.state = MenuState.BT_LIST
        self.scroll_offset = 0
        self._on_invalidate()
        if self.bluetooth:
            self.bluetooth.refresh_paired()
            self.bluetooth.start_scan()

    def _handle_bt_tap(self, button_rects: dict, x: int, y: int):
        if not self.bluetooth:
            return
        for key, rect in button_rects.items():
            if not rect.collidepoint(x, y):
                continue
            if key.startswith('bt_paired_'):
                idx = int(key.split('_')[2])
                paired = self.bluetooth.paired_devices
                if idx < len(paired):
                    dev = paired[idx]
                    if dev.connected:
                        self.bluetooth.disconnect()
                        self._on_toast(f'{dev.name} disconnected')
                    else:
                        self._on_toast(f'Connecting to {dev.name}...')
                        self.bluetooth.connect(dev.mac)
                break
            elif key.startswith('bt_discovered_'):
                idx = int(key.split('_')[2])
                discovered = self.bluetooth.discovered_devices
                if idx < len(discovered):
                    dev = discovered[idx]
                    self.bluetooth.pair_and_connect(dev.mac, dev.name)
                break

    def _check_reconnect_tap(self, button_rects: dict, x: int, y: int):
        for key, rect in button_rects.items():
            if key.startswith('reconnect_') and rect.collidepoint(x, y):
                idx = int(key.split('_')[1])
                if idx < len(self.known_networks):
                    self._reconnect_wifi(self.known_networks[idx])
                break

    def _resolve_ssid(self, con_name: str) -> str:
        """Get the actual SSID for a connection profile name."""
        try:
            result = subprocess.run(
                ['nmcli', '-g', '802-11-wireless.ssid', 'con', 'show', con_name],
                capture_output=True, text=True, timeout=3,
            )
            ssid = result.stdout.strip()
            if ssid:
                return ssid
        except Exception as e:
            logger.debug(f'Could not resolve SSID for {con_name}: {e}')
        return con_name

    def _collect_known_networks(self):
        """Populate known_networks and current_network via nmcli."""
        try:
            active_result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'],
                capture_output=True, text=True, timeout=3,
            )
            active_con_names = [
                line.split(':')[0]
                for line in active_result.stdout.strip().split('\n')
                if line and '802-11-wireless' in line
            ]
            all_result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
                capture_output=True, text=True, timeout=3,
            )
            all_con_names = [
                line.split(':')[0]
                for line in all_result.stdout.strip().split('\n')
                if line and '802-11-wireless' in line
            ]
            skip = _WIFI_SKIP_SSIDS
            seen = set()
            ordered = []
            ssid_map = {}
            active_ssids = []
            for con_name in active_con_names + all_con_names:
                if not con_name or con_name in seen or con_name in skip:
                    continue
                seen.add(con_name)
                ssid = self._resolve_ssid(con_name)
                if ssid in skip or ssid in ssid_map:
                    continue
                ssid_map[ssid] = con_name
                ordered.append(ssid)
                if con_name in active_con_names:
                    active_ssids.append(ssid)
            self._ssid_to_con_name = ssid_map
            self.known_networks = ordered
            self.current_network = active_ssids[0] if active_ssids else None
            logger.info(f'Known WiFi: {self.known_networks}, current: {self.current_network}')
        except Exception as e:
            logger.warning(f'Could not read WiFi connections: {e}')
            self.known_networks = []
            self.current_network = None
            self._ssid_to_con_name = {}

    def _show_wifi_screen(self):
        logger.info('Setup menu: WiFi screen')
        self._collect_known_networks()
        self.state = MenuState.WIFI_LIST
        self.scroll_offset = 0
        self._on_invalidate()

    def _start_wifi_ap(self):
        logger.info('Setup menu: starting wifi-connect AP')
        # Clear _wifi_process BEFORE killing old processes, so update()
        # won't treat the old process's clean exit (code=0) as success.
        self._wifi_process = None
        self.state = MenuState.WIFI_AP
        self.scroll_offset = 0
        self._on_invalidate()

        def _prepare_and_launch():
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'wifi', 'rescan'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
                time.sleep(2)
            except Exception as e:
                logger.warning(f'WiFi rescan failed: {e}')
            # Disable autoconnect BEFORE disconnect — disconnect fails when
            # wlan0 is already disconnected (exit code 6) and won't suppress
            # autoconnect, letting NM reclaim wlan0 from the AP.
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'no'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            except Exception:
                pass
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'disconnect', 'wlan0'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
            except Exception:
                pass
            self._kill_wifi_processes()
            self._delete_stale_ap_profile()
            self._launch_wifi_connect()

        threading.Thread(target=_prepare_and_launch, daemon=True).start()

    def _launch_wifi_connect(self):
        try:
            self._wifi_process = subprocess.Popen(
                ['sudo', 'wifi-connect',
                 '--portal-ssid', 'Mello-Setup',
                 '--ui-directory', '/usr/local/share/wifi-connect/ui'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            logger.info('wifi-connect started')

            def _log_output():
                for line in self._wifi_process.stdout:
                    logger.info(f'wifi-connect: {line.decode().rstrip()}')

            threading.Thread(target=_log_output, daemon=True).start()
        except Exception as e:
            logger.error(f'Failed to start wifi-connect: {e}')

    def _kill_wifi_processes(self):
        """Kill any lingering wifi-connect and dnsmasq processes.

        wifi-connect spawns dnsmasq as a child. When we terminate only the
        sudo wrapper, the real wifi-connect binary and dnsmasq survive,
        holding port 80 on 192.168.42.1 and causing the next launch to
        fail with 'Address already in use'.
        """
        for name in ('wifi-connect', 'dnsmasq'):
            try:
                subprocess.run(
                    ['sudo', 'pkill', '-f' if name == 'wifi-connect' else '-x', name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            except Exception:
                pass
        logger.info('Killed stale wifi-connect/dnsmasq processes')

    def _delete_stale_ap_profile(self):
        """Delete leftover Mello-Setup AP profile from NetworkManager.

        When wifi-connect exits, it leaves the AP connection profile behind.
        On the next launch, wifi-connect sees the old profile and deletes it
        internally, but this triggers NetworkManager to briefly activate the
        profile's dnsmasq — which then holds the port when wifi-connect tries
        to start its own dnsmasq. Deleting the profile upfront avoids this.
        """
        try:
            result = subprocess.run(
                ['sudo', 'nmcli', 'con', 'delete', 'Mello-Setup'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                logger.info('Deleted stale Mello-Setup AP profile')
            time.sleep(1)
        except Exception:
            pass

    def _restore_wifi_autoconnect(self):
        """Re-enable NM autoconnect on wlan0 after leaving AP mode."""
        try:
            subprocess.run(
                ['sudo', 'nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'yes'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            )
        except Exception:
            pass

    def _reconnect_to_known_network(self):
        if self.known_networks:
            ssid = self.known_networks[0]
            con_name = self._ssid_to_con_name.get(ssid, ssid)
            logger.info(f'Auto-reconnecting to known network: {ssid} (con: {con_name})')
            try:
                subprocess.Popen(
                    ['sudo', 'nmcli', 'con', 'up', con_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.error(f'Auto-reconnect failed: {e}')
        else:
            logger.warning('No known networks to reconnect to')

    def _reconnect_wifi(self, ssid: str):
        con_name = self._ssid_to_con_name.get(ssid, ssid)
        logger.info(f'Setup menu: Reconnect to {ssid} (con: {con_name})')

        if self._wifi_process:
            self._kill_wifi_processes()
            self._wifi_process = None

        try:
            subprocess.Popen(
                ['sudo', 'nmcli', 'con', 'up', con_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._on_toast('Connecting...')
        except Exception as e:
            logger.error(f'Reconnect failed: {e}')
            self._on_toast('Connection failed')

        self.close()

    def _factory_reset(self):
        """Full factory reset: catalog, settings, Spotify, Bluetooth, WiFi."""
        logger.info('Setup menu: Factory reset')

        # 1. Clear catalog and progress
        try:
            if CATALOG_PATH.exists():
                CATALOG_PATH.unlink()
            self.catalog_manager.clear_all_progress()
            self._on_library_cleared()
            logger.info('Catalog cleared')
        except Exception as e:
            logger.error(f'Failed to clear catalog: {e}')

        # 2. Clear Spotify credentials
        try:
            if LIBRESPOT_STATE_PATH.exists():
                state = json.loads(LIBRESPOT_STATE_PATH.read_text())
                state['credentials'] = {'username': '', 'data': None}
                LIBRESPOT_STATE_PATH.write_text(json.dumps(state))
                logger.info('Spotify credentials cleared')
        except Exception as e:
            logger.error(f'Failed to clear Spotify credentials: {e}')

        # 3. Delete settings (auto-pause, volume, BT device memory)
        try:
            if SETTINGS_PATH.exists():
                SETTINGS_PATH.unlink()
                logger.info('Settings deleted')
        except Exception as e:
            logger.error(f'Failed to delete settings: {e}')

        # 4. Delete cached album images
        try:
            if IMAGES_DIR.exists():
                shutil.rmtree(IMAGES_DIR)
                logger.info('Image cache deleted')
        except Exception as e:
            logger.error(f'Failed to delete image cache: {e}')

        # 5. Forget all Bluetooth paired devices
        try:
            subprocess.run(
                ['bluetoothctl', 'disconnect'],
                capture_output=True, timeout=5,
            )
            result = subprocess.run(
                ['bluetoothctl', 'devices', 'Paired'],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    mac = parts[1]
                    subprocess.run(
                        ['bluetoothctl', 'remove', mac],
                        capture_output=True, timeout=5,
                    )
            logger.info('Bluetooth devices forgotten')
        except Exception as e:
            logger.error(f'Failed to forget Bluetooth devices: {e}')

        # 6. Forget all WiFi networks (keep Mello-Setup AP)
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
                capture_output=True, text=True, timeout=5,
            )
            skip = _WIFI_SKIP_SSIDS
            for line in result.stdout.strip().splitlines():
                if '802-11-wireless' in line:
                    name = line.split(':')[0]
                    if name and name not in skip:
                        subprocess.run(
                            ['sudo', 'nmcli', 'con', 'delete', name],
                            capture_output=True, timeout=5,
                        )
            logger.info('WiFi networks forgotten')
        except Exception as e:
            logger.error(f'Failed to forget WiFi networks: {e}')

        # 7. Restart app
        def _restart_app():
            time.sleep(2)
            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'restart', 'mello-native'],
                    timeout=10,
                )
            except Exception as ex:
                logger.warning(f'Could not restart mello-native: {ex}')
        threading.Thread(target=_restart_app, daemon=True).start()

        self._on_toast('Reset complete')
        self.close()

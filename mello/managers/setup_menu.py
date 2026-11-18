"""
Setup Menu - WiFi management and library reset.

Extracted from app.py to keep system-admin concerns separate from the player.
"""
import json
import re
import time
import logging
import subprocess
import threading
from typing import Optional, Callable

import shutil

from pathlib import Path

from ..config import (
    CATALOG_PATH, IMAGES_DIR, LIBRESPOT_STATE_PATH, SETTINGS_PATH,
    SOLOIST_DATA_DIR, SOLOIST_HOME, SPOTIFY_ENGINE, SPOTIFY_ENGINE_SERVICE,
)
from ..models import MenuState
from ..update_status import last_run_label, update_problem
from .engine_setup import (
    PROMPT_BUTTON_LAST, PROMPT_BUTTON_LATER,
    PROMPT_TITLE_NEW_OWNER, PROMPT_TITLE_UPDATE,
    SETTINGS_CONNECTED_LINES, SETTINGS_OFFER_LINES, SETUP_LINES,
    lan_address,
)

_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)

# SSIDs to exclude when scanning or forgetting WiFi networks
_WIFI_SKIP_SSIDS = {'Mello-Setup', 'mello-ap', 'mello-setup'}
# wifi-connect's line when the chosen network could not be joined.
_JOIN_FAILED = re.compile(r"Connection to access point not activated '(.*)'")

# How long the self-opened Soloist prompt may stay up without being touched.
_SPOTIFY_PROMPT_TIMEOUT = 180
# How long "Hold to close" must be held: long enough that a child's tap or
# swipe does not close it by accident.
PROMPT_HOLD_SECONDS = 3.0
# How long the update button shows how a check or update ended.
_UPDATE_RESULT_SECONDS = 4.0

logger = logging.getLogger(__name__)


def _update_command(log_path: Path) -> list:
    """How the settings menu starts an update.

    The update ends by restarting Mello, which stops everything Mello itself
    started: run from here, the script was cut off at that point, so its
    last check and "Update complete" never happened. Started as a job of
    the user's own service manager instead, it lives on like the nightly
    run does. The output goes to the same log the nightly run uses (a pipe
    nobody reads fills up during apt/pip and then hangs the update).
    """
    script = f'{_REPO_DIR}/pi/auto-update.sh'
    if shutil.which('systemd-run'):
        return [
            'systemd-run', '--user', '--wait', '--collect', '--quiet',
            f'--unit=mello-manual-update-{int(time.time())}',
            '-p', f'StandardOutput=append:{log_path}',
            '-p', f'StandardError=append:{log_path}',
            # A hung update would hold the update lock until a reboot.
            '-p', 'RuntimeMaxSec=45min',
            'bash', script,
        ]
    return ['bash', '-c', f'exec timeout 45m bash "{script}" >> "{log_path}" 2>&1']


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
        engine_setup=None,
    ):
        self.catalog_manager = catalog_manager
        self.settings = settings
        self._on_toast = on_toast
        self._on_invalidate = on_invalidate
        self._on_library_cleared = on_library_cleared
        self.bluetooth = bluetooth_manager
        self._on_volume_preview = on_volume_preview
        # Key entry page for switching to Spotify Soloist (served only while
        # the Spotify screen is open).
        self.engine_setup = engine_setup
        self.spotify_prompt_active = False
        self._spotify_prompt_since = 0.0
        self._prompt_hold_start: Optional[float] = None

        self.state = MenuState.CLOSED
        self.scroll_offset: int = 0  # pixels scrolled in current menu screen
        self.known_networks: list = []
        self.current_network: Optional[str] = None
        self._ssid_to_con_name: dict = {}
        self._wifi_process: Optional[subprocess.Popen] = None
        self._wifi_ap_attempt = 0
        # Network the owner picked on the phone that could not be joined.
        self.wifi_join_failed: Optional[str] = None
        # Known networks as they were before the hotspot started:
        # {connection name: (ssid, password)}. Only kept in memory.
        self._networks_before_hotspot: dict = {}
        # The WiFi screen is up because Mello found itself offline, not
        # because somebody opened it.
        self._wifi_opened_itself = False
        # The home screen itself is asking to connect Spotify (new Mello).
        self._home_page = False
        self._home_page_retry_at = 0.0

        # Reset confirmation state
        self._reset_confirm_pending: bool = False
        self._reset_confirm_time: float = 0.0

        # Shown under the version when updates have not worked for a while.
        self.update_problem: Optional[str] = None

        # Manual update state
        self._update_checking: bool = False
        self._update_running: bool = False
        self._update_process: Optional[subprocess.Popen] = None
        # How the last check or update ended ("Up to date", "No internet"...),
        # shown on the button itself for a moment: toasts are not drawn
        # while the menu is open, so this is the only place it can be seen.
        self._update_result: Optional[str] = None
        self._update_result_time: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.state != MenuState.CLOSED

    def open(self):
        """Open the setup menu overlay."""
        logger.info('Setup menu opened')
        self.update_problem = update_problem()
        if self.update_problem:
            logger.warning(f'Setup menu: {self.update_problem} (see ~/mello-update.log)')
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
        self._wifi_opened_itself = True

    @property
    def wifi_setup_running(self) -> bool:
        """True while Mello is its own hotspot for joining a new network."""
        return self.state == MenuState.WIFI_AP or self._wifi_process is not None

    def network_restored(self):
        """The connection came back by itself.

        A WiFi screen that opened by itself because there was no connection
        goes away by itself too; one the owner opened stays.
        """
        if self._wifi_opened_itself and self.state == MenuState.WIFI_LIST:
            logger.info('Setup menu: connection is back, closing the WiFi screen')
            self._on_toast('WiFi connected!')
            self.close()

    def close(self):
        """Close the setup menu, stopping wifi-connect and BT scan if running."""
        logger.info('Setup menu closed')
        if self._wifi_process:
            self._kill_wifi_processes()
            self._wifi_process = None
        if self.state == MenuState.WIFI_AP:
            self._restore_wifi_autoconnect()
        if self.bluetooth and self.state == MenuState.BT_LIST:
            self.bluetooth.stop_scan()
        self._leave_spotify_screen()
        self.spotify_prompt_active = False
        self._prompt_hold_start = None
        self._reset_confirm_pending = False
        self.state = MenuState.CLOSED
        self.current_network = None
        self._on_invalidate()

    # ------------------------------------------------------------------
    # Soloist prompt: hold to close
    # ------------------------------------------------------------------

    def handle_press(self, pos, button_rects: dict):
        """A finger went down while the menu is open."""
        rect = button_rects.get('prompt_close')
        if self.spotify_prompt_active and not self.spotify_switching and rect and rect.collidepoint(*pos):
            self._prompt_hold_start = time.time()
            self._spotify_prompt_since = self._prompt_hold_start   # touched: not unattended
            self._on_invalidate()   # the button lights up at once

    def handle_release(self):
        """The finger came up again."""
        if self._prompt_hold_start is not None:
            self._prompt_hold_start = None
            self._on_invalidate()

    @property
    def prompt_hold_progress(self) -> float:
        """0.0-1.0: how far the "Hold to close" press has come."""
        if self._prompt_hold_start is None:
            return 0.0
        return min(1.0, (time.time() - self._prompt_hold_start) / PROMPT_HOLD_SECONDS)

    def handle_tap(self, pos, button_rects: dict):
        """Handle a tap while the menu is open."""
        if self.spotify_prompt_active:
            return  # only the hold button does something here
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
                self._leave_spotify_screen()
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
            elif 'spotify' in button_rects and button_rects['spotify'].collidepoint(x, y):
                self._show_spotify_screen()
            elif 'check_update' in button_rects and button_rects['check_update'].collidepoint(x, y):
                if self._update_running or self._update_checking:
                    return
                self._check_for_update()

    def _show_spotify_screen(self):
        """Show how to reach the key entry page, and serve it meanwhile."""
        if self.engine_setup:
            self._tell_page_who_is_asking()
            self.engine_setup.start()
        self.state = MenuState.SPOTIFY
        self.scroll_offset = 0
        self._on_invalidate()

    def _leave_spotify_screen(self):
        if self.engine_setup and self.state == MenuState.SPOTIFY and not self._home_page:
            self.engine_setup.stop()

    def _tell_page_who_is_asking(self):
        """The phone page words itself for a new Mello or for one that was updated."""
        if self.settings:
            self.engine_setup.new_owner = self.settings.soloist_new_owner
            self.engine_setup.setup_step = self._spotify_is_setup_step

    def home_connect_spotify(self, has_network: bool, library_empty: bool) -> bool:
        """Whether the home screen itself should ask to connect Spotify.

        A new Mello that is online and has nothing saved yet shows it right
        where "Play to Mello via Spotify" would be: no menu, nothing to tap
        away. The phone page exists for as long as the home screen asks.
        Playing to Mello anyway (the old player still works) leaves it.
        """
        if not self.engine_setup or not self.settings:
            return False
        # Once a code is in, the screen stays ("One moment...") until the
        # speaker restarts on the new player, whatever else changes.
        switching = self._home_page and self.engine_setup.state == 'switching'
        want = bool(switching or (
            has_network and library_empty
            and self.settings.soloist_setup_step_open and self.engine_setup.can_offer_soloist()
        ))
        if want and not self.engine_setup.running:
            now = time.time()
            if now >= self._home_page_retry_at:
                self._tell_page_who_is_asking()
                if not self.engine_setup.start():
                    self._home_page_retry_at = now + 30      # port taken: not every frame
        elif not want and self._home_page and self.state != MenuState.SPOTIFY:
            self.engine_setup.stop(wait=False)               # called while drawing
        self._home_page = want
        return want

    @property
    def spotify_switching(self) -> bool:
        return bool(self.engine_setup) and self.engine_setup.state == 'switching'

    @property
    def spotify_items(self) -> list:
        if not self.engine_setup:
            return []
        if (self.spotify_prompt_active or self._home_page) and not self.spotify_switching and not self.engine_setup.url:
            # Seconds after WiFi came up: Mello has no address to point at yet.
            return [('text', 'One moment...')]
        return self.engine_setup.screen_items()

    @property
    def _spotify_is_setup_step(self) -> bool:
        return bool(self.settings) and self.settings.soloist_setup_step_open

    def show_spotify_prompt(self) -> bool:
        """Open the "a faster Mello" offer by itself.

        For a Mello that got Spotify's own player through an update, or a new
        one whose owner started using it without connecting Spotify. (A new
        Mello is asked on its home screen instead, see home_connect_spotify.)

        It has no close button: it goes away when a code is entered (the
        speaker switches and restarts) or when someone holds its button for a
        few seconds, so a child does not tap it away before a parent has seen
        it. Held away, it is back a few days later; the last time the button
        says so, and after that it only lives in Settings.

        Returns False when there is nothing to offer now.
        """
        if self.is_open or not self.engine_setup or not self.settings:
            return False
        if not self.engine_setup.can_offer_soloist():
            return False
        if self._spotify_is_setup_step:
            if self.catalog_manager and self.catalog_manager.items:
                # Saved an album without connecting Spotify: setting up is
                # over, the offer follows in a few days.
                logger.info('Setup menu: new Mello is in use without Spotify connected, offer follows later')
                self.settings.postpone_soloist_prompt()
            return False
        if not self.settings.soloist_prompt_due or not lan_address():
            return False                 # (offline: the phone could not reach the page)
        logger.info('Setup menu: showing the faster-Mello offer')
        self.spotify_prompt_active = True
        self._prompt_hold_start = None
        self._spotify_prompt_since = time.time()
        self._show_spotify_screen()
        return True

    # What the three connect-Spotify screens say.

    @property
    def home_lines(self) -> tuple:
        """Under the logo on a new Mello's home screen, above the code."""
        return SETUP_LINES

    @property
    def spotify_menu_items(self) -> list:
        """Settings > Spotify: two grey lines and the code, like the WiFi screen."""
        if not self.engine_setup or self.state != MenuState.SPOTIFY:
            return []
        items = self.spotify_items
        if self.spotify_switching or not self.engine_setup.url:
            return items
        if self.engine_setup.engine == 'soloist':
            lines = SETTINGS_CONNECTED_LINES
        elif self._spotify_is_setup_step:
            lines = SETUP_LINES
        else:
            lines = SETTINGS_OFFER_LINES
        return [('text', line) for line in lines] + [('spacer',)] + items

    # The self-opened "a faster Mello" offer.

    @property
    def prompt_title(self) -> str:
        new_owner = bool(self.settings) and self.settings.soloist_new_owner
        return PROMPT_TITLE_NEW_OWNER if new_owner else PROMPT_TITLE_UPDATE

    @property
    def prompt_button(self) -> str:
        last = bool(self.settings) and self.settings.soloist_prompt_is_last
        return PROMPT_BUTTON_LAST if last else PROMPT_BUTTON_LATER

    @property
    def update_button_label(self) -> str:
        """What the update button says: what it is doing, or how it ended."""
        if self._update_running:
            return 'Updating...'
        if self._update_checking:
            return 'Checking...'
        if self._update_result:
            return self._update_result
        return 'Check for updates'

    def _set_update_result(self, label: str):
        self._update_result = label
        self._update_result_time = time.time()
        self._on_invalidate()

    def handle_scroll(self, delta: int, max_overflow: int):
        """Adjust scroll offset by delta, clamped to valid range."""
        self.scroll_offset = max(0, min(max_overflow, self.scroll_offset + delta))
        self._on_invalidate()

    def update(self):
        """Called each frame to detect wifi-connect / update exit."""
        if self.spotify_prompt_active:
            if self._prompt_hold_start is not None:
                if self.prompt_hold_progress >= 1.0:
                    logger.info('Setup menu: faster-Mello offer held away')
                    self.settings.postpone_soloist_prompt()
                    self.close()
                else:
                    self._on_invalidate()   # keep the button filling up
            elif (time.time() - max(self._spotify_prompt_since, self.engine_setup.last_request)
                  > _SPOTIFY_PROMPT_TIMEOUT):
                # It steps aside when nobody is there (a phone that has the
                # page open counts as somebody). It is back the next time the
                # speaker wakes.
                logger.info('Setup menu: faster-Mello offer put away (unattended)')
                self.close()
            elif not self.engine_setup.url:
                self._on_invalidate()       # waiting for an address: the code appears by itself

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
                # A good update restarts Mello before this point. Still
                # here: it found nothing to do, or it could not (no
                # internet ends with exit 0, so read what the run wrote).
                label = 'Update failed' if ret != 0 else last_run_label()
                if label:
                    logger.error(f'Manual update: {label} (exit code {ret}), see ~/mello-update.log')
                    self._set_update_result(label)
                self._on_invalidate()

        if self._update_result and time.time() - self._update_result_time > _UPDATE_RESULT_SECONDS:
            self._update_result = None
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
        """Look for a newer version and, when there is one, install it.

        One tap does both: someone who taps "Check for updates" wants the
        update. The outcome shows on the button itself.
        """
        self._update_checking = True
        self._update_result = None
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
                    self._update_checking = False
                    self._run_update()
                    return

                # Same decision as the nightly update (beta/stable channel,
                # never backwards, skip a version that failed here before).
                result = subprocess.run(
                    ['bash', f'{_REPO_DIR}/pi/update-target.sh'],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    reason = result.stderr.strip()
                    self._update_checking = False
                    if result.returncode == 2:
                        logger.warning(f'Update check: GitHub not reachable: {reason}')
                        self._set_update_result('No internet')
                    else:
                        logger.error(f'Update check failed: {reason}')
                        self._set_update_result('Check failed')
                    return
                logger.info(f'Update check: {result.stdout.strip()}')
                status = (result.stdout.split() or [''])[0]
                self._update_checking = False
                if status == 'update':
                    self._run_update()
                elif status == 'failed':
                    # The newest version broke on this speaker before and
                    # was rolled back: waiting for a newer one.
                    self._set_update_result('Update failed')
                else:
                    self._set_update_result('Up to date')
            except Exception as e:
                logger.error(f'Update check failed: {e}')
                self._update_checking = False
                self._set_update_result('Check failed')
            finally:
                self._update_checking = False
                self._on_invalidate()

        threading.Thread(target=_check, daemon=True).start()

    def _run_update(self):
        """Trigger the auto-update script (will restart the app)."""
        self._update_running = True
        self._update_result = None
        self._on_invalidate()
        try:
            self._update_process = subprocess.Popen(
                _update_command(Path.home() / 'mello-update.log'),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info('Manual update triggered')
        except Exception as e:
            logger.error(f'Failed to start update: {e}')
            self._update_running = False
            self._set_update_result('Update failed')

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
            # "--active" also lists a network Mello is still trying to join.
            # Only one that is really up counts as connected: otherwise the
            # list shows it as connected while the speaker is offline.
            active_result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'con', 'show', '--active'],
                capture_output=True, text=True, timeout=3,
            )
            active_con_names = [
                line.rsplit(':', 2)[0]
                for line in active_result.stdout.strip().split('\n')
                if line.endswith(':802-11-wireless:activated')
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
        self._wifi_opened_itself = False
        self._collect_known_networks()
        self.state = MenuState.WIFI_LIST
        self.scroll_offset = 0
        self._on_invalidate()

    def _start_wifi_ap(self):
        logger.info('Setup menu: starting wifi-connect AP')
        # Clear _wifi_process BEFORE killing old processes, so update()
        # won't treat the old process's clean exit (code=0) as success.
        self._wifi_process = None
        self.wifi_join_failed = None
        self.state = MenuState.WIFI_AP
        self.scroll_offset = 0
        self._on_invalidate()

        self._wifi_ap_attempt += 1
        attempt = self._wifi_ap_attempt

        def still_wanted() -> bool:
            # False once the user tapped back (or started over) while this
            # was still preparing.
            return self.state == MenuState.WIFI_AP and self._wifi_ap_attempt == attempt

        def _prepare_and_launch():
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'wifi', 'rescan'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
                time.sleep(2)
            except Exception as e:
                logger.warning(f'WiFi rescan failed: {e}')
            if not still_wanted():
                return  # nothing changed yet
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
            self._remember_networks()
            if still_wanted():
                self._launch_wifi_connect()
            if not still_wanted():
                # Backed out while WiFi was being taken down: nobody is
                # watching a hotspot any more, so put the speaker back online
                # instead of leaving it disconnected with autoconnect off.
                logger.info('WiFi setup cancelled while starting; restoring the connection')
                if self._wifi_process:
                    self._wifi_process = None
                self._kill_wifi_processes()
                self._restore_wifi_autoconnect()
                self._reconnect_to_known_network()

        threading.Thread(target=_prepare_and_launch, daemon=True).start()

    def _launch_wifi_connect(self):
        try:
            self._wifi_process = subprocess.Popen(
                ['sudo', 'wifi-connect',
                 '--portal-ssid', 'Mello-Setup',
                 # Straight from the code folder, so the page updates with the app.
                 '--ui-directory', str(Path(_REPO_DIR) / 'portal')],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            logger.info('wifi-connect started')

            process = self._wifi_process

            def _log_output():
                for line in process.stdout:
                    text = line.decode(errors='replace').rstrip()
                    logger.info(f'wifi-connect: {text}')
                    self._note_wifi_connect_line(text)

            threading.Thread(target=_log_output, daemon=True).start()
        except Exception as e:
            logger.error(f'Failed to start wifi-connect: {e}')

    def _note_wifi_connect_line(self, text: str):
        """Follow what the hotspot is doing, so the screen can say so.

        When joining the chosen network fails, wifi-connect quietly brings
        the hotspot back and the phone shows the password page again: the
        owner then thinks the password was wrong. The screen says what
        happened instead.
        """
        match = _JOIN_FAILED.search(text)
        if match:
            self.wifi_join_failed = match.group(1)
            self._on_invalidate()

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
        self._restore_forgotten_networks()

    def _wifi_connection_names(self) -> list:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
            capture_output=True, text=True, timeout=5,
        )
        names = []
        for line in result.stdout.splitlines():
            name, _, kind = line.rpartition(':')
            if kind == '802-11-wireless' and name and name not in _WIFI_SKIP_SSIDS:
                names.append(name)
        return names

    def _remember_networks(self):
        """Note the known networks before the hotspot starts.

        Typing the password for a network Mello already knows makes
        wifi-connect delete the old entry first. If joining then fails, the
        speaker has forgotten its home network: it can no longer get back
        to it by itself. With this note it can be put back.
        """
        remembered = {}
        try:
            for name in self._wifi_connection_names():
                result = subprocess.run(
                    ['sudo', 'nmcli', '-s', '-g',
                     '802-11-wireless.ssid,802-11-wireless-security.psk', 'con', 'show', name],
                    capture_output=True, text=True, timeout=5,
                )
                lines = result.stdout.splitlines()
                if result.returncode == 0 and lines and lines[0]:
                    remembered[name] = (lines[0], lines[1] if len(lines) > 1 else '')
        except Exception as e:
            logger.warning(f'Could not note known WiFi networks: {e}')
        self._networks_before_hotspot = remembered
        logger.info(f'Known WiFi before setup: {sorted(remembered)}')

    def _restore_forgotten_networks(self):
        """Put back known networks that went missing while the hotspot ran."""
        remembered, self._networks_before_hotspot = self._networks_before_hotspot, {}
        if not remembered:
            return
        try:
            present = set(self._wifi_connection_names())
            present_ssids = {self._resolve_ssid(n) for n in present}
        except Exception as e:
            logger.warning(f'Could not check known WiFi networks: {e}')
            return
        for name, (ssid, psk) in remembered.items():
            if name in present or ssid in present_ssids:
                continue
            cmd = ['sudo', 'nmcli', 'con', 'add', 'type', 'wifi', 'ifname', 'wlan0',
                   'con-name', name, 'ssid', ssid]
            if psk:
                cmd += ['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', psk]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    logger.info(f'Put back forgotten WiFi network: {name}')
                else:
                    logger.warning(f'Could not put back WiFi network {name}: '
                                   f'{(result.stderr or result.stdout).strip()[-200:]}')
            except Exception as e:
                logger.warning(f'Could not put back WiFi network {name}: {e}')

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

    def _forget_soloist_owner(self):
        """Log Soloist out and remove the owner's API key.

        The key belongs to the owner's own Spotify developer account. After a
        factory reset the speaker may go to someone else, so it must not keep
        it: the speaker goes back to the standard Spotify engine (at the end of
        the reset) and a new owner can enter their own key.
        """
        subprocess.run(
            ['sudo', 'systemctl', 'stop', SPOTIFY_ENGINE_SERVICE],
            capture_output=True, timeout=15,
        )
        if SOLOIST_DATA_DIR.exists():
            shutil.rmtree(SOLOIST_DATA_DIR)
        logger.info('Spotify credentials cleared')
        for name in ('key', 'key.tmp'):
            try:
                (SOLOIST_HOME / name).unlink()
            except FileNotFoundError:
                pass
        logger.info('Soloist API key removed')

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
            if SPOTIFY_ENGINE == 'soloist':
                self._forget_soloist_owner()
            elif LIBRESPOT_STATE_PATH.exists():
                state = json.loads(LIBRESPOT_STATE_PATH.read_text())
                state['credentials'] = {'username': '', 'data': None}
                LIBRESPOT_STATE_PATH.write_text(json.dumps(state))
                logger.info('Spotify credentials cleared')
        except Exception as e:
            logger.error(f'Failed to clear Spotify credentials: {e}')

        # 3. Delete settings (auto-pause, volume, BT device memory)
        try:
            if SETTINGS_PATH.exists():
                # The choice about sharing usage data was made at install and
                # cannot be made again on the speaker: a reset must not
                # silently turn an opt-out back into the default (sharing on).
                try:
                    opted_out = json.loads(SETTINGS_PATH.read_text()).get('share_usage_data') is False
                except (ValueError, OSError):
                    opted_out = False
                if opted_out:
                    SETTINGS_PATH.write_text(json.dumps({'share_usage_data': False}))
                else:
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

        # 7. Restart app. A speaker on Soloist has no key any more: it goes
        # back to go-librespot, and that script restarts the app itself.
        def _restart_app():
            time.sleep(2)
            if SPOTIFY_ENGINE == 'soloist':
                script = Path(_REPO_DIR) / 'pi' / 'soloist' / 'soloist-setup.sh'
                try:
                    result = subprocess.run(
                        ['bash', str(script), 'deactivate'],
                        capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode == 0:
                        return
                    logger.warning(f'Switching back to go-librespot failed: {result.stdout[-300:]}')
                except Exception as ex:
                    logger.warning(f'Could not switch back to go-librespot: {ex}')
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

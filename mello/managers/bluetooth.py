"""
Bluetooth Manager - BT device discovery, pairing, and audio routing.

Uses the BlueZ D-Bus API (via dbus-fast) for device discovery and pairing,
and pactl for PipeWire audio sink switching.

Pi 3B BCM43430A1 quirk: adapter often connects without resolving A2DP services.
A full BT service restart before reconnect fixes this reliably.
"""
import asyncio
import re
import sys
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional, List, Callable

from ..config import WM8960_SINK, BT_MONITOR_INTERVAL, BT_SCAN_DURATION

logger = logging.getLogger(__name__)

AUDIO_SINK_UUID = '0000110b-0000-1000-8000-00805f9b34fb'

# A `bluetoothctl show` that times out or finds no controller says little
# about the adapter: bluetoothd is often just busy. Restarting the service
# then drops a headphone that was playing fine, so only an adapter that
# reports `Powered: no`, or gives no answer this many polls in a row, is
# restarted.
ADAPTER_UNKNOWN_POLLS_BEFORE_RESTART = 3
# Polls to wait after a restart that did not bring the adapter back, so a
# dead adapter is not restarted every 5 s all day.
ADAPTER_RECOVERY_BACKOFF_POLLS = 12
AUDIO_ICONS = {'audio-headphones', 'audio-headset', 'audio-card'}

BLUEZ_SERVICE = 'org.bluez'
ADAPTER_PATH = '/org/bluez/hci0'
ADAPTER_IFACE = 'org.bluez.Adapter1'
DEVICE_IFACE = 'org.bluez.Device1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
OBJ_MGR_IFACE = 'org.freedesktop.DBus.ObjectManager'


def _is_audio_device_props(props: dict) -> bool:
    """Check if a D-Bus device has audio capabilities."""
    icon = props.get('Icon', '')
    if icon in AUDIO_ICONS:
        return True
    uuids = props.get('UUIDs', [])
    return AUDIO_SINK_UUID in uuids


# Legacy helper for bluetoothctl-based code paths (monitoring, connect)
def _is_audio_device(info: str) -> bool:
    return AUDIO_SINK_UUID in info or 'audio-headset' in info or 'audio-headphones' in info


@dataclass
class BluetoothDevice:
    mac: str
    name: str
    paired: bool = False
    connected: bool = False
    is_audio: bool = False


class BluetoothManager:
    """Manages Bluetooth device discovery, pairing, and PipeWire audio routing."""

    def __init__(
        self,
        settings,
        on_toast: Callable[[str], None],
        on_invalidate: Callable[[], None],
        on_audio_changed: Callable[[bool], None],
    ):
        self._settings = settings
        self._on_toast = on_toast
        self._on_invalidate = on_invalidate
        self._on_audio_changed = on_audio_changed

        self._lock = threading.Lock()
        # One connect, pair, audio switch or adapter restart at a time. The
        # monitor skips its poll while a tap holds this: it used to see the
        # headphone half connected, find no sink yet and restart the adapter
        # under the connect the person had just started.
        self._connect_lock = threading.RLock()
        self._paired_devices: List[BluetoothDevice] = []
        self._discovered_devices: List[BluetoothDevice] = []
        self._connected_device: Optional[BluetoothDevice] = None
        self._audio_active: bool = False
        self._desired_sink: Optional[str] = None

        self._scanning: bool = False
        self._pairing_mac: Optional[str] = None
        self._scan_stop_event = threading.Event()
        self._scan_thread: Optional[threading.Thread] = None

        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Start unpaused
        self._reconnect_cooldown: int = 0
        self._reconnect_failures: int = 0
        self._adapter_unknown_polls: int = 0
        self._adapter_recovery_cooldown: int = 0
        self._audio_generation: int = 0  # Bumped on each activate/deactivate to cancel stale threads

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    @property
    def paired_devices(self) -> List[BluetoothDevice]:
        with self._lock:
            return list(self._paired_devices)

    @property
    def discovered_devices(self) -> List[BluetoothDevice]:
        with self._lock:
            return list(self._discovered_devices)

    @property
    def connected_device(self) -> Optional[BluetoothDevice]:
        with self._lock:
            return self._connected_device

    @property
    def audio_active(self) -> bool:
        with self._lock:
            return self._audio_active

    @property
    def scanning(self) -> bool:
        with self._lock:
            return self._scanning

    @property
    def pairing_mac(self) -> Optional[str]:
        with self._lock:
            return self._pairing_mac

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_monitoring(self):
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info('Bluetooth: monitoring started')

    def pause_monitoring(self):
        """Pause BT polling (e.g. during sleep) to save power."""
        self._pause_event.clear()
        logger.info('Bluetooth: monitoring paused')

    def resume_monitoring(self):
        """Resume BT polling (e.g. on wake). Triggers immediate poll."""
        self._pause_event.set()
        logger.info('Bluetooth: monitoring resumed')

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # Unblock if paused so thread can exit
        self.stop_scan()

    # ------------------------------------------------------------------
    # D-Bus scan (settings menu)
    # ------------------------------------------------------------------

    def start_scan(self):
        """Start BT discovery via BlueZ D-Bus API in background."""
        self._scan_stop_event.clear()

        def _do():
            try:
                logger.info('Bluetooth: scan started')
                if not self._is_adapter_powered():
                    self._restart_adapter()
                with self._lock:
                    self._scanning = True
                self._on_invalidate()

                asyncio.run(self._dbus_scan_loop())
            except Exception as e:
                logger.error(f'Bluetooth: scan error: {e}', exc_info=True)
            finally:
                with self._lock:
                    self._scanning = False
                self._on_invalidate()

        t = threading.Thread(target=_do, daemon=True)
        self._scan_thread = t
        t.start()

    async def _dbus_scan_loop(self):
        """Run BR/EDR discovery cycles via D-Bus until stop_scan()."""
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType, Variant

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            intr = await bus.introspect(BLUEZ_SERVICE, ADAPTER_PATH)
            obj = bus.get_proxy_object(BLUEZ_SERVICE, ADAPTER_PATH, intr)
            adapter = obj.get_interface(ADAPTER_IFACE)
            adapter_props = obj.get_interface(PROPS_IFACE)

            # Ensure powered
            await adapter_props.call_set(ADAPTER_IFACE, 'Powered', Variant('b', True))

            while not self._scan_stop_event.is_set():
                # Set transport filter to BR/EDR (classic BT for audio devices).
                # Default LE-only scan on Pi 3B misses devices like AirPods Max.
                await adapter.call_set_discovery_filter({
                    'Transport': Variant('s', 'bredr'),
                })

                logger.info('Bluetooth: starting D-Bus bredr discovery')
                try:
                    await adapter.call_start_discovery()
                except Exception as e:
                    logger.warning(f'Bluetooth: start discovery failed: {e}')

                # Poll discovered devices periodically during scan
                deadline = time.monotonic() + BT_SCAN_DURATION
                while time.monotonic() < deadline and not self._scan_stop_event.is_set():
                    await self._dbus_poll_devices(bus)
                    await asyncio.sleep(3)

                try:
                    await adapter.call_stop_discovery()
                except Exception as e:
                    logger.debug(f'Bluetooth: stop_discovery ignored: {e}')

                # Final poll + audio filter
                await self._dbus_poll_devices(bus, filter_audio=True)
                self.refresh_paired()
                self._on_invalidate()
                logger.info(f'Bluetooth: scan cycle done')
        finally:
            bus.disconnect()

    async def _dbus_poll_devices(self, bus, filter_audio: bool = False):
        """Read all discovered devices from BlueZ ObjectManager."""
        from dbus_fast import Variant

        try:
            root_intr = await bus.introspect(BLUEZ_SERVICE, '/')
            root = bus.get_proxy_object(BLUEZ_SERVICE, '/', root_intr)
            mgr = root.get_interface(OBJ_MGR_IFACE)
            objects = await mgr.call_get_managed_objects()
        except Exception as e:
            logger.warning(f'Bluetooth: D-Bus poll error: {e}')
            return

        paired_macs = {d.mac for d in self._paired_devices}
        discovered: List[BluetoothDevice] = []

        for path, ifaces in objects.items():
            if DEVICE_IFACE not in ifaces:
                continue
            props = ifaces[DEVICE_IFACE]
            name = props.get('Name', Variant('s', '')).value
            addr = props.get('Address', Variant('s', '')).value
            paired = props.get('Paired', Variant('b', False)).value
            connected = props.get('Connected', Variant('b', False)).value
            icon = props.get('Icon', Variant('s', '')).value
            uuids = [u.value if hasattr(u, 'value') else u
                     for u in props.get('UUIDs', Variant('as', [])).value]

            if not name or paired or addr in paired_macs:
                continue

            is_audio = icon in AUDIO_ICONS or AUDIO_SINK_UUID in uuids
            if filter_audio and not is_audio:
                continue

            discovered.append(BluetoothDevice(
                mac=addr, name=name, is_audio=is_audio,
            ))

        with self._lock:
            self._discovered_devices = discovered
        self._on_invalidate()

    def stop_scan(self, wait: bool = False):
        logger.info('Bluetooth: stop_scan requested')
        self._scan_stop_event.set()
        if wait and self._scan_thread is not None:
            self._scan_thread.join(timeout=BT_SCAN_DURATION + 5)
            self._scan_thread = None

    # ------------------------------------------------------------------
    # Paired device management
    # ------------------------------------------------------------------

    def refresh_paired(self):
        if sys.platform != 'linux':
            return
        try:
            result = subprocess.run(
                ['bluetoothctl', 'devices', 'Paired'],
                capture_output=True, text=True, timeout=5,
            )
            devices = []
            for line in result.stdout.strip().splitlines():
                m = re.match(r'Device\s+([0-9A-Fa-f:]{17})\s+(.*)', line)
                if not m:
                    continue
                mac, name = m.group(1), m.group(2).strip()
                info = self._get_device_info(mac)
                devices.append(BluetoothDevice(
                    mac=mac, name=name, paired=True,
                    connected='Connected: yes' in info,
                    is_audio=_is_audio_device(info),
                ))
            with self._lock:
                self._paired_devices = devices
        except Exception as e:
            logger.warning(f'Bluetooth: refresh_paired error: {e}')

    def _get_device_info(self, mac: str) -> str:
        try:
            return subprocess.run(
                ['bluetoothctl', 'info', mac],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return ''

    # ------------------------------------------------------------------
    # Connect / disconnect / pair
    # ------------------------------------------------------------------

    def connect(self, mac: str):
        """Connect to a paired device (from settings menu tap)."""
        def _do():
            self._on_toast('Connecting...')
            with self._connect_lock:
                sink = self._reliable_connect(mac)
                if sink:
                    self._set_connected(self._paired_device(mac), sink)
                else:
                    self._on_toast('Connection failed')
            self._on_invalidate()
        threading.Thread(target=_do, daemon=True).start()

    def _paired_device(self, mac: str) -> BluetoothDevice:
        """Fresh paired-device entry for a device we just connected.

        bluetoothctl can lag a moment behind a connect; a device with a sink
        is connected whatever `info` says, so audio is switched regardless.
        """
        self.refresh_paired()
        with self._lock:
            dev = next((d for d in self._paired_devices if d.mac == mac), None)
        if dev is None:
            return BluetoothDevice(mac=mac, name=mac, paired=True,
                                   connected=True, is_audio=True)
        dev.connected = True
        return dev

    def disconnect(self):
        dev = self.connected_device
        if not dev:
            return
        def _do():
            with self._connect_lock:
                logger.info(f'Bluetooth: disconnecting {dev.mac}')
                if self.audio_active:
                    self._deactivate_audio()
                try:
                    subprocess.run(['bluetoothctl', 'disconnect', dev.mac],
                                   capture_output=True, timeout=10)
                except Exception as e:
                    logger.warning(f'Bluetooth: disconnect error: {e}')
                with self._lock:
                    self._connected_device = None
                self.refresh_paired()
            self._on_invalidate()
        threading.Thread(target=_do, daemon=True).start()

    def pair_and_connect(self, mac: str, name: str):
        # Immediate UI feedback — show "Connecting..." before thread starts
        with self._lock:
            self._pairing_mac = mac
        self._on_invalidate()

        def _do():
            logger.info(f'Bluetooth: pairing with {mac} ({name})')
            try:
                # Stop discovery — adapter can't pair while scanning
                self.stop_scan(wait=True)
                with self._connect_lock:
                    self._pair_and_connect_locked(mac)
            except Exception as e:
                logger.warning(f'Bluetooth: pair error: {e}')
                self._on_toast('Pairing failed')
            finally:
                with self._lock:
                    self._pairing_mac = None
                self._on_invalidate()
        threading.Thread(target=_do, daemon=True).start()

    def _pair_and_connect_locked(self, mac: str):
        self._wait_adapter_ready()

        # Pair via D-Bus with NoInputNoOutput agent (required for AirPods etc.)
        paired = asyncio.run(self._dbus_pair(mac))
        if not paired:
            self._on_toast('Pairing failed')
            return

        # Trusted devices may connect back on their own when switched on.
        subprocess.run(['bluetoothctl', 'trust', mac], capture_output=True, timeout=5)

        # Move device from discovered to paired section
        self.refresh_paired()
        with self._lock:
            self._discovered_devices = [
                d for d in self._discovered_devices if d.mac != mac
            ]
        self._on_invalidate()

        sink = self._reliable_connect(mac)
        if sink:
            self._set_connected(self._paired_device(mac), sink)
        else:
            self._on_toast('Connection failed')

    async def _dbus_pair(self, mac: str) -> bool:
        """Pair with device via D-Bus, registering a NoInputNoOutput agent."""
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType
        from dbus_fast.service import ServiceInterface, method

        class PairAgent(ServiceInterface):
            """Minimal BlueZ pairing agent — auto-accepts all requests."""
            def __init__(self):
                super().__init__('org.bluez.Agent1')

            @method()
            def Release(self):
                pass

            @method()
            def RequestConfirmation(self, device: 'o', passkey: 'u'):
                pass

            @method()
            def RequestAuthorization(self, device: 'o'):
                pass

            # Older headphones still use a PIN; nearly all take 0000.
            # Without these BlueZ got "method not found" and pairing failed.
            @method()
            def RequestPinCode(self, device: 'o') -> 's':
                return '0000'

            @method()
            def RequestPasskey(self, device: 'o') -> 'u':
                return 0

            @method()
            def DisplayPinCode(self, device: 'o', pincode: 's'):
                pass

            @method()
            def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'):
                pass

            @method()
            def AuthorizeService(self, device: 'o', uuid: 's'):
                pass

            @method()
            def Cancel(self):
                pass

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            agent = PairAgent()
            agent_path = '/mello/agent'
            bus.export(agent_path, agent)

            # Register agent with BlueZ
            mgr = bus.get_proxy_object('org.bluez', '/org/bluez',
                await bus.introspect('org.bluez', '/org/bluez'))
            agent_mgr = mgr.get_interface('org.bluez.AgentManager1')
            await agent_mgr.call_register_agent(agent_path, 'NoInputNoOutput')
            await agent_mgr.call_request_default_agent(agent_path)

            # Stop discovery before pairing
            try:
                adapter = bus.get_proxy_object('org.bluez', '/org/bluez/hci0',
                    await bus.introspect('org.bluez', '/org/bluez/hci0'))
                await adapter.get_interface('org.bluez.Adapter1').call_stop_discovery()
            except Exception as e:
                logger.debug(f'Bluetooth: stop_discovery before pair ignored: {e}')

            dev_path = '/org/bluez/hci0/dev_' + mac.replace(':', '_')
            dev_obj = bus.get_proxy_object('org.bluez', dev_path,
                await bus.introspect('org.bluez', dev_path))
            device = dev_obj.get_interface('org.bluez.Device1')

            for attempt in range(2):
                try:
                    logger.info(f'Bluetooth: D-Bus pair attempt {attempt + 1}')
                    await asyncio.wait_for(device.call_pair(), timeout=20)
                    logger.info('Bluetooth: D-Bus pair success')
                    return True
                except Exception as e:
                    if 'AlreadyExists' in f'{getattr(e, "type", "")} {e}':
                        logger.info('Bluetooth: already paired')
                        return True
                    logger.info(f'Bluetooth: D-Bus pair attempt {attempt + 1} failed: {e}')
                    if attempt == 0:
                        await asyncio.sleep(2)

            return False
        except Exception as e:
            logger.warning(f'Bluetooth: D-Bus pair error: {e}')
            return False
        finally:
            try:
                await agent_mgr.call_unregister_agent(agent_path)
            except Exception as e:
                logger.debug(f'Bluetooth: unregister agent ignored: {e}')
            bus.disconnect()

    def forget(self, mac: str):
        def _do():
            try:
                subprocess.run(['bluetoothctl', 'remove', mac], capture_output=True, timeout=5)
            except Exception as e:
                logger.warning(f'Bluetooth: forget error: {e}')
            self.refresh_paired()
            self._on_invalidate()
        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # Core: reliable connect (handles Pi 3B adapter quirks)
    # ------------------------------------------------------------------

    def _reliable_connect(self, mac: str) -> Optional[str]:
        """Connect to device and ensure PipeWire sink exists.

        Returns the bluez sink name on success, None on failure.
        Pi 3B often needs a full BT service restart for A2DP to work.
        """
        self._wait_adapter_ready()

        # Attempt 1: plain connect
        logger.info(f'Bluetooth: connecting to {mac}')
        if not self._bt_connect(mac):
            return None

        # Check for sink (wait up to 5s for PipeWire)
        sink = self._find_bt_sink(retries=5, mac=mac)
        if sink:
            logger.info(f'Bluetooth: connected with sink {sink}')
            return sink

        # Attempt 2: disconnect, restart adapter, reconnect
        logger.info(f'Bluetooth: no sink — restarting adapter and reconnecting {mac}')
        try:
            subprocess.run(['bluetoothctl', 'disconnect', mac], capture_output=True, timeout=5)
        except Exception:
            pass
        time.sleep(1)
        self._restart_adapter()

        if not self._bt_connect(mac):
            return None

        sink = self._find_bt_sink(retries=5, mac=mac)
        if sink:
            logger.info(f'Bluetooth: connected with sink {sink} (after adapter restart)')
        else:
            logger.warning(f'Bluetooth: connected to {mac} but no sink available')
        return sink

    def _bt_connect(self, mac: str) -> bool:
        """Run bluetoothctl connect. Returns True if the device is connected.

        A device that is already connected counts as success: BlueZ answers
        `AlreadyConnected` then, which used to end the connect right there,
        so a headphone connected without A2DP (the Pi 3B quirk) never got
        the adapter restart that fixes it.
        """
        try:
            result = subprocess.run(
                ['bluetoothctl', 'connect', mac],
                capture_output=True, text=True, timeout=15,
            )
            out = f'{result.stdout or ""}{result.stderr or ""}'
            if ('Connection successful' in out or 'Connected: yes' in out
                    or 'AlreadyConnected' in out):
                return True
            if self._is_connected(mac):
                return True
            logger.info(f'Bluetooth: connect failed: {out.strip()[:80]}')
            return False
        except subprocess.TimeoutExpired:
            if self._is_connected(mac):
                return True
            logger.info(f'Bluetooth: connect timeout for {mac}')
            return False
        except Exception as e:
            logger.warning(f'Bluetooth: connect error: {e}')
            return False

    def _is_connected(self, mac: str) -> bool:
        return 'Connected: yes' in self._get_device_info(mac)

    # ------------------------------------------------------------------
    # Audio routing
    # ------------------------------------------------------------------

    def toggle_audio(self):
        if self.audio_active:
            self._deactivate_audio()
        else:
            self._activate_audio()

    def set_volume(self, level: int):
        with self._lock:
            sink = self._desired_sink
            active = self._audio_active
        if active and sink:
            try:
                subprocess.run(['pactl', 'set-sink-volume', sink, f'{level}%'],
                               capture_output=True, timeout=5)
            except Exception as e:
                logger.warning(f'Bluetooth: set volume error: {e}')

    def ensure_stream_on_desired_sink(self):
        """Called when playback starts — move stream to desired sink if set."""
        with self._lock:
            sink = self._desired_sink
            active = self._audio_active
        if active and sink:
            self._move_stream(sink)

    def _activate_audio(self):
        """Route audio to BT headphone."""
        dev = self.connected_device
        if not dev:
            return

        # Optimistic: update state immediately so UI responds instantly
        with self._lock:
            self._audio_generation += 1
            my_gen = self._audio_generation
            self._audio_active = True
        self._on_audio_changed(True)
        self._on_invalidate()

        def _do():
            with self._connect_lock:
                sink = self._find_bt_sink(retries=5, mac=dev.mac)
                if not sink:
                    sink = self._reliable_connect(dev.mac)
                # Check if a newer toggle has superseded us
                with self._lock:
                    if self._audio_generation != my_gen:
                        logger.info(f'Bluetooth: activate cancelled (gen {my_gen} != {self._audio_generation})')
                        return
                if not sink:
                    logger.warning('Bluetooth: cannot activate audio — no sink')
                    # Roll back optimistic state
                    with self._lock:
                        if self._audio_generation == my_gen:
                            self._audio_active = False
                    self._on_audio_changed(False)
                    self._on_toast(f'{dev.name} not available')
                    self._on_invalidate()
                    return
                with self._lock:
                    if self._audio_generation != my_gen:
                        return
                    self._desired_sink = sink
                # Again now that the sink is known: the first call came
                # before it was, so the headphone volume was never set and
                # played at whatever level the sink had (often 100%).
                self._on_audio_changed(True)
                self._set_default_sink(sink)
                self._move_stream(sink)
                self._settings.set_last_bt_device_mac(dev.mac)
        threading.Thread(target=_do, daemon=True).start()

    def _deactivate_audio(self):
        """Route audio back to speaker."""
        # Bump generation to cancel any in-flight _activate_audio thread.
        # State update is synchronous so UI responds instantly;
        # PipeWire sink switching happens in background thread.
        with self._lock:
            self._audio_generation += 1
            self._audio_active = False
            self._desired_sink = None
        self._on_audio_changed(False)
        self._on_invalidate()

        def _do():
            self._set_default_sink(WM8960_SINK)
            self._move_stream(WM8960_SINK)
        threading.Thread(target=_do, daemon=True).start()

    def _set_default_sink(self, sink: str):
        """Set PipeWire default sink so new streams go here automatically."""
        try:
            subprocess.run(['pactl', 'set-default-sink', sink],
                           capture_output=True, timeout=5)
            logger.info(f'Bluetooth: default sink → {sink}')
        except Exception as e:
            logger.warning(f'Bluetooth: set-default-sink error: {e}')

    def _move_stream(self, sink: str):
        """Move all active streams to the given sink."""
        try:
            result = subprocess.run(['pactl', 'list', 'sink-inputs', 'short'],
                                    capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts:
                    subprocess.run(['pactl', 'move-sink-input', parts[0], sink],
                                   capture_output=True, timeout=5)
        except Exception as e:
            logger.warning(f'Bluetooth: move-sink-input error: {e}')

    def _find_bt_sink(self, retries: int = 1, mac: Optional[str] = None) -> Optional[str]:
        """Find a Bluetooth A2DP audio sink (not HFP/HSP headset sink).

        With a mac, only that device's sink counts, so audio never goes to
        another Bluetooth device that happens to still be connected.
        """
        mac_key = mac.replace(':', '_').upper() if mac else None
        for attempt in range(retries):
            try:
                result = subprocess.run(['pactl', 'list', 'sinks', 'short'],
                                        capture_output=True, text=True, timeout=5)
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) < 2 or 'bluez' not in parts[1]:
                        continue
                    if mac_key and mac_key not in parts[1].upper():
                        continue
                    # Reject HFP/HSP sinks (mono 8/16kHz) — need A2DP (stereo 44.1/48kHz)
                    spec = ' '.join(parts[2:])
                    if '1ch' in spec and ('8000Hz' in spec or '16000Hz' in spec):
                        logger.info(f'Bluetooth: skipping HFP sink {parts[1]}')
                        continue
                    return parts[1]
            except Exception as e:
                logger.debug(f'Bluetooth: list sinks failed: {e}')
            if attempt < retries - 1:
                time.sleep(1)
        return None

    # ------------------------------------------------------------------
    # Adapter helpers (Pi 3B BCM43430A1 workarounds)
    # ------------------------------------------------------------------

    def _restart_adapter(self):
        """Restart bluetooth service and power on adapter."""
        try:
            subprocess.run(['sudo', 'systemctl', 'restart', 'bluetooth'],
                           timeout=10, capture_output=True)
            time.sleep(1)
            # BCM43430A1 fix: down+up cycle resets firmware scanning state.
            # Without 'down', the adapter can get stuck with LE scanning
            # returning EBUSY (-16), making discovery permanently fail.
            subprocess.run(['sudo', 'hciconfig', 'hci0', 'down'],
                           timeout=5, capture_output=True)
            time.sleep(2)
            # hci0 down triggers an RF soft-block on some Debian installs (Trixie).
            # rfkill unblock is needed before hci0 up can power on the adapter.
            subprocess.run(['sudo', '/usr/sbin/rfkill', 'unblock', 'bluetooth'],
                           timeout=5, capture_output=True)
            subprocess.run(['sudo', 'hciconfig', 'hci0', 'up'],
                           timeout=5, capture_output=True)
            time.sleep(1)
            subprocess.run(['bluetoothctl', 'power', 'on'],
                           timeout=5, capture_output=True)
            time.sleep(1)
        except Exception as e:
            logger.warning(f'Bluetooth: adapter restart failed: {e}')

    def _wait_adapter_ready(self, timeout: float = 15.0):
        """Block until the BT adapter is powered on."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(['bluetoothctl', 'show'],
                                        capture_output=True, text=True, timeout=5)
                if 'Powered: yes' in result.stdout:
                    return
            except Exception:
                pass
            time.sleep(1)
        logger.warning('Bluetooth: adapter not powered, forcing up')
        try:
            subprocess.run(['sudo', 'hciconfig', 'hci0', 'up'], timeout=5, capture_output=True)
            time.sleep(1)
            subprocess.run(['bluetoothctl', 'power', 'on'], timeout=5, capture_output=True)
        except Exception:
            pass

    def _is_adapter_powered(self) -> bool:
        """Check if the BT adapter is powered on."""
        return self._adapter_state() == 'on'

    def _adapter_state(self) -> str:
        """'on', 'off' (adapter says Powered: no) or 'unknown' (no answer)."""
        try:
            result = subprocess.run(
                ['bluetoothctl', 'show'],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return 'unknown'
        if 'Powered: yes' in result.stdout:
            return 'on'
        if 'Powered: no' in result.stdout:
            return 'off'
        return 'unknown'

    def _adapter_healthy(self) -> bool:
        """Check the adapter and restart it when it is really down.

        Returns False when the poll should stop here.
        """
        state = self._adapter_state()
        if state == 'on':
            self._adapter_unknown_polls = 0
            self._adapter_recovery_cooldown = 0
            return True
        if state == 'unknown':
            self._adapter_unknown_polls += 1
            if self._adapter_unknown_polls < ADAPTER_UNKNOWN_POLLS_BEFORE_RESTART:
                logger.info(f'Bluetooth: adapter did not answer '
                            f'({self._adapter_unknown_polls}/{ADAPTER_UNKNOWN_POLLS_BEFORE_RESTART})')
                return False
        if self._adapter_recovery_cooldown > 0:
            self._adapter_recovery_cooldown -= 1
            return False

        logger.warning(f'Bluetooth: adapter {state}, attempting recovery')
        self._restart_adapter()
        if self._is_adapter_powered():
            self._adapter_unknown_polls = 0
            return True

        logger.warning('Bluetooth: adapter recovery failed')
        self._adapter_recovery_cooldown = ADAPTER_RECOVERY_BACKOFF_POLLS
        with self._lock:
            prev = self._connected_device
        if prev:
            self._handle_device_disconnected(prev)
        return False

    # ------------------------------------------------------------------
    # Background monitoring
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        self._reconnect_cooldown = 0
        self.refresh_paired()
        # Immediate first poll — connect ASAP so audio routes to BT before playback
        self._poll_connection_state()
        while not self._stop_event.wait(BT_MONITOR_INTERVAL):
            self._pause_event.wait()  # Block while paused (sleep mode)
            if self._stop_event.is_set():
                break
            self._poll_connection_state()

    def _poll_connection_state(self):
        if sys.platform != 'linux':
            return
        # A connect, pair or audio switch from a tap is running: leave the
        # adapter to it and look again next poll.
        if not self._connect_lock.acquire(blocking=False):
            return
        try:
            self._poll_connection_state_locked()
        finally:
            self._connect_lock.release()

    def _poll_connection_state_locked(self):
        try:
            # Check adapter health — BCM43430A1 can lock up completely
            if not self._adapter_healthy():
                return

            with self._lock:
                prev = self._connected_device
                prev_mac = prev.mac if prev else None

            self.refresh_paired()
            paired = self.paired_devices
            now_connected = next((d for d in paired if d.connected and d.is_audio), None)
            now_mac = now_connected.mac if now_connected else None

            if now_connected and not prev_mac:
                self._handle_device_connected(now_connected)
            elif not now_connected and prev_mac:
                self._handle_device_disconnected(prev)
            elif now_mac and now_mac != prev_mac:
                # Another headphone took over: route audio to it too.
                self._handle_device_connected(now_connected)
            elif not now_connected:
                with self._lock:
                    busy = self._scanning or self._pairing_mac is not None
                # Paging a headphone while the menu scans or pairs makes
                # both fail on this adapter.
                if not busy:
                    self._try_auto_reconnect(paired)
        except Exception as e:
            logger.warning(f'Bluetooth: monitor poll error: {e}')

    def _handle_device_connected(self, dev: BluetoothDevice):
        """Device appeared as connected — ensure sink + auto-switch audio."""
        logger.info(f'Bluetooth: {dev.name} connected — getting sink')

        # Pre-set expected sink as default so new streams go to BT immediately
        # even before PipeWire finishes creating the sink
        expected_sink = f'bluez_output.{dev.mac.replace(":", "_")}.1'
        self._set_default_sink(expected_sink)

        # Check if sink already exists (e.g. fresh connect with A2DP)
        sink = self._find_bt_sink(retries=3, mac=dev.mac)
        if not sink:
            # Pi 3B: often connected without A2DP — restart adapter and reconnect
            logger.info(f'Bluetooth: no sink, restarting adapter')
            sink = self._reliable_connect(dev.mac)
        if sink:
            self._set_connected(dev, sink)
        else:
            logger.warning(f'Bluetooth: {dev.name} connected but no sink')
            # Undo the guess above: a BT sink that shows up later would
            # otherwise take new streams while Mello shows the speaker.
            self._set_default_sink(WM8960_SINK)
            with self._lock:
                self._connected_device = dev
                self._audio_active = False
                self._desired_sink = None
            self._on_audio_changed(False)
            self._on_invalidate()

    def _set_connected(self, dev: BluetoothDevice, sink: str):
        """Set device as connected and auto-switch audio to BT."""
        logger.info(f'Bluetooth: {dev.name} active with sink {sink}')
        with self._lock:
            self._audio_generation += 1  # Supersede any in-flight toggle
            self._connected_device = dev
            self._audio_active = True
            self._desired_sink = sink
            self._reconnect_failures = 0
            self._reconnect_cooldown = 0
        # Volume first, then move the music: otherwise the first moment
        # plays at the headphone's previous (often full) volume.
        self._on_audio_changed(True)
        self._set_default_sink(sink)
        self._move_stream(sink)
        self._settings.set_last_bt_device_mac(dev.mac)
        self._on_invalidate()

    def _handle_device_disconnected(self, prev_dev: BluetoothDevice):
        logger.info(f'Bluetooth: {prev_dev.name} disconnected')
        self._set_default_sink(WM8960_SINK)
        self._move_stream(WM8960_SINK)
        with self._lock:
            self._connected_device = None
            was_active = self._audio_active
            self._audio_active = False
            self._desired_sink = None
        if was_active:
            self._on_audio_changed(False)
        self._on_toast(f'{prev_dev.name} disconnected')
        self._on_invalidate()

    def _try_auto_reconnect(self, paired: list):
        """Try to reconnect a paired audio device (headphone turned on).

        Strategy: try a lightweight `bluetoothctl connect` first. Only
        escalate to _reliable_connect (which restarts the adapter) if the
        light connect succeeds but PipeWire doesn't create a sink.
        Cooldown prevents hammering the adapter into a bad state.
        """
        if self._reconnect_cooldown > 0:
            self._reconnect_cooldown -= 1
            return

        last_mac = self._settings.last_bt_device_mac
        targets = [d for d in paired if d.is_audio]
        targets.sort(key=lambda d: d.mac != last_mac)
        if not targets:
            return

        dev = targets[0]

        # Skip if already connected (poll will pick it up)
        if 'Connected: yes' in self._get_device_info(dev.mac):
            return

        logger.info(f'Bluetooth: auto-reconnect {dev.name} (attempt {self._reconnect_failures + 1})')

        # Light attempt first: plain connect without adapter restart
        if not self._bt_connect(dev.mac):
            # Device not reachable — back off, don't escalate
            self._reconnect_failures += 1
            self._reconnect_cooldown = 6  # ~30s
            return

        # Connected — check if PipeWire created a sink
        sink = self._find_bt_sink(retries=5, mac=dev.mac)
        if not sink:
            # Connected but no A2DP sink — now escalate to full reliable_connect
            logger.info(f'Bluetooth: connected but no sink, escalating to reliable_connect')
            try:
                subprocess.run(['bluetoothctl', 'disconnect', dev.mac],
                               capture_output=True, timeout=5)
            except Exception:
                pass
            sink = self._reliable_connect(dev.mac)

        if sink:
            self._set_connected(self._paired_device(dev.mac), sink)
            self._reconnect_failures = 0
        else:
            self._reconnect_failures += 1
            # Back off: 6 poll cycles (~30s) between attempts
            self._reconnect_cooldown = 6

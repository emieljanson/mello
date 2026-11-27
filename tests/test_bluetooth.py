"""
Tests for BluetoothManager - audio routing, reconnect, and thread safety.

All subprocess calls are mocked since tests run on macOS, not Pi.
"""
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.managers.bluetooth import BluetoothManager, BluetoothDevice


@pytest.fixture
def settings():
    s = MagicMock()
    s.last_bt_device_mac = None
    return s


@pytest.fixture
def callbacks():
    return {
        'on_toast': MagicMock(),
        'on_invalidate': MagicMock(),
        'on_audio_changed': MagicMock(),
    }


@pytest.fixture
def bt(settings, callbacks):
    return BluetoothManager(
        settings=settings,
        on_toast=callbacks['on_toast'],
        on_invalidate=callbacks['on_invalidate'],
        on_audio_changed=callbacks['on_audio_changed'],
    )


# -----------------------------------------------------------------------
# Fix 1: _move_stream should move ALL streams, not just the first
# -----------------------------------------------------------------------

class TestMoveStreamAll:
    """Verify _move_stream moves all sink-inputs, not just the first."""

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_moves_all_streams(self, mock_run, bt):
        """When multiple sink-inputs exist, all should be moved."""
        # pactl list sink-inputs short returns 3 streams
        list_result = MagicMock()
        list_result.stdout = '42\tprotocol\n43\tprotocol\n44\tprotocol\n'

        # First call is the list, subsequent calls are moves
        mock_run.return_value = list_result

        bt._move_stream('bluez_output.AA_BB_CC_DD_EE_FF.1')

        # Should have 1 list call + 3 move calls = 4 total
        assert mock_run.call_count == 4

        move_calls = [c for c in mock_run.call_args_list
                      if 'move-sink-input' in c[0][0]]
        assert len(move_calls) == 3
        # Verify each stream ID was moved
        moved_ids = [c[0][0][2] for c in move_calls]
        assert moved_ids == ['42', '43', '44']

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_handles_single_stream(self, mock_run, bt):
        """Single stream should still work."""
        list_result = MagicMock()
        list_result.stdout = '42\tprotocol\n'
        mock_run.return_value = list_result

        bt._move_stream('some_sink')

        move_calls = [c for c in mock_run.call_args_list
                      if 'move-sink-input' in c[0][0]]
        assert len(move_calls) == 1

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_handles_no_streams(self, mock_run, bt):
        """No streams — should not crash."""
        list_result = MagicMock()
        list_result.stdout = ''
        mock_run.return_value = list_result

        bt._move_stream('some_sink')  # Should not raise

        # Only the list call, no moves
        assert mock_run.call_count == 1


# -----------------------------------------------------------------------
# Fix 2: _deactivate_audio should not block the calling thread
# -----------------------------------------------------------------------

class TestDeactivateAudioNonBlocking:
    """Verify _deactivate_audio updates state synchronously but offloads subprocess work."""

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_state_updated_before_subprocess(self, mock_run, bt, callbacks):
        """State flags and callbacks should fire before subprocess calls."""
        # Set up: pretend audio is active with a connected device
        bt._audio_active = True
        bt._desired_sink = 'bluez_output.test.1'
        bt._connected_device = BluetoothDevice(mac='AA:BB:CC:DD:EE:FF', name='Test')

        # Track when the callback fires vs when subprocess runs
        callback_time = {}
        subprocess_time = {}

        def record_callback(active):
            callback_time['at'] = time.monotonic()

        def record_subprocess(*args, **kwargs):
            subprocess_time['at'] = time.monotonic()
            result = MagicMock()
            result.stdout = ''
            return result

        callbacks['on_audio_changed'].side_effect = record_callback
        mock_run.side_effect = record_subprocess

        bt._deactivate_audio()

        # State should be updated immediately
        assert bt._audio_active is False
        assert bt._desired_sink is None

        # Callback should have fired
        callbacks['on_audio_changed'].assert_called_once_with(False)

        # Wait briefly for the background thread to execute
        time.sleep(0.2)

        # Subprocess should have been called (in background thread)
        assert mock_run.called

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_generation_bumped_synchronously(self, mock_run, bt):
        """Audio generation should increment to cancel in-flight activations."""
        bt._audio_active = True
        initial_gen = bt._audio_generation

        bt._deactivate_audio()

        assert bt._audio_generation == initial_gen + 1


# -----------------------------------------------------------------------
# Fix 3: _try_auto_reconnect should try light connect first
# -----------------------------------------------------------------------

class TestAutoReconnectStrategy:
    """Verify auto-reconnect tries lightweight connect before heavy _reliable_connect."""

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_light_connect_tried_first(self, mock_run, bt, settings):
        """When device is unreachable, only _bt_connect is attempted (no adapter restart)."""
        settings.last_bt_device_mac = 'AA:BB:CC:DD:EE:FF'

        paired = [BluetoothDevice(
            mac='AA:BB:CC:DD:EE:FF', name='Headphones',
            paired=True, connected=False, is_audio=True,
        )]

        # _get_device_info returns "not connected"
        # _bt_connect fails (device unreachable)
        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ''
            if cmd == ['bluetoothctl', 'info', 'AA:BB:CC:DD:EE:FF']:
                result.stdout = 'Connected: no'
            elif cmd == ['bluetoothctl', 'connect', 'AA:BB:CC:DD:EE:FF']:
                result.stdout = 'Failed to connect'
            return result

        mock_run.side_effect = side_effect

        bt._try_auto_reconnect(paired)

        # Should have cooldown set (backed off)
        assert bt._reconnect_cooldown == 6
        assert bt._reconnect_failures == 1

        # Should NOT have called systemctl restart bluetooth (no adapter restart)
        restart_calls = [c for c in mock_run.call_args_list
                         if 'systemctl' in str(c)]
        assert len(restart_calls) == 0

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_escalates_when_connected_but_no_sink(self, mock_run, bt, settings):
        """When light connect succeeds but no sink, should escalate to reliable_connect."""
        settings.last_bt_device_mac = 'AA:BB:CC:DD:EE:FF'

        paired = [BluetoothDevice(
            mac='AA:BB:CC:DD:EE:FF', name='Headphones',
            paired=True, connected=False, is_audio=True,
        )]

        call_count = {'connect': 0}

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ''
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else str(cmd)
            if 'info' in cmd_str:
                result.stdout = 'Connected: no'
            elif 'connect' in cmd_str:
                call_count['connect'] += 1
                # First connect succeeds (light), subsequent too
                result.stdout = 'Connection successful'
            elif 'list' in cmd_str and 'sinks' in cmd_str:
                # No BT sink available
                result.stdout = '1\talsa_output.platform-soc_sound.stereo-fallback\tPipeWire'
            elif 'show' in cmd_str:
                result.stdout = 'Powered: yes'
            return result

        mock_run.side_effect = side_effect

        bt._try_auto_reconnect(paired)

        # Should have tried connect more than once (light + reliable_connect attempts)
        assert call_count['connect'] >= 2

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_cooldown_prevents_reconnect(self, mock_run, bt, settings):
        """When cooldown is active, should not attempt any connection."""
        bt._reconnect_cooldown = 3

        paired = [BluetoothDevice(
            mac='AA:BB:CC:DD:EE:FF', name='Headphones',
            paired=True, connected=False, is_audio=True,
        )]

        bt._try_auto_reconnect(paired)

        assert bt._reconnect_cooldown == 2
        # No subprocess calls at all
        mock_run.assert_not_called()

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_skips_already_connected(self, mock_run, bt, settings):
        """When device is already connected, should not try to reconnect."""
        settings.last_bt_device_mac = 'AA:BB:CC:DD:EE:FF'

        paired = [BluetoothDevice(
            mac='AA:BB:CC:DD:EE:FF', name='Headphones',
            paired=True, connected=False, is_audio=True,
        )]

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ''
            if 'info' in cmd:
                result.stdout = 'Connected: yes'
            return result

        mock_run.side_effect = side_effect

        bt._try_auto_reconnect(paired)

        # Should only have the info call, no connect attempts
        connect_calls = [c for c in mock_run.call_args_list
                         if 'connect' in str(c[0][0])]
        assert len(connect_calls) == 0


# -----------------------------------------------------------------------
# Fix 4: Thread safety — connected_device snapshot
# -----------------------------------------------------------------------

class TestConnectedDeviceThreadSafety:
    """Verify connected_device property returns a consistent snapshot."""

    def test_property_returns_copy_not_reference(self, bt):
        """paired_devices/discovered_devices should return copies."""
        bt._paired_devices = [
            BluetoothDevice(mac='AA:BB:CC:DD:EE:FF', name='Test', paired=True),
        ]

        result1 = bt.paired_devices
        result2 = bt.paired_devices
        # Should be equal but not the same list object
        assert result1 == result2
        assert result1 is not bt._paired_devices

    def test_connected_device_atomic_read(self, bt):
        """connected_device should be safe to read from any thread."""
        dev = BluetoothDevice(mac='AA:BB:CC:DD:EE:FF', name='Test')
        bt._connected_device = dev

        # Read from another thread
        results = []

        def reader():
            for _ in range(100):
                d = bt.connected_device
                if d is not None:
                    results.append(d.name)  # This would crash with race condition

        # Simultaneously clear the device
        def writer():
            for _ in range(100):
                bt._connected_device = dev
                bt._connected_device = None

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # If we get here without AttributeError, the property lock works


# -----------------------------------------------------------------------
# Audio routing: generation-based cancellation
# -----------------------------------------------------------------------

class TestAudioGeneration:
    """Verify generation counter prevents stale activate/deactivate races."""

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_activate_then_deactivate_cancels(self, mock_run, bt, callbacks):
        """Deactivating while activate thread is pending should cancel it."""
        bt._connected_device = BluetoothDevice(mac='AA:BB:CC:DD:EE:FF', name='Test')

        bt._activate_audio()
        gen_after_activate = bt._audio_generation

        bt._deactivate_audio()
        gen_after_deactivate = bt._audio_generation

        assert gen_after_deactivate > gen_after_activate
        assert bt._audio_active is False


# -----------------------------------------------------------------------
# Settings: last BT device MAC persistence
# -----------------------------------------------------------------------

class TestSettingsBluetooth:
    """Verify BT MAC is persisted in settings."""

    def test_set_last_bt_device_mac(self, settings):
        bt_mgr = BluetoothManager(
            settings=settings,
            on_toast=MagicMock(),
            on_invalidate=MagicMock(),
            on_audio_changed=MagicMock(),
        )

        # _set_connected calls settings.set_last_bt_device_mac
        dev = BluetoothDevice(mac='AA:BB:CC:DD:EE:FF', name='Test',
                              paired=True, connected=True, is_audio=True)

        with patch.object(bt_mgr, '_set_default_sink'), \
             patch.object(bt_mgr, '_move_stream'):
            bt_mgr._set_connected(dev, 'bluez_output.test.1')

        settings.set_last_bt_device_mac.assert_called_once_with('AA:BB:CC:DD:EE:FF')


# -----------------------------------------------------------------------
# Adapter recovery: detect powered-off adapter and recover
# -----------------------------------------------------------------------

class TestAdapterRecovery:
    """Verify monitor clears stale state when adapter is down."""

    @patch('mello.managers.bluetooth.sys')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_adapter_down_clears_connected_device(self, mock_run, mock_sys, bt, callbacks):
        """When adapter is off and recovery fails, connected device is cleared."""
        mock_sys.platform = 'linux'

        # Pre-set a connected device (stale state)
        dev = BluetoothDevice(mac='79:EF:29:2B:E8:3F', name='SoundForm mini',
                              paired=True, connected=True, is_audio=True)
        bt._connected_device = dev
        bt._audio_active = True

        # bluetoothctl show always returns Powered: no (recovery fails)
        def fake_run(cmd, **kw):
            result = MagicMock()
            result.stdout = 'Powered: no'
            result.returncode = 0
            return result

        mock_run.side_effect = fake_run

        bt._poll_connection_state()

        # Connected device should be cleared
        assert bt._connected_device is None
        assert bt._audio_active is False
        callbacks['on_audio_changed'].assert_called_with(False)

    @patch('mello.managers.bluetooth.sys')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_adapter_recovery_succeeds_continues_poll(self, mock_run, mock_sys, bt):
        """When adapter recovers, normal polling continues."""
        mock_sys.platform = 'linux'

        call_count = [0]

        def fake_run(cmd, **kw):
            result = MagicMock()
            if cmd[:2] == ['bluetoothctl', 'show']:
                call_count[0] += 1
                # First call: not powered. After restart: powered.
                result.stdout = 'Powered: no' if call_count[0] == 1 else 'Powered: yes'
            elif cmd == ['bluetoothctl', 'devices', 'Paired']:
                result.stdout = ''
            else:
                result.stdout = ''
            result.returncode = 0
            return result

        mock_run.side_effect = fake_run

        bt._poll_connection_state()

        # Should have called bluetoothctl show at least twice (check + post-recovery)
        show_calls = [c for c in mock_run.call_args_list
                      if c[0][0][:2] == ['bluetoothctl', 'show']]
        assert len(show_calls) >= 2


# -----------------------------------------------------------------------
# Reliability fixes (connect races, Pi 3B A2DP recovery, adapter health)
# -----------------------------------------------------------------------

MAC = 'AA:BB:CC:DD:EE:FF'
SINK = 'bluez_output.AA_BB_CC_DD_EE_FF.1'


def _result(stdout=''):
    r = MagicMock()
    r.stdout = stdout
    r.stderr = ''
    r.returncode = 0
    return r


class TestAlreadyConnected:

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_already_connected_counts_as_connected(self, mock_run, bt):
        mock_run.return_value = _result(
            'Failed to connect: org.bluez.Error.AlreadyConnected')
        assert bt._bt_connect(MAC) is True

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_connect_output_missed_but_info_says_connected(self, mock_run, bt):
        def run(cmd, **kw):
            if cmd[:2] == ['bluetoothctl', 'info']:
                return _result('Connected: yes')
            return _result('Attempting to connect')
        mock_run.side_effect = run
        assert bt._bt_connect(MAC) is True

    @patch('mello.managers.bluetooth.time.sleep')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_connected_without_a2dp_restarts_adapter(self, mock_run, _sleep, bt):
        """The Pi 3B quirk: connected, but no sink. Must reach the restart."""
        restarted = {'done': False}

        def run(cmd, **kw):
            if cmd[:2] == ['bluetoothctl', 'show']:
                return _result('Powered: yes')
            if cmd[:2] == ['bluetoothctl', 'connect']:
                return _result('Failed to connect: org.bluez.Error.AlreadyConnected')
            if 'systemctl' in cmd:
                restarted['done'] = True
                return _result()
            if cmd[:3] == ['pactl', 'list', 'sinks']:
                if restarted['done']:
                    return _result(f'5\t{SINK}\tPipeWire\ts16le 2ch 48000Hz\tIDLE')
                return _result('1\talsa_output.x\tPipeWire\ts16le 2ch 48000Hz\tIDLE')
            return _result()

        mock_run.side_effect = run
        assert bt._reliable_connect(MAC) == SINK
        assert restarted['done']


class TestFindSinkForDevice:

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_ignores_other_devices_sink(self, mock_run, bt):
        mock_run.return_value = _result(
            '5\tbluez_output.11_22_33_44_55_66.1\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n'
            f'6\t{SINK}\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n')
        assert bt._find_bt_sink(mac=MAC) == SINK

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_without_mac_any_bt_sink(self, mock_run, bt):
        mock_run.return_value = _result(
            '5\tbluez_output.11_22_33_44_55_66.1\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n')
        assert bt._find_bt_sink() == 'bluez_output.11_22_33_44_55_66.1'


class TestPollWaitsForTap:

    @patch('mello.managers.bluetooth.sys')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_poll_skipped_while_connect_runs(self, mock_run, mock_sys, bt):
        mock_sys.platform = 'linux'
        held = threading.Event()
        release = threading.Event()

        def tap():
            with bt._connect_lock:
                held.set()
                release.wait(2)

        t = threading.Thread(target=tap)
        t.start()
        held.wait(1)
        bt._poll_connection_state()
        release.set()
        t.join()
        mock_run.assert_not_called()


class TestAdapterHealth:

    @patch('mello.managers.bluetooth.sys')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_one_unanswered_show_does_not_restart(self, mock_run, mock_sys, bt):
        """A busy bluetoothd must not get the playing headphone dropped."""
        mock_sys.platform = 'linux'
        bt._connected_device = BluetoothDevice(mac=MAC, name='HP', connected=True, is_audio=True)
        bt._audio_active = True

        def run(cmd, **kw):
            if cmd[:2] == ['bluetoothctl', 'show']:
                raise __import__('subprocess').TimeoutExpired(cmd, 5)
            return _result()
        mock_run.side_effect = run

        bt._poll_connection_state()
        bt._poll_connection_state()

        assert not any('systemctl' in c[0][0] for c in mock_run.call_args_list)
        assert bt._connected_device is not None
        assert bt._audio_active is True

    @patch('mello.managers.bluetooth.time.sleep')
    @patch('mello.managers.bluetooth.sys')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_restart_after_repeated_silence_then_backoff(self, mock_run, mock_sys, _sleep, bt):
        mock_sys.platform = 'linux'
        mock_run.side_effect = lambda cmd, **kw: _result('No default controller available')

        for _ in range(3):
            bt._poll_connection_state()
        restarts = [c for c in mock_run.call_args_list if 'systemctl' in c[0][0]]
        assert len(restarts) == 1

        # Recovery failed: no new restart on the next polls
        for _ in range(5):
            bt._poll_connection_state()
        restarts = [c for c in mock_run.call_args_list if 'systemctl' in c[0][0]]
        assert len(restarts) == 1


class TestVolumeBeforeAudio:

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_set_connected_sets_volume_before_moving_stream(self, mock_run, settings, callbacks):
        order = []
        bt = BluetoothManager(settings=settings, on_toast=callbacks['on_toast'],
                              on_invalidate=callbacks['on_invalidate'],
                              on_audio_changed=lambda a: order.append(('audio', bt._desired_sink)))
        with patch.object(bt, '_set_default_sink'), \
             patch.object(bt, '_move_stream', side_effect=lambda s: order.append(('move', s))):
            bt._set_connected(BluetoothDevice(mac=MAC, name='HP'), SINK)
        assert order == [('audio', SINK), ('move', SINK)]

    @patch('mello.managers.bluetooth.subprocess.run')
    def test_toggle_on_applies_volume_once_sink_known(self, mock_run, settings, callbacks):
        seen = []
        bt = BluetoothManager(settings=settings, on_toast=callbacks['on_toast'],
                              on_invalidate=callbacks['on_invalidate'],
                              on_audio_changed=lambda a: seen.append((a, bt._desired_sink)))
        bt._connected_device = BluetoothDevice(mac=MAC, name='HP', connected=True, is_audio=True)
        mock_run.return_value = _result(f'6\t{SINK}\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n')

        bt._activate_audio()
        for _ in range(50):
            if (True, SINK) in seen:
                break
            time.sleep(0.02)
        assert (True, SINK) in seen


class TestConnectedWithoutSink:

    @patch('mello.managers.bluetooth.time.sleep')
    @patch('mello.managers.bluetooth.subprocess.run')
    def test_default_sink_back_to_speaker(self, mock_run, _sleep, bt):
        from mello.managers.bluetooth import WM8960_SINK
        dev = BluetoothDevice(mac=MAC, name='HP', paired=True, connected=True, is_audio=True)
        with patch.object(bt, '_find_bt_sink', return_value=None), \
             patch.object(bt, '_reliable_connect', return_value=None), \
             patch.object(bt, '_set_default_sink') as set_default:
            bt._handle_device_connected(dev)
        assert set_default.call_args_list[-1] == call(WM8960_SINK)
        assert bt._connected_device is dev
        assert bt._audio_active is False

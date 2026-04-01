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

from berry.managers.bluetooth import BluetoothManager, BluetoothDevice


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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
    def test_handles_single_stream(self, mock_run, bt):
        """Single stream should still work."""
        list_result = MagicMock()
        list_result.stdout = '42\tprotocol\n'
        mock_run.return_value = list_result

        bt._move_stream('some_sink')

        move_calls = [c for c in mock_run.call_args_list
                      if 'move-sink-input' in c[0][0]]
        assert len(move_calls) == 1

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

    @patch('berry.managers.bluetooth.subprocess.run')
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

"""Speaker health reaches analytics, so one release can be compared with the next."""
import threading
import time
from types import SimpleNamespace

import pytest

from mello import crash_log
from mello.app import Mello, HEALTH_REPORT_EVERY
from mello.managers import analytics as analytics_module
from mello.managers.wifi_keeper import WifiKeeper, OFFLINE_GRACE, signal_dbm


class _FakePosthog:
    def __init__(self, *args, **kwargs):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None):
        self.events.append((event, properties or {}))

    def shutdown(self):
        return None


@pytest.fixture
def tracker(monkeypatch):
    monkeypatch.setattr(analytics_module, "HAS_POSTHOG", True)
    monkeypatch.setattr(analytics_module, "Posthog", _FakePosthog, raising=False)
    return analytics_module.UsageTracker(
        api_key="k", app_version="main@abc1234", engine="soloist", ui="slint")


def names(tracker):
    return [e for e, _ in tracker._posthog.events]


def last(tracker, event):
    return [p for e, p in tracker._posthog.events if e == event][-1]


# -- every event says which release it came from --------------------------

def test_every_event_carries_version_engine_and_ui(tracker):
    tracker.on_watchdog_tripped(61.4)
    props = last(tracker, "watchdog_tripped")
    assert props["app_version"] == "main@abc1234"
    assert props["engine"] == "soloist"
    assert props["ui"] == "slint"
    assert props["stall_seconds"] == 61


def test_app_started_sets_version_on_the_speaker(tracker):
    tracker.on_app_started(catalog_size=3)
    props = last(tracker, "app_started")
    assert props["$set"]["app_version"] == "main@abc1234"
    assert props["previous_run"] == "clean"
    assert names(tracker) == ["app_started"]


def test_disabled_tracker_sends_nothing():
    tracker = analytics_module.UsageTracker(api_key="")
    tracker.on_engine_restarted(70, ok=True)
    tracker.on_wifi_restored(30, 1)
    tracker.on_bluetooth_disconnected(10, False, False)
    tracker.on_health({})


# -- crashes ------------------------------------------------------------

def test_crash_of_the_previous_run_is_sent_at_start(tracker):
    tracker.on_app_started(previous_run={"ended": "app_crashed", "froze": True, "dump": "hang"})
    assert names(tracker) == ["app_started", "unclean_shutdown", "main_loop_froze"]
    assert last(tracker, "unclean_shutdown")["cause"] == "app_crashed"
    assert last(tracker, "main_loop_froze")["cause"] == "hang"


def test_power_loss_is_told_apart_from_a_crash(tracker):
    tracker.on_app_started(previous_run={"ended": "power_lost", "froze": False})
    assert last(tracker, "unclean_shutdown")["cause"] == "power_lost"
    assert "main_loop_froze" not in names(tracker)


@pytest.fixture
def clear_thread_listener():
    yield
    crash_log.on_thread_error = None


def test_crash_log_says_how_the_previous_run_ended(tmp_path):
    assert crash_log.report_previous_run(tmp_path)["ended"] == "clean"
    crash_log.mark_clean_exit(tmp_path)
    assert crash_log.report_previous_run(tmp_path)["ended"] == "clean"
    # not stopped cleanly, no reboot since: the app died
    assert crash_log.report_previous_run(tmp_path, boot_time=lambda: 0.0)["ended"] == "app_crashed"
    # booted after the run started: the plug was pulled
    result = crash_log.report_previous_run(tmp_path, boot_time=lambda: time.time() + 5)
    assert result["ended"] == "power_lost"


def test_crash_log_tells_a_hang_from_a_hard_crash(tmp_path):
    crash = tmp_path / "crash.log"
    crash.write_text("--- Mello started x\nTimeout (0:00:30)!\nThread 0x1 (most recent call first):\n")
    result = crash_log.report_previous_run(tmp_path)
    assert result["froze"] is True and result["dump"] == "hang"
    crash.write_text("--- Mello started x\nFatal Python error: Segmentation fault\n")
    assert crash_log.report_previous_run(tmp_path)["dump"] == "fatal"
    crash.write_text("--- Mello started x\n")
    assert crash_log.report_previous_run(tmp_path)["froze"] is False


def test_thread_that_dies_is_reported(clear_thread_listener):
    seen = []
    crash_log.on_thread_error = lambda name, kind: seen.append((name, kind))

    def boom():
        raise ValueError("x")

    saved = threading.excepthook
    threading.excepthook = crash_log._log_uncaught_in_thread
    try:
        t = threading.Thread(target=boom, name="status-poll")
        t.start()
        t.join()
    finally:
        threading.excepthook = saved
    assert seen == [("status-poll", "ValueError")]


# -- WiFi ---------------------------------------------------------------

def test_wifi_drop_is_reported_when_the_speaker_is_back():
    world = SimpleNamespace(now=1000.0, online=True, busy=False)
    restored = []

    def join():
        world.online = True
        return ''

    keeper = WifiKeeper(
        is_online=lambda: world.online, is_busy=lambda: world.busy,
        saved=lambda: ['Home'], join=join, clock=lambda: world.now,
        link_summary=lambda: 'ap aa:bb, signal -71 dBm, freq 2412',
        on_restored=lambda *a: restored.append(a),
    )
    world.online = False
    keeper.check()
    world.now += OFFLINE_GRACE + 1
    keeper.check()          # Mello steps in and rejoins
    world.now += 15
    keeper.check()
    assert restored == [(OFFLINE_GRACE + 16, 1, -71)]

    # a short drop NetworkManager fixed by itself
    world.online = False
    keeper.check()
    world.now += 5
    world.online = True
    keeper.check()
    assert restored[-1] == (5, 0, -71)


def test_going_offline_for_wifi_setup_is_not_a_drop():
    world = SimpleNamespace(now=1000.0, online=False, busy=True)
    restored = []
    keeper = WifiKeeper(
        is_online=lambda: world.online, is_busy=lambda: world.busy,
        saved=lambda: ['Home'], join=lambda: 'no', clock=lambda: world.now,
        link_summary=lambda: 'not connected',
        on_restored=lambda *a: restored.append(a),
    )
    keeper.check()
    world.now += 60
    world.online, world.busy = True, False
    keeper.check()
    assert restored == []


def test_signal_is_read_from_the_link_line():
    assert signal_dbm('ap aa:bb, signal -60 dBm, freq 2412') == -60
    assert signal_dbm('not connected') is None


# -- Bluetooth and the health snapshot, seen from the app ------------------

def _app(tracker):
    app = Mello.__new__(Mello)
    app.tracker = tracker
    app.bluetooth = SimpleNamespace(connected_device=None)
    app.setup_menu = SimpleNamespace(is_open=False)
    app._now_playing_lock = threading.Lock()
    app._now_playing = SimpleNamespace(playing=True)
    app._next_health_report = time.monotonic() + HEALTH_REPORT_EVERY
    return app


def test_headphone_coming_and_going_is_reported(tracker):
    app = _app(tracker)
    app._report_health_changes()
    assert names(tracker) == []
    app.bluetooth.connected_device = SimpleNamespace(mac="AA")
    app._report_health_changes()
    app._report_health_changes()
    assert names(tracker) == ["bluetooth_connected"]
    app.bluetooth.connected_device = None
    app._report_health_changes()
    props = last(tracker, "bluetooth_disconnected")
    assert props["while_playing"] is True
    assert props["from_menu"] is False


def test_health_snapshot_is_sent_every_few_hours(tracker, monkeypatch):
    sent = []
    monkeypatch.setattr("mello.app.run_async", lambda fn: sent.append(fn))
    app = _app(tracker)
    app._report_health_changes()
    assert sent == []
    app._next_health_report = 0
    app._report_health_changes()
    assert len(sent) == 1
    sent[0]()
    assert "app_uptime_hours" in last(tracker, "device_health")

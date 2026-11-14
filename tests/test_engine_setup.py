"""
Tests for the Spotify engine setup page (entering the Soloist API key).
"""
import stat
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.managers.engine_setup import EngineSetup, valid_key
from mello.managers.setup_menu import SetupMenu
from mello.models import MenuState

KEY = 'abcDEF0123456789-_abcDEF0123456789xyz'


def setup_for(tmp_path, script_body='exit 0', engine='librespot', **kwargs):
    script = tmp_path / 'fake-setup.sh'
    script.write_text(f'echo "$1" >> "{tmp_path}/calls"\n{script_body}\n')
    return EngineSetup(engine=engine, soloist_home=tmp_path / 'soloist', port=0, script=script, **kwargs)


def wait_for(condition, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_key_validation_rejects_what_cannot_be_a_key():
    assert valid_key(KEY)
    assert not valid_key('')
    assert not valid_key('short')
    assert not valid_key('this is a sentence, not a key at all')
    assert not valid_key('x' * 300)


def test_saving_a_key_stores_it_privately_and_starts_the_switch(tmp_path):
    setup = setup_for(tmp_path)

    assert setup.submit_key(f'  {KEY}\n') is True

    key_file = tmp_path / 'soloist' / 'key'
    assert key_file.read_text() == KEY + '\n'
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    assert wait_for(lambda: (tmp_path / 'calls').exists())
    assert (tmp_path / 'calls').read_text().split() == ['activate']
    assert setup.state == 'switching'


def test_a_bad_key_is_refused_without_touching_the_speaker(tmp_path):
    setup = setup_for(tmp_path)

    assert setup.submit_key('not a key') is False

    assert setup.state == 'failed'
    assert not (tmp_path / 'soloist').exists()
    assert not (tmp_path / 'calls').exists()


def test_failed_switch_is_reported_with_the_scripts_reason(tmp_path):
    changes = []
    setup = setup_for(
        tmp_path, on_change=lambda: changes.append(1),
        script_body='echo "2026-11-14 [soloist] ERROR: download failed"; exit 1',
    )

    setup.submit_key(KEY)

    assert wait_for(lambda: setup.state == 'failed')
    assert 'download failed' in setup.error
    assert KEY not in setup.error
    assert changes


def test_a_second_request_while_switching_is_ignored(tmp_path):
    setup = setup_for(tmp_path, script_body='sleep 1')

    assert setup.submit_key(KEY) is True
    assert setup.switch_back() is False


def test_page_exists_only_between_start_and_stop_and_never_shows_the_key(tmp_path):
    setup = setup_for(tmp_path, engine='soloist')
    assert setup.start() is True
    port = setup._server.server_address[1]
    url = f'http://127.0.0.1:{port}'
    try:
        page = urllib.request.urlopen(url, timeout=3).read().decode()
        assert 'Soloist API key' in page
        assert 'Switch back to go-librespot' in page      # only offered while on Soloist

        answer = urllib.request.urlopen(
            urllib.request.Request(f'{url}/key', data=f'key={KEY}'.encode()), timeout=3,
        ).read().decode()
        assert 'Switching the speaker over' in answer
        assert KEY not in answer
    finally:
        setup.stop()

    try:
        urllib.request.urlopen(url, timeout=1)
        reachable = True
    except OSError:
        reachable = False
    assert reachable is False


def make_menu(engine_setup):
    return SetupMenu(
        catalog_manager=None, settings=None,
        on_toast=lambda _m: None, on_invalidate=lambda: None, on_library_cleared=lambda: None,
        engine_setup=engine_setup,
    )


class FakeRect:
    def collidepoint(self, _x, _y):
        return True


def test_menu_serves_the_page_only_while_the_spotify_screen_is_open(tmp_path):
    setup = setup_for(tmp_path)
    menu = make_menu(setup)
    menu.open()
    assert setup.running is False

    menu.handle_tap((0, 0), {'spotify': FakeRect()})
    assert menu.state == MenuState.SPOTIFY
    assert setup.running is True

    menu.handle_tap((0, 0), {'close': FakeRect()})       # back to the main menu
    assert menu.state == MenuState.MAIN
    assert setup.running is False

    menu.handle_tap((0, 0), {'spotify': FakeRect()})
    menu.close()                                           # menu closed from the Spotify screen
    assert setup.running is False


def test_screen_tells_where_to_go_and_what_is_in_use(tmp_path, monkeypatch):
    import mello.managers.engine_setup as engine_setup
    monkeypatch.setattr(engine_setup, 'lan_address', lambda: '192.168.1.50')
    setup = setup_for(tmp_path)
    setup.port = 8080

    assert setup.screen_lines() == ['Connect Mello to WiFi first.']    # page not running yet

    setup._server = object()                                           # as if started
    lines = setup.screen_lines()
    assert '192.168.1.50:8080' in lines
    assert lines[0] == 'go-librespot'
    setup._server = None


def test_key_from_wifi_setup_is_kept_until_the_speaker_is_online(tmp_path, monkeypatch):
    import mello.managers.engine_setup as engine_setup
    setup = setup_for(tmp_path)
    setup.start(defer=True)
    try:
        assert setup.submit_key(KEY) is True
    finally:
        setup.stop()

    # Stored, marked pending, but nothing switched: there is no internet yet.
    assert (tmp_path / 'soloist' / 'key').read_text() == KEY + '\n'
    assert setup.pending_file.exists()
    assert not (tmp_path / 'calls').exists()
    assert setup.state == 'saved'

    # Once online (app start, or WiFi just connected) the switch runs.
    class Reachable:
        def close(self):
            pass
    monkeypatch.setattr(engine_setup.socket, 'create_connection', lambda *_a, **_k: Reachable())
    setup.activate_pending_when_online()

    assert wait_for(lambda: (tmp_path / 'calls').exists())
    assert (tmp_path / 'calls').read_text().split() == ['activate']


def test_nothing_happens_online_when_no_key_is_waiting(tmp_path):
    setup = setup_for(tmp_path)

    setup.activate_pending_when_online()
    time.sleep(0.1)

    assert not (tmp_path / 'calls').exists()


def test_wifi_setup_serves_the_key_page_in_deferred_mode(tmp_path, monkeypatch):
    setup = setup_for(tmp_path)
    menu = make_menu(setup)
    monkeypatch.setattr(menu, '_kill_wifi_processes', lambda: None)
    monkeypatch.setattr(menu, '_delete_stale_ap_profile', lambda: None)
    monkeypatch.setattr(menu, '_launch_wifi_connect', lambda: None)
    monkeypatch.setattr(menu, '_restore_wifi_autoconnect', lambda: None)
    monkeypatch.setattr(menu, '_reconnect_to_known_network', lambda: None)
    import mello.managers.setup_menu as setup_menu
    monkeypatch.setattr(setup_menu.subprocess, 'run', lambda *_a, **_k: None)
    monkeypatch.setattr(setup_menu.time, 'sleep', lambda _s: None)

    menu._start_wifi_ap()
    assert menu.state == MenuState.WIFI_AP
    assert setup.running is True and setup._defer is True

    menu.handle_tap((0, 0), {'close': FakeRect()})        # back out of WiFi setup
    assert setup.running is False


def test_wifi_page_posts_the_key_to_mello_before_the_wifi_details():
    page = (Path(__file__).parent.parent / 'portal' / 'index.html').read_text()
    assert "':8080/key'" in page
    assert page.index("':8080/key'") < page.index("fetch('/connect'")


def test_backing_out_while_wifi_setup_is_starting_puts_the_speaker_back_online(tmp_path, monkeypatch):
    # The preparation takes several seconds on its own thread. Tapping back
    # in that window used to leave WiFi disconnected with autoconnect off.
    import mello.managers.setup_menu as setup_menu
    menu = make_menu(None)
    calls = []
    monkeypatch.setattr(menu, '_kill_wifi_processes', lambda: calls.append('kill'))
    monkeypatch.setattr(menu, '_delete_stale_ap_profile', lambda: None)
    monkeypatch.setattr(menu, '_launch_wifi_connect', lambda: calls.append('launch'))
    monkeypatch.setattr(menu, '_restore_wifi_autoconnect', lambda: calls.append('restore'))
    monkeypatch.setattr(menu, '_reconnect_to_known_network', lambda: calls.append('reconnect'))

    def fake_run(command, **_kwargs):
        if 'disconnect' in command:
            menu.state = MenuState.WIFI_LIST          # the user taps back right here
    monkeypatch.setattr(setup_menu.subprocess, 'run', fake_run)
    monkeypatch.setattr(setup_menu.time, 'sleep', lambda _s: None)

    menu._start_wifi_ap()
    assert wait_for(lambda: 'reconnect' in calls)

    assert 'launch' not in calls
    assert calls[-2:] == ['restore', 'reconnect']

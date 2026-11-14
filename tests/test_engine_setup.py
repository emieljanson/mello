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
        assert 'Switching to Soloist' in answer
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
    monkeypatch.setattr(engine_setup, 'qr_matrix', lambda text: [[True]] if text == 'http://192.168.1.50:8080' else None)
    setup = setup_for(tmp_path)
    setup.port = 8080

    assert setup.screen_items() == [('text', 'Connect Mello to WiFi first')]    # page not running yet

    setup._server = object()                                                     # as if started
    items = setup.screen_items()
    assert ('qr', [[True]]) in items                 # scannable: opens the key page
    assert ('text', '192.168.1.50:8080') in items    # and typeable
    setup._server = None


def test_screen_falls_back_to_the_address_when_no_qr_code_can_be_made(tmp_path, monkeypatch):
    import mello.managers.engine_setup as engine_setup
    monkeypatch.setattr(engine_setup, 'lan_address', lambda: '192.168.1.50')
    monkeypatch.setattr(engine_setup, 'qr_matrix', lambda _text: None)   # library not installed yet
    setup = setup_for(tmp_path)
    setup.port = 8080
    setup._server = object()

    items = setup.screen_items()
    assert not [item for item in items if item[0] == 'qr']
    assert ('text', '192.168.1.50:8080') in items
    setup._server = None


# --- the prompt that tells an updated speaker's owner about Soloist ------------

class FakeSettings:
    def __init__(self):
        self.done = False

    @property
    def soloist_prompt_due(self):
        return not self.done

    def finish_soloist_prompt(self):
        self.done = True


def prompt_menu(tmp_path, monkeypatch, machine='aarch64'):
    import mello.managers.engine_setup as engine_setup
    monkeypatch.setattr(engine_setup.platform, 'machine', lambda: machine)
    setup = setup_for(tmp_path)
    menu = make_menu(setup)
    menu.settings = FakeSettings()
    return menu, setup


def test_prompt_cannot_be_tapped_away_only_held_away(tmp_path, monkeypatch):
    import mello.managers.setup_menu as setup_menu
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    try:
        assert menu.show_spotify_prompt() is True
        assert menu.state == MenuState.SPOTIFY and setup.running

        rects = {'prompt_close': FakeRect(), 'close': FakeRect()}
        menu.handle_tap((0, 0), rects)                       # a child taps around
        assert menu.is_open

        clock = [1000.0]
        monkeypatch.setattr(setup_menu.time, 'time', lambda: clock[0])
        menu.handle_press((0, 0), rects)
        clock[0] += 1.0
        menu.update()
        assert menu.is_open and 0.3 < menu.prompt_hold_progress < 0.4
        menu.handle_release()                                # let go too early
        clock[0] += 5.0
        menu.update()
        assert menu.is_open and menu.prompt_hold_progress == 0.0

        menu.handle_press((0, 0), rects)                     # a parent holds it
        clock[0] += setup_menu.PROMPT_HOLD_SECONDS
        menu.update()
        assert not menu.is_open
        assert menu.settings.done is True
        assert setup.running is False

        assert menu.show_spotify_prompt() is False           # closed on purpose: stays away
    finally:
        setup.stop()


def test_unattended_prompt_steps_aside_and_returns_at_the_next_wake(tmp_path, monkeypatch):
    # An open menu keeps the screen on, so it must not sit there all night.
    import mello.managers.setup_menu as setup_menu
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    try:
        clock = [1000.0]
        monkeypatch.setattr(setup_menu.time, 'time', lambda: clock[0])
        assert menu.show_spotify_prompt() is True
        clock[0] += setup_menu._SPOTIFY_PROMPT_TIMEOUT + 1
        menu.update()
        assert not menu.is_open and setup.running is False
        assert menu.settings.done is False

        assert menu.show_spotify_prompt() is True            # next wake
    finally:
        setup.stop()


def test_prompt_is_not_shown_where_soloist_is_no_option_or_already_set_up(tmp_path, monkeypatch):
    menu, setup = prompt_menu(tmp_path, monkeypatch, machine='armv7l')     # 32-bit: no Soloist build
    assert menu.show_spotify_prompt() is False

    menu, setup = prompt_menu(tmp_path, monkeypatch)
    (tmp_path / 'soloist').mkdir(exist_ok=True)
    (tmp_path / 'soloist' / 'key').write_text('k' * 37)                    # key already entered
    assert menu.show_spotify_prompt() is False

    (tmp_path / 'other').mkdir()
    menu, setup = prompt_menu(tmp_path / 'other', monkeypatch)
    setup.engine = 'soloist'                                               # already switched
    assert menu.show_spotify_prompt() is False


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


def test_soloist_is_offered_on_the_speaker_right_after_wifi_setup_not_on_the_wifi_page(tmp_path, monkeypatch):
    # The WiFi page opens in a temporary browser without internet: nobody can
    # fetch a key from Spotify there and come back. So the offer is made on
    # the speaker once WiFi is up.
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    monkeypatch.setattr(menu, '_restore_wifi_autoconnect', lambda: None)

    class Finished:
        def poll(self):
            return 0
    try:
        menu.state = MenuState.WIFI_AP
        menu._wifi_process = Finished()
        menu.update()
        assert menu.state == MenuState.SPOTIFY and menu.spotify_prompt_active
        assert setup.running is True
    finally:
        setup.stop()

    page = (Path(__file__).parent.parent / 'portal' / 'index.html').read_text()
    assert 'soloist' not in page.lower()

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


def test_failed_switch_tells_the_owner_what_to_do_and_logs_the_reason(tmp_path, caplog):
    changes = []
    setup = setup_for(
        tmp_path, on_change=lambda: changes.append(1),
        script_body='echo "2026-11-14 [soloist] ERROR: download failed"; exit 1',
    )

    with caplog.at_level('INFO'):
        setup.submit_key(KEY)
        assert wait_for(lambda: setup.state == 'failed')

    assert 'try again' in setup.error
    assert 'download failed' not in setup.error          # the owner gets plain words
    assert 'download failed' in caplog.text              # the reason is in the log
    assert KEY not in setup.error
    assert changes


def test_a_mello_that_cannot_run_the_new_player_says_so(tmp_path):
    setup = setup_for(
        tmp_path,
        script_body='echo "[soloist] ERROR: no Soloist build for armv7l; this speaker needs a 64-bit system"; exit 1',
    )

    setup.submit_key(KEY)

    assert wait_for(lambda: setup.state == 'failed')
    assert setup.error == "This Mello can't use faster playback yet."


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
        assert 'Paste the code' in page
        assert 'librespot' not in page.replace('action="/librespot"', '')   # no engine names for the owner
        assert 'Turn off faster playback' in page      # only offered while on Soloist

        answer = urllib.request.urlopen(
            urllib.request.Request(f'{url}/key', data=f'key={KEY}'.encode()), timeout=3,
        ).read().decode()
        assert 'Mello restarts by itself' in answer
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
    assert ('text', '192.168.1.50:8080') not in items    # an address is only shown when there is no code
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

class FakeCatalog:
    def __init__(self):
        self.items = []


def prompt_menu(tmp_path, monkeypatch, machine='aarch64'):
    import mello.managers.engine_setup as engine_setup
    import mello.managers.setup_menu as setup_menu_module
    monkeypatch.setattr(engine_setup.platform, 'machine', lambda: machine)
    monkeypatch.setattr(setup_menu_module, 'lan_address', lambda: '192.168.1.50')
    monkeypatch.setattr(engine_setup, 'lan_address', lambda: '192.168.1.50')
    setup = setup_for(tmp_path)
    menu = make_menu(setup)
    from mello.managers.settings import Settings
    menu.settings = Settings(path=tmp_path / 'settings.json')
    menu.catalog_manager = FakeCatalog()
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
        assert setup.running is False

        assert menu.show_spotify_prompt() is False           # held away: not back straight away
    finally:
        setup.stop()


def test_offer_returns_after_a_few_days_three_times_and_then_never(tmp_path, monkeypatch):
    import mello.managers.settings as settings_module
    import mello.managers.setup_menu as setup_menu
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(settings_module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(setup_menu.time, 'time', lambda: clock[0])
    gap = settings_module.SOLOIST_PROMPT_GAP_DAYS * 86400
    rects = {'prompt_close': FakeRect()}

    def hold_away():
        menu.handle_press((0, 0), rects)
        clock[0] += setup_menu.PROMPT_HOLD_SECONDS
        menu.update()
        assert not menu.is_open

    try:
        assert menu.prompt_title == 'New: a faster Mello'
        buttons = []
        for _ in range(settings_module.SOLOIST_PROMPT_TRIES):
            assert menu.show_spotify_prompt() is True
            buttons.append(menu.prompt_button)
            hold_away()
            clock[0] += gap - 3600
            assert menu.show_spotify_prompt() is False       # too soon
            clock[0] += 3600
        assert buttons == ['Hold to do it later', 'Hold to do it later', 'Hold to skip this']

        clock[0] += 100 * gap
        assert menu.show_spotify_prompt() is False           # never again by itself
        # ... and that survives a restart
        assert settings_module.Settings(path=tmp_path / 'settings.json').soloist_prompt_due is False
    finally:
        setup.stop()


def test_a_new_mello_asks_to_connect_spotify_on_its_home_screen(tmp_path, monkeypatch):
    import mello.managers.settings as settings_module
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(settings_module.time, 'time', lambda: clock[0])
    try:
        menu.settings.mark_new_owner()                       # started with an empty library

        assert menu.home_connect_spotify(has_network=False, library_empty=True) is False
        assert setup.running is False                        # offline: "No internet connection" as always

        assert menu.home_connect_spotify(has_network=True, library_empty=True) is True
        assert setup.running and not menu.is_open            # no menu, nothing to tap away
        assert menu.home_lines == ('Scan the code with your phone', 'to connect Spotify')
        assert menu.show_spotify_prompt() is False           # and no pop-up on top of it

        page = setup._render('/')                            # the phone: straight to the code, nothing "new" or "faster"
        assert 'Connect Spotify' in page and 'name="key"' in page
        assert 'faster' not in page.lower() and 'update' not in page.lower()

        # Settings > Spotify shows the same screen; going back keeps the page up for the home screen
        menu.open()
        menu._show_spotify_screen()
        assert ('text', 'to connect Spotify') in menu.spotify_menu_items
        menu.close()
        assert setup.running

        # Played to Mello and saved an album without connecting: setting up is over
        assert menu.home_connect_spotify(has_network=True, library_empty=False) is False
        assert setup.running is False
        menu.catalog_manager.items = ['an album']
        assert menu.show_spotify_prompt() is False           # the offer follows days later
        clock[0] += settings_module.SOLOIST_PROMPT_GAP_DAYS * 86400
        assert menu.show_spotify_prompt() is True
        assert menu.prompt_title == 'Make Mello faster'
        assert menu.prompt_button == 'Hold to do it later'
        assert 'A faster Mello' in setup._render('/') and 'New in this update' not in setup._render('/')
    finally:
        setup.stop()


def test_settings_says_what_scanning_the_code_does(tmp_path, monkeypatch):
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    try:
        menu.open()
        menu._show_spotify_screen()
        items = menu.spotify_menu_items
        assert items[:2] == [('text', 'Scan the code with your phone'), ('text', 'to make Mello faster')]
        setup.engine = 'soloist'
        assert menu.spotify_menu_items[0] == ('text', 'Faster playback is on')
    finally:
        setup.stop()


def test_offer_is_not_made_while_mello_is_offline(tmp_path, monkeypatch):
    import mello.managers.setup_menu as setup_menu
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_menu, 'lan_address', lambda: None)
    assert menu.show_spotify_prompt() is False


def test_a_mello_that_closed_the_old_prompt_is_not_asked_again(tmp_path):
    from mello.managers.settings import Settings
    path = tmp_path / 'settings.json'
    path.write_text('{"soloist_prompt_done": true}')
    assert Settings(path=path).soloist_prompt_due is False


def test_phone_first_explains_then_asks_for_the_code(tmp_path):
    setup = setup_for(tmp_path)
    intro = setup._render('/')
    assert 'New in this update' in intro and 'A faster Mello' in intro
    assert 'href="/code"' in intro and 'name="key"' not in intro

    setup.new_owner = True
    assert 'New in this update' not in setup._render('/')

    code = setup._render('/code')
    assert 'Get your code from Spotify' in code and 'name="key"' in code

    setup._set('failed', 'That code is wrong')               # an error belongs on the code page
    assert 'That code is wrong' in setup._render('/')


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
        assert menu.settings.soloist_prompt_due is True

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


def test_wifi_setup_ends_on_the_normal_screen_without_any_offer(tmp_path, monkeypatch):
    # A new owner first just plays music; the faster-Mello offer comes days later.
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    monkeypatch.setattr(menu, '_restore_wifi_autoconnect', lambda: None)

    class Finished:
        def poll(self):
            return 0
    try:
        menu.state = MenuState.WIFI_AP
        menu._wifi_process = Finished()
        menu.update()
        assert menu.state == MenuState.CLOSED and not menu.spotify_prompt_active
        assert setup.running is False
    finally:
        setup.stop()

    page = (Path(__file__).parent.parent / 'portal' / 'index.html').read_text()
    assert 'soloist' not in page.lower()


# --- edge cases found in review ------------------------------------------------

def test_a_code_that_did_not_switch_the_speaker_is_not_left_behind(tmp_path, monkeypatch):
    # Otherwise Mello would count as "already set up" and never ask again,
    # while the phone says "try again".
    import mello.managers.engine_setup as engine_setup
    monkeypatch.setattr(engine_setup.platform, 'machine', lambda: 'aarch64')
    setup = setup_for(tmp_path, script_body='echo download failed; exit 1')
    assert setup.submit_key(KEY) is True
    assert wait_for(lambda: setup.state == 'failed')
    assert not (setup.soloist_home / 'key').exists()
    assert setup.can_offer_soloist() is True


def test_a_failed_change_of_code_keeps_the_code_a_running_player_uses(tmp_path):
    setup = setup_for(tmp_path, engine='soloist', script_body='exit 1')
    assert setup.submit_key(KEY) is True
    assert wait_for(lambda: setup.state == 'failed')
    assert (setup.soloist_home / 'key').exists()


def test_home_screen_stays_through_the_switch_and_comes_back_if_it_fails(tmp_path, monkeypatch):
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    try:
        menu.settings.mark_new_owner()
        assert menu.home_connect_spotify(has_network=True, library_empty=True) is True

        setup.script.write_text('#!/bin/bash\nsleep 0.6; exit 1\n')
        assert setup.submit_key(KEY) is True                 # the code is in: key file exists now
        assert menu.home_connect_spotify(has_network=True, library_empty=True) is True
        assert menu.spotify_switching and setup.running      # "One moment...", page still answers

        assert wait_for(lambda: setup.state == 'failed')     # the switch did not work
        assert menu.home_connect_spotify(has_network=True, library_empty=True) is True
        assert setup.running                                 # the code is back on screen to try again
        assert 'try again' in setup._render('/').lower()
    finally:
        setup.stop()


def test_offer_steps_aside_even_when_wifi_dropped_while_it_was_showing(tmp_path, monkeypatch):
    import mello.managers.engine_setup as engine_setup
    import mello.managers.setup_menu as setup_menu
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    try:
        clock = [1000.0]
        monkeypatch.setattr(setup_menu.time, 'time', lambda: clock[0])
        assert menu.show_spotify_prompt() is True
        monkeypatch.setattr(engine_setup, 'lan_address', lambda: None)   # router off for the night
        setup._address_at = float('-inf')
        clock[0] += setup_menu._SPOTIFY_PROMPT_TIMEOUT + 1
        menu.update()
        assert not menu.is_open                              # not a lit screen all night
    finally:
        setup.stop()


def test_a_page_that_cannot_start_is_not_retried_on_every_frame(tmp_path, monkeypatch):
    import mello.managers.setup_menu as setup_menu
    menu, setup = prompt_menu(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(setup_menu.time, 'time', lambda: clock[0])
    attempts = []
    monkeypatch.setattr(setup, 'start', lambda: attempts.append(1) and False)
    menu.settings.mark_new_owner()
    for _ in range(50):                                      # fifty frames
        menu.home_connect_spotify(has_network=True, library_empty=True)
    assert len(attempts) == 1
    clock[0] += 31
    menu.home_connect_spotify(has_network=True, library_empty=True)
    assert len(attempts) == 2


def test_damaged_settings_file_does_not_stop_mello_from_starting(tmp_path):
    from mello.managers.settings import Settings
    path = tmp_path / 'settings.json'
    path.write_text('{"soloist_prompt_declined": null, "soloist_prompt_after": "soon"}')
    settings = Settings(path=path)
    assert settings.auto_pause_minutes == 30

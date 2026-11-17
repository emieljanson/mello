"""The speaker gets itself back onto a WiFi network it already knows."""
from mello.managers.wifi_keeper import WifiKeeper, OFFLINE_GRACE, RETRY_EVERY, QUICK_TRIES
from mello.managers.setup_menu import SetupMenu
from mello.models import MenuState


class World:
    """A speaker whose connection, clock and saved networks the test controls."""

    def __init__(self, saved=('Home',)):
        self.now = 1000.0
        self.online = True
        self.busy = False
        self.saved = list(saved)
        self.joins = 0
        self.join_fixes_it = False
        self.keeper = WifiKeeper(
            is_online=lambda: self.online, is_busy=lambda: self.busy,
            saved=lambda: self.saved, join=self._join, clock=lambda: self.now,
            link_summary=lambda: 'ap aa:bb, signal -60 dBm',
        )

    def _join(self):
        self.joins += 1
        if self.join_fixes_it:
            self.online = True
            return ''
        return 'Error: Connection activation failed'

    def wait(self, seconds):
        self.now += seconds
        self.keeper.check()


def test_leaves_a_working_connection_alone():
    w = World()
    for _ in range(20):
        w.wait(15)
    assert w.joins == 0
    assert w.keeper.take_restored() is False


def test_gives_networkmanager_a_moment_then_asks_a_few_times_every_minute():
    w = World()
    w.online = False
    w.wait(0)
    w.wait(OFFLINE_GRACE - 1)
    assert w.joins == 0                 # still NetworkManager's turn
    w.wait(1)
    assert w.joins == QUICK_TRIES       # a few attempts straight away
    w.wait(RETRY_EVERY - 1)
    assert w.joins == QUICK_TRIES       # not hammering the WiFi chip
    w.wait(1)
    assert w.joins == 2 * QUICK_TRIES
    for _ in range(8 * 60):             # a whole night of router trouble
        w.wait(60)
    assert w.joins == (2 + 8 * 60) * QUICK_TRIES   # it never gives up


def test_a_flaky_join_is_retried_straight_away():
    w = World()
    w.online = False
    w.wait(0)
    outcomes = iter(['Error: Connection activation failed', ''])

    def join():
        w.joins += 1
        problem = next(outcomes)
        w.online = not problem
        return problem
    w.keeper._join = join
    w.wait(OFFLINE_GRACE)
    assert w.joins == 2                 # second attempt, no minute lost
    w.wait(15)
    assert w.keeper.take_restored() is True


def test_stops_retrying_when_the_owner_opens_the_hotspot_mid_round():
    w = World()
    w.online = False
    w.wait(0)

    def join():
        w.joins += 1
        w.busy = True                   # owner started "+ New network"
        return 'Error: Connection activation failed'
    w.keeper._join = join
    w.wait(OFFLINE_GRACE)
    assert w.joins == 1


def test_reports_once_when_the_connection_is_back():
    w = World()
    w.online = False
    w.wait(0)
    w.join_fixes_it = True
    w.wait(OFFLINE_GRACE)
    assert w.joins == 1
    w.wait(15)
    assert w.keeper.take_restored() is True
    assert w.keeper.take_restored() is False
    w.wait(600)
    assert w.joins == 1


def test_keeps_off_the_wifi_chip_while_the_owner_sets_up_a_new_network():
    w = World()
    w.online = False
    w.busy = True
    for _ in range(10):
        w.wait(60)
    assert w.joins == 0
    w.busy = False                      # backed out of the hotspot screen
    w.wait(15)
    assert w.joins == QUICK_TRIES


def test_a_mello_that_never_joined_a_network_has_nothing_to_rejoin():
    w = World(saved=())
    w.online = False
    for _ in range(10):
        w.wait(60)
    assert w.joins == 0


def make_menu(toasts):
    menu = SetupMenu(
        catalog_manager=None, settings=None,
        on_toast=toasts.append, on_invalidate=lambda: None, on_library_cleared=lambda: None,
    )
    menu._collect_known_networks = lambda: None
    menu._leave_spotify_screen = lambda: None
    return menu


def test_wifi_screen_that_opened_by_itself_closes_when_the_connection_returns():
    toasts = []
    menu = make_menu(toasts)
    menu.show_wifi()                    # what start-up does when offline
    assert menu.state == MenuState.WIFI_LIST
    menu.network_restored()
    assert menu.state == MenuState.CLOSED
    assert toasts == ['WiFi connected!']


def test_wifi_screen_the_owner_opened_stays_open():
    toasts = []
    menu = make_menu(toasts)
    menu._show_wifi_screen()            # what tapping WiFi in Settings does
    menu.network_restored()
    assert menu.state == MenuState.WIFI_LIST
    assert toasts == []


def test_owner_browsing_from_the_self_opened_screen_is_not_thrown_out():
    menu = make_menu([])
    menu.show_wifi()
    menu._show_wifi_screen()            # went back to Settings and into WiFi again
    menu.network_restored()
    assert menu.state == MenuState.WIFI_LIST


def test_hotspot_counts_as_wifi_setup_running():
    menu = make_menu([])
    assert menu.wifi_setup_running is False
    menu.state = MenuState.WIFI_AP
    assert menu.wifi_setup_running is True


def test_network_still_being_joined_is_not_shown_as_connected(monkeypatch):
    import mello.managers.setup_menu as sm

    def fake_run(cmd, **_kw):
        from types import SimpleNamespace
        if '--active' in cmd:
            out = 'Home:802-11-wireless:activating\nlo:loopback:activated\n'
        elif cmd[:3] == ['nmcli', '-g', '802-11-wireless.ssid']:
            out = cmd[-1] + '\n'
        else:
            out = 'Home:802-11-wireless\nlo:loopback\n'
        return SimpleNamespace(stdout=out, returncode=0)

    monkeypatch.setattr(sm.subprocess, 'run', fake_run)
    menu = SetupMenu(
        catalog_manager=None, settings=None,
        on_toast=lambda _m: None, on_invalidate=lambda: None, on_library_cleared=lambda: None,
    )
    menu._collect_known_networks()
    assert menu.known_networks == ['Home']
    assert menu.current_network is None     # offline: nothing is marked connected


def test_hotspot_screen_says_when_the_chosen_network_could_not_be_joined():
    menu = make_menu([])
    menu.state = MenuState.WIFI_AP
    menu._read_join_credentials = lambda ssid: None     # entry could not be read
    menu._note_wifi_connect_line('Connecting to access point \'Home\'...')
    assert menu.wifi_join_failed is None
    assert menu.wifi_joining == 'Home'
    menu._note_wifi_connect_line(
        "[wifi_connect::network:WARN] Connection to access point not activated 'Home': Activating")
    assert menu.wifi_join_failed == 'Home'
    assert menu.wifi_joining is None
    menu._note_wifi_connect_line('User connected to the captive portal')
    assert menu.wifi_join_failed == 'Home'       # stays up while they try again


class JoiningNetworkManager:
    """nmcli for a join that wifi-connect gave up on too soon."""

    def __init__(self, ups_until_joined):
        self.ups_until_joined = ups_until_joined    # None: never joins
        self.ups = 0
        self.calls = []

    def run(self, cmd, **_kw):
        from types import SimpleNamespace
        args = [a for a in cmd if a != 'sudo']
        self.calls.append(args)
        if args[:2] == ['nmcli', '-s']:
            return SimpleNamespace(stdout='secret\n', stderr='', returncode=0)
        if 'up' in args and args[args.index('up') - 1] == 'con':
            self.ups += 1
            if self.ups_until_joined is not None and self.ups >= self.ups_until_joined:
                return SimpleNamespace(stdout='successfully activated', stderr='', returncode=0)
            return SimpleNamespace(stdout='', stderr='Error: Timeout 45 sec expired.', returncode=4)
        return SimpleNamespace(stdout='', stderr='', returncode=0)


def _join_world(monkeypatch, ups_until_joined):
    import threading
    import mello.managers.setup_menu as sm

    class Now:
        def __init__(self, target, **_kw):
            self.target = target

        def start(self):
            self.target()

    nm = JoiningNetworkManager(ups_until_joined)
    monkeypatch.setattr(sm.subprocess, 'run', nm.run)
    monkeypatch.setattr(threading, 'Thread', Now)
    monkeypatch.setattr(sm.time, 'sleep', lambda _s: None)
    toasts = []
    menu = make_menu(toasts)
    menu.state = MenuState.WIFI_AP
    menu._kill_wifi_processes = lambda: None
    menu._launch_wifi_connect = lambda: nm.calls.append(['wifi-connect'])
    menu._note_wifi_connect_line("Connecting to access point 'Home'...")
    menu._note_wifi_connect_line(
        "[wifi_connect::network:WARN] Connection to access point not activated 'Home': Activating")
    return menu, nm, toasts


def test_a_join_wifi_connect_gave_up_on_too_soon_is_finished_by_mello(monkeypatch):
    menu, nm, toasts = _join_world(monkeypatch, ups_until_joined=2)
    added = [c for c in nm.calls if c[:3] == ['nmcli', 'con', 'add']]
    assert added and added[0][-1] == 'secret'            # same password the phone sent
    assert nm.ups == 2
    assert ['wifi-connect'] not in nm.calls              # no hotspot again
    assert menu.wifi_join_failed is None
    menu.update()
    assert toasts == ['WiFi connected!']
    assert menu.state == MenuState.CLOSED


def test_a_join_that_keeps_failing_brings_the_hotspot_back_and_says_so(monkeypatch):
    from mello.managers.setup_menu import _JOIN_TRIES
    menu, nm, toasts = _join_world(monkeypatch, ups_until_joined=None)
    assert nm.ups == _JOIN_TRIES
    assert ['nmcli', 'con', 'delete', 'Home'] in nm.calls   # the failed entry is not kept
    assert nm.calls[-1] == ['wifi-connect']               # hotspot is back for another try
    assert menu.wifi_join_failed == 'Home'
    assert menu.wifi_joining is None
    menu.update()
    assert menu.state == MenuState.WIFI_AP
    assert toasts == []


def test_leaving_setup_stops_the_extra_tries(monkeypatch):
    import mello.managers.setup_menu as sm
    nm = JoiningNetworkManager(ups_until_joined=None)
    toasts = []
    menu = make_menu(toasts)

    def run(cmd, **kw):
        result = nm.run(cmd, **kw)
        if nm.ups == 1:
            menu.state = MenuState.WIFI_LIST                # owner tapped back meanwhile
        return result

    import threading

    class Now:
        def __init__(self, target, **_kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(sm.subprocess, 'run', run)
    monkeypatch.setattr(threading, 'Thread', Now)
    monkeypatch.setattr(sm.time, 'sleep', lambda _s: None)
    menu.state = MenuState.WIFI_AP
    menu._kill_wifi_processes = lambda: None
    menu._launch_wifi_connect = lambda: nm.calls.append(['wifi-connect'])
    menu._note_wifi_connect_line("Connecting to access point 'Home'...")
    menu._note_wifi_connect_line("Connection to access point not activated 'Home': Activating")
    assert nm.ups == 1
    assert ['wifi-connect'] not in nm.calls


def test_a_fresh_hotspot_starts_without_the_old_failure(monkeypatch):
    import threading
    monkeypatch.setattr(threading, 'Thread', lambda **_kw: type('T', (), {'start': lambda self: None})())
    menu = make_menu([])
    menu.wifi_join_failed = 'Home'
    menu._start_wifi_ap()
    assert menu.wifi_join_failed is None


class FakeNetworkManager:
    """Just enough of nmcli for the known-networks bookkeeping."""

    def __init__(self, networks):
        self.networks = dict(networks)      # name -> (ssid, psk)
        self.added = []

    def run(self, cmd, **_kw):
        from types import SimpleNamespace
        args = [a for a in cmd if a != 'sudo']
        out, code = '', 0
        if args[:5] == ['nmcli', '-t', '-f', 'NAME,TYPE', 'con']:
            out = ''.join(f'{n}:802-11-wireless\n' for n in self.networks) + 'lo:loopback\n'
        elif args[:2] == ['nmcli', '-s']:
            ssid, psk = self.networks[args[-1]]
            out = f'{ssid}\n{psk}\n'
        elif args[:3] == ['nmcli', '-g', '802-11-wireless.ssid']:
            out = self.networks[args[-1]][0] + '\n'
        elif args[:3] == ['nmcli', 'con', 'add']:
            name = args[args.index('con-name') + 1]
            ssid = args[args.index('ssid') + 1]
            psk = args[args.index('wifi-sec.psk') + 1] if 'wifi-sec.psk' in args else ''
            self.networks[name] = (ssid, psk)
            self.added.append(name)
        return SimpleNamespace(stdout=out, stderr='', returncode=code)


def test_known_network_deleted_by_a_failed_retype_is_put_back(monkeypatch):
    import mello.managers.setup_menu as sm
    nm = FakeNetworkManager({'Home': ('Home', 'secret1'), 'Cabin': ('Cabin', '')})
    monkeypatch.setattr(sm.subprocess, 'run', nm.run)
    menu = make_menu([])
    menu._remember_networks()
    del nm.networks['Home']           # owner typed Home again, wifi-connect deleted it, join failed
    menu._restore_wifi_autoconnect()  # owner went back / hotspot ended
    assert nm.networks['Home'] == ('Home', 'secret1')
    assert nm.added == ['Home']


def test_a_network_joined_again_is_not_doubled(monkeypatch):
    import mello.managers.setup_menu as sm
    nm = FakeNetworkManager({'Home': ('Home', 'old')})
    monkeypatch.setattr(sm.subprocess, 'run', nm.run)
    menu = make_menu([])
    menu._remember_networks()
    del nm.networks['Home']
    nm.networks['Home 1'] = ('Home', 'new')    # retype worked: new entry for the same network
    menu._restore_wifi_autoconnect()
    assert nm.added == []
    menu._restore_wifi_autoconnect()            # a second clean-up does nothing either
    assert nm.added == []


def test_log_says_which_access_point_and_signal_when_wifi_drops(caplog):
    w = World()
    caplog.set_level('INFO', logger='mello.managers.wifi_keeper')
    w.online = False
    w.wait(15)
    w.online = True
    w.wait(15)
    lines = [r.getMessage() for r in caplog.records]
    assert 'WiFi keeper: offline (ap aa:bb, signal -60 dBm)' in lines
    assert any(l.startswith('WiFi keeper: back online') and 'signal -60 dBm' in l for l in lines)


def test_link_summary_reads_access_point_signal_and_power_save(monkeypatch):
    from types import SimpleNamespace
    from mello.managers import wifi_keeper
    outputs = {
        'link': 'Connected to 08:36:c9:73:ed:8f (on wlan0)\n\tSSID: Home\n\tfreq: 2452.0\n\tsignal: -66 dBm\n',
        'get': 'Power save: off\n',
    }
    monkeypatch.setattr(wifi_keeper.subprocess, 'run',
                        lambda cmd, **kw: SimpleNamespace(stdout=outputs[cmd[3]]))
    assert wifi_keeper.wifi_link_summary() == 'ap 08:36:c9:73:ed:8f, freq 2452.0, signal -66 dBm, power save: off'

"""The speaker gets itself back onto a WiFi network it already knows."""
from mello.managers.wifi_keeper import WifiKeeper, OFFLINE_GRACE, RETRY_EVERY
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


def test_gives_networkmanager_a_moment_then_asks_again_every_minute():
    w = World()
    w.online = False
    w.wait(0)
    w.wait(OFFLINE_GRACE - 1)
    assert w.joins == 0                 # still NetworkManager's turn
    w.wait(1)
    assert w.joins == 1
    w.wait(RETRY_EVERY - 1)
    assert w.joins == 1                 # not hammering the WiFi chip
    w.wait(1)
    assert w.joins == 2
    for _ in range(8 * 60):             # a whole night of router trouble
        w.wait(60)
    assert w.joins == 2 + 8 * 60        # it never gives up


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
    assert w.joins == 1


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

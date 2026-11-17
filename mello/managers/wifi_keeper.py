"""
WiFi keeper - brings the speaker back onto a WiFi network it already knows.

NetworkManager stops trying by itself once joining has failed a few times
(a router that was slow to answer after a power cut is enough). Until
something asks again the speaker stays offline, even with the network back
and in range: it then sat on the WiFi screen all night. So while Mello is
offline, this asks again: a few times straight away, since on some speakers
only about half the attempts get through, then every half minute.
"""
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

CHECK_EVERY = 15      # seconds between looks at the connection
OFFLINE_GRACE = 20    # NetworkManager gets this long to come back by itself
RETRY_EVERY = 30      # seconds between rounds of our own attempts
QUICK_TRIES = 3       # attempts in a row per round
_JOIN_WAIT = 30       # NetworkManager's own limit for one attempt
_CONNECT_TIMEOUT = _JOIN_WAIT + 10
_NOT_OURS = {'Mello-Setup', 'mello-ap', 'mello-setup'}


def saved_networks() -> List[str]:
    """Names of the WiFi networks this speaker has joined before."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    names = []
    for line in result.stdout.splitlines():
        name, _, kind = line.rpartition(':')
        if kind == '802-11-wireless' and name and name not in _NOT_OURS:
            names.append(name)
    return names


def join_known_network() -> str:
    """Ask NetworkManager to join whichever known network is in range.

    Asking outright also works after NetworkManager gave up on its own.
    Returns '' on success, otherwise why it did not work.
    """
    try:
        result = subprocess.run(
            ['sudo', 'nmcli', '--wait', str(_JOIN_WAIT), 'device', 'connect', 'wlan0'],
            capture_output=True, text=True, timeout=_CONNECT_TIMEOUT,
        )
    except Exception as e:
        return str(e)
    if result.returncode == 0:
        return ''
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f'exit {result.returncode}'


def _iw() -> str:
    for path in ('/usr/sbin/iw', '/sbin/iw'):
        if Path(path).exists():
            return path
    return 'iw'


def wifi_link_summary() -> str:
    """One line on the WiFi link for the log: access point, signal, power save.

    The daily log review needs this to tell a weak signal, roaming between
    access points and a dozing chip apart when the speaker drops off WiFi.
    """
    parts = []
    try:
        link = subprocess.run([_iw(), 'dev', 'wlan0', 'link'],
                              capture_output=True, text=True, timeout=5).stdout
        for line in link.splitlines():
            line = line.strip()
            if line.startswith('Connected to '):
                parts.append(f"ap {line.split()[2]}")
            elif line.startswith(('signal:', 'freq:')):
                parts.append(line.replace(':', '', 1))
        if not parts:
            parts.append('not connected')
        ps = subprocess.run([_iw(), 'dev', 'wlan0', 'get', 'power_save'],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if ps:
            parts.append(ps.lower())
    except Exception as e:
        return f'unknown ({e})'
    return ', '.join(parts)


def signal_dbm(link_summary: str) -> Optional[int]:
    """The signal strength from a wifi_link_summary line, or None."""
    match = re.search(r'signal (-?\d+)', link_summary)
    return int(match.group(1)) if match else None


class WifiKeeper:
    """Watches the connection and rejoins a known network when it is gone."""

    def __init__(
        self,
        is_online: Callable[[], bool],
        is_busy: Callable[[], bool],
        saved: Callable[[], List[str]] = saved_networks,
        join: Callable[[], str] = join_known_network,
        clock: Callable[[], float] = time.time,
        link_summary: Callable[[], str] = wifi_link_summary,
        on_restored: Optional[Callable[[float, int, Optional[int]], None]] = None,
    ):
        self._is_online = is_online
        # True while the owner is setting up a new network: the WiFi chip is
        # then Mello's own hotspot and must be left alone.
        self._is_busy = is_busy
        self._saved = saved
        self._join = join
        self._clock = clock
        self._link_summary = link_summary
        # Told (offline seconds, rounds of our own rejoin attempts, signal in
        # dBm or None) each time the connection comes back: for analytics.
        self._on_restored = on_restored
        self._rounds = 0
        self._setup_seen = False
        self._offline_since: Optional[float] = None
        self._last_try = 0.0
        self._restored = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name='wifi-keeper')
            self._thread.start()

    def stop(self):
        self._stop.set()

    def take_restored(self) -> bool:
        """True once after the connection came back from being gone."""
        if self._restored.is_set():
            self._restored.clear()
            return True
        return False

    def _run(self):
        logger.info(f'WiFi keeper: started ({self._link_summary()})')
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.check()
            except Exception as e:
                logger.warning(f'WiFi keeper check failed: {e}')

    def check(self):
        """One look at the connection; rejoins when it has been gone a while."""
        now = self._clock()
        if self._is_online():
            if self._offline_since is not None:
                offline = now - self._offline_since
                link = self._link_summary()
                logger.info(f'WiFi keeper: back online after {offline:.0f}s ({link})')
                self._offline_since = None
                self._restored.set()
                # Going offline for WiFi setup is the owner's choice, not a drop.
                if self._on_restored is not None and not self._setup_seen:
                    self._on_restored(offline, self._rounds, signal_dbm(link))
                self._rounds = 0
                self._setup_seen = False
            return
        if self._offline_since is None:
            self._offline_since = now
            logger.info(f'WiFi keeper: offline ({self._link_summary()})')
        if self._is_busy():
            self._setup_seen = True
            return
        if now - self._offline_since < OFFLINE_GRACE or now - self._last_try < RETRY_EVERY:
            return
        self._last_try = now
        names = self._saved()
        if not names:
            return      # a Mello that never joined a network has nothing to go back to
        logger.info(f'WiFi keeper: offline for {now - self._offline_since:.0f}s, rejoining (known: {names})')
        self._rounds += 1
        for attempt in range(1, QUICK_TRIES + 1):
            problem = self._join()
            if not problem:
                logger.info(f'WiFi keeper: rejoin accepted (attempt {attempt})')
                return
            logger.info(f'WiFi keeper: attempt {attempt} could not rejoin: {problem}')
            if self._is_busy() or self._is_online():
                return
        self._last_try = self._clock()     # the next round counts from the end of this one

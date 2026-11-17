"""
Soloist API Client - local WebSocket API of Spotify Soloist.

Soloist is Spotify's official headless Connect client. Unlike go-librespot it
has no REST endpoint: one WebSocket carries both commands and state events.
This client keeps that socket open, caches the playback state it is told
about, and presents it in the same shape as go-librespot's /status payload so
the rest of the app does not need to know which engine is running.

It fills two roles the app keeps separate for go-librespot:
  - LibrespotAPIProtocol (status + playback commands)
  - EventListener (start/stop, on_update / on_connect / on_inactive, context_uri)
"""
import json
import logging
import threading
import time
from typing import Callable, Optional

import websocket

from .play_orders import PlayOrders, one_round

logger = logging.getLogger(__name__)

# Cover sizes Soloist can report, best first for a 720px-wide screen.
_COVER_PREFERENCE = ('xlarge', 'large', 'default', 'small')

_AUTH_ERROR = 'command requires authentication'

# `play <uri>` is accepted at once, but the switch itself takes 0.5s when this
# device is already active and up to ~12s when it first has to take over.
_SWITCH_TIMEOUT = 15.0
_PING_INTERVAL = 10
_PING_TIMEOUT = 5

# Becoming the active device took ~1s on the device.
_ACTIVATE_TIMEOUT = 6.0
# Backstop for the "loading" state shown while a resume is being positioned.
_POSITIONING_MAX = 45.0
# Soloist reports "buffering" when a track starts loading but does not always
# report "playing" once it plays: after a few quick skips the daughter's
# speaker said buffering for a minute while the episode played on, and Mello
# kept restarting it as "not started". While it says buffering, ask for the
# full state this often; that answer has the real status.
_BUFFERING_RECHECK = 2.0
# Skips are sent in bursts of this size. Measured on the device for 40 skips:
# bursts of 12 took 11.6s, of 20 10.5s, and one burst of 40 lost 26 of them
# (45s with the retries). A burst is also the longest a swipe has to wait.
_SKIP_BURST = 12
_SKIP_ROUNDS = 12
# Slack for a burst to land, on top of 0.5s per skip.
_SKIP_TIMEOUT = 4.0
# Broadcast queue updates are capped at this many entries (a full answer to
# get_queue is not), so a list of exactly this size may be a broadcast.
_QUEUE_BROADCAST_CAP = 10
# A full queue answer holds at most this many items (seen on the device).
_QUEUE_CAP = 80
# Playing a track directly: how long to wait for it to start (measured 0.3 s).
_DIRECT_TIMEOUT = 8.0
# A track counts as finished when Soloist reports it this close to its end.
# With track repeat on, Soloist says "buffering" at the very end and starts the
# same track again a second later; Mello starts the next one at that moment.
_END_MARGIN_MS = 2500
# After a fresh start (no saved track), how long to wait for the album to
# play before reading its order.
_LEARN_WAIT = 6.0
# Quick taps on next/previous are counted and only the last one is played:
# a burst of skips to Soloist loses some (seen: 6 of 12 arrived) and makes it
# open every track in between (seconds each for a podcast episode).
_STEP_SETTLE_S = 0.35
# A playlist's order is checked again on its first start after this long
# (starting a playlist and reading its order costs ~0.5 s); albums never
# change, and a show is not re-read for this (starting a show costs 10-12 s):
# new episodes come at its end, where it is read again anyway.
_PLAYLIST_FRESH_S = 20 * 3600
# If the next track did not start, try again after this long.
_ADVANCE_RETRY_S = 6.0
# The next track is started this long before the current one ends: with track
# repeat on, waiting for the end let a moment of the same track start again
# (seen on the device). The last 0.25 s of a track is nearly always silence.
_EARLY_START_S = 0.25


def _name(entity: Optional[dict]) -> Optional[str]:
    if not isinstance(entity, dict):
        return None
    return ((entity.get('decorations') or {}).get('identity') or {}).get('name')


def _is_show(context_uri: str) -> bool:
    return context_uri.startswith('spotify:show:')


def _never_changes(context_uri: str) -> bool:
    """Albums stay the same; playlists get edited and shows get new episodes.

    Only for an album may direct play go round from the last track to the
    first: elsewhere the end of the known order may be followed by something
    new, so the end is reached the normal way (and the order learned again).
    """
    return context_uri.startswith('spotify:album:')


def _playable(item: Optional[dict]) -> Optional[dict]:
    """Soloist briefly reports an item with an empty URI while it switches."""
    if isinstance(item, dict) and item.get('uri'):
        return item
    return None


def _cover_url(entity: Optional[dict]) -> Optional[str]:
    if not isinstance(entity, dict):
        return None
    covers = ((entity.get('decorations') or {}).get('visual_identity') or {}).get('cover') or []
    by_size = {c.get('size'): c.get('url') for c in covers if isinstance(c, dict) and c.get('url')}
    for size in _COVER_PREFERENCE:
        if size in by_size:
            return by_size[size]
    return next(iter(by_size.values()), None)


class SoloistAPI:
    """Playback control and state for a local Soloist daemon."""

    def __init__(
        self,
        url: str,
        on_update: Optional[Callable[[], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_inactive: Optional[Callable[[], None]] = None,
        command_timeout: float = 3.0,
        play_orders: Optional[PlayOrders] = None,
    ):
        """
        Args:
            url: WebSocket URL of the daemon (e.g. ws://127.0.0.1:9090)
            on_update: Called after any state change
            on_connect: Called when the socket reconnects after a drop
            on_inactive: Called when this device stops being the active Spotify device
            command_timeout: Seconds to wait for the daemon to accept a command
            play_orders: Known track orders; with them a resume plays the saved
                track directly instead of skipping to it (None: always skip)
        """
        self.url = url
        self.on_update = on_update
        self.on_connect = on_connect
        self.on_inactive = on_inactive
        self.command_timeout = command_timeout
        self.play_orders = play_orders
        # Direct play: a single track was started in place of its album, and
        # Mello plays the album's next track itself when it ends. Spotify only
        # sees one track (with track repeat on, so it never moves on to
        # something else by itself). {'context', 'order', 'index', 'ended'}
        self._direct: Optional[dict] = None
        self._learn_epoch = 0
        self._queue_lock = threading.Lock()   # one get_queue at a time
        self._end_timer: Optional[threading.Timer] = None
        self._step_timer: Optional[threading.Timer] = None
        # Taps on next/previous not yet sent: {'index', 'beyond'}
        self._pending_step: Optional[dict] = None
        self._reset_track_repeat = False

        self.ws: Optional[websocket.WebSocketApp] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self._was_connected = False

        self._state_lock = threading.Lock()
        self._state_changed = threading.Condition(self._state_lock)
        self._queue: Optional[dict] = None
        # Counts snapshots that name a track; lets play() tell a snapshot
        # caused by its own request from state that was already there.
        self._named_snapshots = 0
        # Track a resume was asked to land on; seek() refuses if we are not there.
        self._resume_target: Optional[str] = None
        # While a resume is skipping its way to the saved track, the app is
        # told "loading <album>" instead of the tracks flying by: otherwise a
        # swipe at that moment would save one of them as the child's place.
        self._positioning_uri: Optional[str] = None
        self._positioning_since = 0.0
        # Album whose resume was abandoned half-way: whatever track Soloist
        # stopped on is not where anybody was listening, so it is reported as
        # "paused on nothing" until the next play().
        self._abandoned_uri: Optional[str] = None
        # Bumped by abort_play(); a running play() notices and stops skipping.
        self._play_epoch = 0
        self._socket_open = False
        self._logged_in = False
        self._is_active = False
        self._status = 'idle'
        self._status_since = 0.0
        self._last_recheck = 0.0
        self._item: Optional[dict] = None
        self._context: Optional[dict] = None
        self._position: Optional[dict] = None
        self._volume: Optional[int] = None
        self._volume_reset_at = 0.0
        self._repeat = 'off'

        # Soloist answers a command with a command_result naming the command,
        # or with an error that names nothing. Commands therefore go out one
        # at a time and wait for their own result or the next error.
        self._command_lock = threading.Lock()
        self._reply_ready = threading.Event()
        self._reply: Optional[dict] = None
        self._pending_command: Optional[str] = None
        self._failure_count: dict = {}

    # ------------------------------------------------------------------
    # Connection lifecycle (same surface as handlers.EventListener)
    # ------------------------------------------------------------------

    @property
    def context_uri(self) -> Optional[str]:
        """Context playing on THIS device.

        While another device is active Soloist mirrors that device's playback
        (same Spotify account), which is not ours to show or remember.
        """
        with self._state_lock:
            if not self._is_active:
                return None
            uri = self._context_uri_unlocked()
            if uri and uri == self._abandoned_uri:
                return None
            return self._positioning_uri or uri

    def start(self):
        """Connect in a background thread and keep reconnecting."""
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info(f'Started Soloist client: {self.url}')

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()
        logger.info('Stopped Soloist client')

    def _run(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # State is pushed, never polled, so a frozen daemon would go
                # unnoticed: its last state would just keep looking current.
                # The ping drops the socket instead, status() turns unknown
                # and the app's engine recovery takes over.
                self.ws.run_forever(ping_interval=_PING_INTERVAL, ping_timeout=_PING_TIMEOUT)
            except Exception as e:
                logger.warning(f'Soloist socket error: {e}')
            if self.running:
                time.sleep(1)

    def _on_open(self, _ws):
        with self._state_lock:
            self._socket_open = True
        if self._was_connected:
            logger.info('Soloist socket reconnected')
            if self.on_connect:
                self.on_connect()
        else:
            logger.info('Soloist socket connected')
            self._was_connected = True

    def _on_error(self, _ws, error):
        if error:
            logger.debug(f'Soloist socket error: {error}')

    def _on_close(self, _ws, _close_status, _close_msg):
        with self._state_lock:
            was_open = self._socket_open
            self._socket_open = False
        if was_open:
            logger.warning('Soloist socket disconnected')
        # Unblock a command that was waiting on a daemon that just went away.
        self._reply = None
        self._reply_ready.set()

    def _on_message(self, _ws, message: str):
        try:
            self._handle_event(json.loads(message))
        except Exception as e:
            logger.warning(f'Error handling Soloist event: {e}')

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _handle_event(self, event: dict):
        event_type = event.get('type')
        advance = None
        need_snapshot = False

        if event_type in ('command_result', 'error'):
            self._deliver_reply(event)
            return

        if event_type in ('track_changed', 'context_changed'):
            # These arrive one by one, in no fixed order (seen on the device:
            # the new album announced while the old album's track was still
            # current). Acting on them would pair a track with the wrong
            # album for a moment, which is how a wrong cover or a wrong
            # "where was I" gets saved. Only full snapshots change track and
            # context; ask for one in case the daemon does not send it itself.
            self._request_snapshot()
            return

        became_inactive = False
        with self._state_lock:
            was_active = self._is_active
            if event_type == 'auth_state':
                self._logged_in = bool(event.get('logged_in'))
                self._is_active = bool(event.get('is_active'))
                if not self._logged_in:
                    self._clear_playback()
            elif event_type == 'playback_state':
                self._set_status(event.get('status'))
                context = event.get('context')
                item = _playable(event.get('item'))
                same_context = (context or {}).get('uri') == (self._context or {}).get('uri')
                # A snapshot without an item is a switch in progress: keep the
                # track we know within the same context, forget it otherwise.
                if item or not same_context:
                    self._item = item
                if item:
                    self._named_snapshots += 1
                self._context = context
                self._position = event.get('position')
                self._volume = event.get('volume')
                self._is_active = bool(event.get('is_active'))
                self._repeat = (event.get('options') or {}).get('repeat') or 'off'
                advance = self._direct_follow_unlocked()
                need_snapshot = self._arm_end_timer_unlocked()
            elif event_type == 'playback_changed':
                self._set_status(event.get('status'))
                need_snapshot = self._arm_end_timer_unlocked()
            elif event_type == 'volume_changed':
                self._volume = event.get('volume')
            elif event_type == 'device_changed':
                self._is_active = bool(event.get('is_active'))
            elif event_type == 'options_changed':
                self._repeat = (event.get('options') or {}).get('repeat') or 'off'
            elif event_type == 'position_sync':
                self._position = event.get('position')
                need_snapshot = self._arm_end_timer_unlocked()
            elif event_type == 'queue_changed':
                # Mello has no queue view; only resume-in-album reads this.
                self._queue = event
                self._state_changed.notify_all()
                return
            else:
                logger.debug(f'Unhandled Soloist event: {event_type}')
                return
            became_inactive = was_active and not self._is_active
            self._state_changed.notify_all()
        if event_type == 'playback_state' and advance is not None:
            self._play_next_direct(*advance)
        elif need_snapshot:
            self._request_snapshot()
        if self._reset_track_repeat:
            self._reset_track_repeat = False
            self._send('set_repeat_track', enabled=False)

        self._keep_spotify_volume_full()
        if became_inactive:
            logger.info('Soloist is no longer the active Spotify device')
            if self.on_inactive:
                self.on_inactive()
        if self.on_update:
            self.on_update()

    def _keep_spotify_volume_full(self):
        """Undo a volume change made in the Spotify app.

        Mello sets the loudness with the speaker itself; Spotify's own volume
        has to stay at 100 or everything gets quieter on top of that. With
        go-librespot a setting (external_volume) made the slider in the
        Spotify app do nothing. Soloist has no such setting and turns the
        music down itself, so put it back whenever it moves.
        """
        with self._state_lock:
            volume, active = self._volume, self._is_active
            now = time.monotonic()
            # Only this speaker's own volume, and not in a tight loop if
            # Soloist were ever to refuse.
            if not active or volume is None or volume == 100 or now - self._volume_reset_at < 1.0:
                return
            self._volume_reset_at = now
        logger.info(f'Spotify volume was moved to {volume}%, setting it back to 100%')
        self._send('set_volume', volume=100)

    def _send(self, command: str, **fields) -> bool:
        """Send a command without waiting for its answer."""
        ws = self.ws
        if ws is None:
            return False
        try:
            ws.send(json.dumps({'type': 'command', 'command': command, **fields}))
            return True
        except Exception as e:
            logger.warning(f'Soloist {command} failed to send: {e}')
            return False

    def _request_snapshot(self):
        self._send('get_state')

    def _set_status(self, status: Optional[str]):
        status = status or 'idle'
        if status != self._status:
            self._status_since = time.time()
        self._status = status

    def _recheck_if_stuck_buffering(self):
        """Ask for the real status when "buffering" has lasted a while."""
        now = time.time()
        with self._state_lock:
            stuck = (
                self._status == 'buffering'
                and now - self._status_since >= _BUFFERING_RECHECK
                and now - self._last_recheck >= _BUFFERING_RECHECK
            )
            if stuck:
                self._last_recheck = now
        if stuck:
            logger.info('Soloist still says buffering, asking for the current state')
            self._request_snapshot()

    def _clear_playback(self):
        self._status = 'idle'
        self._item = None
        self._context = None
        self._position = None

    def _deliver_reply(self, event: dict):
        if self._pending_command is None:
            if event.get('type') == 'error':
                logger.warning(f'Soloist error with no command pending: {event.get("message")}')
            return
        if event.get('type') == 'command_result' and event.get('command') != self._pending_command:
            # Late answer to a command we already gave up on.
            logger.debug(f'Ignoring stale Soloist result: {event.get("command")}')
            return
        self._reply = event
        self._reply_ready.set()

    # ------------------------------------------------------------------
    # Status (go-librespot /status shape)
    # ------------------------------------------------------------------

    def status(self) -> Optional[dict]:
        """Current playback state in go-librespot's /status shape.

        Returns:
            dict: Playback payload, or a minimal "stopped" payload when the
                  daemon is reachable but nothing is playing here (not logged
                  in, idle, or another device is active).
            None: The daemon is unreachable (status unknown).
        """
        self._recheck_if_stuck_buffering()
        with self._state_lock:
            if not self._socket_open:
                return None
            if not self._logged_in or not self._is_active or self._status == 'idle':
                return {'stopped': True, 'paused': False, 'context_uri': None, 'track': None}

            if self._abandoned_uri and (self._context or {}).get('uri') == self._abandoned_uri:
                # Not "stopped": the app reads a sudden stop as Spotify having
                # moved to another device and then cancels the next play.
                return {'stopped': False, 'paused': True, 'context_uri': None, 'track': None}
            if self._positioning_uri and time.time() - self._positioning_since < _POSITIONING_MAX:
                return {
                    'stopped': False,
                    'paused': False,
                    'buffering': True,
                    'volume': self._volume,
                    'context_uri': self._positioning_uri,
                    'repeat_context': self._repeat == 'context' or self._direct is not None,
                    'track': None,
                }

            context_uri = self._context_uri_unlocked()
            item = self._item
            if not item:
                if not context_uri:
                    return {'stopped': True, 'paused': False, 'context_uri': None, 'track': None}
                # New context accepted, first track not announced yet.
                return {
                    'stopped': False,
                    'paused': self._status == 'paused',
                    'buffering': self._status != 'paused',
                    'volume': self._volume,
                    'context_uri': context_uri,
                    'repeat_context': self._repeat == 'context' or self._direct is not None,
                    'track': None,
                }

            decorations = item.get('decorations') or {}
            album = (decorations.get('parent') or {}).get('entity')
            artists = [
                _name(c.get('entity')) for c in (decorations.get('creators') or [])
                if isinstance(c, dict)
            ]
            pending, direct = self._pending_step, self._direct
            if pending and direct and not pending['beyond']:
                # Quick taps on next/previous: show where they lead at once;
                # the track itself starts when the tapping stops.
                target = direct['order'][pending['index']]
                return {
                    'stopped': False,
                    'paused': False,
                    'buffering': False,
                    'volume': self._volume,
                    'context_uri': context_uri,
                    'repeat_context': True,
                    'track': {
                        'uri': target,
                        'name': self.play_orders.name(direct['context'], target) if self.play_orders else None,
                        'artist_names': [a for a in artists if a],
                        'album_name': _name(album),
                        'album_cover_url': _cover_url(item) or _cover_url(album),
                        'position': 0,
                        'duration': 0,
                    },
                }
            return {
                'stopped': False,
                'paused': self._status == 'paused',
                'buffering': self._status == 'buffering',
                'volume': self._volume,
                'context_uri': context_uri,
                'repeat_context': self._repeat == 'context' or self._direct is not None,
                'track': {
                    'uri': item.get('uri') or None,
                    'name': _name(item),
                    'artist_names': [a for a in artists if a],
                    'album_name': _name(album),
                    'album_cover_url': _cover_url(item) or _cover_url(album),
                    'position': self._current_position_ms(),
                    'duration': (decorations.get('playback') or {}).get('duration_ms', 0),
                },
            }

    def _current_position_ms(self) -> int:
        """Soloist reports an anchor (position at a moment, plus speed), not a ticking clock."""
        anchor = self._position
        if not anchor:
            return 0
        position = anchor.get('position_ms', 0)
        speed = anchor.get('speed', 0.0)
        if speed and self._status == 'playing':
            elapsed_ms = time.time() * 1000 - anchor.get('timestamp_ms', 0)
            position += max(0.0, elapsed_ms) * speed
        return int(position)

    @property
    def logged_in(self) -> bool:
        """Whether Soloist is logged in to a Spotify account."""
        with self._state_lock:
            return self._logged_in

    def is_connected(self) -> bool:
        """Whether the daemon is reachable (it may or may not be logged in)."""
        with self._state_lock:
            return self._socket_open

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _command(self, command: str, **fields) -> Optional[bool]:
        """Send a command and wait until the daemon accepts or rejects it.

        Returns True when accepted, None when the daemon has no Spotify
        session, False for any other failure.
        """
        with self._command_lock:
            ws = self.ws
            if ws is None or not self.is_connected():
                logger.warning(f'Soloist {command} failed: daemon not connected')
                return self._failed(command)

            self._reply = None
            self._reply_ready.clear()
            self._pending_command = command
            try:
                ws.send(json.dumps({'type': 'command', 'command': command, **fields}))
                answered = self._reply_ready.wait(self.command_timeout)
            except Exception as e:
                logger.error(f'Soloist {command} failed to send: {e}')
                return self._failed(command)
            finally:
                self._pending_command = None

            reply = self._reply
            if not answered or reply is None:
                logger.warning(f'Soloist {command} got no answer within {self.command_timeout}s')
                return self._failed(command)
            if reply.get('type') == 'error':
                message = reply.get('message')
                logger.warning(f'Soloist {command} rejected: {message}')
                self._failed(command)
                return None if message == _AUTH_ERROR else False

            self._failure_count[command] = 0
            return True

    def _failed(self, command: str) -> bool:
        self._failure_count[command] = self._failure_count.get(command, 0) + 1
        return False

    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False,
             native: bool = False) -> Optional[bool]:
        """Play a Spotify URI (album/playlist), optionally landing on a track.

        Soloist can only start a context at its first track, and starting a
        track on its own drops the album around it. To resume inside an album
        the album is started and skipped forward to `skip_to_uri`. That is
        audible unless the speaker is muted: the caller mutes before asking
        for a resume and unmutes after its seek() and resume().

        `paused` means "I will seek before this may be heard". Soloist is not
        actually paused for that (it costs two extra round trips, ~0.6s per
        album switch): until resume() the app is shown "loading", and the
        speaker is muted by the caller.

        Returns:
            True  - Soloist accepted the request (and reached the track, or
                    the track is no longer part of the context)
            None  - Soloist has no Spotify session (user must connect via the app)
            False - request failed, or abort_play() was called meanwhile
        """
        logger.info(f'Soloist play: context={uri[:50]}...')
        with self._state_changed:
            epoch = self._play_epoch
            self._resume_target = None
            self._positioning_uri = None
            self._abandoned_uri = None

        if skip_to_uri and paused and self._is_on(uri, skip_to_uri):
            # Already on that track of that album (play after pause): no need
            # to start the album over.
            self._resume_target = skip_to_uri
            return True

        with self._state_lock:
            self._end_direct_unlocked()
            self._learn_epoch += 1
        if native or not self.play_orders:
            return self._play_context(uri, skip_to_uri, paused, epoch, learn=False)

        entry = self.play_orders.entry(uri)
        if skip_to_uri and entry and self._order_usable(uri, entry, skip_to_uri):
            items, complete, _learned_at = entry
            result = self._play_direct(uri, items, items.index(skip_to_uri), paused, epoch,
                                       complete and _never_changes(uri))
            if result is not False or self._aborted(epoch):
                return result
            # Next attempt starts the album the normal way (and learns again).
            self.play_orders.forget(uri)
            return False
        # Order unknown, possibly out of date, or no saved track: start the
        # album the normal way, read its order on the spot, and go to the
        # saved track directly instead of skipping there.
        return self._play_context(uri, skip_to_uri, paused, epoch, learn=True)

    def _order_usable(self, uri: str, entry, track: str) -> bool:
        items, complete, learned_at = entry
        if track not in items:
            return False
        last_known = items.index(track) == len(items) - 1
        if last_known and not (complete and _never_changes(uri)):
            return False   # what follows it is not known (yet)
        if not _never_changes(uri) and not _is_show(uri):
            return time.time() - learned_at < _PLAYLIST_FRESH_S
        return True

    def _aborted(self, epoch: int) -> bool:
        with self._state_lock:
            return self._play_epoch != epoch

    def _play_context(self, uri: str, skip_to_uri: Optional[str], paused: bool, epoch: int,
                      learn: bool, hand_over: bool = False) -> Optional[bool]:
        """Start the album itself (at its first track) and get to `skip_to_uri`.

        hand_over: direct play of this album just ended; keep reporting the
        album (loading) until Spotify plays it, not the track that was on.
        """
        result = self._activate()
        if result is not True:
            if hand_over:
                self._end_positioning()
            return result
        with self._state_lock:
            track_repeat = self._repeat == 'track'
            seen_before = self._named_snapshots
            if skip_to_uri or paused:
                self._positioning_uri = uri
                self._positioning_since = time.time()
        if track_repeat:
            # Left on by direct play: an album would play its first track
            # over and over, a show its first episode.
            self._command('set_repeat_track', enabled=False)
        result = self._command('play', uri=uri)
        if result is not True or not (skip_to_uri or paused):
            if result is True and hand_over:
                self._wait_for(lambda: self._play_epoch != epoch or self._is_playing_context(uri),
                               _SWITCH_TIMEOUT)
            self._end_positioning()
            if result is True and learn:
                self._learn_order_later(uri)
            return result

        def wait(predicate, timeout):
            """Wait for an event; False on timeout or when the play was aborted."""
            self._wait_for(lambda: self._play_epoch != epoch or predicate(), timeout)
            with self._state_lock:
                return self._play_epoch == epoch and bool(predicate())

        # Wait for a snapshot that this request caused. What was cached before
        # does not count: right after a daemon restart Soloist still shows the
        # album and track it restored, answers `play` with a snapshot that
        # names no track, and only starts the album seconds later (seen on
        # the device; skipping then started from the wrong place).
        started = time.time()
        positioned = wait(
            lambda: self._named_snapshots > seen_before
            and self._is_playing_context(uri)
            and self._status in ('buffering', 'playing'),
            _SWITCH_TIMEOUT,
        )
        if positioned and skip_to_uri and learn and self._item_uri() != skip_to_uri:
            if uri.startswith(('spotify:album:', 'spotify:playlist:')):
                # Looping makes the queue show the whole round of a short
                # album (the app switches it on after the start anyway).
                self.set_repeat_context(True)
            order, complete = self._learn_now(uri)
            if skip_to_uri in order or _is_show(uri):
                # Straight to the saved track. For a show that starts past it
                # (Spotify picks the starting episode), the saved episode is
                # still the right one to play: what follows it is learned at
                # its end.
                items = order if skip_to_uri in order else [skip_to_uri]
                complete = complete if skip_to_uri in order else False
                logger.info(f'Soloist play: order read in {time.time() - started:.1f}s, '
                            f'going to the saved track directly')
                return self._play_direct(uri, items, items.index(skip_to_uri), paused, epoch,
                                         complete and _never_changes(uri))
        elif positioned and learn:
            self._learn_order_later(uri)
        if positioned and skip_to_uri:
            self._resume_target = skip_to_uri
            positioned = self._skip_to(uri, skip_to_uri, wait)
        if positioned and paused:
            # A seek is refused while a track is still being opened.
            wait(lambda: self._status == 'playing', 5.0)

        with self._state_lock:
            aborted = self._play_epoch != epoch
        if aborted:
            logger.info('Soloist play: aborted while positioning')
            self._end_positioning(abandoned=True)
            return False
        if not positioned:
            logger.warning(f'Soloist play: could not position {uri[:50]} ({time.time() - started:.1f}s)')
            self._end_positioning(abandoned=True)
            return False
        if not paused:
            self._end_positioning()
        logger.info(f'Soloist play: positioned in {time.time() - started:.1f}s')
        return True

    # ------------------------------------------------------------------
    # Direct play (see play_orders.py)
    # ------------------------------------------------------------------

    def _play_direct(self, uri: str, order: list, index: int, paused: bool, epoch: int,
                     complete: bool = True) -> Optional[bool]:
        """Start order[index] on its own, in place of skipping through `uri`."""
        track = order[index]
        result = self._activate()
        if result is not True:
            return result
        with self._state_lock:
            seen_before = self._named_snapshots
            self._positioning_uri = uri
            self._positioning_since = time.time()
            self._end_direct_unlocked()
            self._direct = self._new_direct(uri, order, index, complete, started=False)
        started = time.time()
        result = self._command('play', uri=track)
        if result is not True:
            with self._state_lock:
                self._end_direct_unlocked()
            self._end_positioning()
            return result
        # Without track repeat Spotify carries on with a suggestion of its own
        # when the track ends (seen on the device: an unrelated podcast).
        self._command('set_repeat_track', enabled=True)

        def wait(predicate, timeout):
            self._wait_for(lambda: self._play_epoch != epoch or predicate(), timeout)
            with self._state_lock:
                return self._play_epoch == epoch and bool(predicate())

        positioned = wait(
            lambda: self._named_snapshots > seen_before
            and self._item_uri_unlocked() == track
            and self._status in ('buffering', 'playing'),
            _DIRECT_TIMEOUT,
        )
        if positioned and paused:
            # A seek is refused while a track is still being opened.
            wait(lambda: self._status == 'playing', 5.0)
        with self._state_lock:
            aborted = self._play_epoch != epoch
        if aborted or not positioned:
            logger.warning(f'Soloist play: {track} did not start directly '
                           f'({"aborted" if aborted else "no answer"}, {time.time() - started:.1f}s)')
            with self._state_lock:
                self._end_direct_unlocked()
            self._end_positioning(abandoned=True)
            return False
        self._resume_target = track
        if not paused:
            self._end_positioning()
        logger.info(f'Soloist play: track {index + 1}/{len(order)} of {uri[:50]} played directly '
                    f'in {time.time() - started:.1f}s')
        return True

    @staticmethod
    def _new_direct(context: str, order: list, index: int, complete: bool, started: bool) -> dict:
        return {'context': context, 'order': order, 'index': index, 'complete': complete,
                'started': started, 'ended': None, 'ended_at': 0.0, 'last_pos': None,
                'left': None, 'hold': False}

    def _end_direct_unlocked(self):
        for timer in (self._end_timer, self._step_timer):
            if timer:
                timer.cancel()
        self._end_timer = self._step_timer = None
        self._pending_step = None
        self._direct = None

    def _direct_follow_unlocked(self):
        """After a snapshot during direct play: keep track of where we are.

        Returns (context, next_index) when the current track just ended and
        the next one should start (next_index None: beyond what is known),
        else None.
        """
        direct = self._direct
        if not direct and self.play_orders and self._is_active:
            direct = self._recover_direct_unlocked()
        if not direct or not self._is_active:
            return None
        item = self._item_uri_unlocked()
        context = (self._context or {}).get('uri')
        order = direct['order']
        if not item:
            return None
        # The album itself playing the normal way (next/previous were used
        # before it ever went direct) is ours too.
        ours = context == direct['context'] or context in order
        if not ours or item not in order:
            # Something else is playing now (started from the Spotify app, or
            # a new album): direct play is over. Before our track was seen,
            # this is still the old album's state arriving.
            if direct['started'] and context and not ours:
                logger.info('Direct play ended: something else is playing')
                self._end_direct_unlocked()
                # Track repeat is direct play's; it must not stay on.
                self._reset_track_repeat = self._repeat == 'track'
            return None
        direct['started'] = True
        position = (self._position or {}).get('position_ms') or 0
        duration = ((self._item.get('decorations') or {}).get('playback') or {}).get('duration_ms') or 0
        # The last position seen, per track: a wrap is the same track going
        # from its end back to its start.
        last_item, last = direct['last_pos'] or (None, None)
        if last_item != item:
            last = None
        direct['last_pos'] = (item, position)
        if item == direct.get('left') and item != order[direct['index']] and \
                duration and position >= duration - 2 * _END_MARGIN_MS:
            return None   # late news about the end of the track we just moved on from
        index = direct['index'] if order[direct['index']] == item else order.index(item)
        direct['index'] = index
        if not duration or direct['hold'] or self._pending_step:
            return None
        at_end = self._status == 'buffering' and position >= duration - _END_MARGIN_MS
        # Fallback: track repeat already started it again from the top.
        wrapped = last is not None and last >= duration - 5000 and position < 3000
        if not (at_end or wrapped):
            return None
        retry = direct['ended'] == index and wrapped and time.time() - direct['ended_at'] > _ADVANCE_RETRY_S
        if direct['ended'] == index and not retry:
            return None
        if retry:
            logger.warning('Soloist: the next track did not start, trying again')
        return direct['context'], self._next_index_unlocked(direct, index)

    def _next_index_unlocked(self, direct: dict, index: int) -> Optional[int]:
        """The track after `index`, or None when that is not known."""
        if index + 1 < len(direct['order']):
            return index + 1
        return 0 if direct['complete'] else None

    def _recover_direct_unlocked(self) -> Optional[dict]:
        """Pick up a direct play this app did not start (or forgot in a restart).

        Spotify keeps playing the single track with track repeat on: without
        Mello moving on, it would play that one track over and over.
        """
        item = self._item_uri_unlocked()
        context = (self._context or {}).get('uri')
        # Track repeat is only ever switched on by direct play: a single
        # track the child picked in Mello (or in the Spotify app) is left alone.
        if not item or context != item or self._repeat != 'track':
            return None
        found = self.play_orders.find(item)
        if not found:
            return None
        album, order, complete = found
        complete = complete and _never_changes(album)
        self._direct = self._new_direct(album, order, order.index(item), complete, started=True)
        logger.info(f'Direct play picked up again: track {order.index(item) + 1}/{len(order)} of {album[:50]}')
        return self._direct

    def _arm_end_timer_unlocked(self) -> Optional[bool]:
        """During direct play, start the next track just before this one ends.

        Returns True when a fresh snapshot is needed to know the position.
        """
        if self._end_timer:
            self._end_timer.cancel()
            self._end_timer = None
        direct = self._direct
        if (not direct or not direct.get('started') or direct['hold'] or self._pending_step
                or self._status != 'playing' or not self._item):
            return None
        item = self._item_uri_unlocked()
        order = direct['order']
        if order[direct['index']] != item:
            return None
        duration = ((self._item.get('decorations') or {}).get('playback') or {}).get('duration_ms') or 0
        speed = (self._position or {}).get('speed') or 0
        if not duration:
            return None
        if speed <= 0:
            # Playing, but the last position was taken while it stood still
            # (after a seek or a resume): ask for a fresh one.
            if not direct.get('asked'):
                direct['asked'] = True
                return True
            return None
        direct['asked'] = False
        remaining = (duration - self._current_position_ms()) / 1000 / speed - _EARLY_START_S
        if remaining < 0:
            return None
        context, index = direct['context'], direct['index']

        def fire():
            with self._state_lock:
                now = self._direct
                if (not now or now['context'] != context or now['index'] != index or now['hold']
                        or now['ended'] == index or self._pending_step or self._status != 'playing'
                        or self._item_uri_unlocked() != item):
                    return
                if self._current_position_ms() < duration - 1500:
                    return   # moved meanwhile; a newer timer takes over
                next_index = self._next_index_unlocked(now, index)
            self._play_next_direct(context, next_index)

        self._end_timer = threading.Timer(remaining, fire)
        self._end_timer.daemon = True
        self._end_timer.start()
        return None

    def _play_next_direct(self, context: str, index: Optional[int]):
        """Start the next track of a direct play (from the socket or a timer thread)."""
        with self._state_lock:
            direct = self._direct
            if not direct or direct['context'] != context or direct['hold']:
                return
            current = direct['index']
            direct['ended'] = current
            direct['ended_at'] = time.time()
            if index is None:
                last = direct['order'][current]
                complete = False
            else:
                left = direct['order'][current]
                direct.update(index=index, last_pos=None, left=left)
                track = direct['order'][index]
                total = len(direct['order'])
        if index is None:
            self._continue_beyond_known(context, last)
            return
        logger.info(f'Soloist: track ended, playing track {index + 1}/{total} directly')
        # Not _command(): it waits for its answer, which arrives on the socket
        # thread this may be running on.
        self._send('play', uri=track)
        self._send('set_repeat_track', enabled=True)

    def _continue_beyond_known(self, context: str, last: str):
        """What follows `last` is not known: start the album the normal way.

        Reads the order there (new episodes of a show, edits of a playlist,
        the next part of a long audiobook) and plays the track after `last`
        directly. A playlist simply starts over at its first track.
        """
        with self._state_lock:
            self._end_direct_unlocked()
            # Until Spotify reports the album itself, only the bare track that
            # played directly is known: report the album as loading, else the
            # app shows that track as a new item and jumps to it.
            self._positioning_uri = context
            self._positioning_since = time.time()
        logger.info(f'Soloist: end of the known order of {context[:50]}, reading it again')

        def go_on():
            with self._state_lock:
                epoch = self._play_epoch
            if not _never_changes(context) and not _is_show(context):
                self._play_context(context, None, False, epoch, learn=True, hand_over=True)
                return
            # Start it paused-ish: it begins wherever Spotify starts it.
            if self._play_context(context, None, True, epoch, learn=False) is not True:
                return
            order, complete = self._learn_now(context)
            if last in order and order.index(last) + 1 < len(order):
                index = order.index(last) + 1
                self._play_direct(context, order, index, False, epoch, complete and _never_changes(context))
            else:
                # Not in sight from where Spotify started: the old way.
                self._end_positioning()
                if self.play(context, skip_to_uri=last, paused=False, native=True) is True:
                    self._transport('skip_next')

        threading.Thread(target=go_on, daemon=True, name='beyond-known').start()

    def _direct_step(self, step: int) -> Optional[bool]:
        """Next/previous using the known order. None: not known, use Spotify's own skip."""
        restart = False
        with self._state_lock:
            if not self._is_active or self._positioning_uri:
                return None
            direct = self._direct or self._enter_direct_from_context_unlocked()
            if not direct:
                return None
            direct['hold'] = False
            order = direct['order']
            pending = self._pending_step
            base = pending['index'] if pending else direct['index']
            just_restarted = time.time() - direct.get('restarted_at', 0) < 2.5
            if step < 0 and not pending and not just_restarted and (
                    self._current_position_ms() > 3000 or (base == 0 and not direct['complete'])):
                # Like Spotify: "previous" first goes back to the start of the
                # track; a second tap soon after goes to the one before.
                restart = True
                direct['restarted_at'] = time.time()
            else:
                target = base + step
                beyond = False
                if target < 0:
                    target = len(order) - 1 if direct['complete'] else 0
                elif target >= len(order):
                    if direct['complete']:
                        target = 0
                    else:
                        target, beyond = len(order) - 1, True
                self._pending_step = {'index': target, 'beyond': beyond}
                if self._end_timer:
                    self._end_timer.cancel()
                    self._end_timer = None
                if self._step_timer:
                    self._step_timer.cancel()
                self._step_timer = threading.Timer(_STEP_SETTLE_S, self._send_step)
                self._step_timer.daemon = True
                self._step_timer.start()
        if restart:
            with self._state_lock:
                direct['last_pos'] = None   # going back to 0 is not the track ending
            return self._transport('seek', position_ms=0)
        if self.on_update:
            self.on_update()   # the title changes at once
        return True

    def _send_step(self):
        """The taps stopped: play the track they ended on."""
        with self._state_lock:
            pending, direct = self._pending_step, self._direct
            self._pending_step = None
            self._step_timer = None
            if not pending or not direct:
                return
            index = pending['index']
            current = direct['index']
            if pending['beyond']:
                last = direct['order'][index]
            elif index != current:
                left = direct['order'][current]
                direct.update(index=index, ended=None, last_pos=None, left=left)
            track = direct['order'][index]
            context = direct['context']
        if pending['beyond']:
            self._continue_beyond_known(context, last)
            return
        if index == current:
            return
        if self._transport('play', uri=track):
            self._command('set_repeat_track', enabled=True)

    def _enter_direct_from_context_unlocked(self) -> Optional[dict]:
        """The album plays the normal way; with its order known, next/previous go direct."""
        if not self.play_orders:
            return None
        context = (self._context or {}).get('uri')
        item = self._item_uri_unlocked()
        if not context or not item or context == item:
            return None
        entry = self.play_orders.entry(context)
        if not entry or item not in entry[0]:
            return None
        items, complete, _ = entry
        self._direct = self._new_direct(context, items, items.index(item),
                                        complete and _never_changes(context), started=True)
        return self._direct

    def _learn_now(self, uri: str):
        """Read the order of `uri`, which plays the normal way right now."""
        with self._state_lock:
            current = self._item_uri_unlocked()
        upcoming = self._upcoming_items()
        if not current or not upcoming:
            logger.info(f'Play order not learned: no queue for {uri[:50]}')
            entry = self.play_orders.entry(uri)
            return (entry[0], entry[1]) if entry else ([], False)
        uris = [u for u, _ in upcoming]
        order, complete = one_round(current, uris)
        complete = complete and _never_changes(uri)
        names = {u: n for u, n in upcoming if n}
        with self._state_lock:
            if self._item:
                names.setdefault(current, _name(self._item))
        self.play_orders.put(uri, order, complete, names)
        entry = self.play_orders.entry(uri)
        return (entry[0], entry[1]) if entry else (order, complete)

    def _learn_order_later(self, uri: str):
        """Read the order of `uri` as soon as it plays (in the background)."""
        with self._state_lock:
            self._learn_epoch += 1
            epoch = self._learn_epoch

        def learn():
            deadline = time.time() + _LEARN_WAIT
            while time.time() < deadline:
                with self._state_lock:
                    if self._learn_epoch != epoch:
                        return
                    playing = self._is_playing_context(uri) and self._status in ('playing', 'buffering')
                    # An album's queue only shows a whole round once it loops
                    # (the app switches that on right after the start).
                    ready = playing and (self._repeat == 'context' or not _never_changes(uri))
                if ready:
                    break
                time.sleep(0.2)
            with self._state_lock:
                if self._learn_epoch != epoch or not self._is_playing_context(uri):
                    return
            self._learn_now(uri)

        threading.Thread(target=learn, daemon=True, name='learn-order').start()

    def abort_play(self):
        """Make a play() that is still skipping to its track give up now."""
        with self._state_changed:
            self._play_epoch += 1
            self._learn_epoch += 1
            # The child swiped away: the album that was playing must not start
            # its next track (or a tapped one) any more.
            if self._direct:
                self._direct['hold'] = True
            for timer in (self._end_timer, self._step_timer):
                if timer:
                    timer.cancel()
            self._end_timer = self._step_timer = None
            self._pending_step = None
            self._state_changed.notify_all()

    def _end_positioning(self, abandoned: bool = False):
        with self._state_changed:
            if abandoned and self._positioning_uri:
                self._abandoned_uri = self._positioning_uri
            self._positioning_uri = None
            self._state_changed.notify_all()
        if self.on_update:
            self.on_update()

    def _activate(self) -> Optional[bool]:
        """Make this device the one Spotify plays on.

        Needed before `play <uri>`: an inactive Soloist first hands the
        request to whichever device is active (seen on the device: the album
        started on the other speaker). Whatever was playing there moves here,
        so the caller keeps the speaker muted until our own content plays.
        """
        with self._state_lock:
            if self._is_active:
                return True
        result = self._command('activate')
        if result is not True:
            return result
        if not self._wait_for(lambda: self._is_active, _ACTIVATE_TIMEOUT):
            logger.warning(f'Soloist did not become the active device within {_ACTIVATE_TIMEOUT:.0f}s')
            return False
        return True

    def _skip_to(self, context_uri: str, track_uri: str, wait) -> bool:
        """Skip within the context that just started until `track_uri`.

        Skips go out in bursts rather than one by one: measured on the device,
        12 skips sent back to back land on the right track in about 3s,
        against 5s when each skip waits for the next track to be announced
        (and a burst of 40 loses most of its skips). After each burst the
        track it should have reached is confirmed before more are sent, so a
        lost skip is noticed and a swipe away can interrupt.

        With repeat on, "previous" at the first track wraps round to the last
        one (seen on the device), so a track past the middle is reached
        backwards: track 41 of 50 is 10 skips instead of 40.

        Returns False only when playback left our hands (another device took
        over, the context changed, or the play was aborted); a track that is
        no longer in the context leaves playback where it is and returns True.
        """
        for _ in range(_SKIP_ROUNDS):
            current = self._item_uri()
            if current == track_uri:
                return True
            upcoming = self._upcoming()
            if (upcoming and track_uri not in upcoming and current not in upcoming
                    and len(upcoming) >= _QUEUE_CAP - 5):
                # The queue shows only the next ~80 (a long audiobook): move
                # towards its end and look again.
                forward = len(upcoming)
                burst = min(forward, _SKIP_BURST)
                logger.info(f'Soloist resume: {track_uri} beyond the queue, skip_next x{burst} to look further')
                if not self._skip_burst(context_uri, 'skip_next', burst, upcoming[burst - 1], wait):
                    return False
                continue
            if upcoming is None or track_uri not in upcoming:
                logger.warning(f'Soloist resume: {track_uri} not coming up in {context_uri[:50]}')
                return True

            forward = upcoming.index(track_uri) + 1
            command, remaining = 'skip_next', forward
            burst = min(forward, _SKIP_BURST)
            checkpoint = upcoming[burst - 1]

            with self._state_lock:
                repeating = self._repeat == 'context'
            if repeating and current in upcoming:
                # The queue runs on into the next round of the album; where
                # the current track comes round again is the album's length.
                length = upcoming.index(current) + 1
                backward = length - forward
                if 0 < backward < forward:
                    one_round = [current] + upcoming[:length - 1]
                    command, remaining = 'skip_prev', backward
                    burst = min(backward, _SKIP_BURST)
                    checkpoint = one_round[length - burst]

            logger.info(f'Soloist resume: {command} x{burst} of {remaining} to {track_uri}')
            for _ in range(burst):
                # Never skip unless this device is playing our context: when it
                # is not active, Soloist forwards commands to the device that is.
                with self._state_lock:
                    ours = self._is_playing_context(context_uri)
                if not ours:
                    logger.warning('Soloist resume: playback moved away while skipping')
                    return False
                # Sent without waiting for each answer: Soloist only answers a
                # skip once it has carried it out (~0.5s), and waiting for that
                # is exactly what made one-by-one skipping slow.
                if not self._send(command):
                    return False
            if not wait(lambda: self._item_uri_unlocked() == checkpoint, _SKIP_TIMEOUT + burst * 0.5):
                with self._state_lock:
                    ours = self._is_playing_context(context_uri)
                if not ours or not wait(lambda: True, 0):
                    logger.warning('Soloist resume: stopped skipping (aborted or playback moved away)')
                    return False
                # Not where the burst should have ended: look again and go on from there.
                logger.warning('Soloist resume: burst did not land on its track, re-reading the queue')
        return self._item_uri() == track_uri

    def _skip_burst(self, context_uri: str, command: str, burst: int, checkpoint: str, wait) -> bool:
        """Send `burst` skips and wait for `checkpoint`; False when playback left our hands."""
        for _ in range(burst):
            with self._state_lock:
                ours = self._is_playing_context(context_uri)
            if not ours or not self._send(command):
                return False
        if not wait(lambda: self._item_uri_unlocked() == checkpoint, _SKIP_TIMEOUT + burst * 0.5):
            with self._state_lock:
                ours = self._is_playing_context(context_uri)
            if not ours or not wait(lambda: True, 0):
                return False
        return True

    def _upcoming(self) -> Optional[list]:
        """Track URIs still to come in the current context, or None if unknown."""
        items = self._upcoming_items()
        return None if items is None else [u for u, _ in items]

    def _upcoming_items(self) -> Optional[list]:
        """(uri, name) of what is still to come in the current context."""
        with self._queue_lock:
            return self._upcoming_locked()

    def _upcoming_locked(self) -> Optional[list]:
        upcoming = None
        for _ in range(2):
            with self._state_changed:
                self._queue = None
            # limit 0 = the whole queue. Answered with queue_changed, not command_result.
            if not self._send('get_queue', limit=0):
                return None
            if not self._wait_for(lambda: self._queue is not None, self.command_timeout):
                return None
            with self._state_lock:
                upcoming = [
                    ((entry.get('item') or {}).get('uri'), _name(entry.get('item')))
                    for entry in (self._queue or {}).get('upcoming') or []
                    if entry.get('source') == 'context'
                ]
            # A list of exactly the broadcast size may be a capped broadcast
            # that crossed our request; ask once more to be sure.
            if len(upcoming) != _QUEUE_BROADCAST_CAP:
                break
        return upcoming

    def _wait_for(self, predicate: Callable[[], object], timeout: float) -> bool:
        """Wait until an event makes `predicate` true (checked under the state lock)."""
        with self._state_changed:
            return bool(self._state_changed.wait_for(predicate, timeout))

    def _is_on(self, context_uri: str, track_uri: str) -> bool:
        with self._state_lock:
            return self._is_playing_context(context_uri) and self._item_uri_unlocked() == track_uri

    def _is_playing_context(self, uri: str) -> bool:
        """Unlocked: this device is the active one and on `uri`."""
        return self._is_active and self._context_uri_unlocked() == uri

    def _context_uri_unlocked(self) -> Optional[str]:
        """The album/playlist/show playing; during direct play the one the track belongs to."""
        uri = (self._context or {}).get('uri') or None
        direct = self._direct
        if direct and uri and uri in direct['order']:
            return direct['context']
        return uri

    def _item_uri_unlocked(self) -> Optional[str]:
        return (self._item or {}).get('uri') or None

    def _item_uri(self) -> Optional[str]:
        with self._state_lock:
            return self._item_uri_unlocked()

    def _transport(self, command: str, **fields) -> bool:
        """Send a command that acts on whatever is playing.

        Refused while another device is active: Soloist would forward it and
        pause, skip or change the volume of that other speaker.
        """
        with self._state_lock:
            active = self._is_active
        if not active:
            logger.info(f'Soloist {command} not sent: this device is not the active one')
            return False
        return bool(self._command(command, **fields))

    def pause(self) -> bool:
        # A pause while still skipping to the saved track abandons the resume.
        self._end_positioning(abandoned=True)
        return self._transport('pause')

    def resume(self) -> bool:
        with self._state_lock:
            was_positioning = self._positioning_uri is not None
            already_playing = self._is_active and self._status in ('playing', 'buffering')
            abandoned = bool(self._abandoned_uri) and (self._context or {}).get('uri') == self._abandoned_uri
        if abandoned:
            logger.info('Soloist resume not sent: the loaded album was left half-way through a resume')
            return False
        self._end_positioning()
        with self._state_lock:
            if self._direct:
                self._direct['hold'] = False
        if was_positioning and already_playing:
            return True  # play(paused=True) left it running, see there
        return self._transport('play')

    def next(self) -> bool:
        handled = self._direct_step(1)
        return self._transport('skip_next') if handled is None else handled

    def prev(self) -> bool:
        handled = self._direct_step(-1)
        return self._transport('skip_prev') if handled is None else handled

    def seek(self, position: int) -> bool:
        target, self._resume_target = self._resume_target, None
        if target and self._item_uri() != target:
            # The resume did not land on the remembered track, so the
            # remembered position belongs to a different track.
            logger.info('Soloist seek skipped: not on the track being resumed')
            return False
        return self._transport('seek', position_ms=position)

    def set_volume(self, level: int) -> bool:
        return self._transport('set_volume', volume=level)

    def set_repeat_context(self, enabled: bool) -> bool:
        # Track repeat and context repeat are separate switches in Soloist;
        # context repeat only takes effect with track repeat off.
        with self._state_lock:
            already_set = self._is_active and (self._repeat == 'context') == enabled
            direct = self._direct is not None
        if already_set:
            return True
        if direct:
            # Direct play loops the album itself and needs track repeat on.
            return True
        if enabled and not self._transport('set_repeat_track', enabled=False):
            return False
        return self._transport('set_repeat_context', enabled=enabled)

    def metrics_snapshot(self) -> dict:
        return {'suppressed': {}, 'failures': dict(self._failure_count)}

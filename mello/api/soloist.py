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
# Skips are sent in bursts of this size (12 and 25 at once both landed
# correctly on the device; a burst is also the longest a swipe has to wait).
_SKIP_BURST = 12
_SKIP_ROUNDS = 12
# Slack for a burst to land, on top of 0.5s per skip.
_SKIP_TIMEOUT = 4.0
# Broadcast queue updates are capped at this many entries (a full answer to
# get_queue is not), so a list of exactly this size may be a broadcast.
_QUEUE_BROADCAST_CAP = 10


def _name(entity: Optional[dict]) -> Optional[str]:
    if not isinstance(entity, dict):
        return None
    return ((entity.get('decorations') or {}).get('identity') or {}).get('name')


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
    ):
        """
        Args:
            url: WebSocket URL of the daemon (e.g. ws://127.0.0.1:9090)
            on_update: Called after any state change
            on_connect: Called when the socket reconnects after a drop
            on_inactive: Called when this device stops being the active Spotify device
            command_timeout: Seconds to wait for the daemon to accept a command
        """
        self.url = url
        self.on_update = on_update
        self.on_connect = on_connect
        self.on_inactive = on_inactive
        self.command_timeout = command_timeout

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
        self._item: Optional[dict] = None
        self._context: Optional[dict] = None
        self._position: Optional[dict] = None
        self._volume: Optional[int] = None
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
            uri = (self._context or {}).get('uri') or None
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
                self._status = event.get('status') or 'idle'
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
            elif event_type == 'playback_changed':
                self._status = event.get('status') or 'idle'
            elif event_type == 'volume_changed':
                self._volume = event.get('volume')
            elif event_type == 'device_changed':
                self._is_active = bool(event.get('is_active'))
            elif event_type == 'options_changed':
                self._repeat = (event.get('options') or {}).get('repeat') or 'off'
            elif event_type == 'position_sync':
                self._position = event.get('position')
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

        if became_inactive:
            logger.info('Soloist is no longer the active Spotify device')
            if self.on_inactive:
                self.on_inactive()
        if self.on_update:
            self.on_update()

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
                    'repeat_context': self._repeat == 'context',
                    'track': None,
                }

            context_uri = (self._context or {}).get('uri') or None
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
                    'repeat_context': self._repeat == 'context',
                    'track': None,
                }

            decorations = item.get('decorations') or {}
            album = (decorations.get('parent') or {}).get('entity')
            artists = [
                _name(c.get('entity')) for c in (decorations.get('creators') or [])
                if isinstance(c, dict)
            ]
            return {
                'stopped': False,
                'paused': self._status == 'paused',
                'buffering': self._status == 'buffering',
                'volume': self._volume,
                'context_uri': context_uri,
                'repeat_context': self._repeat == 'context',
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

    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False) -> Optional[bool]:
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

        result = self._activate()
        if result is not True:
            return result
        with self._state_lock:
            seen_before = self._named_snapshots
            if skip_to_uri or paused:
                self._positioning_uri = uri
                self._positioning_since = time.time()
        result = self._command('play', uri=uri)
        if result is not True or not (skip_to_uri or paused):
            self._end_positioning()
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

    def abort_play(self):
        """Make a play() that is still skipping to its track give up now."""
        with self._state_changed:
            self._play_epoch += 1
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
        """Skip forward within the context that just started until `track_uri`.

        Skips go out in bursts rather than one by one: measured on the device,
        12 skips sent back to back land on the right track in 2.4s, against
        5.3s when each skip waits for the next track to be announced. After
        each burst the track it should have reached is confirmed before more
        are sent, so a lost skip is noticed and a swipe away can interrupt.

        Returns False only when playback left our hands (another device took
        over, the context changed, or the play was aborted); a track that is
        no longer in the context leaves playback where it is and returns True.
        """
        for _ in range(_SKIP_ROUNDS):
            if self._item_uri() == track_uri:
                return True
            upcoming = self._upcoming()
            if upcoming is None or track_uri not in upcoming:
                logger.warning(f'Soloist resume: {track_uri} not coming up in {context_uri[:50]}')
                return True

            remaining = upcoming.index(track_uri) + 1
            burst = min(remaining, _SKIP_BURST)
            checkpoint = upcoming[burst - 1]
            logger.info(f'Soloist resume: skipping {burst} of {remaining} track(s) to {track_uri}')
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
                if not self._send('skip_next'):
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

    def _upcoming(self) -> Optional[list]:
        """Track URIs still to come in the current context, or None if unknown."""
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
                    (entry.get('item') or {}).get('uri')
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
        return self._is_active and (self._context or {}).get('uri') == uri

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
        if was_positioning and already_playing:
            return True  # play(paused=True) left it running, see there
        return self._transport('play')

    def next(self) -> bool:
        return self._transport('skip_next')

    def prev(self) -> bool:
        return self._transport('skip_prev')

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
        if already_set:
            return True
        if enabled and not self._transport('set_repeat_track', enabled=False):
            return False
        return self._transport('set_repeat_context', enabled=enabled)

    def metrics_snapshot(self) -> dict:
        return {'suppressed': {}, 'failures': dict(self._failure_count)}

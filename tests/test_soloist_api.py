"""
Tests for SoloistAPI: state caching, go-librespot-shaped status, and commands.
"""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.player import PlayerAPIProtocol
from mello.api.soloist import SoloistAPI
from mello.models import PlayerStatus


STOPPED = {'stopped': True, 'paused': False, 'context_uri': None, 'track': None}


def entity(uri, name, **decorations):
    return {'uri': uri, 'entity_type': uri.split(':')[1], 'decorations': {'identity': {'name': name}, **decorations}}


TRACK = entity(
    'spotify:track:t1', 'My Song',
    parent={'entity': entity(
        'spotify:album:a1', 'Album Name',
        visual_identity={'cover': [
            {'url': 'https://img/small', 'size': 'small'},
            {'url': 'https://img/large', 'size': 'large'},
        ]},
    )},
    creators=[{'entity': entity('spotify:artist:x', 'Artist A')}, {'entity': entity('spotify:artist:y', 'Artist B')}],
    playback={'duration_ms': 210000, 'content_ratings': []},
)


def playback_state(status='playing', position_ms=45000, speed=1.0, is_active=True, repeat='off'):
    return {
        'type': 'playback_state',
        'status': status,
        'item': TRACK,
        'context': entity('spotify:album:a1', 'Album Name'),
        'position': {'position_ms': position_ms, 'timestamp_ms': time.time() * 1000, 'speed': speed},
        'volume': 100,
        'is_active': is_active,
        'options': {'shuffle': False, 'repeat': repeat},
    }


def connected_api(logged_in=True, reply=None, active=None, **kwargs):
    """An API with an open (fake) socket. `reply` is what the daemon answers to commands."""
    api = SoloistAPI('ws://127.0.0.1:9090', command_timeout=0.2, **kwargs)
    api.ws = MagicMock()
    api.sent = []

    def send(raw):
        message = json.loads(raw)
        api.sent.append(message)
        answer = reply(message) if callable(reply) else reply
        if answer is None and reply is None:
            answer = {'type': 'command_result', 'command': message['command']}
        if answer:
            api._handle_event(answer)

    api.ws.send = send
    api._on_open(api.ws)
    is_active = logged_in if active is None else active
    api._handle_event({'type': 'auth_state', 'logged_in': logged_in, 'is_active': is_active, 'device_name': 'Mello'})
    return api


def test_implements_the_engine_protocol():
    assert isinstance(SoloistAPI('ws://x'), PlayerAPIProtocol)


# --- status -----------------------------------------------------------------

def test_status_is_none_while_daemon_unreachable():
    api = SoloistAPI('ws://127.0.0.1:9090')
    assert api.status() is None
    assert api.is_connected() is False


def test_status_is_stopped_when_not_logged_in():
    api = connected_api(logged_in=False)
    assert api.is_connected() is True
    assert api.status() == STOPPED


def test_status_maps_playing_track_to_librespot_shape():
    api = connected_api()
    api._handle_event(playback_state(position_ms=45000, speed=0.0))

    status = PlayerStatus.from_dict(api.status())

    assert status.playing is True
    assert status.paused is False
    assert status.context_uri == 'spotify:album:a1'
    assert status.track_uri == 'spotify:track:t1'
    assert status.track_name == 'My Song'
    assert status.track_artist == 'Artist A, Artist B'
    assert status.track_album == 'Album Name'
    assert status.track_cover == 'https://img/large'
    assert status.position == 45000
    assert status.duration == 210000
    assert status.volume == 100
    assert api.context_uri == 'spotify:album:a1'


def test_position_advances_between_events_while_playing():
    api = connected_api()
    state = playback_state(position_ms=10000)
    state['position']['timestamp_ms'] = time.time() * 1000 - 2000
    api._handle_event(state)

    assert 11900 <= api.status()['track']['position'] <= 12500


def test_position_holds_still_while_paused():
    api = connected_api()
    state = playback_state(status='paused', position_ms=10000)
    state['position']['timestamp_ms'] = time.time() * 1000 - 2000
    api._handle_event(state)

    status = api.status()
    assert status['paused'] is True
    assert status['track']['position'] == 10000


def test_buffering_is_not_reported_as_playing():
    api = connected_api()
    api._handle_event(playback_state(status='buffering'))

    assert PlayerStatus.from_dict(api.status()).playing is False


def test_granular_events_update_cached_state():
    api = connected_api()
    api._handle_event(playback_state())

    api._handle_event({'type': 'playback_changed', 'status': 'paused'})
    api._handle_event({'type': 'options_changed', 'options': {'repeat': 'context'}})

    status = api.status()
    assert status['paused'] is True
    assert status['repeat_context'] is True


def test_volume_changed_in_the_spotify_app_is_put_back_to_100():
    # Mello sets the loudness with the speaker itself. Seen on a speaker: the
    # slider in the Spotify app at 25% made everything quiet on top of that.
    api = connected_api()
    api._handle_event(playback_state())
    api.sent.clear()

    api._handle_event({'type': 'volume_changed', 'volume': 25})

    assert api.sent == [{'type': 'command', 'command': 'set_volume', 'volume': 100}]


def test_volume_is_put_back_when_playback_arrives_from_the_app_already_turned_down():
    api = connected_api()
    snapshot = playback_state()
    snapshot['volume'] = 25

    api._handle_event(snapshot)

    assert {'type': 'command', 'command': 'set_volume', 'volume': 100} in api.sent


def test_volume_of_another_speaker_is_left_alone():
    # While another device plays, Soloist mirrors its state, volume included.
    api = connected_api()
    snapshot = playback_state(is_active=False)
    snapshot['volume'] = 25

    api._handle_event(snapshot)
    api._handle_event({'type': 'volume_changed', 'volume': 30})

    assert not [m for m in api.sent if m['command'] == 'set_volume']


def test_track_and_context_change_only_through_full_snapshots():
    # Seen on the device: after `play <album B>` the new context was announced
    # while album A's track was still current, and Mello saved album B under
    # album A's name and cover. A lone track/context event must not be paired
    # with the other half of the old state; it asks for a snapshot instead.
    api = connected_api()
    api._handle_event(playback_state())

    api._handle_event({'type': 'context_changed', 'context': entity('spotify:playlist:p1', 'Mix')})
    api._handle_event({'type': 'track_changed', 'item': entity('spotify:track:t2', 'Other')})

    status = api.status()
    assert (status['context_uri'], status['track']['uri']) == ('spotify:album:a1', 'spotify:track:t1')
    assert [m['command'] for m in api.sent] == ['get_state', 'get_state']

    snapshot = playback_state()
    snapshot['context'] = entity('spotify:playlist:p1', 'Mix')
    snapshot['item'] = entity('spotify:track:t2', 'Other')
    api._handle_event(snapshot)

    status = api.status()
    assert (status['context_uri'], status['track']['uri']) == ('spotify:playlist:p1', 'spotify:track:t2')


def test_status_is_stopped_when_another_device_takes_over():
    on_inactive = MagicMock()
    api = connected_api(on_inactive=on_inactive)
    api._handle_event(playback_state())

    api._handle_event({'type': 'device_changed', 'is_active': False, 'device_name': 'Mello'})

    assert api.status() == STOPPED
    on_inactive.assert_called_once()


def test_other_devices_playback_is_not_reported_as_ours():
    # Same Spotify account playing on another speaker: Soloist mirrors it
    # with is_active false (seen on the device), Mello must not adopt it.
    api = connected_api()
    api._handle_event(playback_state(is_active=False))

    assert api.status() == STOPPED
    assert api.context_uri is None


def test_new_context_without_item_reports_loading_not_old_track():
    # Real sequence after `play <album>`: a snapshot for the new context
    # arrives with an empty item, the track follows in track_changed.
    api = connected_api()
    api._handle_event(playback_state())
    switching = playback_state()
    switching['context'] = entity('spotify:album:a2', 'Next Album')
    switching['item'] = {'uri': '', 'entity_type': '', 'decorations': {}}
    api._handle_event(switching)

    status = PlayerStatus.from_dict(api.status())
    assert status.context_uri == 'spotify:album:a2'
    assert status.track_uri is None
    assert status.playing is False

    switching['item'] = entity('spotify:track:t9', 'First')
    api._handle_event(switching)
    assert PlayerStatus.from_dict(api.status()).track_uri == 'spotify:track:t9'


def test_empty_item_within_same_context_keeps_known_track():
    # Real sequence after `pause`: a snapshot with an empty item, then the
    # same track again. The title must not flicker away in between.
    api = connected_api()
    api._handle_event(playback_state())
    paused = playback_state(status='paused')
    paused['item'] = {'uri': '', 'entity_type': '', 'decorations': {}}
    api._handle_event(paused)

    status = api.status()
    assert status['paused'] is True
    assert status['track']['uri'] == 'spotify:track:t1'


def test_logout_clears_playback():
    api = connected_api()
    api._handle_event(playback_state())

    api._handle_event({'type': 'auth_state', 'logged_in': False, 'is_active': False})

    assert api.status() == STOPPED
    assert api.context_uri is None


def test_every_state_event_notifies_the_app():
    on_update = MagicMock()
    api = connected_api(on_update=on_update)
    on_update.reset_mock()

    api._handle_event(playback_state())
    api._handle_event({'type': 'position_sync', 'position': {'position_ms': 1, 'timestamp_ms': 0, 'speed': 0.0}})

    assert on_update.call_count == 2


def test_reconnect_notifies_the_app_but_first_connect_does_not():
    on_connect = MagicMock()
    api = connected_api(on_connect=on_connect)
    on_connect.assert_not_called()

    api._on_close(api.ws, None, None)
    assert api.status() is None
    api._on_open(api.ws)

    on_connect.assert_called_once()


# --- commands ---------------------------------------------------------------

def test_play_sends_uri_and_reports_accepted():
    api = connected_api()

    assert api.play('spotify:album:a1') is True
    assert api.sent == [{'type': 'command', 'command': 'play', 'uri': 'spotify:album:a1'}]


def test_play_returns_none_without_spotify_session():
    api = connected_api(logged_in=False, reply={'type': 'error', 'message': 'command requires authentication'})

    assert api.play('spotify:album:a1') is None


def test_command_rejected_for_other_reason_returns_false():
    api = connected_api(reply={'type': 'error', 'message': 'invalid uri'})

    assert api.play('nonsense') is False
    assert api.metrics_snapshot()['failures'] == {'play': 1}


def test_command_times_out_when_daemon_does_not_answer():
    api = connected_api(reply=lambda _message: False)

    assert api.pause() is False


def test_command_fails_fast_when_daemon_unreachable():
    api = SoloistAPI('ws://127.0.0.1:9090')

    assert api.pause() is False
    assert api.play('spotify:album:a1') is False


def test_simple_commands_map_to_soloist_names():
    api = connected_api()

    assert api.pause() and api.resume() and api.next() and api.prev()
    assert api.seek(30000) and api.set_volume(50)

    assert [(m['command'], {k: v for k, v in m.items() if k not in ('type', 'command')}) for m in api.sent] == [
        ('pause', {}), ('play', {}), ('skip_next', {}), ('skip_prev', {}),
        ('seek', {'position_ms': 30000}), ('set_volume', {'volume': 50}),
    ]


def test_enabling_context_repeat_turns_track_repeat_off_first():
    api = connected_api()

    assert api.set_repeat_context(True) is True
    assert [(m['command'], m['enabled']) for m in api.sent] == [
        ('set_repeat_track', False), ('set_repeat_context', True),
    ]


def test_late_result_of_an_earlier_command_is_not_taken_as_the_answer():
    # command_result names its command (verified on the device).
    def reply(message):
        if message['command'] == 'pause':
            return {'type': 'command_result', 'command': 'seek'}
        return None
    api = connected_api(reply=reply)

    assert api.pause() is False


def test_stray_error_without_pending_command_is_ignored():
    api = connected_api()

    api._handle_event({'type': 'error', 'message': 'malformed'})

    assert api.pause() is True


def test_transport_commands_are_not_sent_while_another_device_is_active():
    # Seen on the device: an inactive Soloist forwards pause/skip/volume to
    # the speaker that is playing. Mello must never do that.
    api = connected_api(active=False)

    assert not api.pause() and not api.resume() and not api.next() and not api.prev()
    assert not api.seek(1000) and not api.set_volume(100) and not api.set_repeat_context(True)
    assert api.sent == []


# --- resume inside an album ---------------------------------------------------

ALBUM = 'spotify:album:a1'
TRACKS = [f'spotify:track:t{n}' for n in range(1, 6)]


def fake_daemon(api_box, tracks=TRACKS, takeover_after_skips=None, repeat=False):
    """Answers like the real daemon: play starts track 1, skips move on.

    With `repeat` the album loops like on the device: the queue runs on into
    the next round, and "previous" at the first track wraps to the last.
    """
    state = {'index': 0, 'skips': 0}

    def snapshot(status='playing', is_active=True):
        event = playback_state(status=status, is_active=is_active, repeat='context' if repeat else 'off')
        event['context'] = entity(ALBUM, 'Album Name')
        event['item'] = entity(tracks[state['index']], 'Track')
        return event

    def reply(message):
        api = api_box[0]
        command = message['command']
        if command == 'get_state':
            return snapshot(status=state.get('status', 'playing'))
        if command == 'get_queue':
            coming = tracks[state['index'] + 1:]
            if repeat:
                coming = coming + tracks + tracks
            upcoming = [{'uid': u, 'source': 'context', 'item': entity(u, 'Track')} for u in coming]
            return {'type': 'queue_changed', 'previous': [], 'upcoming': upcoming}
        api._handle_event({'type': 'command_result', 'command': command})
        if command == 'play' and 'uri' in message:
            state['index'] = 0
            api._handle_event(snapshot())
        elif command == 'skip_next':
            state['skips'] += 1
            if takeover_after_skips == state['skips']:
                api._handle_event({'type': 'device_changed', 'is_active': False, 'device_name': 'Mello'})
            else:
                state['index'] += 1
                api._handle_event({'type': 'track_changed', 'item': entity(tracks[state['index']], 'Track')})
        elif command == 'skip_prev':
            state['index'] = (state['index'] - 1) % len(tracks) if repeat else max(0, state['index'] - 1)
            api._handle_event({'type': 'track_changed', 'item': entity(tracks[state['index']], 'Track')})
        elif command == 'pause':
            state['status'] = 'paused'
            api._handle_event({'type': 'playback_changed', 'status': 'paused'})
        return False

    return reply


def resumable_api(**daemon_kwargs):
    box = []
    api = connected_api(reply=fake_daemon(box, **daemon_kwargs))
    box.append(api)
    return api


def commands(api):
    return [m['command'] for m in api.sent if m['command'] != 'get_state']


def test_resume_starts_album_then_skips_to_saved_track_and_pauses():
    api = resumable_api()

    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True

    assert commands(api) == ['play', 'get_queue', 'skip_next', 'skip_next', 'skip_next']
    # Until the caller has seeked and resumed, the app only sees "loading".
    loading = PlayerStatus.from_dict(api.status())
    assert (loading.context_uri, loading.track_uri, loading.playing) == (ALBUM, None, False)

    assert api.seek(45000) is True
    assert api.resume() is True
    assert commands(api) == ['play', 'get_queue', 'skip_next', 'skip_next', 'skip_next', 'seek']
    status = api.status()
    assert status['track']['uri'] == TRACKS[3]
    assert status['context_uri'] == ALBUM


def test_resume_on_first_track_needs_no_skips():
    api = resumable_api()

    assert api.play(ALBUM, skip_to_uri=TRACKS[0], paused=True) is True
    assert commands(api) == ['play']


def test_resume_of_track_no_longer_in_album_starts_at_the_beginning_without_seeking():
    api = resumable_api()

    assert api.play(ALBUM, skip_to_uri='spotify:track:gone', paused=True) is True

    assert 'skip_next' not in commands(api)
    # The saved position belongs to the missing track, not to track 1.
    assert api.seek(45000) is False
    assert 'seek' not in commands(api)
    assert api.resume() is True
    assert api.status()['track']['uri'] == TRACKS[0]


def test_resume_stops_skipping_when_another_device_takes_over():
    api = resumable_api(takeover_after_skips=1)

    assert api.play(ALBUM, skip_to_uri=TRACKS[4], paused=True) is False
    assert commands(api).count('skip_next') == 1


def test_resume_fails_when_album_never_starts():
    api = connected_api()   # accepts commands but never reports the new context
    api.command_timeout = 0.05
    import mello.api.soloist as soloist
    original, soloist._SWITCH_TIMEOUT = soloist._SWITCH_TIMEOUT, 0.1
    try:
        assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is False
    finally:
        soloist._SWITCH_TIMEOUT = original
    assert commands(api) == ['play']


def test_play_becomes_the_active_device_before_starting_content():
    # Seen on the device: `play <uri>` from an inactive Soloist started the
    # album on the OTHER speaker. Activating first keeps it here.
    def reply(message):
        if message['command'] == 'activate':
            api._handle_event({'type': 'command_result', 'command': 'activate'})
            api._handle_event({'type': 'device_changed', 'is_active': True, 'device_name': 'Mello'})
            return False
        return {'type': 'command_result', 'command': message['command']}
    api = connected_api(active=False, reply=reply)

    assert api.play(ALBUM) is True
    assert commands(api) == ['activate', 'play']


def test_play_fails_when_device_cannot_become_active():
    import mello.api.soloist as soloist
    api = connected_api(active=False)   # accepts activate, never becomes active
    original, soloist._ACTIVATE_TIMEOUT = soloist._ACTIVATE_TIMEOUT, 0.1
    try:
        assert api.play(ALBUM) is False
    finally:
        soloist._ACTIVATE_TIMEOUT = original
    assert commands(api) == ['activate']


def test_play_after_pause_on_same_track_does_not_restart_the_album():
    api = resumable_api()
    assert api.play(ALBUM, skip_to_uri=TRACKS[0], paused=True) is True
    api.resume()
    api.pause()
    api.sent.clear()

    assert api.play(ALBUM, skip_to_uri=TRACKS[0], paused=True) is True

    assert commands(api) == []
    assert api.seek(16000) is True
    assert api.resume() is True
    assert commands(api) == ['seek', 'play']


def test_resume_ignores_state_restored_before_the_album_really_starts():
    # Seen on the device right after a daemon restart: the same album and an
    # old track are already cached, `play` is answered with a snapshot that
    # names no track, and the album only starts (at track 1) seconds later.
    import threading
    box = []
    daemon = fake_daemon(box)

    def reply(message):
        api = box[0]
        if message['command'] == 'activate':
            api._handle_event({'type': 'command_result', 'command': 'activate'})
            api._handle_event({'type': 'device_changed', 'is_active': True, 'device_name': 'Mello'})
            return False
        if message['command'] == 'play' and 'uri' in message:
            api._handle_event({'type': 'command_result', 'command': 'play'})
            nameless = playback_state()
            nameless['context'] = entity(ALBUM, 'Album Name')
            nameless['item'] = {'uri': '', 'entity_type': '', 'decorations': {}}
            api._handle_event(nameless)
            real_start = playback_state(status='buffering')
            real_start['context'] = entity(ALBUM, 'Album Name')
            real_start['item'] = entity(TRACKS[0], 'Track')
            threading.Timer(0.05, api._handle_event, [real_start]).start()
            return False
        return daemon(message)

    api = connected_api(reply=reply)
    box.append(api)
    restored = playback_state(status='paused')
    restored['context'] = entity(ALBUM, 'Album Name')
    restored['item'] = entity(TRACKS[2], 'Track')
    api._handle_event(restored)
    api._handle_event({'type': 'device_changed', 'is_active': False, 'device_name': 'Mello'})

    assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is True

    # It waited for the real start at track 1 and then skipped twice.
    assert commands(api) == ['activate', 'play', 'get_queue', 'skip_next', 'skip_next']
    api.resume()
    assert api.status()['track']['uri'] == TRACKS[2]


def test_swiping_away_aborts_a_resume_that_is_still_skipping():
    import threading
    box = []
    daemon = fake_daemon(box)

    def reply(message):
        api = box[0]
        if message['command'] == 'skip_next':
            # Accepted, but the tracks are not announced yet when the child swipes away.
            api._handle_event({'type': 'command_result', 'command': 'skip_next'})
            return False
        return daemon(message)

    api = connected_api(reply=reply)
    box.append(api)
    threading.Timer(0.1, api.abort_play).start()
    started = time.time()

    assert api.play(ALBUM, skip_to_uri=TRACKS[4], paused=True) is False

    assert time.time() - started < 1.0          # did not sit out the skip timeout
    # Seen on the device: the app then paused, read "paused on track 2" and
    # saved that as the child's place in the album. The half-skipped album
    # is reported as paused on nothing, also after the pause.
    nothing = {'stopped': False, 'paused': True, 'context_uri': None, 'track': None}
    assert api.status() == nothing
    api.pause()
    assert api.status() == nothing
    assert api.context_uri is None
    # Not reported as stopped: the app takes a sudden stop for "Spotify moved
    # to another device" and cancelled the play the child swiped to.
    assert api.resume() is False

    # The next real play clears it.
    assert api.play(ALBUM) is True
    assert api.status()['track']['uri'] == TRACKS[0]


def test_deep_resume_skips_in_bursts_and_confirms_each_one():
    tracks = [f'spotify:track:c{n}' for n in range(1, 31)]
    box = []
    api = connected_api(reply=fake_daemon(box, tracks=tracks))
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=tracks[29], paused=True) is True

    sent = commands(api)
    assert sent.count('skip_next') == 29
    # 12 + 12 + 5, the queue re-read before each burst.
    assert sent.count('get_queue') == 3
    api.resume()
    assert api.status()['track']['uri'] == tracks[29]


def test_resume_recovers_when_a_skip_gets_lost():
    box = []
    daemon = fake_daemon(box)
    lost = []

    def reply(message):
        api = box[0]
        if message['command'] == 'skip_next' and not lost:
            lost.append(1)
            api._handle_event({'type': 'command_result', 'command': 'skip_next'})
            return False
        return daemon(message)

    api = connected_api(reply=reply)
    box.append(api)
    import mello.api.soloist as soloist
    original, soloist._SKIP_TIMEOUT = soloist._SKIP_TIMEOUT, -1.4   # 3 skips -> 0.1s
    try:
        assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True
    finally:
        soloist._SKIP_TIMEOUT = original

    api.resume()
    assert api.status()['track']['uri'] == TRACKS[3]
    assert commands(api).count('skip_next') == 4      # 3, then 1 more for the lost one


def test_tracks_passed_while_skipping_are_never_reported():
    seen = []
    box = []
    daemon = fake_daemon(box)

    def reply(message):
        answer = daemon(message)
        if message['command'] == 'skip_next':
            seen.append(box[0].status()['track'])
        return answer

    api = connected_api(reply=reply)
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True
    assert seen == [None, None, None]


def test_repeat_is_not_sent_again_when_already_on():
    api = connected_api()
    api._handle_event(playback_state(repeat='context'))

    assert api.set_repeat_context(True) is True
    assert api.sent == []


def test_track_past_the_middle_is_reached_backwards_when_the_album_repeats():
    # Seen on the device: with repeat on, "previous" at track 1 goes to the
    # last track. Chapter 26 of 30 is then 5 skips back instead of 25 forward.
    tracks = [f'spotify:track:c{n}' for n in range(1, 31)]
    box = []
    api = connected_api(reply=fake_daemon(box, tracks=tracks, repeat=True))
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=tracks[25], paused=True) is True

    sent = commands(api)
    assert (sent.count('skip_prev'), sent.count('skip_next')) == (5, 0)
    api.resume()
    assert api.status()['track']['uri'] == tracks[25]


def test_track_before_the_middle_is_still_reached_forwards_when_the_album_repeats():
    tracks = [f'spotify:track:c{n}' for n in range(1, 31)]
    box = []
    api = connected_api(reply=fake_daemon(box, tracks=tracks, repeat=True))
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=tracks[8], paused=True) is True

    sent = commands(api)
    assert (sent.count('skip_prev'), sent.count('skip_next')) == (0, 8)
    api.resume()
    assert api.status()['track']['uri'] == tracks[8]


def test_far_track_goes_backwards_in_confirmed_bursts():
    tracks = [f'spotify:track:c{n}' for n in range(1, 61)]
    box = []
    api = connected_api(reply=fake_daemon(box, tracks=tracks, repeat=True))
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=tracks[35], paused=True) is True      # 25 back, not 35 forward

    sent = commands(api)
    assert (sent.count('skip_prev'), sent.count('skip_next')) == (25, 0)
    assert sent.count('get_queue') == 3                                       # 12 + 12 + 1
    api.resume()
    assert api.status()['track']['uri'] == tracks[35]


def test_buffering_that_never_turns_into_playing_is_checked_with_a_snapshot(monkeypatch):
    """Seen on the daughter's speaker: after quick skips Soloist said
    "buffering" for a minute while the episode played, and Mello restarted it
    again and again. A fresh snapshot has the real status."""
    import mello.api.soloist as soloist_mod
    clock = [1000.0]
    monkeypatch.setattr(soloist_mod.time, 'time', lambda: clock[0])

    def reply(message):
        if message['command'] == 'get_state':
            return playback_state(status='playing')
        return {'type': 'command_result', 'command': message['command']}

    api = connected_api(reply=reply)
    api._handle_event({'type': 'playback_changed', 'status': 'buffering'})
    api.sent.clear()

    assert PlayerStatus.from_dict(api.status()).playing is False   # a short load: just wait
    assert not [m for m in api.sent if m['command'] == 'get_state']

    clock[0] += 2.5
    assert PlayerStatus.from_dict(api.status()).playing is True    # asked, and it plays
    assert [m['command'] for m in api.sent].count('get_state') == 1


def test_stuck_buffering_is_rechecked_at_most_every_two_seconds(monkeypatch):
    import mello.api.soloist as soloist_mod
    clock = [1000.0]
    monkeypatch.setattr(soloist_mod.time, 'time', lambda: clock[0])
    api = connected_api(reply=lambda m: {'type': 'command_result', 'command': m['command']})
    api._handle_event({'type': 'playback_changed', 'status': 'buffering'})
    api.sent.clear()
    clock[0] += 3
    for _ in range(10):                 # the app asks for status many times a second
        api.status()
    clock[0] += 1
    api.status()
    clock[0] += 1.5
    api.status()
    assert [m['command'] for m in api.sent].count('get_state') == 2

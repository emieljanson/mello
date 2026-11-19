"""Direct play: Mello knows each album's order and plays tracks itself.

The fake daemon behaves like Soloist on the device: an album or playlist
starts at its first track, a show wherever Spotify decides; the queue shows
at most 80 items and runs on into the next round when the context repeats;
a single track played on its own is its own context.
"""
import json
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

from mello.api import soloist as soloist_module
from mello.api.play_orders import PlayOrders, one_round
from mello.models import PlayerStatus
from test_soloist_api import connected_api, entity, playback_state

ALBUM = 'spotify:album:a1'
PLAYLIST = 'spotify:playlist:p1'
SHOW = 'spotify:show:s1'
TRACKS = [f'spotify:track:t{n}' for n in range(1, 7)]
EPISODES = [f'spotify:episode:e{n}' for n in range(1, 7)]
DURATION = 180000


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(soloist_module, '_STEP_SETTLE_S', 0.05)
    monkeypatch.setattr(soloist_module, '_LEARN_WAIT', 1.0)


class Daemon:
    """Soloist as seen on the device, for one album/playlist/show."""

    def __init__(self, contexts=None, show_starts_at=0, queue_cap=80):
        self.contexts = contexts or {ALBUM: TRACKS, PLAYLIST: TRACKS, SHOW: EPISODES}
        self.show_starts_at = show_starts_at
        self.queue_cap = queue_cap
        self.context = None
        self.item = None
        self.repeat = 'off'
        self.status = 'playing'
        self.position = 1000
        self.api = None
        self.plays = []
        self.context_delay = 0.0   # Spotify takes a while to start an album
        self.placeholder = False   # Spotify's queue may hold non-playable entries

    def snapshot(self):
        event = playback_state(status=self.status, position_ms=self.position, repeat=self.repeat)
        event['context'] = entity(self.context or self.item, 'Ctx')
        event['item'] = entity(self.item, f'Name {self.item}', playback={'duration_ms': DURATION})
        return event

    def tracks(self):
        return self.contexts.get(self.context)

    def __call__(self, message):
        api, command = self.api, message['command']
        if command == 'get_state':
            return self.snapshot() if self.item else False
        if command == 'get_queue':
            tracks = self.tracks()
            if tracks:
                i = tracks.index(self.item)
                coming = tracks[i + 1:] + (tracks * 3 if self.repeat == 'context' else [])
            else:
                coming = [self.item] * 10 if self.repeat != 'off' else []
            coming = coming[:self.queue_cap]
            upcoming = [{'uid': u, 'source': 'context', 'item': entity(u, f'Name {u}')} for u in coming]
            if self.placeholder:
                upcoming.insert(1, {'uid': 'p', 'source': 'context',
                                    'item': entity('spotify:meta:node_rules_placeholder', '')})
            return {'type': 'queue_changed', 'previous': [], 'upcoming': upcoming}
        api._handle_event({'type': 'command_result', 'command': command})
        if command == 'play' and 'uri' in message:
            uri = message['uri']
            self.plays.append(uri)
            if uri in self.contexts and self.context_delay:
                time.sleep(self.context_delay)
            self.status, self.position = 'playing', 1000
            if uri in self.contexts:
                self.context = uri
                self.item = self.contexts[uri][self.show_starts_at if uri == SHOW else 0]
            else:
                self.context, self.item = None, uri
            api._handle_event(self.snapshot())
        elif command == 'play':
            self.status = 'playing'
            api._handle_event({'type': 'playback_changed', 'status': 'playing'})
        elif command == 'pause':
            self.status = 'paused'
            api._handle_event({'type': 'playback_changed', 'status': 'paused'})
        elif command == 'seek':
            self.position = message['position_ms']
            api._handle_event(self.snapshot())
        elif command in ('skip_next', 'skip_prev'):
            tracks = self.tracks()
            step = 1 if command == 'skip_next' else -1
            self.item = tracks[(tracks.index(self.item) + step) % len(tracks)]
            api._handle_event(self.snapshot())
        elif command == 'set_repeat_track':
            self.repeat = 'track' if message['enabled'] else 'off'
            api._handle_event({'type': 'options_changed', 'options': {'repeat': self.repeat}})
        elif command == 'set_repeat_context':
            self.repeat = 'context' if message['enabled'] else ('off' if self.repeat == 'context' else self.repeat)
            api._handle_event({'type': 'options_changed', 'options': {'repeat': self.repeat}})
        return False

    def end_of_track(self, status='buffering'):
        self.position, self.status = DURATION, status
        self.api._handle_event(self.snapshot())
        self.status = 'playing'


def setup(store=None, **daemon_kwargs):
    daemon = Daemon(**daemon_kwargs)
    store = store if store is not None else PlayOrders(None)
    api = connected_api(reply=daemon, play_orders=store)
    daemon.api = api
    return api, daemon, store


def wait_until(predicate, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def plays(api):
    return [m['uri'] for m in api.sent if m['command'] == 'play' and 'uri' in m]


# --- the order -----------------------------------------------------------------

def test_one_round_of_a_looping_queue():
    assert one_round('b', ['c', 'd', 'a', 'b', 'c']) == (['b', 'c', 'd', 'a'], True)
    assert one_round('b', ['c', 'd']) == (['b', 'c', 'd'], False)    # only the next part


def test_parts_of_a_long_album_are_joined():
    store = PlayOrders(None)
    t = TRACKS
    store.put(ALBUM, t[0:4], complete=False)
    store.put(ALBUM, t[2:6], complete=False)
    assert store.get(ALBUM) == t
    store.put(ALBUM, t[1:3], complete=False)
    assert store.get(ALBUM) == t


def test_orders_and_names_survive_a_restart_and_do_not_expire(tmp_path):
    now = [1000.0]
    path = tmp_path / 'play_orders.json'
    PlayOrders(path, clock=lambda: now[0]).put(ALBUM, TRACKS, names={TRACKS[0]: 'Hoofdstuk 1'})
    now[0] += 365 * 24 * 3600
    again = PlayOrders(path, clock=lambda: now[0])
    assert again.get(ALBUM) == TRACKS
    assert again.name(ALBUM, TRACKS[0]) == 'Hoofdstuk 1'


# --- starting ------------------------------------------------------------------

def test_first_resume_reads_the_order_and_jumps_there_without_skipping():
    api, daemon, store = setup()

    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True

    assert daemon.plays == [ALBUM, TRACKS[3]]
    assert not any(m['command'] in ('skip_next', 'skip_prev') for m in api.sent)
    assert store.get(ALBUM) == TRACKS
    assert store.entry(ALBUM)[1] is True                # looped: the whole album
    assert store.name(ALBUM, TRACKS[3]) == f'Name {TRACKS[3]}'
    assert api.seek(45000) is True and api.resume() is True
    status = api.status()
    assert (status['context_uri'], status['track']['uri']) == (ALBUM, TRACKS[3])


def test_known_order_plays_the_saved_track_at_once():
    api, daemon, store = setup()
    store.put(ALBUM, TRACKS)
    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True
    assert daemon.plays == [TRACKS[3]]
    assert daemon.repeat == 'track'
    loading = PlayerStatus.from_dict(api.status())
    assert (loading.context_uri, loading.track_uri) == (ALBUM, None)     # "loading" until resumed
    api.resume()
    assert api.status()['repeat_context'] is True
    assert api.context_uri == ALBUM


def test_playlist_order_is_checked_again_after_a_day(monkeypatch):
    api, daemon, store = setup()
    store.put(PLAYLIST, TRACKS, complete=False)
    store._orders[PLAYLIST]['learned_at'] -= 21 * 3600
    assert api.play(PLAYLIST, skip_to_uri=TRACKS[2], paused=True) is True
    assert daemon.plays == [PLAYLIST, TRACKS[2]]


def test_show_order_is_not_checked_again_by_age():
    api, daemon, store = setup()
    store.put(SHOW, EPISODES, complete=False)
    store._orders[SHOW]['learned_at'] -= 30 * 24 * 3600
    assert api.play(SHOW, skip_to_uri=EPISODES[2], paused=True) is True
    assert daemon.plays == [EPISODES[2]]


def test_saved_track_not_in_the_order_reads_it_again():
    api, daemon, store = setup()
    store.put(PLAYLIST, TRACKS[:3], complete=False)
    assert api.play(PLAYLIST, skip_to_uri=TRACKS[4], paused=True) is True
    assert daemon.plays == [PLAYLIST, TRACKS[4]]
    assert TRACKS[4] in store.get(PLAYLIST)


def test_show_starting_past_the_saved_episode_still_plays_the_saved_episode():
    api, daemon, store = setup(show_starts_at=4)       # Spotify picks episode 5
    assert api.play(SHOW, skip_to_uri=EPISODES[1], paused=True) is True
    assert daemon.plays == [SHOW, EPISODES[1]]


def test_start_without_saved_track_learns_in_the_background():
    api, daemon, store = setup()
    assert api.play(ALBUM) is True
    assert daemon.plays == [ALBUM]
    api.set_repeat_context(True)                      # the app does this right after
    assert wait_until(lambda: store.get(ALBUM) == TRACKS)


def test_long_audiobook_beyond_the_queue_skips_the_old_way_once():
    long_book = [f'spotify:track:c{n}' for n in range(1, 101)]
    api, daemon, store = setup(contexts={ALBUM: long_book})
    assert api.play(ALBUM, skip_to_uri=long_book[90], paused=True) is True
    assert daemon.item == long_book[90]
    assert any(m['command'] in ('skip_next', 'skip_prev') for m in api.sent)


def test_swiping_away_during_a_direct_start_keeps_the_order():
    api, daemon, store = setup()
    store.put(ALBUM, TRACKS)

    def silent(message):          # play accepted, nothing ever starts; the child swipes on
        api._handle_event({'type': 'command_result', 'command': message['command']})
        if message['command'] == 'play':
            api.abort_play()
        return False

    api.ws.send = lambda raw: (api.sent.append(__import__('json').loads(raw)),
                               silent(__import__('json').loads(raw)))
    assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is False
    assert store.get(ALBUM) == TRACKS


def test_a_direct_start_that_never_starts_forgets_the_order(monkeypatch):
    monkeypatch.setattr(soloist_module, '_DIRECT_TIMEOUT', 0.2)
    api, daemon, store = setup()
    store.put(ALBUM, TRACKS)
    api.ws.send = lambda raw: (api.sent.append(__import__('json').loads(raw)),
                               api._handle_event({'type': 'command_result',
                                                  'command': __import__('json').loads(raw)['command']}))
    assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is False
    assert store.get(ALBUM) is None


def test_starting_an_album_the_normal_way_turns_track_repeat_off():
    api, daemon, store = setup()
    store.put(ALBUM, TRACKS)
    api.play(ALBUM, skip_to_uri=TRACKS[2])
    assert daemon.repeat == 'track'
    api.play(SHOW)                                    # a show, no saved episode
    assert daemon.repeat == 'off'                     # otherwise episode 1 plays forever


# --- while it plays ------------------------------------------------------------

def playing(track=TRACKS[1], context=ALBUM, store_kwargs=None, **setup_kwargs):
    api, daemon, store = setup(**setup_kwargs)
    order = TRACKS if context != SHOW else EPISODES
    store.put(context, order, **(store_kwargs or {}))
    api.play(context, skip_to_uri=track)
    daemon.plays.clear()
    api.sent.clear()
    return api, daemon, store


def test_next_track_starts_when_one_ends_and_only_once():
    api, daemon, _ = playing()
    daemon.end_of_track()
    assert daemon.plays == [TRACKS[2]]
    api._handle_event(track_snapshot_end(TRACKS[1]))      # late news about the old one
    assert daemon.plays == [TRACKS[2]]


def track_snapshot_end(uri):
    event = playback_state(status='buffering', position_ms=DURATION, repeat='track')
    event['context'] = entity(uri, 'Ctx')
    event['item'] = entity(uri, 'x', playback={'duration_ms': DURATION})
    return event


def test_next_track_starts_just_before_the_end():
    api, daemon, _ = playing()
    daemon.position = DURATION - 400
    api._handle_event(daemon.snapshot())
    assert wait_until(lambda: daemon.plays == [TRACKS[2]], 1.5)


def test_a_track_that_started_over_moves_on_and_is_retried_if_stuck(monkeypatch):
    monkeypatch.setattr(soloist_module, '_ADVANCE_RETRY_S', 0.1)
    api, daemon, _ = playing()
    daemon.position = DURATION - 3000
    api._handle_event(daemon.snapshot())
    daemon.reply_play = False
    api.ws.send_backup = api.ws.send
    # The next track does not start (no network): the old one starts over.
    real = daemon.__call__
    daemon_calls = []

    def offline(raw):
        import json
        message = json.loads(raw)
        api.sent.append(message)
        daemon_calls.append(message)
        if message['command'] == 'play' and len([m for m in daemon_calls if m['command'] == 'play']) == 1:
            return   # lost
        real(message)

    api.ws.send = offline
    daemon.position = 200
    api._handle_event(daemon.snapshot())              # started over: move on
    assert plays(api) == [TRACKS[2]]
    time.sleep(0.15)
    daemon.position = DURATION - 3000
    api._handle_event(daemon.snapshot())
    daemon.position = 200
    api._handle_event(daemon.snapshot())              # still the old track: try again
    assert plays(api) == [TRACKS[2], TRACKS[2]]


def test_last_track_of_an_album_goes_round_to_the_first():
    api, daemon, _ = playing(track=TRACKS[-1])
    daemon.end_of_track()
    assert daemon.plays == [TRACKS[0]]


def test_end_of_a_playlist_starts_it_over_the_normal_way():
    api, daemon, store = playing(track=TRACKS[-2], context=PLAYLIST, store_kwargs={'complete': False})
    daemon.end_of_track()
    assert daemon.plays == [TRACKS[-1]]
    daemon.end_of_track()
    assert wait_until(lambda: PLAYLIST in daemon.plays)
    assert daemon.item == TRACKS[0]


def test_while_reading_the_list_again_the_album_is_shown_not_the_last_track():
    """Seen on the device: next tapped past the last known track of a list.

    For a moment only the bare track that played directly was known; the app
    showed it as a new item and the carousel jumped to it and back.
    """
    api, daemon, store = playing(track=TRACKS[-1], context=PLAYLIST, store_kwargs={'complete': False})
    daemon.context_delay = 0.3
    seen = []
    api.next()
    end = time.time() + 1.5
    while time.time() < end:
        status = api.status()
        seen.append(status and status.get('context_uri'))
        time.sleep(0.01)
    assert PLAYLIST in daemon.plays
    assert set(seen) == {PLAYLIST}, set(seen)
    assert daemon.item == TRACKS[0]


def test_end_of_the_known_episodes_reads_the_show_again_for_new_ones():
    api, daemon, store = playing(track=EPISODES[4], context=SHOW, store_kwargs={'complete': False},
                                 show_starts_at=4)
    store.put(SHOW, EPISODES[:5], complete=False)     # episode 6 is new
    api._direct['order'] = EPISODES[:5]
    daemon.end_of_track()
    assert wait_until(lambda: daemon.plays[-1:] == [EPISODES[5]], 3)
    assert SHOW in daemon.plays
    assert EPISODES[5] in store.get(SHOW)


def test_quick_taps_on_next_send_one_play_and_show_the_title_at_once():
    api, daemon, store = playing(track=TRACKS[0])
    store.put(ALBUM, TRACKS, names={t: f'Hoofdstuk {i + 1}' for i, t in enumerate(TRACKS)})
    for _ in range(4):
        assert api.next() is True
    shown = api.status()['track']
    assert (shown['uri'], shown['name']) == (TRACKS[4], 'Hoofdstuk 5')
    assert daemon.plays == []
    assert wait_until(lambda: daemon.plays == [TRACKS[4]])


def test_next_and_previous_mixed_end_where_they_add_up():
    api, daemon, _ = playing(track=TRACKS[2])
    api.next(); api.next(); api.prev(); api.next(); api.next()
    assert wait_until(lambda: daemon.plays == [TRACKS[5]])


def test_previous_well_into_a_track_goes_back_to_its_start():
    api, daemon, _ = playing(track=TRACKS[2])
    daemon.position = 60000
    api._handle_event(daemon.snapshot())
    api.prev()
    assert [m['command'] for m in api.sent if m['command'] != 'get_state'][-1] == 'seek'
    api.prev()                                        # at the start now: the one before
    assert wait_until(lambda: daemon.plays == [TRACKS[1]])


def test_two_quick_taps_on_previous_go_to_the_track_before():
    api, daemon, _ = playing(track=TRACKS[2])
    daemon.position = 60000
    api._handle_event(daemon.snapshot())
    api.ws.send_real = api.ws.send
    # The seek's answer has not arrived yet when the second tap comes.
    api.prev()
    daemon.position = 60000
    api._handle_event(daemon.snapshot())
    api.prev()
    assert wait_until(lambda: daemon.plays == [TRACKS[1]])


def test_previous_on_the_first_episode_of_a_show_restarts_it():
    api, daemon, _ = playing(track=EPISODES[0], context=SHOW, store_kwargs={'complete': False})
    api.prev()
    time.sleep(0.15)
    assert daemon.plays == []
    assert any(m['command'] == 'seek' for m in api.sent)


def test_next_past_the_known_episodes_reads_the_show_again():
    api, daemon, _ = playing(track=EPISODES[-1], context=SHOW, store_kwargs={'complete': False})
    api.next()
    assert wait_until(lambda: SHOW in daemon.plays, 3)


def test_swiping_away_stops_the_album_from_moving_on():
    api, daemon, _ = playing()
    api.abort_play()                                  # the child swiped to another album
    api.pause()
    daemon.end_of_track(status='buffering')
    daemon.position = DURATION - 300
    api._handle_event(daemon.snapshot())
    time.sleep(0.4)
    assert daemon.plays == []
    api.resume()                                      # back again and play
    daemon.end_of_track()
    assert daemon.plays == [TRACKS[2]]


def test_pending_taps_are_dropped_when_swiping_away():
    api, daemon, _ = playing()
    api.next(); api.next()
    api.abort_play()
    time.sleep(0.15)
    assert daemon.plays == []


def test_album_playing_the_normal_way_uses_its_order_for_next():
    api, daemon, store = setup()
    store.put(ALBUM, TRACKS)
    api.play(ALBUM)                                   # from the start: the normal way
    daemon.plays.clear()
    api.next(); api.next()
    assert wait_until(lambda: daemon.plays == [TRACKS[2]])


def test_unknown_album_uses_spotifys_own_skip():
    api, daemon, store = setup()
    api.play(ALBUM)
    store.forget(ALBUM)
    api.sent.clear()
    api.next()
    assert [m['command'] for m in api.sent if m['command'] != 'get_state'] == ['skip_next']


def test_context_repeat_request_keeps_track_repeat_on():
    api, daemon, _ = playing()
    api.set_repeat_context(True)
    assert daemon.repeat == 'track'


def test_something_else_playing_ends_direct_play():
    api, daemon, _ = playing()
    other = playback_state(repeat='off')
    other['context'] = entity('spotify:album:other', 'x')
    other['item'] = entity('spotify:track:x', 'x', playback={'duration_ms': DURATION})
    api._handle_event(other)
    assert api.context_uri == 'spotify:album:other'
    other['status'] = 'buffering'
    other['position']['position_ms'] = DURATION
    api._handle_event(other)
    assert daemon.plays == []


def test_direct_play_is_picked_up_after_an_app_restart():
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS)
    api = connected_api(play_orders=store)           # Spotify is still on the single track
    event = playback_state(repeat='track')
    event['context'] = entity(TRACKS[2], 'x')
    event['item'] = entity(TRACKS[2], 'x', playback={'duration_ms': DURATION})
    api._handle_event(event)
    assert api.context_uri == ALBUM
    api.sent.clear()
    event['status'], event['position']['position_ms'] = 'buffering', DURATION
    api._handle_event(event)
    assert plays(api) == [TRACKS[3]]


def test_a_single_track_picked_by_the_child_is_not_taken_for_its_album():
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS)
    api = connected_api(play_orders=store)
    event = playback_state(repeat='off')
    event['context'] = entity(TRACKS[2], 'x')
    event['item'] = entity(TRACKS[2], 'x', playback={'duration_ms': DURATION})
    api._handle_event(event)
    assert api.context_uri == TRACKS[2]


def test_saved_track_gone_from_a_changing_playlist_does_not_skip_round_and_round():
    api, daemon, store = setup()
    assert api.play(PLAYLIST, skip_to_uri='spotify:track:gone', paused=True) is True
    skips = [m for m in api.sent if m['command'] in ('skip_next', 'skip_prev')]
    assert skips == []
    assert daemon.item == TRACKS[0]                    # it starts at the beginning


def test_taps_while_the_playlist_plays_the_normal_way_are_not_lost():
    api, daemon, store = setup()
    api.play(PLAYLIST)                                 # normal way, from the start
    assert wait_until(lambda: store.get(PLAYLIST) is not None)
    daemon.plays.clear()
    api.next(); api.next(); api.next()
    api._handle_event(daemon.snapshot())               # the playlist's own news meanwhile
    assert wait_until(lambda: daemon.plays == [TRACKS[3]])
    # The step timer sends play, then track repeat: the second may still be on its way.
    assert wait_until(lambda: daemon.repeat == 'track')


def test_leaving_direct_play_switches_track_repeat_off():
    api, daemon, _ = playing()
    assert daemon.repeat == 'track'
    other = playback_state(repeat='track')
    other['context'] = entity('spotify:album:other', 'x')
    other['item'] = entity('spotify:track:x', 'x', playback={'duration_ms': DURATION})
    api._handle_event(other)
    assert daemon.repeat == 'off'


def test_placeholders_in_spotifys_queue_are_not_learned_as_tracks(tmp_path):
    """Seen on the device: a single's queue held spotify:meta:node_rules_placeholder;
    "next" then asked Spotify to play it and was refused."""
    api, daemon, store = setup()
    daemon.placeholder = True
    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True
    assert store.get(ALBUM) == TRACKS

    path = tmp_path / 'play_orders.json'
    path.write_text(json.dumps({ALBUM: {'items': [TRACKS[0], 'spotify:meta:node_rules_placeholder'],
                                        'complete': False, 'learned_at': time.time()}}))
    assert PlayOrders(path).get(ALBUM) == [TRACKS[0]]


def test_a_pause_does_not_count_as_listened_time():
    """Audiobook chapters were cut short after a pause.

    Soloist answers pause/resume with only a status change, no new position.
    Counting on from the position seen before the pause added the paused time
    to it, so the next chapter started that much too early.
    """
    api, daemon, _ = playing(track=EPISODES[1], context=SHOW)
    daemon.position = DURATION - 2000                  # 2 s left
    api._handle_event(daemon.snapshot())
    daemon({'command': 'pause'})
    time.sleep(1.5)                                    # paused for a while
    daemon({'command': 'play'})                        # resume: status only
    time.sleep(1.0)                                    # 1 s of the chapter still to go
    assert daemon.plays == []
    assert wait_until(lambda: daemon.plays == [EPISODES[2]], 3.0)


def test_position_stands_still_while_paused():
    api, daemon, _ = playing(track=EPISODES[1], context=SHOW)
    daemon.position = 60000
    api._handle_event(daemon.snapshot())
    daemon({'command': 'pause'})
    time.sleep(0.5)
    daemon({'command': 'play'})
    assert api._current_position_ms() < 60300

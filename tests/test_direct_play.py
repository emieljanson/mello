"""Direct play: start the saved track itself instead of skipping to it."""
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from mello.api import soloist as soloist_module
from mello.api.play_orders import PlayOrders, one_round
from mello.models import LibrespotStatus
from test_soloist_api import connected_api, entity, playback_state, fake_daemon, ALBUM, TRACKS

DURATION = 180000


def track_snapshot(uri, status='playing', position_ms=1000, context=None, is_active=True, repeat='track'):
    event = playback_state(status=status, position_ms=position_ms, is_active=is_active, repeat=repeat)
    event['context'] = entity(context or uri, 'Track')
    event['item'] = entity(uri, 'Track', playback={'duration_ms': DURATION})
    return event


def direct_daemon(box, start_status='playing'):
    """Answers like the device when single tracks are played."""
    def reply(message):
        api = box[0]
        command = message['command']
        api._handle_event({'type': 'command_result', 'command': command})
        if command == 'play' and 'uri' in message:
            api._handle_event(track_snapshot(message['uri'], status=start_status))
        elif command == 'seek':
            api._handle_event(track_snapshot(api._item_uri(), position_ms=message['position_ms']))
        return False
    return reply


def direct_api(orders=None, **kwargs):
    box = []
    store = PlayOrders(None)
    for context, items in (orders or {ALBUM: TRACKS}).items():
        store.put(context, items)
    api = connected_api(reply=direct_daemon(box, **kwargs), play_orders=store)
    box.append(api)
    return api


def sent(api):
    return [(m['command'], m.get('uri') or m.get('enabled')) for m in api.sent if m['command'] != 'get_state']


# --- orders ------------------------------------------------------------------

def test_one_round_of_a_looping_queue():
    assert one_round('b', ['c', 'd', 'a', 'b', 'c']) == (['b', 'c', 'd', 'a'], True)
    # A long audiobook: the queue (80 at most) only shows the next part.
    assert one_round('b', ['c', 'd']) == (['b', 'c', 'd'], False)


def test_parts_of_a_long_album_are_joined():
    store = PlayOrders(None)
    store.put(ALBUM, ['t1', 't2', 't3', 't4'], complete=False)
    store.put(ALBUM, ['t3', 't4', 't5', 't6'], complete=False)
    assert store.get(ALBUM) == ['t1', 't2', 't3', 't4', 't5', 't6']
    store.put(ALBUM, ['t2', 't3'], complete=False)          # a shorter look keeps the rest
    assert store.get(ALBUM) == ['t1', 't2', 't3', 't4', 't5', 't6']


def test_last_known_track_of_a_partial_order_is_not_played_directly():
    box = []
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS[:3], complete=False)
    api = connected_api(reply=fake_daemon(box), play_orders=store)
    box.append(api)
    assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is True
    assert api.sent[0]['uri'] == ALBUM                    # the album, not the track


def test_end_of_a_partial_order_continues_the_normal_way():
    box = []
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS[:3], complete=False)
    api = connected_api(reply=direct_daemon(box), play_orders=store)
    box.append(api)
    assert api.play(ALBUM, skip_to_uri=TRACKS[1]) is True      # t3 is still known after it
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[1], status='buffering', position_ms=DURATION))
    assert sent(api)[0] == ('play', TRACKS[2])
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[2], status='buffering', position_ms=DURATION))
    deadline = time.time() + 2
    while not api.sent and time.time() < deadline:
        time.sleep(0.02)
    assert api.sent[0] == {'type': 'command', 'command': 'play', 'uri': ALBUM}


def test_orders_survive_a_restart_and_get_old(tmp_path):
    now = [1000.0]
    path = tmp_path / 'play_orders.json'
    PlayOrders(path, clock=lambda: now[0]).put(ALBUM, TRACKS)
    again = PlayOrders(path, max_age=60, clock=lambda: now[0])
    assert again.get(ALBUM) == TRACKS
    now[0] += 61
    assert again.get(ALBUM) is None


# --- starting -----------------------------------------------------------------

def test_resume_plays_the_saved_track_directly():
    api = direct_api()

    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True

    assert sent(api) == [('play', TRACKS[3]), ('set_repeat_track', True)]
    loading = LibrespotStatus.from_dict(api.status())
    assert (loading.context_uri, loading.track_uri) == (ALBUM, None)   # "loading" until resumed
    assert api.seek(45000) is True
    assert api.resume() is True
    status = api.status()
    assert status['context_uri'] == ALBUM          # the app still sees the album
    assert status['track']['uri'] == TRACKS[3]
    assert status['repeat_context'] is True
    assert api.context_uri == ALBUM


def test_track_not_in_the_known_order_uses_the_album():
    box = []
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS[:2])
    api = connected_api(reply=fake_daemon(box), play_orders=store)
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=TRACKS[3], paused=True) is True
    assert [c for c, _ in sent(api)][:1] == ['play']
    assert api.sent[0]['uri'] == ALBUM


def test_start_without_saved_track_uses_the_album():
    box = []
    api = connected_api(reply=fake_daemon(box), play_orders=PlayOrders(None))
    box.append(api)
    api.play_orders.put(ALBUM, TRACKS)

    assert api.play(ALBUM) is True
    assert api.sent[0]['uri'] == ALBUM


def test_failed_direct_start_forgets_the_order_so_the_retry_uses_the_album(monkeypatch):
    monkeypatch.setattr(soloist_module, '_DIRECT_TIMEOUT', 0.2)
    box = []
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS)

    def silent(message):          # accepts play, but nothing ever starts
        box[0]._handle_event({'type': 'command_result', 'command': message['command']})
        return False

    api = connected_api(reply=silent, play_orders=store)
    box.append(api)
    assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is False
    assert store.get(ALBUM) is None


# --- while playing ------------------------------------------------------------

def test_next_track_starts_when_one_ends():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[1])
    api.sent.clear()

    api._handle_event(track_snapshot(TRACKS[1], position_ms=DURATION - 60000))
    assert sent(api) == []
    api._handle_event(track_snapshot(TRACKS[1], status='buffering', position_ms=DURATION - 200))
    assert sent(api) == [('play', TRACKS[2]), ('set_repeat_track', True)]
    # The same end reported twice does not skip twice.
    api._handle_event(track_snapshot(TRACKS[1], status='buffering', position_ms=DURATION - 100))
    assert len(sent(api)) == 2


def test_track_that_already_started_again_also_moves_on():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[1])
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[1], position_ms=DURATION - 3000))
    api._handle_event(track_snapshot(TRACKS[1], position_ms=200))       # repeat started it over
    assert sent(api)[0] == ('play', TRACKS[2])


def test_last_track_goes_round_to_the_first():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[-1])
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[-1], status='buffering', position_ms=DURATION))
    assert sent(api)[0] == ('play', TRACKS[0])


def test_next_and_previous_buttons_play_tracks_directly():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[2])
    api.sent.clear()

    assert api.next() is True
    assert sent(api) == [('play', TRACKS[3]), ('set_repeat_track', True)]
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[3], position_ms=1000))
    assert api.prev() is True
    assert sent(api)[0] == ('play', TRACKS[2])
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[2], position_ms=60000))
    assert api.prev() is True                    # well into the track: back to its start
    assert sent(api) == [('seek', None)]


def test_context_repeat_request_keeps_track_repeat_on():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[2])
    api.sent.clear()
    assert api.set_repeat_context(True) is True
    assert sent(api) == []


def test_something_else_playing_ends_direct_play():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[2])
    other = track_snapshot('spotify:track:x', context='spotify:album:other')
    api._handle_event(other)
    assert api.context_uri == 'spotify:album:other'
    api.sent.clear()
    api._handle_event(track_snapshot('spotify:track:x', status='buffering', position_ms=DURATION,
                                     context='spotify:album:other'))
    assert sent(api) == []                        # not ours to move on


def test_old_album_state_while_starting_does_not_end_direct_play():
    api = direct_api()
    old = track_snapshot('spotify:track:old', context='spotify:album:old')
    api._handle_event(old)
    assert api.play(ALBUM, skip_to_uri=TRACKS[2]) is True
    assert api.context_uri == ALBUM


# --- learning -----------------------------------------------------------------

def test_order_is_learned_after_the_album_plays_normally(monkeypatch):
    monkeypatch.setattr(soloist_module, '_LEARN_DELAY', 0.0)
    box = []
    store = PlayOrders(None)
    api = connected_api(reply=fake_daemon(box, repeat=True), play_orders=store)
    box.append(api)

    assert api.play(ALBUM, skip_to_uri=TRACKS[2], paused=True) is True
    api.resume()
    deadline = time.time() + 3
    while store.get(ALBUM) is None and time.time() < deadline:
        time.sleep(0.05)
    order = store.get(ALBUM)
    assert order is not None and sorted(order) == sorted(TRACKS)
    assert order[(order.index(TRACKS[2]) + 1) % len(order)] == TRACKS[3]


def test_next_track_starts_just_before_the_end(monkeypatch):
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[1])
    api.sent.clear()
    # 0.4 s left of the track: the next one starts after ~0.15 s, without
    # waiting for Soloist to report the end.
    api._handle_event(track_snapshot(TRACKS[1], position_ms=DURATION - 400))
    deadline = time.time() + 1.5
    while not api.sent and time.time() < deadline:
        time.sleep(0.02)
    assert sent(api)[:1] == [('play', TRACKS[2])]
    # Soloist's own end report afterwards does not skip a second time.
    api._handle_event(track_snapshot(TRACKS[1], status='buffering', position_ms=DURATION))
    assert [c for c in sent(api) if c[0] == 'play'] == [('play', TRACKS[2])]


def test_paused_track_does_not_move_on():
    api = direct_api()
    api.play(ALBUM, skip_to_uri=TRACKS[1])
    api._handle_event(track_snapshot(TRACKS[1], position_ms=DURATION - 400))
    api._handle_event({'type': 'playback_changed', 'status': 'paused'})
    api.sent.clear()
    time.sleep(0.6)
    assert sent(api) == []


def test_direct_play_is_picked_up_after_a_restart():
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS)
    api = connected_api(play_orders=store)       # a fresh app; Spotify is still on the single track
    api._handle_event(track_snapshot(TRACKS[2], position_ms=5000))
    assert api.context_uri == ALBUM
    api.sent.clear()
    api._handle_event(track_snapshot(TRACKS[2], status='buffering', position_ms=DURATION))
    assert [m.get('uri') for m in api.sent if m['command'] == 'play'] == [TRACKS[3]]


def test_a_single_track_that_is_in_no_album_is_left_alone():
    api = connected_api(play_orders=PlayOrders(None))
    api._handle_event(track_snapshot('spotify:track:alone'))
    assert api.context_uri == 'spotify:track:alone'


def test_a_single_track_picked_by_the_child_is_not_taken_for_its_album():
    store = PlayOrders(None)
    store.put(ALBUM, TRACKS)
    api = connected_api(play_orders=store)
    api._handle_event(track_snapshot(TRACKS[2], repeat='off'))     # Mello plays a track item
    assert api.context_uri == TRACKS[2]


def test_orders_do_not_expire():
    now = [1000.0]
    store = PlayOrders(None, clock=lambda: now[0])
    store.put(ALBUM, TRACKS)
    now[0] += 365 * 24 * 3600
    assert store.get(ALBUM) == TRACKS


def test_end_of_a_show_is_reached_the_normal_way_to_find_new_episodes():
    show = 'spotify:show:s1'
    episodes = [f'spotify:episode:e{n}' for n in range(1, 4)]
    box = []
    store = PlayOrders(None)
    store.put(show, episodes)                       # stored as a whole round
    api = connected_api(reply=direct_daemon(box), play_orders=store)
    box.append(api)
    assert api.play(show, skip_to_uri=episodes[1]) is True
    api.sent.clear()
    api._handle_event(track_snapshot(episodes[1], status='buffering', position_ms=DURATION))
    assert sent(api)[0] == ('play', episodes[2])
    api.sent.clear()
    api._handle_event(track_snapshot(episodes[2], status='buffering', position_ms=DURATION))
    deadline = time.time() + 2
    while not api.sent and time.time() < deadline:
        time.sleep(0.02)
    assert api.sent[0] == {'type': 'command', 'command': 'play', 'uri': show}   # not back to episode 1


def test_saved_episode_missing_from_the_order_starts_the_show_normally():
    show = 'spotify:show:s1'
    box = []
    store = PlayOrders(None)
    store.put(show, ['spotify:episode:e1', 'spotify:episode:e2'], complete=False)
    api = connected_api(reply=fake_daemon(box), play_orders=store)
    box.append(api)
    api.play(show, skip_to_uri='spotify:episode:new')
    assert api.sent[0]['uri'] == show

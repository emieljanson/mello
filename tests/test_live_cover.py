"""Live now-playing cover state, caching, race, and WebSocket tests."""
import threading
import types
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.app import Mello
from mello.models import NowPlaying


def _now(track: str, cover: str | None = None, *, playing: bool = True,
         paused: bool = False, context: str = 'spotify:playlist:test') -> NowPlaying:
    return NowPlaying(
        playing=playing,
        paused=paused,
        stopped=not playing and not paused,
        context_uri=context,
        track_uri=f'spotify:track:{track}',
        track_cover=cover if cover is not None else f'https://covers.test/{track}.jpg',
    )


def _make_app(now_playing: NowPlaying) -> Mello:
    app = Mello.__new__(Mello)
    app._now_playing_lock = threading.Lock()
    app._now_playing = now_playing
    app._live_cover_lock = threading.Lock()
    app._live_cover_path = None
    app._live_cover_key = None
    app._live_cover_display_key = None
    app._live_cover_pending_key = None
    app._live_cover_inflight = set()
    app._live_cover_cache = OrderedDict()
    app._live_cover_failures = OrderedDict()
    app._temp_item_lock = threading.Lock()
    app.temp_item = None
    app.catalog_manager = SimpleNamespace(download_temp_image=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app.events = SimpleNamespace(context_uri=now_playing.context_uri)
    app.sleep_manager = SimpleNamespace(is_sleeping=False)
    app._poll_wake_event = MagicMock()
    return app


def _capture_jobs():
    jobs = []

    def capture(fn, *args):
        jobs.append((fn, args))

    return jobs, capture


def _run_job(job):
    fn, args = job
    fn(*args)


class TestLiveCoverState:
    def test_track_a_to_b_keeps_a_until_b_is_ready(self):
        app = _make_app(_now('a'))
        app.catalog_manager.download_temp_image.side_effect = ['/images/a.png', '/images/b.png']
        jobs, capture = _capture_jobs()

        with patch('mello.app.run_async', side_effect=capture):
            app._update_live_cover(app.now_playing)
            _run_job(jobs.pop(0))
            assert app._live_cover_for_render(app.now_playing) == '/images/a.png'

            app.now_playing = _now('b')
            app._update_live_cover(app.now_playing)
            assert app._live_cover_for_render(app.now_playing) == '/images/a.png'

            _run_job(jobs.pop(0))

        assert app._live_cover_for_render(app.now_playing) == '/images/b.png'

    def test_stale_b_completion_cannot_overwrite_c(self):
        app = _make_app(_now('a'))
        app.catalog_manager.download_temp_image.side_effect = [
            '/images/a.png', '/images/b.png', '/images/c.png'
        ]
        jobs, capture = _capture_jobs()

        with patch('mello.app.run_async', side_effect=capture):
            app._update_live_cover(app.now_playing)
            _run_job(jobs.pop(0))
            app.now_playing = _now('b')
            app._update_live_cover(app.now_playing)
            b_job = jobs.pop(0)
            app.now_playing = _now('c')
            app._update_live_cover(app.now_playing)
            c_job = jobs.pop(0)

            _run_job(b_job)
            assert app._live_cover_for_render(app.now_playing) == '/images/a.png'
            assert app._live_cover_cache[Mello._live_cover_key_for(_now('b'))] == '/images/b.png'
            _run_job(c_job)

        assert app._live_cover_for_render(app.now_playing) == '/images/c.png'

    def test_failed_download_keeps_previous_cover(self):
        app = _make_app(_now('a'))
        app.catalog_manager.download_temp_image.side_effect = ['/images/a.png', None]
        jobs, capture = _capture_jobs()

        with patch('mello.app.run_async', side_effect=capture):
            app._update_live_cover(app.now_playing)
            _run_job(jobs.pop(0))
            app.now_playing = _now('b')
            app._update_live_cover(app.now_playing)
            _run_job(jobs.pop(0))

        assert app._live_cover_for_render(app.now_playing) == '/images/a.png'
        with patch('mello.app.run_async') as retry:
            app._update_live_cover(app.now_playing)
        retry.assert_not_called()

    def test_cached_cover_switches_immediately_without_download(self):
        app = _make_app(_now('a'))
        key = Mello._live_cover_key_for(app.now_playing)
        app._live_cover_cache[key] = '/images/a.png'

        with patch('mello.app.run_async') as run_async:
            app._update_live_cover(app.now_playing)

        assert app._live_cover_for_render(app.now_playing) == '/images/a.png'
        run_async.assert_not_called()
        app.catalog_manager.download_temp_image.assert_not_called()

    def test_same_pending_cover_does_not_start_parallel_downloads(self):
        app = _make_app(_now('a'))
        jobs, capture = _capture_jobs()

        with patch('mello.app.run_async', side_effect=capture):
            app._update_live_cover(app.now_playing)
            app._update_live_cover(app.now_playing)

        assert len(jobs) == 1

    def test_playlist_tracks_from_different_albums_get_distinct_covers(self):
        app = _make_app(_now('album-a'))
        app.catalog_manager.download_temp_image.side_effect = [
            '/images/album-a.png', '/images/album-b.png'
        ]
        jobs, capture = _capture_jobs()

        with patch('mello.app.run_async', side_effect=capture):
            app._update_live_cover(app.now_playing)
            _run_job(jobs.pop(0))
            app.now_playing = _now('album-b')
            app._update_live_cover(app.now_playing)
            _run_job(jobs.pop(0))

        assert app._live_cover_for_render(app.now_playing) == '/images/album-b.png'
        assert len(app._live_cover_cache) == 2

    def test_pause_resume_keeps_cover(self):
        paused = _now('a', playing=False, paused=True)
        app = _make_app(paused)
        key = Mello._live_cover_key_for(paused)
        app._live_cover_cache[key] = '/images/a.png'
        app._update_live_cover(paused)

        resumed = _now('a', playing=True)
        app.now_playing = resumed
        app._update_live_cover(resumed)

        assert app._live_cover_for_render(resumed) == '/images/a.png'

    def test_missing_cover_url_falls_back_to_catalog(self):
        missing = _now('a', cover='')
        app = _make_app(missing)
        app._live_cover_path = '/images/old.png'

        with patch('mello.app.run_async') as run_async:
            app._update_live_cover(missing)

        assert app._live_cover_for_render(missing) is None
        run_async.assert_not_called()

    def test_cache_is_lru_bounded(self):
        app = _make_app(_now('0'))
        for index in range(45):
            now = _now(str(index))
            key = Mello._live_cover_key_for(now)
            app.now_playing = now
            app._live_cover_display_key = key
            app.catalog_manager.download_temp_image.return_value = f'/images/{index}.png'
            app._live_cover_inflight.add(key)
            app._download_live_cover_async(key)

        assert len(app._live_cover_cache) == 40


def test_websocket_event_wakes_single_status_poller_without_starting_worker():
    app = _make_app(_now('a'))

    with patch('mello.app.run_async') as run_async:
        app._on_ws_update()

    app._poll_wake_event.set.assert_called_once_with()
    run_async.assert_not_called()


def test_status_refresh_lock_prevents_parallel_requests():
    app = Mello.__new__(Mello)
    app._status_refresh_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def refresh_locked():
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(timeout=1)
        with state_lock:
            active -= 1

    app._refresh_status_locked = refresh_locked
    first = threading.Thread(target=app._refresh_status)
    second = threading.Thread(target=app._refresh_status)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert max_active == 1

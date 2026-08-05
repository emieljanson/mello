"""
Tests for the browse-does-not-autoplay gate.

Swiping the carousel is browsing. It switches albums while something is already
playing, but from a stopped or paused device only the play button starts audio.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.app import Mello
from mello.models import NowPlaying


def _app(playing=False, paused=False, stopped=True, play_in_progress=False) -> Mello:
    app = Mello.__new__(Mello)
    app._now_playing_lock = threading.Lock()  # now_playing goes through it
    app._now_playing = NowPlaying(playing=playing, paused=paused, stopped=stopped)
    app.playback = SimpleNamespace(play_in_progress=play_in_progress)
    return app


def test_playing_allows_switching_albums():
    """Mid-playback, swiping to another album should still switch to it."""
    assert _app(playing=True, stopped=False)._playback_is_live() is True


def test_stopped_does_not_autoplay():
    assert _app()._playback_is_live() is False


def test_paused_does_not_autoplay():
    """The complaint that started this: paused, swipe, music starts. It must not."""
    assert _app(paused=True, stopped=False)._playback_is_live() is False


def test_in_flight_request_keeps_the_gate_open():
    """During a context switch status reports nothing playing; the retry logic
    inside the gate still has to run, or a dropped request never recovers."""
    assert _app(play_in_progress=True)._playback_is_live() is True


def test_paused_with_request_in_flight_is_live():
    assert _app(paused=True, stopped=False, play_in_progress=True)._playback_is_live() is True

"""
Tests for UsageTracker privacy defaults and payload behavior.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from berry.managers import analytics as analytics_module
from berry.models import NowPlaying


class _FakePosthog:
    def __init__(self, *args, **kwargs):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None):
        self.events.append((event, distinct_id, properties or {}))

    def shutdown(self):
        return None


def test_analytics_excludes_content_fields_by_default(monkeypatch):
    monkeypatch.setattr(analytics_module, "HAS_POSTHOG", True)
    monkeypatch.setattr(analytics_module, "Posthog", _FakePosthog, raising=False)

    tracker = analytics_module.UsageTracker(api_key="k", include_content=False)
    np = NowPlaying(
        playing=True,
        context_uri="spotify:album:test",
        track_name="Song",
        track_artist="Artist",
        track_album="Album",
    )
    tracker.update(np)

    event, _, properties = tracker._posthog.events[-1]
    assert event == "session_start"
    assert "track" not in properties
    assert "artist" not in properties
    assert "album" not in properties
    assert properties["content_type"] == "album"


def test_analytics_can_include_content_fields_when_enabled(monkeypatch):
    monkeypatch.setattr(analytics_module, "HAS_POSTHOG", True)
    monkeypatch.setattr(analytics_module, "Posthog", _FakePosthog, raising=False)

    tracker = analytics_module.UsageTracker(api_key="k", include_content=True)
    np = NowPlaying(
        playing=True,
        context_uri="spotify:playlist:test",
        track_name="Song",
        track_artist="Artist",
        track_album="Album",
    )
    tracker.update(np)

    event, _, properties = tracker._posthog.events[-1]
    assert event == "session_start"
    assert properties["track"] == "Song"
    assert properties["artist"] == "Artist"
    assert properties["album"] == "Album"
    assert properties["content_type"] == "playlist"


def test_device_id_defaults_to_hostname_without_machine_id(monkeypatch):
    monkeypatch.setattr(analytics_module.socket, "gethostname", lambda: "berry-box")
    device_id = analytics_module.UsageTracker._get_device_id(use_machine_id=False)
    assert device_id == "berry-box"


def test_explicit_distinct_id_overrides_derived_device_id(monkeypatch):
    monkeypatch.setattr(analytics_module, "HAS_POSTHOG", True)
    monkeypatch.setattr(analytics_module, "Posthog", _FakePosthog, raising=False)
    monkeypatch.setattr(analytics_module.socket, "gethostname", lambda: "berry-host")

    tracker = analytics_module.UsageTracker(
        api_key="k",
        distinct_id="berry-custom-id",
        include_content=False,
    )
    tracker.on_app_started()

    _, distinct_id, _ = tracker._posthog.events[-1]
    assert distinct_id == "berry-custom-id"

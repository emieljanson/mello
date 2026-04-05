"""
Mello Data Models - Core data structures.
"""
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, List, Literal


class MenuState(Enum):
    """Setup menu states (replaces 3 separate booleans)."""
    CLOSED = auto()
    MAIN = auto()
    WIFI_LIST = auto()
    WIFI_AP = auto()
    BT_LIST = auto()
    VOLUME_LEVELS = auto()


@dataclass
class LibrespotStatus:
    """Parsed response from the go-librespot /status endpoint."""
    playing: bool = False
    paused: bool = False
    stopped: bool = True
    volume: Optional[int] = None
    context_uri: Optional[str] = None
    track_name: Optional[str] = None
    track_artist: Optional[str] = None
    track_album: Optional[str] = None
    track_cover: Optional[str] = None
    track_uri: Optional[str] = None
    position: int = 0
    duration: int = 0

    @classmethod
    def from_dict(cls, data: dict, context_uri: Optional[str] = None) -> 'LibrespotStatus':
        """Parse raw API dict into a typed object."""
        track = data.get('track') or {}
        if not isinstance(track, dict):
            track = {}
        
        artist_names = track.get('artist_names', [])
        artist = ', '.join(artist_names) if artist_names else None
        
        raw_context_uri = data.get('context_uri') if isinstance(data, dict) else None
        resolved_context_uri = raw_context_uri or context_uri

        return cls(
            playing=not data.get('stopped', True) and not data.get('paused', False),
            paused=data.get('paused', False),
            stopped=data.get('stopped', True),
            volume=data.get('volume'),
            context_uri=resolved_context_uri,
            track_name=track.get('name'),
            track_artist=artist,
            track_album=track.get('album_name'),
            track_cover=track.get('album_cover_url'),
            track_uri=track.get('uri'),
            position=track.get('position', 0),
            duration=track.get('duration', 0),
        )


@dataclass
class CatalogItem:
    """Represents an album or playlist in the catalog."""
    id: str
    uri: str
    name: str
    type: str = 'album'
    artist: Optional[str] = None
    image: Optional[str] = None
    images: Optional[List[str]] = None  # For playlist composite covers
    current_track: Optional[dict] = None
    is_temp: bool = False


@dataclass
class NowPlaying:
    """Current playback state from librespot."""
    playing: bool = False
    paused: bool = False
    stopped: bool = True
    context_uri: Optional[str] = None
    track_name: Optional[str] = None
    track_artist: Optional[str] = None
    track_album: Optional[str] = None
    track_cover: Optional[str] = None
    track_uri: Optional[str] = None
    position: int = 0
    duration: int = 0
    
    @property
    def progress(self) -> float:
        """Get playback progress as 0.0-1.0."""
        if self.duration <= 0:
            return 0.0
        return min(1.0, self.position / self.duration)
    
    def __repr__(self) -> str:
        state = 'playing' if self.playing else ('paused' if self.paused else 'stopped')
        track = self.track_name or '(none)'
        return f'NowPlaying({state}, {track}, {self.position // 1000}s/{self.duration // 1000}s)'


@dataclass
class PlayState:
    """
    Unified play/loading state for UI feedback.
    
    Replaces multiple separate variables:
    - _optimistic_playing
    - _is_loading / _should_show_loading  
    - _loading_start_time
    """
    pending_action: Optional[Literal['play', 'pause']] = None
    loading_since: Optional[float] = None
    
    # Delay before showing spinner (prevents flicker)
    SPINNER_DELAY = 0.2
    
    def set_pending(self, action: Literal['play', 'pause']):
        """Set a pending play/pause action."""
        self.pending_action = action
        if action == 'play':
            self.loading_since = time.time()
        else:
            self.loading_since = None
    
    def clear(self):
        """Clear pending state (real data received)."""
        self.pending_action = None
        self.loading_since = None
    
    def start_loading(self):
        """Start loading state (for navigation pause, play timer, etc.)."""
        if self.loading_since is None:
            self.loading_since = time.time()
    
    def stop_loading(self):
        """Stop loading state."""
        self.loading_since = None
    
    @property
    def is_loading(self) -> bool:
        """True if loading long enough to show spinner (200ms delay)."""
        if self.loading_since is None:
            return False
        return time.time() - self.loading_since > self.SPINNER_DELAY
    
    @property
    def should_show_loading(self) -> bool:
        """True if in any loading state (for play button icon)."""
        if self.pending_action == 'pause':
            return False
        return self.loading_since is not None

    @property
    def pause_intent_active(self) -> bool:
        """True when a user pause intent should dominate UI state."""
        return self.pending_action == 'pause'
    
    def display_playing(self, actual_playing: bool) -> bool:
        """What the UI should show for play/pause state."""
        if self.pause_intent_active:
            return False
        if self.pending_action == 'play' or self.should_show_loading:
            return True
        return actual_playing


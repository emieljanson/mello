"""
The player interface: what the app asks of Spotify's player (Soloist, see
api/soloist.py), and a stand-in that does nothing for mock mode.
"""
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class PlayerAPIProtocol(Protocol):
    """Interface that both real and mock API must implement."""

    def status(self) -> Optional[dict]: ...
    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False) -> Optional[bool]: ...
    def pause(self) -> bool: ...
    def resume(self) -> bool: ...
    def next(self) -> bool: ...
    def prev(self) -> bool: ...
    def seek(self, position: int) -> bool: ...
    def set_volume(self, level: int) -> bool: ...
    def set_repeat_context(self, enabled: bool) -> bool: ...
    def abort_play(self) -> None: ...
    def is_connected(self) -> bool: ...
    def metrics_snapshot(self) -> dict: ...


class NullPlayerAPI:
    """
    Null object API for mock/test mode.
    
    All methods return success but do nothing.
    Use this instead of if mock_mode checks throughout the code. Like
    SoloistAPI it is also the app's event source (which never fires).
    """

    context_uri: Optional[str] = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def status(self) -> Optional[dict]:
        return None
    
    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False) -> Optional[bool]:
        return True
    
    def pause(self) -> bool:
        return True
    
    def resume(self) -> bool:
        return True
    
    def next(self) -> bool:
        return True
    
    def prev(self) -> bool:
        return True
    
    def seek(self, position: int) -> bool:
        return True
    
    def set_volume(self, level: int) -> bool:
        return True

    def set_repeat_context(self, enabled: bool) -> bool:
        return True

    def abort_play(self) -> None:
        pass
    
    def is_connected(self) -> bool:
        return True

    def metrics_snapshot(self) -> dict:
        return {'suppressed': {}, 'failures': {}}

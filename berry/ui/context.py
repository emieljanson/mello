"""
Render Context - Bundles all state needed for rendering.
"""
from dataclasses import dataclass
from typing import Optional, List

from ..models import CatalogItem, NowPlaying


@dataclass
class RenderContext:
    """All state needed to render a frame."""
    items: List[CatalogItem]
    selected_index: int
    now_playing: NowPlaying
    scroll_x: float
    drag_offset: float
    dragging: bool
    is_sleeping: bool
    connected: bool
    volume_index: int
    delete_mode_id: Optional[str]
    pressed_button: Optional[str]
    is_loading: bool
    is_playing: bool  # What to show for play/pause button
    needs_setup: bool
    admin_menu_open: bool
    admin_version: str
    admin_confirm_action: Optional[str]  # Action awaiting confirmation


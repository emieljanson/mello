"""
Render Context - Bundles all state needed for rendering.
"""
from dataclasses import dataclass, field
from typing import Optional, List

from ..models import CatalogItem, MenuState, NowPlaying
from ..managers.bluetooth import BluetoothDevice


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
    volume_index: int
    delete_mode_id: Optional[str]
    pressed_button: Optional[str]
    is_loading: bool
    is_playing: bool  # What to show for play/pause button
    pending_focus_uri: Optional[str] = None
    requested_focus_uri: Optional[str] = None
    play_in_progress: bool = False
    toast_message: Optional[str] = None
    menu_state: MenuState = MenuState.CLOSED
    menu_known_networks: List[str] = field(default_factory=list)
    menu_current_network: Optional[str] = None
    auto_pause_minutes: int = 30
    progress_expiry_hours: int = 96
    app_version_label: str = ''
    bt_connected: bool = False          # A BT audio device is connected
    bt_audio_active: bool = False       # Audio is routed to BT (headphone icon purple)
    bt_connected_name: Optional[str] = None
    bt_paired_devices: List[BluetoothDevice] = field(default_factory=list)
    bt_discovered_devices: List[BluetoothDevice] = field(default_factory=list)
    bt_scanning: bool = False
    volume_levels: list = field(default_factory=list)  # For volume settings screen
    menu_scroll_offset: int = 0


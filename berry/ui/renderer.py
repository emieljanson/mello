"""
Renderer - All drawing/rendering logic for the Berry UI.
"""
import logging
import time
import math
from typing import Optional, List, Dict, Tuple, Callable

import pygame
import pygame.gfxdraw

from .helpers import draw_aa_circle, draw_aa_rounded_rect
from .image_cache import ImageCache
from .context import RenderContext
from ..models import CatalogItem, NowPlaying
from ..config import (
    SCREEN_WIDTH, SCREEN_HEIGHT, COLORS,
    COVER_SIZE, COVER_SIZE_SMALL, COVER_SPACING,
    TRACK_INFO_X, CAROUSEL_X, CONTROLS_X, CAROUSEL_CENTER_Y,
    BTN_SIZE, PLAY_BTN_SIZE, BTN_SPACING, PROGRESS_BAR_WIDTH,
    VOLUME_LEVELS,
)

logger = logging.getLogger(__name__)


class Renderer:
    """Handles all drawing/rendering for Berry UI."""
    
    def __init__(self, screen: pygame.Surface, image_cache: ImageCache, icons: Dict[str, pygame.Surface]):
        self.screen = screen
        self.image_cache = image_cache
        self.icons = icons
        
        # Fonts
        self.font_large = pygame.font.Font(None, 42)
        self.font_medium = pygame.font.Font(None, 32)
        self.font_small = pygame.font.Font(None, 24)
        
        # Caches
        self._bg_cache: Optional[pygame.Surface] = None
        self._progress_cache: Dict[str, pygame.Surface] = {}
        self._text_cache: Dict[str, pygame.Surface] = {}
        self._last_track_key: Optional[Tuple[str, str]] = None
        self._spinner_cache: Dict[int, List[pygame.Surface]] = {}  # size -> list of frames
        self._spinner_overlay_cache: Dict[int, pygame.Surface] = {}  # size -> overlay
        self._spinner_frame_idx: int = 0  # Simple frame counter for consistent rotation
        
        # Partial update state
        self._needs_full_redraw = True
        self._static_layer: Optional[pygame.Surface] = None
        # Portrait mode: carousel spans Y axis (user's horizontal), X for vertical positioning
        self._carousel_rect = pygame.Rect(CAROUSEL_X - 50, 0, COVER_SIZE + 100, SCREEN_HEIGHT)
        self._last_playing_state: Optional[bool] = None
        self._last_selected_index: Optional[int] = None
        
        # Button hit rectangles (updated during draw)
        self.add_button_rect: Optional[Tuple[int, int, int, int]] = None
        self.delete_button_rect: Optional[Tuple[int, int, int, int]] = None
        self.admin_menu_rects: Dict[str, Tuple[int, int, int, int]] = {}
        
        # Profiler callback (set by app.py when profiling is enabled)
        self._profile_mark: Optional[Callable[[str], None]] = None
    
    def set_profiler(self, mark_fn: Optional[Callable[[str], None]]):
        """Set the profiler mark function for detailed draw timing."""
        self._profile_mark = mark_fn
    
    def _mark(self, section: str):
        """Mark a profiler section if profiling is enabled."""
        if self._profile_mark:
            self._profile_mark(section)
    
    def invalidate(self):
        """Force a full redraw on next frame."""
        self._needs_full_redraw = True
    
    def draw(self, ctx: RenderContext) -> Optional[List[pygame.Rect]]:
        """
        Main draw method.
        
        Args:
            ctx: RenderContext with all state needed to render
        
        Returns list of dirty rects for partial update, or None for full flip.
        """
        # Sleep mode - show black screen only
        if ctx.is_sleeping:
            self.screen.fill((0, 0, 0))
            self._needs_full_redraw = True
            return None

        # WiFi reset status screen (takes priority over admin menu)
        if ctx.wifi_reset_status:
            self._draw_wifi_reset_status(ctx.wifi_reset_status)
            self._needs_full_redraw = True
            return None

        # Admin menu overlay
        if ctx.admin_menu_open:
            self._draw_admin_menu(ctx.admin_version, ctx.admin_confirm_action)
            self._needs_full_redraw = True
            return None

        # Setup mode - show Spotify connect instructions
        if ctx.needs_setup:
            self._draw_background()
            self._draw_setup_screen()
            self._needs_full_redraw = True
            return None
        
        # Clear button hit rects
        self.add_button_rect = None
        self.delete_button_rect = None
        
        # Get current item to check track info
        current_item = ctx.items[ctx.selected_index] if ctx.selected_index < len(ctx.items) else None
        
        # Determine current track key (same logic as _draw_track_info)
        if current_item:
            if ctx.now_playing.context_uri == current_item.uri and ctx.now_playing.track_name:
                current_track_key = (ctx.now_playing.track_name, ctx.now_playing.track_artist or '')
            elif current_item.current_track and isinstance(current_item.current_track, dict):
                name = current_item.current_track.get('name', current_item.name) or current_item.name
                artist = current_item.current_track.get('artist', current_item.artist or '') or current_item.artist or ''
                current_track_key = (name, artist)
            else:
                current_track_key = (current_item.name or 'Unknown', current_item.artist or '')
        else:
            current_track_key = None
        
        # Check if we need a full redraw
        state_changed = (
            self._last_playing_state != ctx.now_playing.playing or
            self._last_selected_index != ctx.selected_index or
            self._last_track_key is None or
            self._last_track_key != current_track_key
        )
        
        if state_changed:
            self._needs_full_redraw = True
            self._last_playing_state = ctx.now_playing.playing
            self._last_selected_index = ctx.selected_index
        
        # Disconnected state
        if not ctx.connected:
            self._draw_background()
            self._draw_disconnected()
            self._needs_full_redraw = True
            return None
        
        # Empty state
        if not ctx.items:
            self._draw_background()
            self._draw_empty_state()
            self._needs_full_redraw = True
            return None
        
        # Calculate effective scroll position
        if ctx.dragging:
            drag_index_offset = -ctx.drag_offset / (COVER_SIZE + COVER_SPACING)
            effective_scroll = ctx.selected_index + drag_index_offset
        else:
            effective_scroll = ctx.scroll_x
        
        # Determine if animating
        is_animating = ctx.dragging or abs(ctx.scroll_x - ctx.selected_index) > 0.01
        
        if self._needs_full_redraw:
            # Full redraw
            self._draw_background()
            self._mark('draw_bg')
            
            self._draw_track_info(current_item, ctx.now_playing)
            self._mark('draw_track')
            
            self._draw_controls(ctx.is_playing, ctx.volume_index, ctx.pressed_button)
            self._mark('draw_controls')
            
            # Cache static parts
            if self._static_layer is None:
                self._static_layer = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
            self._static_layer.blit(self.screen, (0, 0))
            self._mark('cache_static')
            
            # Draw carousel
            self._draw_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id, ctx.is_loading)
            self._mark('draw_carousel')
            
            self._needs_full_redraw = False
            return None
        
        elif is_animating:
            # Partial update - only carousel area
            self.screen.blit(self._static_layer, 
                           self._carousel_rect.topleft, 
                           self._carousel_rect)
            self._mark('blit_static')
            
            self._draw_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id, ctx.is_loading)
            self._mark('draw_carousel')
            return [self._carousel_rect]
        
        else:
            # Idle - update progress bar if playing
            if ctx.now_playing.playing or ctx.is_loading:
                self.screen.blit(self._static_layer,
                               self._carousel_rect.topleft,
                               self._carousel_rect)
                self._mark('blit_static')
                
                self._draw_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id, ctx.is_loading)
                self._mark('draw_carousel')
                return [self._carousel_rect]
            return []
    
    def _draw_background(self):
        """Draw pre-rendered background with gradient (portrait mode)."""
        if not self._bg_cache:
            self._bg_cache = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
            self._bg_cache.fill(COLORS['bg_primary'])
            # Portrait mode: gradient from right edge (user's top) along X axis
            # X=720 is user's top, so gradient fades from X=720 towards X=570
            for offset in range(150):
                x = SCREEN_WIDTH - 1 - offset  # Start at right edge (user's top)
                alpha = int(30 * (1 - offset / 150))
                color = (
                    min(255, COLORS['bg_primary'][0] + int(alpha * 0.75)),
                    min(255, COLORS['bg_primary'][1] + int(alpha * 0.4)),
                    min(255, COLORS['bg_primary'][2] + alpha),
                )
                pygame.draw.line(self._bg_cache, color, (x, 0), (x, SCREEN_HEIGHT))
            self._bg_cache = self._bg_cache.convert()
        
        self.screen.blit(self._bg_cache, (0, 0))
    
    def _render_text_rotated(self, text: str, font: pygame.font.Font, color: tuple) -> pygame.Surface:
        """Render text rotated 90° CW for portrait display mode."""
        text_surface = font.render(text, True, color)
        return pygame.transform.rotate(text_surface, -90)  # -90 = 90° CW
    
    def _draw_disconnected(self):
        """Draw disconnected state (portrait mode)."""
        text = self._render_text_rotated('Connecting to Berry...', self.font_large, COLORS['text_secondary'])
        # Portrait center: X=360 (vertical center), Y=640 (horizontal center)
        rect = text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
        self.screen.blit(text, rect)
    
    def _draw_setup_screen(self):
        """Draw Spotify setup instructions for first-time users (portrait mode)."""
        # Portrait: X is user's vertical (720 total), Y is user's horizontal (1280 total)
        center_x = SCREEN_WIDTH // 2   # 360 - vertical center
        center_y = SCREEN_HEIGHT // 2  # 640 - horizontal center
        
        # Title (rotated text, positioned at user's top)
        title = self._render_text_rotated('Welkom bij Berry', self.font_large, COLORS['text_primary'])
        # User's top = high X value. Offset from center along X.
        title_rect = title.get_rect(center=(center_x + 120, center_y))
        self.screen.blit(title, title_rect)
        
        # Spotify icon (using accent color circle as placeholder)
        # Position slightly below title in user's view = slightly lower X
        icon_x = center_x + 30
        pygame.draw.circle(self.screen, COLORS['accent'], (icon_x, center_y), 40)
        
        # Music note symbol (rotated)
        note = self._render_text_rotated('♪', self.font_large, COLORS['text_primary'])
        note_rect = note.get_rect(center=(icon_x, center_y))
        self.screen.blit(note, note_rect)
        
        # Instructions (each on separate line in user's view = spaced along Y)
        instructions = [
            "Open Spotify op je telefoon",
            "Tik op het speaker icoon",
            "Kies 'Berry'",
        ]
        
        # Start position: below icon in user's view = lower X value
        x_pos = center_x - 60
        y_start = center_y - 200  # Start from user's left side
        
        for i, line in enumerate(instructions):
            y_pos = y_start + i * 150  # Space along Y (user's horizontal)
            
            # Step number
            step_text = f"{i + 1}."
            step = self._render_text_rotated(step_text, self.font_medium, COLORS['accent'])
            step_rect = step.get_rect(center=(x_pos + 30, y_pos))
            self.screen.blit(step, step_rect)
            
            # Instruction text
            text = self._render_text_rotated(line, self.font_medium, COLORS['text_secondary'])
            text_rect = text.get_rect(center=(x_pos - 10, y_pos))
            self.screen.blit(text, text_rect)
        
        # Waiting indicator (at user's bottom = low X value)
        waiting = self._render_text_rotated('Wachten op verbinding...', self.font_small, COLORS['text_muted'])
        waiting_rect = waiting.get_rect(center=(60, center_y))
        self.screen.blit(waiting, waiting_rect)
    
    def _draw_admin_menu(self, version: str, confirm_action: Optional[str]):
        """Draw admin settings menu (portrait mode).

        Portrait coordinate system:
        - Physical X (0-720) = user's vertical. X=0 is user's bottom, X=720 is user's top.
        - Physical Y (0-1280) = user's horizontal. Y=0 is user's left, Y=1280 is user's right.
        For vertical stacking (user's POV), rows are spaced along X axis.
        Each row spans full Y width (user's horizontal).
        """
        self.admin_menu_rects.clear()

        # Dark background
        self.screen.fill(COLORS['bg_primary'])

        center_y = SCREEN_HEIGHT // 2  # 640 (user's horizontal center)

        # Title — at user's top (high X)
        title = self._render_text_rotated('Instellingen', self.font_large, COLORS['text_primary'])
        title_rect = title.get_rect(center=(SCREEN_WIDTH - 80, center_y))
        self.screen.blit(title, title_rect)

        # Menu items — stacked vertically (along physical X axis)
        menu_items = [
            ('reset_spotify', 'Reset Spotify'),
            ('reset_wifi', 'Reset WiFi'),
            ('restart', 'Herstart Berry'),
            ('close', 'Sluiten'),
        ]

        row_height = 110  # Height along X (user's vertical spacing)
        total_height = len(menu_items) * row_height
        # Center vertically in user's view, offset down from title
        # High X = user's top, so first item starts at high X
        start_x = (SCREEN_WIDTH + 80) // 2 + total_height // 2 - row_height

        for i, (action, label) in enumerate(menu_items):
            row_center_x = start_x - i * row_height

            # Determine label and color
            if confirm_action == action:
                display_label = 'Tik nogmaals om te bevestigen'
                color = COLORS['error']
            elif action == 'close':
                display_label = label
                color = COLORS['text_muted']
            else:
                display_label = label
                color = COLORS['text_primary']

            # Separator line (horizontal in user's view = along Y axis in physical)
            if i > 0:
                sep_x = row_center_x + row_height // 2
                pygame.draw.line(self.screen, COLORS['bg_elevated'],
                               (sep_x, 100), (sep_x, SCREEN_HEIGHT - 100), 1)

            # Label text — centered in row
            text = self._render_text_rotated(display_label, self.font_medium, color)
            text_rect = text.get_rect(center=(row_center_x, center_y))
            self.screen.blit(text, text_rect)

            # Hit rect: (x, y, width, height) — spans full Y, row_height along X
            rect_x = row_center_x - row_height // 2
            self.admin_menu_rects[action] = (rect_x, 0, row_height, SCREEN_HEIGHT)

        # Version at user's bottom (low X)
        version_text = self._render_text_rotated(f'v. {version}', self.font_small, COLORS['text_muted'])
        version_rect = version_text.get_rect(center=(60, center_y))
        self.screen.blit(version_text, version_rect)

    def _draw_wifi_reset_status(self, status: str):
        """Draw WiFi reset status screen (portrait mode).

        Shows progress/instructions during WiFi reset process.
        """
        self.screen.fill(COLORS['bg_primary'])
        center_x = SCREEN_WIDTH // 2
        center_y = SCREEN_HEIGHT // 2

        status_messages = {
            'deleting': ('WiFi resetten...', None),
            'portal_active': ('Verbind met', "'Berry-Setup' WiFi"),
            'success': ('WiFi verbonden!', 'Herstarten...'),
            'error': ('WiFi reset mislukt', 'Terug naar menu...'),
        }

        title, subtitle = status_messages.get(status, ('WiFi...', None))

        title_surf = self._render_text_rotated(title, self.font_large, COLORS['text_primary'])
        title_rect = title_surf.get_rect(center=(center_x + (40 if subtitle else 0), center_y))
        self.screen.blit(title_surf, title_rect)

        if subtitle:
            sub_surf = self._render_text_rotated(subtitle, self.font_medium, COLORS['text_muted'])
            sub_rect = sub_surf.get_rect(center=(center_x - 40, center_y))
            self.screen.blit(sub_surf, sub_rect)

    def _draw_empty_state(self):
        """Draw empty catalog state (portrait mode)."""
        center_x = SCREEN_WIDTH // 2   # 360
        center_y = SCREEN_HEIGHT // 2  # 640
        
        # Draw plus icon (already rotated when loaded)
        icon = self.icons.get('plus')
        if icon:
            icon_size = 64
            scaled_icon = pygame.transform.smoothscale(icon, (icon_size, icon_size))
            tinted = scaled_icon.copy()
            tinted.fill(COLORS['accent'], special_flags=pygame.BLEND_RGB_MULT)
            # Position above center in user's view = higher X
            icon_rect = tinted.get_rect(center=(center_x + 40, center_y))
            self.screen.blit(tinted, icon_rect)
        
        title = self._render_text_rotated('No music yet', self.font_large, COLORS['text_primary'])
        title_rect = title.get_rect(center=(center_x - 30, center_y))
        self.screen.blit(title, title_rect)
        
        sub = self._render_text_rotated('Play music via Spotify and tap + to add', self.font_medium, COLORS['text_secondary'])
        sub_rect = sub.get_rect(center=(center_x - 70, center_y))
        self.screen.blit(sub, sub_rect)
    
    def _draw_track_info(self, item: Optional[CatalogItem], now_playing: NowPlaying):
        """Draw track name and artist (portrait mode - at user's top)."""
        if not item:
            return
        
        # Determine what to show
        if now_playing.context_uri == item.uri and now_playing.track_name:
            name = now_playing.track_name
            artist = now_playing.track_artist or ''
        elif item.current_track and isinstance(item.current_track, dict):
            name = item.current_track.get('name', item.name) or item.name
            artist = item.current_track.get('artist', item.artist or '') or item.artist or ''
        else:
            name = item.name or 'Unknown'
            artist = item.artist or ''
        
        # Check if text changed
        track_key = (name, artist)
        if track_key != self._last_track_key:
            self._last_track_key = track_key
            
            # Portrait mode: max_width is along Y axis (user's horizontal)
            max_width = SCREEN_HEIGHT - 100
            display_name = name
            
            # First render unrotated to check width
            name_surface = self.font_large.render(display_name, True, COLORS['text_primary'])
            if name_surface.get_width() > max_width:
                while name_surface.get_width() > max_width - 30 and len(display_name) > 3:
                    display_name = display_name[:-1]
                name_surface = self.font_large.render(display_name + '...', True, COLORS['text_primary'])
            
            # Now rotate for portrait display
            name_surface = pygame.transform.rotate(name_surface, -90)
            self._text_cache['name_surface'] = name_surface
            # Position: X=TRACK_INFO_X (user's top), Y centered
            self._text_cache['name_rect'] = name_surface.get_rect(center=(TRACK_INFO_X, CAROUSEL_CENTER_Y))
            
            if artist:
                artist_surface = self._render_text_rotated(artist, self.font_medium, COLORS['text_secondary'])
                self._text_cache['artist_surface'] = artist_surface
                # Artist below title in user's view = lower X value
                self._text_cache['artist_rect'] = artist_surface.get_rect(center=(TRACK_INFO_X - 35, CAROUSEL_CENTER_Y))
            else:
                self._text_cache['artist_surface'] = None
        
        self.screen.blit(self._text_cache['name_surface'], self._text_cache['name_rect'])
        if self._text_cache.get('artist_surface'):
            self.screen.blit(self._text_cache['artist_surface'], self._text_cache['artist_rect'])
    
    def _draw_carousel(self, items: List[CatalogItem], scroll_x: float, 
                       now_playing: NowPlaying, delete_mode_id: Optional[str], loading: bool = False):
        """Draw album cover carousel (portrait mode - covers along Y axis)."""
        # Portrait mode: covers laid out along Y axis (user's horizontal)
        center_y = CAROUSEL_CENTER_Y  # 640
        x = CAROUSEL_X  # Vertical position for covers
        
        max_index = max(0, len(items) - 1)
        scroll_x = max(0, min(scroll_x, max_index))
        
        start_i = max(0, int(scroll_x) - 2)
        end_i = min(len(items), int(scroll_x) + 3)
        
        center_cover_rect = None
        center_item = None
        
        # Draw covers
        for i in range(start_i, end_i):
            item = items[i]
            offset = i - scroll_x
            # Y position based on scroll (along user's horizontal)
            y = center_y + offset * (COVER_SIZE + COVER_SPACING)
            
            is_center = abs(offset) < 0.5
            size = COVER_SIZE if is_center else COVER_SIZE_SMALL
            
            draw_y = int(y - size // 2)
            # X position: center vertically, with smaller covers slightly offset
            draw_x = x + (COVER_SIZE - size) // 2
            
            if draw_y + size < 0 or draw_y > SCREEN_HEIGHT:
                continue
            
            # All items (albums and playlists) use single image field
            # Composites for playlists are pre-rendered and stored as single image
            if is_center:
                cover = self.image_cache.get(item.image, size)
                center_cover_rect = (draw_x, draw_y, size, size)
                center_item = item
            else:
                cover = self.image_cache.get_dimmed(item.image, size)
            
            self.screen.blit(cover, (draw_x, draw_y))
        
        self._mark('carousel_covers')
        
        if center_cover_rect and center_item:
            self._draw_cover_progress(center_cover_rect, center_item, now_playing)
            self._mark('carousel_progress')
            
            # Draw loading spinner if loading
            if loading:
                self._draw_loading_spinner(center_cover_rect)
                self._mark('carousel_spinner')
            
            if center_item.is_temp:
                self._draw_add_button(center_cover_rect)
            elif delete_mode_id == center_item.id:
                self._draw_delete_button(center_cover_rect)
    
    def _draw_cover_progress(self, cover_rect: tuple, item: CatalogItem, now_playing: NowPlaying):
        """Draw progress bar at the edge of the cover (portrait mode - left edge = user's bottom)."""
        cover_x, cover_y, cover_w, cover_h = cover_rect
        
        if now_playing.context_uri != item.uri:
            return
        
        progress = now_playing.progress
        if progress <= 0:
            return
        
        # Portrait mode: progress bar on left edge of cover (user sees as bottom)
        bar_width = PROGRESS_BAR_WIDTH
        fill_height = int(cover_h * min(progress, 1.0))
        
        if fill_height <= 0:
            return
        
        # Cache progress bar mask
        mask_key = f'_progress_mask_{cover_w}'
        if mask_key not in self._progress_cache:
            radius = max(12, cover_w // 25)
            mask = pygame.Surface((cover_w, cover_h), pygame.SRCALPHA)
            pygame.draw.rect(mask, (255, 255, 255, 255), (0, 0, cover_w, cover_h), border_radius=radius)
            self._progress_cache[mask_key] = mask
        
        # Reuse cached progress surface
        surf_key = f'_progress_surf_{cover_w}'
        if surf_key not in self._progress_cache:
            self._progress_cache[surf_key] = pygame.Surface((cover_w, cover_h), pygame.SRCALPHA)
        
        progress_surf = self._progress_cache[surf_key]
        progress_surf.fill((0, 0, 0, 0))
        
        # Progress bar on left edge (user's bottom), growing from top to bottom
        # User sees this as progress from left to right
        pygame.draw.rect(progress_surf, COLORS['accent'],
                        (0, 0, bar_width, fill_height))
        
        progress_surf.blit(self._progress_cache[mask_key], (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        self.screen.blit(progress_surf, (cover_x, cover_y))
    
    def _lighten_color(self, color: tuple, amount: float = 0.3) -> tuple:
        """Make a color lighter by blending with white."""
        r, g, b = color[:3]
        return (
            min(255, int(r + (255 - r) * amount)),
            min(255, int(g + (255 - g) * amount)),
            min(255, int(b + (255 - b) * amount)),
        )
    
    def _draw_controls(self, is_playing: bool, volume_index: int, pressed_button: Optional[str] = None):
        """Draw playback control buttons (portrait mode - buttons along Y axis)."""
        # Portrait mode: buttons laid out along Y axis (user's horizontal)
        x = CONTROLS_X  # Position along physical X (user's vertical = bottom)
        center_y = CAROUSEL_CENTER_Y  # 640
        btn_spacing = BTN_SPACING
        
        # Base colors
        gray_color = COLORS['bg_elevated']
        play_color = COLORS['accent']
        
        # Prev button (left of play button in user's view = lower Y)
        prev_center = (x, center_y - btn_spacing)
        prev_color = self._lighten_color(gray_color) if pressed_button == 'prev' else gray_color
        draw_aa_circle(self.screen, prev_color, prev_center, BTN_SIZE // 2)
        self._draw_icon('prev', prev_center)
        
        # Play/Pause button (center)
        play_center = (x, center_y)
        play_btn_color = self._lighten_color(play_color) if pressed_button == 'play' else play_color
        draw_aa_circle(self.screen, play_btn_color, play_center, PLAY_BTN_SIZE // 2)
        self._draw_icon('pause' if is_playing else 'play', play_center)
        
        # Next button (right of play button in user's view = higher Y)
        next_center = (x, center_y + btn_spacing)
        next_color = self._lighten_color(gray_color) if pressed_button == 'next' else gray_color
        draw_aa_circle(self.screen, next_color, next_center, BTN_SIZE // 2)
        self._draw_icon('next', next_center)
        
        # Volume button (far right in user's view = highest Y)
        right_cover_edge = center_y + (COVER_SIZE + COVER_SPACING) + COVER_SIZE_SMALL // 2
        vol_center = (x, right_cover_edge - BTN_SIZE // 2)
        vol_color = self._lighten_color(gray_color) if pressed_button == 'volume' else gray_color
        draw_aa_circle(self.screen, vol_color, vol_center, BTN_SIZE // 2)
        icon_key = VOLUME_LEVELS[volume_index]['icon']
        self._draw_icon(icon_key, vol_center)
    
    def _draw_icon(self, name: str, center: tuple):
        """Draw an icon centered at position."""
        icon = self.icons.get(name)
        if icon:
            rect = icon.get_rect(center=center)
            self.screen.blit(icon, rect)
    
    def _draw_add_button(self, cover_rect: tuple):
        """Draw + button on cover for temp items (portrait mode)."""
        cover_x, cover_y, cover_w, cover_h = cover_rect
        
        btn_size = 100
        icon_size = 72
        margin = 16
        # Portrait mode: button at bottom-right of cover (user sees as top-right)
        # Physical: low X, high Y
        btn_x = cover_x + margin
        btn_y = cover_y + cover_h - btn_size - margin
        center = (btn_x + btn_size // 2, btn_y + btn_size // 2)
        
        icon = self.icons.get('plus')
        if icon:
            draw_aa_circle(self.screen, (255, 255, 255), center, 28)
            scaled_icon = pygame.transform.smoothscale(icon, (icon_size, icon_size))
            tinted = scaled_icon.copy()
            tinted.fill(COLORS['accent'], special_flags=pygame.BLEND_RGB_MULT)
            icon_rect = tinted.get_rect(center=center)
            self.screen.blit(tinted, icon_rect)
        
        self.add_button_rect = (btn_x, btn_y, btn_size, btn_size)
    
    def _draw_delete_button(self, cover_rect: tuple):
        """Draw - button on cover for delete mode (portrait mode)."""
        cover_x, cover_y, cover_w, cover_h = cover_rect
        
        btn_size = 100
        icon_size = 72
        margin = 16
        # Portrait mode: button at bottom-right of cover (user sees as top-right)
        btn_x = cover_x + margin
        btn_y = cover_y + cover_h - btn_size - margin
        center = (btn_x + btn_size // 2, btn_y + btn_size // 2)
        
        icon = self.icons.get('minus')
        if icon:
            draw_aa_circle(self.screen, (255, 255, 255), center, 28)
            scaled_icon = pygame.transform.smoothscale(icon, (icon_size, icon_size))
            tinted = scaled_icon.copy()
            tinted.fill(COLORS['error'], special_flags=pygame.BLEND_RGB_MULT)
            icon_rect = tinted.get_rect(center=center)
            self.screen.blit(tinted, icon_rect)
        
        self.delete_button_rect = (btn_x, btn_y, btn_size, btn_size)
    
    def _generate_spinner_frames(self, size: int, num_frames: int = 30) -> List[pygame.Surface]:
        """Generate pre-rendered spinner frames for smooth animation with ease-in-out."""
        frames = []
        spinner_size = size * 0.15 * 1.25  # 15% of cover size, 25% larger
        dot_radius = max(2, int(spinner_size * 0.15 * 1.25 * 0.9))  # 25% larger, then 10% smaller
        dot_distance = spinner_size * 0.45 * 1.25  # 25% larger
        
        def ease_in_out(t: float) -> float:
            """Stronger cubic ease-in-out function for more pronounced acceleration/deceleration."""
            if t < 0.5:
                return 4.0 * t * t * t
            else:
                return 1.0 - pow(-2.0 * t + 2.0, 3) / 2.0
        
        for frame_idx in range(num_frames):
            # Create frame surface
            frame = pygame.Surface((size, size), pygame.SRCALPHA)
            center_x = size // 2
            center_y = size // 2
            
            # Split rotation into two 180-degree halves, each with ease-in-out
            half_frames = num_frames / 2.0
            if frame_idx < half_frames:
                # First half: 0 to 180 degrees with ease-in-out
                t = frame_idx / half_frames if half_frames > 0 else 0.0
                eased_t = ease_in_out(t)
                rotation_rad = math.radians(eased_t * 180)
            else:
                # Second half: 180 to 360 degrees with ease-in-out
                t = (frame_idx - half_frames) / half_frames if half_frames > 0 else 0.0
                eased_t = ease_in_out(t)
                rotation_rad = math.radians(180 + eased_t * 180)
            
            # Draw 4 solid white dots
            for i in range(4):
                corner_angle = rotation_rad + (i * math.pi / 2)
                dot_x = int(center_x + math.cos(corner_angle) * dot_distance)
                dot_y = int(center_y + math.sin(corner_angle) * dot_distance)
                pygame.draw.circle(frame, (255, 255, 255), (dot_x, dot_y), dot_radius)
            
            frames.append(frame.convert_alpha())
        
        return frames
    
    def _get_spinner_overlay(self, size: int) -> pygame.Surface:
        """Get or create cached dimming overlay for spinner."""
        if size not in self._spinner_overlay_cache:
            overlay = pygame.Surface((size, size), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 115))  # 45% dark overlay
            self._spinner_overlay_cache[size] = overlay.convert_alpha()
        return self._spinner_overlay_cache[size]
    
    def _draw_loading_spinner(self, cover_rect: tuple):
        """Draw loading spinner overlay on cover art using pre-rendered frames."""
        cover_x, cover_y, cover_w, cover_h = cover_rect
        size = max(cover_w, cover_h)
        
        # Get or create cached overlay
        overlay = self._get_spinner_overlay(size)
        self.screen.blit(overlay, (cover_x, cover_y))
        
        # Get or create cached spinner frames
        if size not in self._spinner_cache:
            self._spinner_cache[size] = self._generate_spinner_frames(size, num_frames=30)
        
        frames = self._spinner_cache[size]
        
        # Simple frame counter - increments every draw call
        # At 30 FPS, this gives 1 rotation per second
        # At 60 FPS, 2 rotations per second (still smooth)
        frame = frames[self._spinner_frame_idx % len(frames)]
        self._spinner_frame_idx += 1
        
        # Blit the pre-rendered frame
        self.screen.blit(frame, (cover_x, cover_y))


"""
Renderer - All drawing/rendering logic for the Berry UI.
"""
import logging
import time
import math
from typing import Optional, List, Dict, Tuple

import pygame
import pygame.gfxdraw

from .helpers import draw_aa_circle
from .image_cache import ImageCache
from .context import RenderContext
from ..models import CatalogItem, MenuState, NowPlaying
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
        self._last_toast: Optional[str] = None
        
        # Button hit rectangles (updated during draw)
        self.add_button_rect: Optional[Tuple[int, int, int, int]] = None
        self.delete_button_rect: Optional[Tuple[int, int, int, int]] = None
        
        # Menu button rects (updated when menu is drawn)
        self.menu_button_rects: Dict[str, pygame.Rect] = {}
    
    def invalidate(self):
        """Force a full redraw on next frame."""
        self._needs_full_redraw = True
    
    @staticmethod
    def _get_track_key(item: Optional[CatalogItem], now_playing: NowPlaying) -> Optional[Tuple[str, str]]:
        """Return (name, artist) tuple for the track to display, or None."""
        if not item:
            return None
        if now_playing.context_uri == item.uri and now_playing.track_name:
            return (now_playing.track_name, now_playing.track_artist or '')
        if item.current_track and isinstance(item.current_track, dict):
            name = item.current_track.get('name', item.name) or item.name
            artist = item.current_track.get('artist', item.artist or '') or item.artist or ''
            return (name, artist)
        return (item.name or 'Unknown', item.artist or '')
    
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
        
        # Menu overlay — draw full scene then overlay on top
        if ctx.menu_state != MenuState.CLOSED:
            self.add_button_rect = None
            self.delete_button_rect = None
            self._draw_menu_frame(ctx)
            return None
        
        # Clear button hit rects
        self.add_button_rect = None
        self.delete_button_rect = None
        
        current_item = ctx.items[ctx.selected_index] if ctx.selected_index < len(ctx.items) else None
        current_track_key = self._get_track_key(current_item, ctx.now_playing)
        
        # Check if we need a full redraw
        state_changed = (
            self._last_playing_state != ctx.now_playing.playing or
            self._last_selected_index != ctx.selected_index or
            self._last_track_key is None or
            self._last_track_key != current_track_key
        )
        
        # Toast changes need redraw too
        toast_changed = self._last_toast != ctx.toast_message
        
        if state_changed:
            self._needs_full_redraw = True
            self._last_playing_state = ctx.now_playing.playing
            self._last_selected_index = ctx.selected_index
        
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
            self._draw_track_info(current_item, ctx.now_playing)
            self._draw_controls(ctx.is_playing, ctx.volume_index, ctx.pressed_button)
            
            if self._static_layer is None:
                self._static_layer = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
            self._static_layer.blit(self.screen, (0, 0))
            
            self._draw_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id, ctx.is_loading)
            if ctx.toast_message:
                self._draw_toast(ctx.toast_message)
            self._last_toast = ctx.toast_message
            
            self._needs_full_redraw = False
            return None
        
        elif is_animating or toast_changed:
            # Partial update - only carousel area
            self.screen.blit(self._static_layer, 
                           self._carousel_rect.topleft, 
                           self._carousel_rect)
            self._draw_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id, ctx.is_loading)
            if ctx.toast_message:
                self._draw_toast(ctx.toast_message)
            self._last_toast = ctx.toast_message
            return [self._carousel_rect]
        
        else:
            if ctx.now_playing.playing or ctx.is_loading:
                self.screen.blit(self._static_layer,
                               self._carousel_rect.topleft,
                               self._carousel_rect)
                self._draw_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id, ctx.is_loading)
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
    
    def _draw_empty_state(self):
        """Draw idle screen when catalog is empty (portrait mode)."""
        center_x = SCREEN_WIDTH // 2
        center_y = SCREEN_HEIGHT // 2
        
        line1 = self._render_text_rotated('Speel naar Berry via Spotify', self.font_medium, COLORS['text_secondary'])
        line1_rect = line1.get_rect(center=(center_x + 20, center_y))
        self.screen.blit(line1, line1_rect)
        
        line2 = self._render_text_rotated('Tik + om op te slaan', self.font_medium, COLORS['text_secondary'])
        line2_rect = line2.get_rect(center=(center_x - 20, center_y))
        self.screen.blit(line2, line2_rect)
    
    def _draw_track_info(self, item: Optional[CatalogItem], now_playing: NowPlaying):
        """Draw track name and artist (portrait mode - at user's top)."""
        if not item:
            return
        
        track_key = self._get_track_key(item, now_playing)
        if not track_key:
            return
        name, artist = track_key
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
        
        if center_cover_rect and center_item:
            self._draw_cover_progress(center_cover_rect, center_item, now_playing)
            
            if loading:
                self._draw_loading_spinner(center_cover_rect)
            
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
        x = CONTROLS_X
        center_y = CAROUSEL_CENTER_Y
        btn_spacing = BTN_SPACING
        
        gray_color = COLORS['bg_elevated']
        play_color = COLORS['accent']
        
        # Prev button
        prev_center = (x, center_y - btn_spacing)
        prev_color = self._lighten_color(gray_color) if pressed_button == 'prev' else gray_color
        draw_aa_circle(self.screen, prev_color, prev_center, BTN_SIZE // 2)
        self._draw_icon('prev', prev_center)
        
        # Play/Pause button
        play_center = (x, center_y)
        play_btn_color = self._lighten_color(play_color) if pressed_button == 'play' else play_color
        draw_aa_circle(self.screen, play_btn_color, play_center, PLAY_BTN_SIZE // 2)
        self._draw_icon('pause' if is_playing else 'play', play_center)
        
        # Next button
        next_center = (x, center_y + btn_spacing)
        next_color = self._lighten_color(gray_color) if pressed_button == 'next' else gray_color
        draw_aa_circle(self.screen, next_color, next_center, BTN_SIZE // 2)
        self._draw_icon('next', next_center)
        
        # Volume button
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
    
    def _draw_overlay_button(self, cover_rect: tuple, icon_name: str, tint: tuple) -> tuple:
        """Draw a tinted icon button on the cover. Returns (x, y, w, h) hit rect."""
        cover_x, cover_y, cover_w, cover_h = cover_rect
        btn_size, icon_size, margin = 100, 72, 16
        circle_radius = int(icon_size * (42 / 56) / 2)
        btn_x = cover_x + margin
        btn_y = cover_y + cover_h - btn_size - margin
        center = (btn_x + btn_size // 2, btn_y + btn_size // 2)
        
        draw_aa_circle(self.screen, (255, 255, 255), center, circle_radius)
        
        icon = self.icons.get(icon_name)
        if icon:
            scaled = pygame.transform.smoothscale(icon, (icon_size, icon_size))
            tinted = scaled.copy()
            tinted.fill(tint, special_flags=pygame.BLEND_RGB_MULT)
            self.screen.blit(tinted, tinted.get_rect(center=center))
        
        return (btn_x, btn_y, btn_size, btn_size)
    
    def _draw_add_button(self, cover_rect: tuple):
        """Draw + button on cover for temp items."""
        self.add_button_rect = self._draw_overlay_button(cover_rect, 'plus', COLORS['accent'])
    
    def _draw_delete_button(self, cover_rect: tuple):
        """Draw - button on cover for delete mode."""
        self.delete_button_rect = self._draw_overlay_button(cover_rect, 'minus', COLORS['error'])
    
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
    
    def _draw_toast(self, message: str):
        """Draw a toast pill with rotated text, centered on the carousel area."""
        text_surface = self.font_medium.render(message, True, COLORS['text_primary'])
        text_w, text_h = text_surface.get_size()
        
        # Pill dimensions (padding around text, then rotated)
        pad_x, pad_y = 24, 14
        pill_w = text_w + pad_x * 2
        pill_h = text_h + pad_y * 2
        
        pill = pygame.Surface((pill_w, pill_h), pygame.SRCALPHA)
        pygame.draw.rect(pill, (30, 30, 30, 220), (0, 0, pill_w, pill_h), border_radius=pill_h // 2)
        pill.blit(text_surface, (pad_x, pad_y))
        
        # Rotate for portrait display
        rotated = pygame.transform.rotate(pill, -90)
        
        # Center on carousel area
        rect = rotated.get_rect(center=(CAROUSEL_X + COVER_SIZE // 2, CAROUSEL_CENTER_Y))
        self.screen.blit(rotated, rect)
    
    # ============================================
    # SETUP MENU
    # ============================================

    # Shared layout constants for all menu screens.
    # Physical portrait 720x1280; user holds left-side up.
    # High physical X = user's top. Buttons stack downward (decreasing X).
    _MENU_BTN_H = 80
    _MENU_BTN_GAP = 10
    _MENU_BTN_W = 400
    _MENU_BTN_Y = 440      # physical Y start (centered on 640)
    _MENU_TOP_X = 565       # first button X
    _MENU_TITLE_X = 670     # title X
    
    def _draw_menu_frame(self, ctx: 'RenderContext'):
        """Draw fully black background then the active menu screen."""
        self.screen.fill((0, 0, 0))
        
        if ctx.menu_state == MenuState.WIFI_LIST:
            buttons = []
            for i, ssid in enumerate(ctx.menu_known_networks[:5]):
                is_current = ssid == ctx.menu_current_network
                color = COLORS['accent'] if is_current else COLORS['bg_elevated']
                display = ssid if len(ssid) <= 20 else ssid[:18] + '..'
                buttons.append((f'reconnect_{i}', display, color))
            buttons.append(None)
            buttons.append(('new_network', '+ Nieuw netwerk', COLORS['bg_elevated']))
            self._draw_menu_screen('WiFi', buttons)
        
        elif ctx.menu_state == MenuState.WIFI_AP:
            self._draw_menu_screen('WiFi', [], close_label='Terug',
                                   lines=['1. Verbind je telefoon met',
                                          '    WiFi netwerk "Berry-Setup"',
                                          '2. Kies je WiFi netwerk',
                                          '3. Voer het wachtwoord in'])
        
        else:
            buttons = [
                ('wifi', 'WiFi', COLORS['accent']),
                ('library', 'Wis bibliotheek', COLORS['accent']),
                None,
                ('auto_pause', f'Auto-pauze: {ctx.auto_pause_minutes} min', COLORS['bg_elevated']),
                ('progress_expiry', f'Onthouden: {ctx.progress_expiry_hours} uur', COLORS['bg_elevated']),
            ]
            self._draw_menu_screen('Instellingen', buttons)
        
        self._needs_full_redraw = True
    
    def _draw_menu_screen(self, title: str, buttons: list,
                          close_label: str = 'Sluiten', lines: Optional[List[str]] = None):
        """Draw a menu screen with consistent layout.
        
        buttons: list of (id, label, color) tuples. None entries add extra spacing.
        lines: optional text lines shown below title (for instruction screens).
        """
        self.menu_button_rects = {}
        H, GAP, W, Y = self._MENU_BTN_H, self._MENU_BTN_GAP, self._MENU_BTN_W, self._MENU_BTN_Y
        
        title_surf = self._render_text_rotated(title, self.font_large, COLORS['text_primary'])
        self.screen.blit(title_surf, title_surf.get_rect(center=(self._MENU_TITLE_X, CAROUSEL_CENTER_Y)))
        
        if lines:
            line_x = self._MENU_TOP_X
            for line in lines:
                surf = self._render_text_rotated(line, self.font_medium, COLORS['text_secondary'])
                self.screen.blit(surf, surf.get_rect(center=(line_x, CAROUSEL_CENTER_Y)))
                line_x -= 35
        
        x = self._MENU_TOP_X
        for entry in buttons:
            if entry is None:
                x -= GAP
                continue
            btn_id, label, color = entry
            btn = pygame.Rect(x, Y, H, W)
            self._draw_menu_button(btn, label, color)
            self.menu_button_rects[btn_id] = btn
            x -= H + GAP
        
        x -= GAP
        close_btn = pygame.Rect(max(20, x), Y, H, W)
        self._draw_menu_button(close_btn, close_label, COLORS['bg_elevated'])
        self.menu_button_rects['close'] = close_btn
    
    def _draw_menu_button(self, rect: pygame.Rect, label: str, bg_color: tuple,
                          text_color: Optional[tuple] = None):
        """Draw a rounded rectangle button with rotated label."""
        text_color = text_color or COLORS['text_primary']
        pygame.draw.rect(self.screen, bg_color, rect, border_radius=18)
        text_surf = self._render_text_rotated(label, self.font_medium, text_color)
        self.screen.blit(text_surf, text_surf.get_rect(center=rect.center))


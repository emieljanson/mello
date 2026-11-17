"""
Renderer - decides what is on Mello's screen and where.

It does not draw: it describes each screen as boxes, pictures and labels (a
Scene) plus where the album covers are (carousel_frame), and Slint draws that
(ui/slint_display.py, ui/mello.slint). The same rectangles tell the app what
a touch hit, so a button and its touch area are always the same place.

The panel is portrait 720x1280 and mounted sideways: the child's "up" is the
panel's right edge (high x), the child's "left to right" is the panel's y.
"""
import logging
from typing import NamedTuple, Optional, List, Dict, Tuple

from .bitmaps import Bitmaps, text_size, FONT_LARGE, FONT_MEDIUM, FONT_SMALL, QR_SIZE
from .context import RenderContext
from .geometry import Rect
from ..models import CatalogItem, MenuState, NowPlaying
from ..config import (
    SCREEN_WIDTH, SCREEN_HEIGHT, COLORS,
    COVER_SIZE, COVER_SIZE_SMALL, COVER_SPACING,
    TRACK_INFO_X, CAROUSEL_X, CONTROLS_X, CAROUSEL_CENTER_Y,
    BTN_SIZE, PLAY_BTN_SIZE, BTN_SPACING, PROGRESS_BAR_WIDTH,
    DEFAULT_VOLUME_LEVELS,
)

# Headphone button Y position — symmetric to volume button on the opposite side.
# Volume: center_y + (COVER_SIZE + COVER_SPACING) + COVER_SIZE_SMALL//2 - BTN_SIZE//2 ≈ 1173
# Headphone: center_y - (COVER_SIZE + COVER_SPACING) - COVER_SIZE_SMALL//2 + BTN_SIZE//2 ≈ 107
_HEADPHONE_BTN_Y = CAROUSEL_CENTER_Y - (COVER_SIZE + COVER_SPACING) - COVER_SIZE_SMALL // 2 + BTN_SIZE // 2
_VOLUME_BTN_Y = CAROUSEL_CENTER_Y + (COVER_SIZE + COVER_SPACING) + COVER_SIZE_SMALL // 2 - BTN_SIZE // 2

# The + / - button on the focused cover (cover coordinates)
OVERLAY_BTN_SIZE, OVERLAY_ICON_SIZE, OVERLAY_MARGIN, OVERLAY_TOUCH_PADDING = 100, 72, 16, 60

logger = logging.getLogger(__name__)


class Box(NamedTuple):
    x: int
    y: int
    w: int
    h: int
    color: tuple
    radius: float = 0


class Picture(NamedTuple):
    key: tuple          # see Bitmaps
    x: int
    y: int
    w: int
    h: int


class Label(NamedTuple):
    text: str
    x: float            # centre
    y: float
    size: int
    color: tuple


class Stroke(NamedTuple):
    points: tuple       # ((x, y), ...)
    color: tuple
    width: int


class Scene:
    """Everything on screen except the album covers."""

    def __init__(self, background: str = 'black'):
        self.background = background        # 'gradient' or 'black'
        self.clip_left: Optional[int] = None  # menu lists are cut off left of this x
        self.header_fade = False
        self.boxes: List[Box] = []
        self.strokes: List[Stroke] = []
        self.pictures: List[Picture] = []
        self.labels: List[Label] = []
        # Above the header fade (a menu's title and close/back button)
        self.top_boxes: List[Box] = []
        self.top_pictures: List[Picture] = []
        self.top_labels: List[Label] = []

    def _parts(self):
        return (self.background, self.clip_left, self.header_fade, self.boxes, self.strokes,
                self.pictures, self.labels, self.top_boxes, self.top_pictures, self.top_labels)

    def __eq__(self, other) -> bool:
        return isinstance(other, Scene) and self._parts() == other._parts()


def lighten(color: tuple, amount: float = 0.3) -> tuple:
    """A colour blended towards white (a pressed button)."""
    r, g, b = color[:3]
    return (
        min(255, int(r + (255 - r) * amount)),
        min(255, int(g + (255 - g) * amount)),
        min(255, int(b + (255 - b) * amount)),
    )


def circle(center: tuple, radius: int, color: tuple) -> Box:
    """A filled circle covering the pixels center-radius .. center+radius."""
    cx, cy = int(center[0]), int(center[1])
    return Box(cx - radius, cy - radius, 2 * radius + 1, 2 * radius + 1, color, radius + 0.5)


def overlay_button_rect(cover_x: int, cover_y: int) -> tuple:
    """Touch area of the + / - button on a focused cover at (cover_x, cover_y)."""
    btn_x = cover_x + OVERLAY_MARGIN
    btn_y = cover_y + COVER_SIZE - OVERLAY_BTN_SIZE - OVERLAY_MARGIN
    size = OVERLAY_BTN_SIZE + OVERLAY_TOUCH_PADDING * 2
    return (btn_x - OVERLAY_TOUCH_PADDING, btn_y - OVERLAY_TOUCH_PADDING, size, size)


class Renderer:
    """Turns the app's state (a RenderContext) into what Slint shows."""

    def __init__(self, bitmaps: Bitmaps):
        self.bitmaps = bitmaps

        self.scene: Scene = Scene()
        self.carousel_frame: Optional[dict] = None   # covers, when the main screen is up
        self._main_key: Optional[tuple] = None
        self._title_cache: Dict[tuple, str] = {}

        # Button hit rectangles (updated during draw)
        self.add_button_rect: Optional[Tuple[int, int, int, int]] = None
        self.delete_button_rect: Optional[Tuple[int, int, int, int]] = None
        self.settings_button_rect: Optional[Tuple[int, int, int, int]] = None

        # Menu button rects (updated when menu is drawn)
        self.menu_button_rects: Dict[str, Rect] = {}
        self.menu_content_overflow: int = 0

    def invalidate(self):
        """Something changed that the next frame must show."""
        self._main_key = None

    @staticmethod
    def _get_track_key(item: Optional[CatalogItem], now_playing: NowPlaying,
                       is_loading: bool, pending_focus_uri: Optional[str],
                       requested_focus_uri: Optional[str], play_in_progress: bool) -> Optional[Tuple[str, str]]:
        """Return (name, artist) tuple for the title area, or None."""
        if not item:
            return None
        # Keep title tied to the focused context, and preserve it while paused
        # so resume shows the same track user paused on.
        if ((now_playing.playing or now_playing.paused) and
                now_playing.context_uri == item.uri and
                now_playing.track_name):
            return (now_playing.track_name, now_playing.track_artist or '')
        # Fallback: show last saved track metadata for this context while
        # live now_playing has not caught up yet.
        current_track = item.current_track if isinstance(item.current_track, dict) else None
        if current_track and current_track.get('name'):
            return (current_track.get('name'), current_track.get('artist') or '')
        return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _label(text: str, center: tuple, size: int, color: tuple) -> Label:
        return Label(text, center[0], center[1], size, color)

    @staticmethod
    def _label_user(text: str, left: int, top: int, size: int, color: tuple) -> Tuple[Label, Rect]:
        """A label placed by its top-left corner as the user sees it, and its rect."""
        w, h = text_size(text, size)
        rect = Rect(SCREEN_WIDTH - top - h, left, h, w)
        return Label(text, rect.x + h / 2, rect.y + w / 2, size, color), rect

    def _icon(self, scene: Scene, name: str, center: tuple, top: bool = False):
        key = ('icon', name)
        w, h = self.bitmaps.size(key)
        picture = Picture(key, int(center[0]) - w // 2, int(center[1]) - h // 2, w, h)
        (scene.top_pictures if top else scene.pictures).append(picture)

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    def draw(self, ctx: RenderContext):
        """Work out this frame's scene and covers."""
        self.add_button_rect = None
        self.delete_button_rect = None
        self.settings_button_rect = None
        self.carousel_frame = None

        if ctx.is_sleeping:
            self._main_key = None
            self.scene = Scene('black')
            return

        if ctx.menu_state != MenuState.CLOSED:
            self._main_key = None
            self.scene = self._menu_scene(ctx)
            return

        if not ctx.items or ctx.spotify_needs_pairing:
            # Empty state (also while Mello still has to be chosen in Spotify once)
            self._main_key = None
            scene = Scene('gradient')
            if ctx.home_connect_spotify and not ctx.items:
                # A new Mello asks to connect Spotify right here.
                self._home_connect_spotify(scene, ctx)
            else:
                self._empty_state(scene, ctx)
            self.scene = scene
            return

        current_item = ctx.items[ctx.selected_index] if ctx.selected_index < len(ctx.items) else None
        track_key = self._get_track_key(
            current_item, ctx.now_playing, ctx.is_loading,
            ctx.pending_focus_uri, ctx.requested_focus_uri, ctx.play_in_progress,
        )
        main_key = ('main', track_key, ctx.is_playing, ctx.volume_index, ctx.pressed_button,
                    ctx.bt_connected, ctx.bt_audio_active)
        if main_key != self._main_key:
            # Title and buttons only change on a press or a new track: the
            # same Scene object is kept otherwise, so Slint has nothing to do.
            self._main_key = main_key
            scene = Scene('gradient')
            self._track_info(scene, track_key)
            self._controls(scene, ctx)
            self.scene = scene

        if ctx.dragging:
            effective_scroll = ctx.selected_index - ctx.drag_offset / (COVER_SIZE + COVER_SPACING)
        else:
            effective_scroll = ctx.scroll_x
        self._describe_carousel(ctx.items, effective_scroll, ctx.now_playing, ctx.delete_mode_id,
                                ctx.is_loading, ctx.toast_message)

    # ------------------------------------------------------------------
    # Main screen
    # ------------------------------------------------------------------

    def _track_info(self, scene: Scene, track_key: Optional[Tuple[str, str]]):
        """Track name and artist at the user's top."""
        if not track_key:
            return
        name, artist = track_key
        scene.labels.append(self._label(self._fit_title(name), (TRACK_INFO_X, CAROUSEL_CENTER_Y),
                                        FONT_LARGE, COLORS['text_primary']))
        if artist:
            # Artist below title in user's view = lower X value
            scene.labels.append(self._label(artist, (TRACK_INFO_X - 35, CAROUSEL_CENTER_Y),
                                            FONT_MEDIUM, COLORS['text_secondary']))

    def _fit_title(self, name: str) -> str:
        """The title, shortened with '...' when it is wider than the screen."""
        cached = self._title_cache.get(name)
        if cached is not None:
            return cached
        max_width = SCREEN_HEIGHT - 100
        display = name
        if text_size(name, FONT_LARGE)[0] > max_width:
            while len(display) > 3:
                if text_size(display + '...', FONT_LARGE)[0] <= max_width - 30:
                    break
                display = display[:-1]
            display += '...'
        if len(self._title_cache) > 50:
            self._title_cache.clear()
        self._title_cache[name] = display
        return display

    def _controls(self, scene: Scene, ctx: RenderContext):
        """Playback buttons, stacked along the panel's y at x=CONTROLS_X."""
        x = CONTROLS_X
        center_y = CAROUSEL_CENTER_Y
        gray = COLORS['bg_elevated']
        pressed = ctx.pressed_button

        def button(name: str, center: tuple, radius: int, color: tuple, icon: str):
            scene.boxes.append(circle(center, radius, lighten(color) if pressed == name else color))
            self._icon(scene, icon, center)

        # Headphone button — only when BT is connected, opposite corner from volume
        if ctx.bt_connected:
            button('headphone', (x, _HEADPHONE_BTN_Y), BTN_SIZE // 2,
                   COLORS['accent'] if ctx.bt_audio_active else gray, 'headphone')
        button('prev', (x, center_y - BTN_SPACING), BTN_SIZE // 2, gray, 'prev')
        button('play', (x, center_y), PLAY_BTN_SIZE // 2, COLORS['accent'],
               'pause' if ctx.is_playing else 'play')
        button('next', (x, center_y + BTN_SPACING), BTN_SIZE // 2, gray, 'next')
        button('volume', (x, _VOLUME_BTN_Y), BTN_SIZE // 2, gray,
               DEFAULT_VOLUME_LEVELS[ctx.volume_index]['icon'])

    def _describe_carousel(self, items: List[CatalogItem], scroll_x: float, now_playing: NowPlaying,
                           delete_mode_id: Optional[str], loading: bool, toast: Optional[str]):
        """Where each cover is, for Slint to draw."""
        max_index = max(0, len(items) - 1)
        scroll_x = max(0, min(scroll_x, max_index))
        start_i = max(0, int(scroll_x) - 2)
        end_i = min(len(items), int(scroll_x) + 3)
        center_i = next((i for i in range(start_i, end_i) if abs(i - scroll_x) < 0.5), None)

        fill = 0
        button = None
        if center_i is not None:
            item = items[center_i]
            draw_y = int(CAROUSEL_CENTER_Y + (center_i - scroll_x) * (COVER_SIZE + COVER_SPACING)
                         - COVER_SIZE // 2)
            if not (draw_y + COVER_SIZE < 0 or draw_y > SCREEN_HEIGHT):
                progress = self._cover_progress(item, now_playing)
                fill = int(COVER_SIZE * min(progress, 1.0)) if progress > 0 else 0
                button = 'add' if item.is_temp else ('delete' if delete_mode_id == item.id else None)
                if button == 'add':
                    self.add_button_rect = overlay_button_rect(CAROUSEL_X, draw_y)
                elif button == 'delete':
                    self.delete_button_rect = overlay_button_rect(CAROUSEL_X, draw_y)
            else:
                center_i = None

        tops = {}
        for i in range(start_i, end_i):
            size = COVER_SIZE if i == center_i else COVER_SIZE_SMALL
            tops[i] = int(CAROUSEL_CENTER_Y + (i - scroll_x) * (COVER_SIZE + COVER_SPACING) - size // 2)

        self.carousel_frame = {
            'items': items,
            'first': start_i,
            'last': end_i,
            'center': center_i if center_i is not None else -1,
            'tops': tops,
            'loading': bool(loading and center_i is not None),
            'progress_fill': fill,
            'button': button,
            'toast': toast or '',
        }

    @staticmethod
    def _cover_progress(item: CatalogItem, now_playing: NowPlaying) -> float:
        """How far along the focused album's track is, 0.0-1.0.

        Live when Spotify is on this album and knows the position. Otherwise
        the place saved for it: while an album is still loading (which takes
        seconds deep into an audiobook) the bar already shows where the child
        was, next to the saved track title.
        """
        if now_playing.context_uri == item.uri and now_playing.progress > 0:
            return now_playing.progress
        saved = item.current_track if isinstance(item.current_track, dict) else None
        if saved:
            duration = saved.get('duration') or 0
            if duration > 0:
                return max(0.0, min(1.0, (saved.get('position') or 0) / duration))
        return 0.0

    # ------------------------------------------------------------------
    # Home screens (nothing saved yet)
    # ------------------------------------------------------------------

    def _logo(self, scene: Scene, width: int, center: tuple):
        key = ('logo', width)
        w, h = self.bitmaps.size(key)
        scene.pictures.append(Picture(key, center[0] - w // 2, center[1] - h // 2, w, h))

    def _empty_state(self, scene: Scene, ctx: RenderContext):
        """Idle screen when catalog is empty."""
        center_x = SCREEN_WIDTH // 2
        center_y = SCREEN_HEIGHT // 2
        self._logo(scene, 320, (center_x + 80, center_y))

        if not ctx.has_network:
            # No internet: show message + tappable Settings button
            scene.labels.append(self._label('No internet connection', (center_x - 30, center_y),
                                            FONT_MEDIUM, COLORS['text_secondary']))
            w, h = text_size('Settings', FONT_MEDIUM)
            # The turned text is h wide and w high on the panel
            text_x, text_y = center_x - 90 - h // 2, center_y - w // 2
            pad_x, pad_y = 14, 10
            bg = (text_x - pad_x, text_y - pad_y, h + pad_x * 2, w + pad_y * 2)
            scene.boxes.append(Box(*bg, COLORS['accent'], 12))
            scene.labels.append(self._label('Settings', (center_x - 90, center_y),
                                            FONT_MEDIUM, COLORS['text_primary']))
            self.settings_button_rect = bg
            return

        if ctx.spotify_needs_pairing and ctx.items:
            # Just switched to the new player: it logs in the first time
            # someone plays to it from the Spotify app.
            first, second = 'Play to Mello via Spotify', 'to finish setting up'
        else:
            first, second = 'Play to Mello via Spotify', 'Tap + to save'
        scene.labels.append(self._label(first, (center_x - 30, center_y), FONT_MEDIUM, COLORS['text_secondary']))
        scene.labels.append(self._label(second, (center_x - 60, center_y), FONT_MEDIUM, COLORS['text_secondary']))

    def _switching(self, scene: Scene, ctx: RenderContext):
        """Two centred lines while the speaker restarts on the other player."""
        center_x = SCREEN_WIDTH // 2
        center_y = SCREEN_HEIGHT // 2
        lines = [item[1] for item in ctx.menu_spotify_items if item[0] == 'text']
        for index, text in enumerate(lines):
            color = COLORS['text_primary'] if index == 0 else COLORS['text_secondary']
            scene.labels.append(self._label(text, (center_x + 15 - index * 34, center_y), FONT_MEDIUM, color))

    def _home_connect_spotify(self, scene: Scene, ctx: RenderContext):
        """A new Mello's home screen while Spotify still has to be connected.

        Looks like the other home messages (logo, two grey lines), with the
        code to scan underneath. Nothing to tap: it changes by itself once a
        code was entered on the phone.
        """
        if ctx.menu_spotify_switching:
            self._switching(scene, ctx)
            return
        center_x = SCREEN_WIDTH // 2
        center_y = SCREEN_HEIGHT // 2
        matrix = next((item[1] for item in ctx.menu_spotify_items if item[0] == 'qr'), None)
        lines = ctx.home_lines if matrix else tuple(
            item[1] for item in ctx.menu_spotify_items if item[0] == 'text')[-2:]

        # The panel is sideways: "down" for the user is towards smaller x.
        x = center_x + 162
        self._logo(scene, 240, (x, center_y))
        x -= 80
        for text in lines:
            scene.labels.append(self._label(text, (x, center_y), FONT_MEDIUM, COLORS['text_secondary']))
            x -= 30
        if matrix:
            self._qr(scene, matrix, x - 22)

    def _qr(self, scene: Scene, matrix: list, x: int, center_y: int = CAROUSEL_CENTER_Y):
        """A QR code whose top edge (as the user sees it) is at `x`."""
        key = ('qr', tuple(tuple(bool(v) for v in row) for row in matrix))
        left = x + 16 - QR_SIZE                      # top edge lines up with a text row
        top = center_y - QR_SIZE // 2
        scene.pictures.append(Picture(key, left, top, QR_SIZE, QR_SIZE))

    # ------------------------------------------------------------------
    # Setup menu
    # ------------------------------------------------------------------

    # Shared layout constants for all menu screens.
    # Physical portrait 720x1280; user holds left-side up.
    # High physical X = user's top. Buttons stack downward (decreasing X).
    _MENU_BTN_H = 80
    _MENU_BTN_GAP = 10
    _MENU_BTN_W = 400
    _MENU_BTN_Y = 440           # physical Y start (centered on 640)
    _MENU_TITLE_X = 670         # title (fixed top)
    _MENU_CONTENT_TOP = 530     # first button x (below gradient fade)
    _MENU_CONTENT_BOT = 20      # content extends to near screen edge
    _MENU_NAV_SIZE = 60          # close/back icon button diameter
    _MENU_NAV_CENTER = (670, 50)   # close/back icon button center (user's top-left)

    _VOL_LABELS = [
        ('speaker', ['Speaker low', 'Speaker mid', 'Speaker high']),
        ('bt', ['BT low', 'BT mid', 'BT high']),
    ]

    def _menu_scene(self, ctx: RenderContext) -> Scene:
        """The active settings screen on black."""
        self.menu_button_rects = {}

        if ctx.menu_state == MenuState.MAIN:
            title, nav_icon, items = 'Settings', 'close', self._build_main_content(ctx)
        elif ctx.menu_state == MenuState.WIFI_LIST:
            title, nav_icon, items = 'WiFi', 'back', self._build_wifi_content(ctx)
        elif ctx.menu_state == MenuState.WIFI_AP:
            title, nav_icon = 'WiFi', 'back'
            items = [
                ('text', '1. Connect to WiFi'),
                ('text', '    network "Mello-Setup"'),
                ('spacer',),
                ('text', '2. Choose your WiFi network'),
                ('spacer',),
                ('text', '3. Enter the password'),
            ]
            if ctx.menu_wifi_joining:
                items = [
                    ('text', f'Connecting to {ctx.menu_wifi_joining}...'),
                    ('text', 'This can take a minute'),
                ]
            elif ctx.menu_wifi_join_failed:
                # The phone just shows the password page again; say why.
                items = [
                    ('text', f'Could not join {ctx.menu_wifi_join_failed}'),
                    ('text', 'Check the password and try again'),
                    ('spacer',),
                ] + items
        elif ctx.menu_state == MenuState.SPOTIFY:
            if ctx.menu_spotify_prompt:
                # Opened by itself: not a settings page but an offer.
                return self._soloist_prompt(ctx)
            title, nav_icon, items = 'Spotify', 'back', list(ctx.menu_spotify_menu_items)
        elif ctx.menu_state == MenuState.BT_LIST:
            title, nav_icon, items = 'Bluetooth', 'back', self._build_bt_content(ctx)
        elif ctx.menu_state == MenuState.VOLUME_LEVELS:
            title, nav_icon, items = 'Volume', 'back', self._build_volume_content(ctx)
        else:
            return Scene('black')

        scene = Scene('black')
        # 1. Scrollable content (cut off near the screen edge)
        self._menu_content(scene, items, ctx.menu_scroll_offset)
        # 2. Header fade: content scrolls visibly under the title, fading out — iOS-style.
        scene.header_fade = True
        # 3. Title + nav button on top
        scene.top_labels.append(self._label(title, (self._MENU_TITLE_X, CAROUSEL_CENTER_Y),
                                            FONT_LARGE, COLORS['text_primary']))
        self._menu_nav(scene, ctx, nav_icon)
        return scene

    def _menu_nav(self, scene: Scene, ctx: RenderContext, nav_icon: str):
        """The round close/back button in the corner of a menu screen."""
        center = self._MENU_NAV_CENTER
        radius = self._MENU_NAV_SIZE // 2
        color = COLORS['bg_elevated']
        if ctx.pressed_button == 'menu_close':
            color = lighten(color)
        scene.top_boxes.append(circle(center, radius, color))
        key = ('icon', nav_icon, 32)
        scene.top_pictures.append(Picture(key, center[0] - 16, center[1] - 16, 32, 32))
        self.menu_button_rects['close'] = Rect(
            center[0] - radius, center[1] - radius, self._MENU_NAV_SIZE, self._MENU_NAV_SIZE)

    def _soloist_prompt(self, ctx: RenderContext) -> Scene:
        """Full-screen "a faster Mello" offer (see SetupMenu.show_spotify_prompt).

        There is no close button; its button has to be held, and fills up
        while it is.
        """
        scene = Scene('gradient')
        self.menu_button_rects = {}
        if ctx.menu_spotify_switching:
            self._switching(scene, ctx)
            return scene
        self._step_screen(
            scene,
            title=ctx.menu_prompt_title, benefits=ctx.menu_prompt_benefits,
            button=ctx.menu_prompt_button, button_key='prompt_close',
            hold_progress=ctx.menu_prompt_hold_progress,
            matrix=next((item[1] for item in ctx.menu_spotify_items if item[0] == 'qr'), None),
            hint=ctx.menu_prompt_hint,
            fallback=tuple(item[1] for item in ctx.menu_spotify_items if item[0] == 'text')[-2:],
        )
        return scene

    def _step_screen(self, scene: Scene, title: str, button: str, button_key: str,
                     lines: tuple = (), benefits: tuple = (), hold_progress: float = 0.0,
                     matrix: Optional[list] = None, hint: str = '', fallback: tuple = ()):
        """One title, a few short lines and a button on the left, a code to scan on the right.

        Positions are in the user's view (1280 wide, 720 high); the panel is
        mounted sideways, so the user's x is the panel's y.
        """
        center_x = SCREEN_WIDTH // 2
        left = 310
        height = 62 + 34 * len(lines) + 42 * len(benefits) + (24 + 54 if button else 0)
        y = center_x - height // 2 - 6

        scene.labels.append(self._label_user(title, left, y, FONT_LARGE, COLORS['text_primary'])[0])
        y += 62
        for text in lines:
            scene.labels.append(self._label_user(text, left, y, FONT_MEDIUM, COLORS['text_secondary'])[0])
            y += 34
        for benefit in benefits:
            tick = tuple((SCREEN_WIDTH - (y + dy), left + dx) for dx, dy in ((0, 10), (7, 17), (19, 3)))
            scene.strokes.append(Stroke(tick, COLORS['accent'], 4))
            scene.labels.append(self._label_user(benefit, left + 34, y, FONT_MEDIUM, (216, 216, 216))[0])
            y += 42

        if button:
            # The one button, sized to its text. Held ones fill up while held.
            pad_side, pad_top = 30, 16
            label, label_rect = self._label_user(button, left + pad_side, y + 24 + pad_top,
                                                 FONT_MEDIUM, COLORS['text_primary'])
            pill = Rect(label_rect.x - pad_top, label_rect.y - pad_side,
                        label_rect.width + pad_top * 2, label_rect.height + pad_side * 2)
            scene.boxes.append(Box(*pill, COLORS['bg_elevated'], 12))
            if hold_progress > 0:
                filled = (pill.x, pill.y, pill.width, max(24, int(pill.height * hold_progress)))
                scene.boxes.append(Box(*filled, COLORS['accent'], 12))
            scene.labels.append(label)
            self.menu_button_rects[button_key] = pill

        # Right: the code to scan, or two lines of text when no code can be made.
        column = 870
        if matrix:
            qr_top = center_x - (QR_SIZE + (45 if hint else 0)) // 2
            self._qr(scene, matrix, SCREEN_WIDTH - qr_top - 16, center_y=column)
            if hint:
                scene.labels.append(self._label(hint, (SCREEN_WIDTH - (qr_top + QR_SIZE + 30), column),
                                                FONT_MEDIUM, COLORS['text_secondary']))
        else:
            for index, text in enumerate(fallback):
                scene.labels.append(self._label(text, (center_x + 15 - index * 34, column),
                                                FONT_MEDIUM, COLORS['text_secondary']))

    def _build_main_content(self, ctx: RenderContext) -> list:
        items = [
            ('button', 'wifi', 'WiFi', COLORS['bg_elevated']),
            ('button', 'bluetooth', 'Bluetooth', COLORS['bg_elevated']),
            ('button', 'volume', 'Volume levels', COLORS['bg_elevated']),
            ('button', 'spotify', 'Spotify', COLORS['bg_elevated']),
            ('separator',),
            ('button', 'auto_pause', f'Auto-pause: {ctx.auto_pause_minutes} min', COLORS['bg_elevated']),
            ('button', 'progress_expiry', f'Remember: {ctx.progress_expiry_hours} hrs', COLORS['bg_elevated']),
            ('separator',),
        ]
        # The update button says what it is doing, or how it ended
        items.append(('button', 'check_update', ctx.update_label, COLORS['bg_elevated']))
        items += [
            ('separator',),
            ('button', 'reset', 'Confirm Reset?' if ctx.reset_confirm_pending else 'Reset', COLORS['error']),
        ]
        if ctx.app_version_label:
            items.append(('footer', f'Version: {ctx.app_version_label}'))
        return items

    def _build_wifi_content(self, ctx: RenderContext) -> list:
        items = []
        for i, ssid in enumerate(ctx.menu_known_networks):
            is_current = ssid == ctx.menu_current_network
            color = COLORS['accent'] if is_current else COLORS['bg_elevated']
            display = ssid if len(ssid) <= 20 else ssid[:18] + '..'
            items.append(('button', f'reconnect_{i}', display, color))
        items.append(('separator',))
        items.append(('button', 'new_network', '+ New network', COLORS['bg_elevated']))
        return items

    def _build_bt_content(self, ctx: RenderContext) -> list:
        items = []
        if ctx.bt_paired_devices:
            items.append(('header', 'Paired'))
            for i, dev in enumerate(ctx.bt_paired_devices):
                color = COLORS['accent'] if dev.connected else COLORS['bg_elevated']
                label = dev.name if len(dev.name) <= 22 else dev.name[:20] + '..'
                items.append(('button', f'bt_paired_{i}', label, color))
            items.append(('separator',))
        if ctx.bt_discovered_devices:
            items.append(('header', 'Found'))
            for i, dev in enumerate(ctx.bt_discovered_devices):
                if ctx.bt_pairing_mac == dev.mac:
                    label = 'Connecting...'
                    color = COLORS['accent']
                else:
                    label = dev.name if len(dev.name) <= 22 else dev.name[:20] + '..'
                    color = COLORS['bg_elevated']
                items.append(('button', f'bt_discovered_{i}', label, color))
        elif ctx.bt_scanning:
            items.append(('header', 'Searching...'))
            items.append(('placeholder',))
        return items

    def _build_volume_content(self, ctx: RenderContext) -> list:
        items = []
        levels = ctx.volume_levels
        for idx, (output_type, names) in enumerate(self._VOL_LABELS):
            if idx > 0:
                items.append(('separator',))
            for i, name in enumerate(names):
                val = levels[i][output_type] if i < len(levels) else 0
                items.append(('vol_row', i, output_type, name, val))
        return items

    _ITEM_HEIGHTS = {
        'button': _MENU_BTN_H + _MENU_BTN_GAP, 'vol_row': _MENU_BTN_H + _MENU_BTN_GAP,
        'placeholder': _MENU_BTN_H + _MENU_BTN_GAP, 'separator': _MENU_BTN_GAP,
        'header': 30, 'text': 35, 'qr': QR_SIZE + 10, 'spacer': 15, 'footer': 30,
    }

    def _menu_content(self, scene: Scene, items: list, scroll_offset: int = 0):
        """Content items in the scrollable zone between title and screen edge."""
        H, GAP, W, Y = self._MENU_BTN_H, self._MENU_BTN_GAP, self._MENU_BTN_W, self._MENU_BTN_Y
        content_top = self._MENU_CONTENT_TOP
        content_bot = self._MENU_CONTENT_BOT

        total_height = sum(self._ITEM_HEIGHTS.get(item[0], 0) for item in items)
        if total_height > 0:
            total_height -= GAP  # remove trailing gap
        self.menu_content_overflow = max(0, total_height - (content_top - content_bot))
        scene.clip_left = content_bot

        def button(rect: Rect, label: str, color: tuple, radius: int = 18):
            scene.boxes.append(Box(*rect, color, radius))
            scene.labels.append(self._label(label, rect.center, FONT_MEDIUM, COLORS['text_primary']))

        x = content_top + scroll_offset
        btn_w_vol = 70  # +/- button width for volume rows
        label_w_vol = W - btn_w_vol * 2 - 10
        for item in items:
            kind = item[0]
            if kind == 'button':
                _, btn_id, label, color = item
                rect = Rect(x, Y, H, W)
                button(rect, label, color)
                self.menu_button_rects[btn_id] = rect
            elif kind == 'header':
                scene.labels.append(self._label(item[1], (x, CAROUSEL_CENTER_Y), FONT_SMALL, COLORS['text_muted']))
            elif kind == 'text':
                scene.labels.append(self._label(item[1], (x, CAROUSEL_CENTER_Y), FONT_MEDIUM, COLORS['text_secondary']))
            elif kind == 'qr':
                self._qr(scene, item[1], x)
            elif kind == 'vol_row':
                _, i, output_type, name, val = item
                minus_rect = Rect(x, Y, H, btn_w_vol)
                button(minus_rect, '−', COLORS['bg_elevated'])
                self.menu_button_rects[f'vol_minus_{i}_{output_type}'] = minus_rect
                label_rect = Rect(x, Y + btn_w_vol + 5, H, label_w_vol)
                button(label_rect, f'{name}: {val}%', COLORS['bg_secondary'])
                plus_rect = Rect(x, Y + btn_w_vol + 5 + label_w_vol + 5, H, btn_w_vol)
                button(plus_rect, '+', COLORS['bg_elevated'])
                self.menu_button_rects[f'vol_plus_{i}_{output_type}'] = plus_rect
            elif kind == 'footer':
                scene.labels.append(self._label(item[1], (x, CAROUSEL_CENTER_Y), FONT_SMALL, COLORS['text_muted']))
            x -= self._ITEM_HEIGHTS.get(kind, 0)

        # Only buttons in the visible content zone can be tapped
        for btn_id in [b for b, r in self.menu_button_rects.items()
                       if b != 'close' and (r.x + r.width <= content_bot or r.x >= content_top + H)]:
            del self.menu_button_rects[btn_id]

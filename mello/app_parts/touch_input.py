"""
Touch and key input for the Mello app: taps, swipes, the play/skip buttons
and delete mode.
"""
import logging
import time
from typing import Optional

from ..config import (
    COVER_SIZE, COVER_SIZE_SMALL, COVER_SPACING,
    CAROUSEL_X, CAROUSEL_CENTER_Y, CONTROLS_X, BTN_SIZE, PLAY_BTN_SIZE, BTN_SPACING,
    CAROUSEL_TOUCH_MARGIN, MAX_SWIPE_JUMP, VELOCITY_THRESHOLDS,
    ACTION_DEBOUNCE, SKIP_DEBOUNCE,
)
from ..models import CatalogItem
from ..utils import run_async

logger = logging.getLogger('mello.app')


class TouchInputMixin:
    """Taps, swipes, the play/skip buttons and delete mode."""

    def _handle_events(self):
        """Handle the touches (and mouse/keys) since the last frame."""
        for event in self.evdev_touch.take_events():
            if event.kind == 'down':
                logger.debug(f'Event: touch down at {event.pos}')
                if self.sleep_manager.is_sleeping:
                    self._wake_from_sleep(f'touch:{event.pos}')
                    continue
                self.sleep_manager.reset_timer()
                self._handle_touch_down(event.pos, event.touched_at)

            elif event.kind == 'key':
                if self.sleep_manager.is_sleeping:
                    self._wake_from_sleep(f'key:{event.key}')
                    continue
                self.sleep_manager.reset_timer()
                self._handle_key(event.key)

            elif event.kind == 'move':
                if self.setup_menu.is_open and self._menu_touch_start is not None:
                    self.sleep_manager.reset_timer()
                    # Menu scroll: track vertical drag (physical x-axis)
                    dx = event.pos[0] - self._menu_touch_start[0]
                    if abs(dx) > 10 and self.renderer.menu_content_overflow > 0:
                        self._menu_touch_scrolled = True
                        self.setup_menu.handle_scroll(
                            dx, self.renderer.menu_content_overflow)
                        self._menu_touch_start = event.pos
                elif self.touch.dragging:
                    self.sleep_manager.reset_timer()
                    self.touch.on_move(event.pos)

                    # Cancel delete mode when user starts swiping
                    if self.touch.is_swiping and self.delete_mode_id:
                        self.delete_mode_id = None
                        self.renderer.invalidate()

            elif event.kind == 'up':
                logger.debug(f'Event: touch up at {event.pos}')
                if not self.sleep_manager.is_sleeping:
                    if self.setup_menu.is_open and self._menu_touch_start is not None:
                        self.setup_menu.handle_release()
                        if not self._menu_touch_scrolled:
                            # Flash pressed state on close/back button
                            close_rect = self.renderer.menu_button_rects.get('close')
                            if close_rect and close_rect.collidepoint(*event.pos):
                                self._pressed_button = 'menu_close'
                                self._pressed_time = time.time()
                            self.setup_menu.handle_tap(event.pos, self.renderer.menu_button_rects)
                        self._menu_touch_start = None
                        self._menu_touch_scrolled = False
                    else:
                        self._handle_touch_up(event.pos, event.touched_at)
                    self._handle_button_up()

    def _handle_key(self, key: str):
        """Keyboard (on a computer)."""
        if key == 'escape':
            self.running = False
        elif key == 'left':
            self._navigate(-1)
        elif key == 'right':
            self._navigate(1)
        elif key in (' ', 'return'):
            self._toggle_play()
        elif key == 'n':
            self._skip_track(self.api.next)
        elif key == 'p':
            self._skip_track(self.api.prev)
    
    def _handle_touch_down(self, pos, touched_at: Optional[float] = None):
        """Handle touch/mouse down."""
        # Menu intercept — track touch start for scroll vs tap detection
        if self.setup_menu.is_open:
            self._menu_touch_start = pos
            self._menu_touch_scrolled = False
            self.setup_menu.handle_press(pos, self.renderer.menu_button_rects)
            return
        
        x, y = pos
        
        carousel_x_min = CAROUSEL_X - CAROUSEL_TOUCH_MARGIN
        carousel_x_max = CAROUSEL_X + COVER_SIZE + CAROUSEL_TOUCH_MARGIN
        
        logger.debug(f'Touch down: pos={pos}, carousel_x_range={carousel_x_min}-{carousel_x_max}')

        # Delete mode is modal: the next tap confirms delete or cancels it.
        # It must never fall through to cover/play handling.
        if self.delete_mode_id:
            self._handle_delete_mode_tap(pos)
            return
        
        # Check button clicks
        if self._check_button_click(pos):
            logger.debug('Touch down: button click')
            return
        
        # Carousel swipes - within carousel X zone, full Y range
        if carousel_x_min <= x <= carousel_x_max:
            logger.debug('Touch down: carousel swipe start')
            self.touch.on_down(pos, touched_at)
            self.user_interacting = True
        else:
            logger.debug('Touch down: outside carousel')
            self._handle_button_tap(pos)
    
    def _check_button_click(self, pos) -> bool:
        """Check if click is on add/delete button."""
        x, y = pos
        
        if self.renderer.add_button_rect:
            bx, by, bw, bh = self.renderer.add_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._save_temp_item()
                return True
        
        if self.renderer.delete_button_rect:
            bx, by, bw, bh = self.renderer.delete_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._delete_current_item()
                return True

        if self.renderer.settings_button_rect:
            bx, by, bw, bh = self.renderer.settings_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.setup_menu.open()
                return True

        return False

    @staticmethod
    def _point_in_rect(pos, rect: Optional[tuple]) -> bool:
        """Return True if pos is inside an (x, y, w, h) rect."""
        if not rect:
            return False
        x, y = pos
        bx, by, bw, bh = rect
        return bx <= x <= bx + bw and by <= y <= by + bh

    def _delete_fallback_rect(self) -> tuple:
        """Expected delete overlay hit rect for the centered cover.

        Mirrors Renderer._draw_overlay_button geometry. This is only a
        safety net for the rare case where the visual delete mode is active
        but the renderer hit rect was not captured yet.
        """
        btn_size, margin, touch_padding = 100, 16, 60
        cover_x = CAROUSEL_X
        cover_y = CAROUSEL_CENTER_Y - COVER_SIZE // 2
        btn_x = cover_x + margin
        btn_y = cover_y + COVER_SIZE - btn_size - margin
        hit_x = btn_x - touch_padding
        hit_y = btn_y - touch_padding
        hit_size = btn_size + touch_padding * 2
        return (hit_x, hit_y, hit_size, hit_size)

    def _focused_delete_item(self) -> Optional[CatalogItem]:
        """Return focused catalog item while delete mode is active."""
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return None
        item = items[self.selected_index]
        if item.is_temp or item.id != self.delete_mode_id:
            return None
        return item

    def _handle_delete_mode_tap(self, pos):
        """Confirm or cancel delete mode, consuming the tap either way."""
        item = self._focused_delete_item()
        renderer_rect = self.renderer.delete_button_rect
        remembered_rect = self._delete_button_rect
        fallback_rect = self._delete_fallback_rect()
        active_rect = renderer_rect or remembered_rect

        if item is None:
            logger.info(
                'Delete confirm hit-test | result=stale_item '
                f'| pos={pos} | rect={active_rect} | fallback={fallback_rect} '
                f'| delete_mode_id={self.delete_mode_id}'
            )
            self.delete_mode_id = None
            self._delete_button_rect = None
            self.renderer.invalidate()
            return

        if active_rect is None:
            result = 'confirm' if self._point_in_rect(pos, fallback_rect) else 'rect_missing'
            hit = result == 'confirm'
        else:
            hit = self._point_in_rect(pos, active_rect) or self._point_in_rect(pos, fallback_rect)
            result = 'confirm' if hit else 'miss_cancel'

        logger.info(
            'Delete confirm hit-test | '
            f'result={result} | pos={pos} | rect={active_rect} | fallback={fallback_rect} '
            f'| focused="{item.name}" | focused_id={item.id} | focused_uri={item.uri[:50]} '
            f'| delete_mode_id={self.delete_mode_id}'
        )

        if hit:
            self._delete_current_item()
            return

        self.delete_mode_id = None
        self._delete_button_rect = None
        self.renderer.invalidate()
    
    def _handle_touch_up(self, pos, touched_at: Optional[float] = None):
        """Handle touch/mouse up."""
        logger.debug(f'Touch up: pos={pos}, dragging={self.touch.dragging}')
        if not self.touch.dragging:
            logger.debug('Touch up: ignored (not dragging)')
            return
        
        drag_index_offset = -self.touch.drag_offset / (COVER_SIZE + COVER_SPACING)
        visual_position = self.selected_index + drag_index_offset
        
        action, velocity = self.touch.on_up(pos, touched_at)
        self.carousel.scroll_x = visual_position
        
        x, y = pos
        
        if action in ('left', 'right'):
            abs_vel = abs(velocity)
            v_low, v_mid, v_high = VELOCITY_THRESHOLDS
            velocity_bonus = 0 if abs_vel < v_low else (1 if abs_vel < v_mid else (2 if abs_vel < v_high else 3))
            
            base_target = round(visual_position)
            target = base_target + velocity_bonus if velocity < 0 else base_target - velocity_bonus
            
            target = max(self.selected_index - MAX_SWIPE_JUMP, min(target, self.selected_index + MAX_SWIPE_JUMP))
            target = max(0, min(target, len(self.display_items) - 1))
            
            self._snap_to(target)
        elif action == 'tap':
            # Debounce tap actions
            now = time.time()
            if now - self._last_action_time < ACTION_DEBOUNCE:
                logger.debug('Carousel tap debounced')
                return
            
            # Carousel runs along Y axis - check Y position for tap target
            center_y = CAROUSEL_CENTER_Y  # 640
            if y < center_y - COVER_SIZE // 2:
                # Tap on previous item (lower Y)
                self._navigate(-1)
            elif y > center_y + COVER_SIZE // 2:
                # Tap on next item (higher Y)
                self._navigate(1)
            else:
                logger.info('Carousel tap: play')
                self._last_action_time = now
                self._pressed_button = 'play'
                self._pressed_time = now
                self._toggle_play()
                self.renderer.invalidate()
    
    def _handle_button_tap(self, pos):
        """Handle direct tap on control buttons with debouncing.
        
        Portrait mode: buttons stacked vertically at X=CONTROLS_X, along Y axis.
        """
        now = time.time()
        x, y = pos
        center_y = CAROUSEL_CENTER_Y
        btn_spacing = BTN_SPACING  # 155
        on_skip = (CONTROLS_X - PLAY_BTN_SIZE <= x <= CONTROLS_X + PLAY_BTN_SIZE and
                   any(abs(y - (center_y + s * btn_spacing)) <= BTN_SIZE for s in (-1, 1)))
        limit = SKIP_DEBOUNCE if on_skip else ACTION_DEBOUNCE
        if now - self._last_action_time < limit:
            logger.debug(f'Button tap debounced at ({pos[0]}, {pos[1]})')
            return

        # Volume button Y position (matches renderer)
        vol_y = center_y + (COVER_SIZE + COVER_SPACING) + COVER_SIZE_SMALL // 2 - BTN_SIZE // 2

        # Headphone button Y position (matches renderer constant)
        hp_y = center_y - (COVER_SIZE + COVER_SPACING) - COVER_SIZE_SMALL // 2 + BTN_SIZE // 2

        # Portrait mode: check if X is in button column
        if CONTROLS_X - PLAY_BTN_SIZE <= x <= CONTROLS_X + PLAY_BTN_SIZE:
            button_pressed = None

            # Headphone: Y = hp_y (~107) — only active when BT device connected
            if hp_y - BTN_SIZE <= y <= hp_y + BTN_SIZE and self.bluetooth.connected_device:
                button_pressed = 'headphone'
                self.bluetooth.toggle_audio()
            # Prev: Y = center_y - btn_spacing (485)
            elif center_y - btn_spacing - BTN_SIZE <= y <= center_y - btn_spacing + BTN_SIZE:
                button_pressed = 'prev'
                self._skip_track(self.api.prev)
            # Play: Y = center_y (640)
            elif center_y - PLAY_BTN_SIZE <= y <= center_y + PLAY_BTN_SIZE:
                button_pressed = 'play'
                self._toggle_play()
            # Next: Y = center_y + btn_spacing (795)
            elif center_y + btn_spacing - BTN_SIZE <= y <= center_y + btn_spacing + BTN_SIZE:
                button_pressed = 'next'
                self._skip_track(self.api.next)
            # Volume: Y = vol_y (~1173) — start hold timer; action fires on release
            elif vol_y - BTN_SIZE <= y <= vol_y + BTN_SIZE:
                button_pressed = 'volume'
                self._volume_hold_start = now
                self._menu_hold_triggered = False
                # Don't toggle volume here — wait for button up (short tap) or hold (menu)
            
            if button_pressed:
                logger.info(f'Button press: {button_pressed}')
                self._last_action_time = now
                self._pressed_button = button_pressed
                self._pressed_time = now
                self.renderer.invalidate()
    
    def _snap_to(self, target_index: int):
        """Snap carousel to a specific index.

        When the index changes this is the single point where playback
        is interrupted: timer cancelled, running play-thread invalidated,
        and instant silence sent to Spotify.
        """
        items = self.display_items
        if not items:
            return

        target_index = max(0, min(target_index, len(items) - 1))

        if target_index != self.selected_index:
            old_index = self.selected_index
            self.selected_index = target_index
            self.carousel.set_target(target_index)
            self._bump_focus_epoch(f'snap {old_index}->{target_index}')
            self._clear_manual_pause_lock('focus_changed')

            self.play_timer.cancel()
            was_playing_or_loading = self.now_playing.playing or self.playback.play_in_progress
            self.playback.stop_all()
            self.volume.mute()
            now = time.time()
            should_pause_remote = (
                now - self._last_snap_pause_at > 0.4 and
                was_playing_or_loading
            )
            if should_pause_remote:
                self._last_snap_pause_at = now
                run_async(self.api.pause)
            item = items[target_index]
            self._user_driving = not item.is_temp and not self._is_item_playing(item)
            if self._user_driving:
                self._pending_focus_uri = item.uri
                self.playback.play_state.start_loading()
            logger.info(f'Snap: {old_index} -> {target_index}, item={item.name}, play_pending={self._user_driving}')
        else:
            self.carousel.set_target(target_index)
    
    def _navigate(self, direction: int):
        """Navigate carousel."""
        items = self.display_items
        if not items:
            return
        
        new_index = max(0, min(self.selected_index + direction, len(items) - 1))
        self._snap_to(new_index)
    
    def _delete_current_item(self):
        """Delete the current item from catalog."""
        logger.info(
            f'Delete requested | delete_mode_id={self.delete_mode_id} '
            f'| deleting={self._deleting}'
        )
        if not self.delete_mode_id:
            logger.warning('Delete ignored | reason=no_delete_mode_id')
            return
        if self._deleting:
            logger.warning('Delete ignored | reason=already_deleting')
            return
        self._deleting = True
        
        try:
            item_id = self.delete_mode_id
            old_index = self.selected_index
            
            item = next((i for i in self.catalog_manager.items if i.id == item_id), None)
            if item:
                logger.info(f'Deleting: {item.name} | id={item.id} | uri={item.uri[:50]}')
            else:
                logger.warning(f'Delete target not found in catalog | id={item_id}')
            
            success = self.catalog_manager.delete_item(item_id)
            
            if success:
                self.catalog_manager.load()
                self._update_carousel_max_index()
                
                new_index = max(0, old_index - 1)
                if self.display_items:
                    new_index = min(new_index, len(self.display_items) - 1)
                    self.selected_index = new_index
                    self._bump_focus_epoch(f'delete select -> {new_index}')
                    self.carousel.scroll_x = float(new_index)
                    self.carousel.set_target(new_index)
                    
                    new_item = self.display_items[new_index]
                    logger.info(
                        f'Delete success | removed_id={item_id} | '
                        f'new_index={new_index} | new_item="{new_item.name}" | '
                        f'new_uri={new_item.uri[:50]}'
                    )
                    if not new_item.is_temp:
                        self._play_item(new_item.uri)
                else:
                    logger.info(f'Delete success | removed_id={item_id} | catalog empty')
            else:
                logger.warning(f'Delete failed | id={item_id}')
            
            self.delete_mode_id = None
            self._delete_button_rect = None
            self.renderer.invalidate()
        finally:
            self._deleting = False
    
    def _trigger_delete_mode(self):
        """Trigger delete mode for the currently selected item."""
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return
        
        item = items[self.selected_index]
        if item.is_temp:
            return
        
        logger.info(f'Delete mode: {item.name}')
        self.delete_mode_id = item.id
        self._delete_button_rect = None
        self.renderer.invalidate()
    
    def _handle_button_up(self):
        """Handle MOUSEBUTTONUP for volume hold: short tap → toggle, long hold → already opened menu."""
        if self._volume_hold_start is None:
            return
        if self._menu_hold_triggered:
            self._volume_hold_start = None
            self._menu_hold_triggered = False
            return
        # Short tap: toggle volume
        self.volume.toggle()
        # Also set BT sink volume when BT audio is active
        if self._bt_audio_active:
            self.bluetooth.set_volume(self.volume.bt_level)
        self._last_action_time = time.time()
        self._volume_hold_start = None

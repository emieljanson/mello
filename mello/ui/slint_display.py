"""
Slint display (MELLO_UI=slint): puts Mello's screen on the display with Slint.

The Pygame renderer still draws everything except the album covers, into an
off-screen surface: text, buttons and menus look exactly as before. Slint
shows that surface as a backdrop and draws and animates the covers on the
graphics chip. Swiping used to cost ~27 ms of drawing plus ~13 ms of pushing
the frame to the screen per frame on a Pi 3; with Slint it runs at the
screen's 60 fps.

Pygame surfaces are handed over only when they changed (a CRC of the pixels),
so an idle screen or a swipe costs no image uploads.
"""
import gc
import logging
import time
import zlib
from datetime import timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pygame

from ..config import COVER_SIZE, COVER_SIZE_SMALL

logger = logging.getLogger(__name__)

SLINT_FILE = Path(__file__).with_name('mello.slint')
# How often Slint calls Mello's loop; frames are paced in app._run_slint_loop.
# (4 ms gave no smoother swipes on the device but 9% CPU when idle, vs 3%.)
TICK = timedelta(milliseconds=8)


def surface_to_image(surface: pygame.Surface, alpha: bool = True):
    """A Slint image with the same pixels as a Pygame surface."""
    import slint
    width, height = surface.get_size()
    mode, depth = ('RGBA', 4) if alpha else ('RGB', 3)
    data = pygame.image.tobytes(surface, mode)
    return slint.Image.load_from_array(memoryview(data).cast('B', (height, width, depth)))


class SlintDisplay:
    """Owns the Slint window; the app calls present() after each frame it drew."""

    def __init__(self, renderer=None, image_cache=None):
        import slint
        gc.disable()  # see run(): garbage is collected on the UI thread only
        self._slint = slint
        started = time.monotonic()
        module = slint.load_file(str(SLINT_FILE))
        self.window = module.MelloWindow()
        logger.info(f'Slint window ready in {time.monotonic() - started:.2f}s')
        self.renderer = renderer
        self.image_cache = image_cache
        self._backdrop_crc: Optional[int] = None
        self._backdrop_key: Optional[tuple] = None
        self._titles_key: Optional[tuple] = None
        self._covers_key: Optional[tuple] = None
        self._cover_images: Dict[Tuple[str, int, bool], object] = {}
        self._overlay_key: Optional[tuple] = None
        self._toast_key: Optional[tuple] = None
        self._spinner_ready = False
        self._timer = None
        self.uploads = 0

    def attach(self, renderer, image_cache):
        """Hand over the renderer once the app is set up."""
        self.renderer = renderer
        self.image_cache = image_cache

    def show_surface(self, surface: pygame.Surface):
        """Show a whole Pygame surface (the boot splash)."""
        self._backdrop_key = None
        self._set_backdrop(surface)
        self.window.show_carousel = False

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def run(self, tick: Callable[[], bool]):
        """Run the Slint event loop, calling tick() often; returns when tick() says stop."""
        slint = self._slint

        last = {'end': None}

        def on_tick():
            start = time.monotonic()
            if last['end'] is not None and start - last['end'] > 0.05:
                # Time between two of our ticks is Slint drawing (and uploading
                # new images to the graphics chip): normally a few ms.
                logger.info(f'Slint: {(start - last["end"]) * 1000:.0f} ms outside Mello\'s loop '
                            f'(drawing or uploading images)')
            try:
                keep_going = tick()
            except Exception:
                logger.exception('Main loop tick failed')
                keep_going = True
            last['end'] = time.monotonic()
            if not keep_going:
                self._timer.stop()
                slint.quit_event_loop()

        # Slint objects may only be freed on the thread that made them
        # (otherwise: "PyStruct is unsendable, but sent to another thread" and
        # an abort, seen on the device). Python's automatic garbage collection
        # runs on whichever thread happens to allocate, such as the analytics
        # upload thread, so it is switched off and run from the UI loop with
        # the same thresholds instead.
        gc.disable()
        thresholds = gc.get_threshold()

        def collect_garbage():
            counts = gc.get_count()
            generation = -1
            for g in range(3):
                if counts[g] > thresholds[g]:
                    generation = g
            if generation >= 0:
                gc.collect(generation)

        def on_tick_with_gc():
            on_tick()
            collect_garbage()

        self._timer = slint.Timer()
        self._timer.start(slint.TimerMode.Repeated, TICK, on_tick_with_gc)
        try:
            self.window.run()
        except RuntimeError as e:
            # The linuxkms backend refuses the asyncio clean-up that runs after
            # quit ("Re-entering the linuxkms event loop"); the loop is done.
            if 'Re-entering' not in str(e):
                raise
            logger.debug(f'Slint event loop ended: {e}')

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    def present(self, screen: pygame.Surface):
        """Show what the renderer drew this frame."""
        r = self.renderer
        w = self.window
        frame = r.carousel_frame if r.frame_kind == 'main' else None
        if r.backdrop_changed:
            r.backdrop_changed = False
            key = r.backdrop_key
            if key is None or key != self._backdrop_key:
                self._backdrop_key = key
                self._set_backdrop(screen)
            if frame is not None:
                self._set_titles(r.title_key, r.title_layers)
        w.show_carousel = frame is not None
        if frame is None:
            w.show_toast = False
            return
        self._set_covers(frame['items'])
        for i, top in frame['tops'].items():
            if self._tops.row_data(i) != top:
                self._tops.set_row_data(i, top)
        w.first = frame['first']
        w.last = frame['last']
        w.center_index = frame['center']
        w.loading = frame['loading']
        if frame['loading']:
            self._ensure_spinner()
            w.spinner_index = frame['spinner_index']
        self._set_overlay(frame['overlay_key'], frame['overlay'])
        self._set_toast(frame['toast'])

    def _set_backdrop(self, screen: pygame.Surface):
        data = pygame.image.tobytes(screen, 'RGB')
        crc = zlib.crc32(data)
        if crc == self._backdrop_crc:
            return
        self._backdrop_crc = crc
        width, height = screen.get_size()
        self.window.backdrop = self._slint.Image.load_from_array(memoryview(data).cast('B', (height, width, 3)))
        self.uploads += 1

    def _set_titles(self, key, layers):
        if key == self._titles_key:
            return
        self._titles_key = key
        self.window.titles = self._slint.ListModel([
            {'source': surface_to_image(surface, alpha=False), 'x': rect.x, 'y': rect.y}
            for surface, rect in layers])
        self.uploads += 1

    def _cover_image(self, image_path: Optional[str], size: int, dimmed: bool):
        key = (image_path or '', size, dimmed)
        image = self._cover_images.get(key)
        if image is None:
            path = self.image_cache._get_variant_path(image_path, size, dimmed) if image_path else None
            if path is not None and Path(path).exists():
                image = self._slint.Image.load_from_path(str(path))
            else:
                image = surface_to_image(self.image_cache.get_placeholder(size))
            self._cover_images[key] = image
        return image

    def _set_covers(self, items):
        key = tuple(item.image for item in items)
        if key == self._covers_key:
            return
        self._covers_key = key
        covers = [{
            'full': self._cover_image(item.image, COVER_SIZE, False),
            'dim': self._cover_image(item.image, COVER_SIZE, True),
            'small': self._cover_image(item.image, COVER_SIZE_SMALL, True),
        } for item in items]
        # Images of albums that are no longer in the list can go.
        wanted = {(item.image or '', size, dim) for item in items
                  for size, dim in ((COVER_SIZE, False), (COVER_SIZE, True), (COVER_SIZE_SMALL, True))}
        self._cover_images = {k: v for k, v in self._cover_images.items() if k in wanted}
        self.window.covers = self._slint.ListModel(covers)
        self._tops = self._slint.ListModel([0] * len(covers))
        self.window.tops = self._tops
        logger.info(f'Slint: {len(covers)} covers loaded')

    def _ensure_spinner(self):
        if self._spinner_ready:
            return
        frames = self.renderer.spinner_frames(COVER_SIZE)
        self.window.spinner_frames = self._slint.ListModel([surface_to_image(f) for f in frames])
        self._spinner_ready = True

    def _set_overlay(self, key, surface):
        if key == self._overlay_key:
            return
        self._overlay_key = key
        if surface is None:
            self.window.show_cover_overlay = False
            return
        self.window.cover_overlay = surface_to_image(surface, alpha=False)
        self.window.show_cover_overlay = True
        self.uploads += 1

    def _set_toast(self, toast):
        key = toast[0] if toast else None
        if key == self._toast_key:
            return
        self._toast_key = key
        if not toast:
            self.window.show_toast = False
            return
        _message, surface, rect = toast
        self.window.toast = surface_to_image(surface)
        self.window.toast_x = rect.x
        self.window.toast_y = rect.y
        self.window.show_toast = True
        self.uploads += 1

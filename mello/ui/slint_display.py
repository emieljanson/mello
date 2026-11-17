"""
Slint display: puts Mello's screen on the display with Slint.

The renderer describes each screen (ui/renderer.py: boxes, pictures, labels
and where the covers are); this hands that to the Slint window
(ui/mello.slint), which draws it on the graphics chip. Lists are only handed
over when they changed, so an idle screen or a swipe costs no work besides
moving the covers.
"""
import gc
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from ..config import COLORS, COVER_SIZE, COVER_SIZE_SMALL, SCREEN_WIDTH, SCREEN_HEIGHT
from .bitmaps import Bitmaps
from .renderer import Scene, Picture

logger = logging.getLogger(__name__)

SLINT_FILE = Path(__file__).with_name('mello.slint')
# How often Slint calls Mello's loop; frames are paced in app._run_loop.
# (4 ms gave no smoother swipes on the device but 9% CPU when idle, vs 3%.)
TICK = timedelta(milliseconds=8)


def pil_to_image(image):
    """A Slint image with the same pixels as a Pillow image."""
    import slint
    image = image.convert('RGBA')
    width, height = image.size
    return slint.Image.load_from_array(memoryview(image.tobytes()).cast('B', (height, width, 4)))


class SlintDisplay:
    """Owns the Slint window; the app calls present() after each frame."""

    def __init__(self, bitmaps: Bitmaps):
        import slint
        gc.disable()  # see run(): garbage is collected on the UI thread only
        self._slint = slint
        started = time.monotonic()
        module = slint.load_file(str(SLINT_FILE))
        self.window = module.MelloWindow()
        logger.info(f'Slint window ready in {time.monotonic() - started:.2f}s')
        self.bitmaps = bitmaps
        self.renderer = None
        self.image_cache = None
        self._scene: Optional[Scene] = None
        self._covers_key: Optional[tuple] = None
        self._cover_images: Dict[Tuple[str, int, bool], object] = {}
        self._pictures: Dict[tuple, object] = {}
        self._colors: Dict[tuple, object] = {}
        self._button: Optional[str] = None
        self._tops = None
        self._timer = None
        self.window.accent = self._color(COLORS['accent'])
        self.show_splash()

    def attach(self, renderer, image_cache):
        """Hand over the renderer once the app is set up."""
        self.renderer = renderer
        self.image_cache = image_cache

    def show_splash(self):
        """The Mello logo on the background, while the app starts."""
        scene = Scene('gradient')
        key = ('logo', 320)
        w, h = self.bitmaps.size(key)
        scene.pictures.append(Picture(key, (SCREEN_WIDTH - w) // 2, (SCREEN_HEIGHT - h) // 2, w, h))
        self._set_scene(scene)
        self.window.show_carousel = False

    def forward_input(self, on_pointer: Callable[[str, Tuple[int, int]], None],
                      on_key: Callable[[str], None]):
        """Without a touchscreen (a computer), take mouse and keys from Slint."""
        self.window.pointer = lambda kind, x, y: on_pointer(kind, (int(x), int(y)))
        self.window.key = on_key
        self.window.forward_pointer = True

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

    def present(self):
        """Show what the renderer worked out this frame."""
        r = self.renderer
        w = self.window
        if r.scene is not self._scene:
            if r.scene != self._scene:
                self._set_scene(r.scene)
            self._scene = r.scene
        frame = r.carousel_frame
        w.show_carousel = frame is not None
        if frame is None:
            return
        self._set_covers(frame['items'])
        for i, top in frame['tops'].items():
            if self._tops.row_data(i) != top:
                self._tops.set_row_data(i, top)
        w.first = frame['first']
        w.last = frame['last']
        w.center_index = frame['center']
        w.loading = frame['loading']
        w.progress_fill = frame['progress_fill']
        self._set_cover_button(frame['button'])
        w.toast = frame['toast']

    def _color(self, rgb: tuple):
        color = self._colors.get(rgb)
        if color is None:
            text = '#' + ''.join(f'{v:02x}' for v in rgb)
            color = self._colors[rgb] = self._slint.Color(text)
        return color

    def _picture(self, key: tuple):
        image = self._pictures.get(key)
        if image is None:
            if key[0] == 'qr':
                for old in [k for k in self._pictures if k[0] == 'qr']:
                    del self._pictures[old]
            image = self._pictures[key] = pil_to_image(self.bitmaps.image(key))
        return image

    def _boxes(self, boxes):
        return self._slint.ListModel([
            {'x': b.x, 'y': b.y, 'w': b.w, 'h': b.h, 'color': self._color(b.color), 'radius': b.radius}
            for b in boxes])

    def _picture_list(self, pictures):
        return self._slint.ListModel([
            {'source': self._picture(p.key), 'x': p.x, 'y': p.y, 'w': p.w, 'h': p.h}
            for p in pictures])

    def _labels(self, labels):
        return self._slint.ListModel([
            {'text': l.text, 'x': l.x, 'y': l.y, 'size': l.size, 'color': self._color(l.color)}
            for l in labels])

    def _set_scene(self, scene: Scene):
        w = self.window
        old = self._scene or Scene(None)
        self._scene = scene
        w.background_kind = scene.background
        w.clip_content = scene.clip_left is not None
        w.clip_left = scene.clip_left or 0
        w.header_fade = scene.header_fade
        # Only the lists that changed (pressing a button changes one box).
        if scene.boxes != old.boxes:
            w.boxes = self._boxes(scene.boxes)
        if scene.strokes != old.strokes:
            w.strokes = self._slint.ListModel([
                {'commands': 'M ' + ' L '.join(f'{x} {y}' for x, y in s.points),
                 'color': self._color(s.color), 'width': s.width}
                for s in scene.strokes])
        if scene.pictures != old.pictures:
            w.pictures = self._picture_list(scene.pictures)
        if scene.labels != old.labels:
            w.labels = self._labels(scene.labels)
        if scene.top_boxes != old.top_boxes:
            w.top_boxes = self._boxes(scene.top_boxes)
        if scene.top_pictures != old.top_pictures:
            w.top_pictures = self._picture_list(scene.top_pictures)
        if scene.top_labels != old.top_labels:
            w.top_labels = self._labels(scene.top_labels)

    def _cover_image(self, image_path: Optional[str], size: int, dimmed: bool):
        key = (image_path or '', size, dimmed)
        image = self._cover_images.get(key)
        if image is None:
            path = self.image_cache.variant_path(image_path, size, dimmed) if image_path else None
            if path is not None:
                try:
                    image = self._slint.Image.load_from_path(str(path))
                except Exception as e:
                    logger.warning(f'Could not load cover {path}: {e}')
            if image is None:
                image = self._picture(('placeholder', size))
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

    def _set_cover_button(self, button: Optional[str]):
        if button == self._button:
            return
        self._button = button
        if button:
            icon, tint = ('plus', COLORS['accent']) if button == 'add' else ('minus', COLORS['error'])
            self.window.cover_button = self._picture(('icon', icon, 72, tint))
        self.window.show_cover_button = bool(button)

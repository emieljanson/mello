"""With MELLO_UI=slint the renderer describes the covers instead of drawing them.

Laying the described covers, overlay and title over the backdrop must give
exactly the picture the Pygame renderer draws. (Pixel checks on the device,
with Slint itself, are in the PR; here Slint's part is played by blits.)

Other test modules replace pygame with a stub, so these checks run in a fresh
interpreter with the real pygame.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

CHECKS = textwrap.dedent('''
    import os
    os.environ['SDL_VIDEODRIVER'] = 'dummy'
    import pygame
    import pytest
    from mello.config import COVER_SIZE, COVER_SIZE_SMALL, COLORS
    from mello.models import CatalogItem, NowPlaying
    from mello.ui.renderer import Renderer
    from mello.ui.context import RenderContext

    pygame.display.init(); pygame.font.init()
    SCREEN = pygame.display.set_mode((720, 1280), 0, 32)

    class Covers:
        """Image cache stand-in: a distinct colour per album, size and dimming."""
        def _surface(self, image, size, dimmed):
            s = pygame.Surface((size, size), pygame.SRCALPHA)
            n = int(image or 0)
            s.fill(((n * 40) % 256, (n * 90) % 256, 200 if dimmed else 60, 255))
            pygame.draw.rect(s, (0, 0, 0, 0), (0, 0, 12, 12))   # a transparent corner
            return s
        def get(self, image, size=COVER_SIZE):
            return self._surface(image, size, False)
        def get_dimmed(self, image, size=COVER_SIZE):
            return self._surface(image, size, True)

    ICONS = {name: pygame.Surface((40, 40), pygame.SRCALPHA) for name in
             ('play', 'pause', 'prev', 'next', 'volume_none', 'volume_low', 'volume_high', 'plus', 'minus', 'headphone')}
    for icon in ICONS.values():
        pygame.draw.circle(icon, (255, 255, 255, 255), (20, 20), 14)
    ITEMS = [CatalogItem(id=str(i), uri=f'spotify:album:{i}', name=f'Album {i}', image=str(i)) for i in range(8)]
    ITEMS[2].current_track = {'name': 'Liedje', 'artist': 'Iemand', 'position': 50000, 'duration': 180000}

    def ctx(**kw):
        base = dict(items=ITEMS, selected_index=0, now_playing=NowPlaying(), scroll_x=0.0, drag_offset=0,
                    dragging=False, is_sleeping=False, volume_index=1, delete_mode_id=None, pressed_button=None,
                    is_loading=False, is_playing=False)
        base.update(kw)
        return RenderContext(**base)

    def composite(r):
        """Do what mello.slint does with the renderer's description."""
        out = SCREEN.copy()
        f = r.carousel_frame
        for surface, rect in r.title_layers:
            out.blit(surface, rect)
        cache = r.image_cache
        for i in range(f['first'], f['last']):
            center = i == f['center']
            size = COVER_SIZE if center else COVER_SIZE_SMALL
            top = f['tops'][i]
            if top + size < 0 or top > 1280:
                continue
            item = f['items'][i]
            image = (cache.get_dimmed(item.image, size) if f['loading'] else cache.get(item.image, size)) \\
                if center else cache.get_dimmed(item.image, size)
            out.blit(image, (185 if center else 236, top))
            if center and f['overlay'] is not None:
                out.blit(f['overlay'], (185, top))
            if center and f['loading']:
                out.blit(r.spinner_frames(COVER_SIZE)[f['spinner_index']], (185, top))
        if f['toast']:
            out.blit(f['toast'][1], f['toast'][2])
        return out

    PLAYING = NowPlaying(playing=True, paused=False, stopped=False, context_uri=ITEMS[2].uri,
                         track_name='Liedje', track_artist='Iemand', position=72000, duration=180000)

    @pytest.mark.parametrize('scene', [
        dict(),
        dict(selected_index=2, scroll_x=2.0, now_playing=PLAYING, is_playing=True),
        dict(selected_index=3, scroll_x=3.3),
        dict(selected_index=3, scroll_x=2.7),
        dict(selected_index=4, scroll_x=4.5),
        dict(selected_index=7, scroll_x=7.0),
        dict(selected_index=5, scroll_x=5.0, is_loading=True),
        dict(selected_index=6, scroll_x=6.0, delete_mode_id='6'),
        dict(selected_index=1, scroll_x=1.0, toast_message='Added to Mello'),
    ])
    def test_slint_description_gives_the_same_picture(scene):
        ref = Renderer(SCREEN, Covers(), ICONS)
        ref._spinner_frame_idx = 3
        ref.draw(ctx(**scene))
        expected = pygame.image.tobytes(SCREEN, 'RGB')

        r = Renderer(SCREEN, Covers(), ICONS)
        r.compositor = True
        r._spinner_frame_idx = 3
        r.draw(ctx(**scene))
        actual = pygame.image.tobytes(composite(r), 'RGB')
        # Overlays, title and toast are blended twice here (onto a transparent
        # layer, then onto the screen): one step of 255 may round differently.
        worst = max(abs(a - b) for a, b in zip(actual, expected))
        assert worst <= 1, worst

    def test_album_without_title_is_not_redrawn_every_frame():
        r = Renderer(SCREEN, Covers(), ICONS)
        c = ctx(selected_index=1, scroll_x=1.0)   # album 1 has no saved title
        assert r.draw(c) is None                  # first frame: everything
        assert r.draw(c) == []                    # nothing changed: nothing drawn

    def test_slint_backdrop_key_ignores_the_title():
        r = Renderer(SCREEN, Covers(), ICONS)
        r.compositor = True
        r.draw(ctx(selected_index=1, scroll_x=1.0))
        first = r.backdrop_key
        r.draw(ctx(selected_index=2, scroll_x=2.0))    # other album, other title
        assert r.backdrop_key == first
        assert r.title_layers
        r.invalidate()                                 # the app does this on a press
        r.draw(ctx(selected_index=2, scroll_x=2.0, pressed_button='play'))
        assert r.backdrop_key != first
''')


def test_slint_compositor(tmp_path):
    pytest.importorskip('pygame')
    script = tmp_path / 'test_compositor.py'
    script.write_text(CHECKS)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(script)],
        cwd=ROOT, capture_output=True, text=True,
        env={'PYTHONPATH': str(ROOT), 'SDL_VIDEODRIVER': 'dummy', 'PATH': '/usr/bin:/bin'},
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert '11 passed' in result.stdout, result.stdout[-2000:]

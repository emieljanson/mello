"""Swiping past an album without a saved title must not redraw the whole screen.

Other test modules replace pygame with a stub, so this runs in a fresh
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
    from mello.models import CatalogItem, NowPlaying
    from mello.ui.renderer import Renderer
    from mello.ui.context import RenderContext

    pygame.display.init(); pygame.font.init()
    SCREEN = pygame.display.set_mode((720, 1280), 0, 32)

    class Covers:
        def get(self, image, size=410):
            return pygame.Surface((size, size))
        get_dimmed = get

    ICONS = {n: pygame.Surface((40, 40), pygame.SRCALPHA) for n in
             ('play', 'pause', 'prev', 'next', 'volume_none', 'volume_low', 'volume_high')}
    ITEMS = [CatalogItem(id=str(i), uri=f'spotify:album:{i}', name=f'Album {i}', image=None) for i in range(4)]
    ITEMS[2].current_track = {'name': 'Liedje', 'artist': 'Iemand', 'position': 1000, 'duration': 9000}

    def ctx(i, scroll):
        return RenderContext(items=ITEMS, selected_index=i, now_playing=NowPlaying(), scroll_x=scroll,
                             drag_offset=0, dragging=False, is_sleeping=False, volume_index=1,
                             delete_mode_id=None, pressed_button=None, is_loading=False, is_playing=False)

    def test_no_title_no_full_redraw_every_frame():
        r = Renderer(SCREEN, Covers(), ICONS)
        assert r.draw(ctx(1, 1.0)) is None          # first frame: everything
        assert r.draw(ctx(1, 1.2)) == [r._carousel_rect]   # swiping: covers only
        assert r.draw(ctx(1, 1.0)) is not None      # settled again: no full redraw either

    def test_title_change_still_redraws():
        r = Renderer(SCREEN, Covers(), ICONS)
        r.draw(ctx(2, 2.0))
        assert r.draw(ctx(1, 1.0)) is None          # other album: title goes away
        assert r.draw(ctx(2, 2.0)) is None          # and comes back
''')


def test_renderer_redraw(tmp_path):
    pytest.importorskip('pygame')
    script = tmp_path / 'test_redraw.py'
    script.write_text(CHECKS)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(script)],
        cwd=ROOT, capture_output=True, text=True,
        env={'PYTHONPATH': str(ROOT), 'SDL_VIDEODRIVER': 'dummy', 'PATH': '/usr/bin:/bin'},
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert '2 passed' in result.stdout, result.stdout[-2000:]

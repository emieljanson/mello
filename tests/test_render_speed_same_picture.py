"""The faster cover and progress-bar drawing must give the same picture.

Other test modules replace pygame with a stub, so the pixel checks run in a
fresh interpreter with the real pygame.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

CHECKS = textwrap.dedent('''
import os

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

import pygame
import pytest

from mello.config import COLORS, PROGRESS_BAR_WIDTH
from mello.ui.renderer import Renderer


@pytest.fixture(scope='module')
def screen():
    pygame.display.init()
    surface = pygame.display.set_mode((720, 1280), 0, 32)
    yield surface
    pygame.display.quit()


def _old_progress(screen, cover_rect, progress):
    """The previous implementation: a cover-sized layer every frame."""
    cover_x, cover_y, cover_w, cover_h = cover_rect
    fill_height = int(cover_h * min(progress, 1.0))
    radius = max(12, cover_w // 25)
    mask = pygame.Surface((cover_w, cover_h), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), (0, 0, cover_w, cover_h), border_radius=radius)
    surf = pygame.Surface((cover_w, cover_h), pygame.SRCALPHA)
    pygame.draw.rect(surf, COLORS['accent'], (0, 0, PROGRESS_BAR_WIDTH, fill_height))
    surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    screen.blit(surf, (cover_x, cover_y))


@pytest.mark.parametrize('progress', [0.01, 0.02, 0.5, 0.97, 1.0])
def test_progress_strip_matches_old_drawing(screen, progress):
    rect = (185, 435, 410, 410)
    screen.fill((40, 40, 60))
    _old_progress(screen, rect, progress)
    expected = pygame.image.tobytes(screen, 'RGB')

    screen.fill((40, 40, 60))
    renderer = Renderer.__new__(Renderer)
    renderer.screen = screen
    renderer._progress_cache = {}
    item = type('Item', (), {'uri': 'u', 'current_track': None})()
    now_playing = type('NP', (), {'context_uri': 'u', 'progress': progress})()
    renderer._draw_cover_progress(rect, item, now_playing)
    assert pygame.image.tobytes(screen, 'RGB') == expected


def test_rle_cover_blit_matches_plain_blit(screen, tmp_path):
    cover = pygame.Surface((410, 410), pygame.SRCALPHA)
    cover.fill((0, 0, 0, 0))
    pygame.draw.rect(cover, (200, 120, 30, 255), (0, 0, 410, 410), border_radius=16)
    pygame.draw.circle(cover, (10, 200, 90, 255), (200, 200), 120)
    for x in range(256):  # semi-transparent edge pixels, like anti-aliased corners
        cover.set_at((x + 50, 0), (250, 30, 30, x))
    cover = cover.convert_alpha()

    screen.fill((40, 40, 60))
    screen.blit(cover, (185, 435))
    expected = pygame.image.tobytes(screen, 'RGB')

    rle = cover.copy()
    rle.set_alpha(255, pygame.RLEACCEL)
    screen.fill((40, 40, 60))
    screen.blit(rle, (185, 435))
    actual = pygame.image.tobytes(screen, 'RGB')
    # Opaque pixels are copied exactly; SDL's run-length blender may round
    # half-transparent edge pixels one step (of 255) differently.
    assert max(abs(a - b) for a, b in zip(actual, expected)) <= 1

''')


def test_faster_drawing_gives_the_same_picture(tmp_path):
    pytest.importorskip('pygame')
    script = tmp_path / 'test_pixels.py'
    script.write_text(CHECKS)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(script)],
        cwd=ROOT, capture_output=True, text=True,
        env={'PYTHONPATH': str(ROOT), 'SDL_VIDEODRIVER': 'dummy', 'PATH': '/usr/bin:/bin'},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '6 passed' in result.stdout, result.stdout

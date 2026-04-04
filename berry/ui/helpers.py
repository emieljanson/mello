"""
UI Helpers - Drawing utilities for pygame.
"""
import pygame
import pygame.gfxdraw


def draw_aa_circle(surface: pygame.Surface, color: tuple, center: tuple, radius: int):
    """Draw an anti-aliased filled circle."""
    cx, cy = int(center[0]), int(center[1])
    r = int(radius)
    pygame.gfxdraw.aacircle(surface, cx, cy, r, color)
    pygame.gfxdraw.filled_circle(surface, cx, cy, r, color)

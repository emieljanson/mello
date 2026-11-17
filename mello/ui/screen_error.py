"""
Screen error - a plain message on the screen when Slint cannot start.

Slint draws Mello's screen. When it cannot load (it needs Raspberry Pi OS
Trixie or newer: Python 3.12+), there is nothing to draw with, and the speaker
would show a black screen. This writes a short message straight into the
display's framebuffer (/dev/fb0) with Pillow instead, so whoever looks at the
speaker knows what is wrong.
"""
import logging
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw

from ..config import COLORS, SCREEN_WIDTH, SCREEN_HEIGHT
from .bitmaps import FONT_FILE, FONT_LARGE, FONT_MEDIUM

logger = logging.getLogger(__name__)

FB_DEVICE = Path('/dev/fb0')
FB_SYSFS = Path('/sys/class/graphics/fb0')


def message_image(lines: List[str]) -> Image.Image:
    """The message as the panel shows it (portrait, text turned for the sideways mount)."""
    from PIL import ImageFont
    view = Image.new('RGB', (SCREEN_HEIGHT, SCREEN_WIDTH), COLORS['bg_primary'])   # what the user sees
    draw = ImageDraw.Draw(view)
    y = SCREEN_WIDTH // 2 - 20 * len(lines)
    for index, text in enumerate(lines):
        size = FONT_LARGE if index == 0 else FONT_MEDIUM
        font = ImageFont.truetype(str(FONT_FILE), size)
        color = COLORS['text_primary'] if index == 0 else COLORS['text_secondary']
        width = font.getlength(text)
        draw.text(((SCREEN_HEIGHT - width) / 2, y), text, font=font, fill=color)
        y += 50 if index == 0 else 36
    return view.rotate(-90, expand=True)


def _to_framebuffer_bytes(image: Image.Image, bits: int, stride: int) -> bytes:
    import numpy as np
    width, height = image.size
    rgb = np.asarray(image.convert('RGB'), dtype=np.uint16)
    if bits == 16:
        pixels = ((rgb[..., 0] >> 3) << 11) | ((rgb[..., 1] >> 2) << 5) | (rgb[..., 2] >> 3)
        rows = pixels.astype('<u2').view(np.uint8).reshape(height, width * 2)
    else:
        bgra = np.empty((height, width, 4), np.uint8)
        bgra[..., 0], bgra[..., 1], bgra[..., 2], bgra[..., 3] = rgb[..., 2], rgb[..., 1], rgb[..., 0], 255
        rows = bgra.reshape(height, width * 4)
    out = np.zeros((height, stride), np.uint8)
    out[:, :rows.shape[1]] = rows
    return out.tobytes()


def show(lines: List[str]) -> bool:
    """Put the message on the screen. True when it worked."""
    try:
        bits = int((FB_SYSFS / 'bits_per_pixel').read_text())
        width, height = (int(v) for v in (FB_SYSFS / 'virtual_size').read_text().split(','))
        stride = int((FB_SYSFS / 'stride').read_text())
        image = message_image(lines)
        if image.size != (width, height):
            image = image.resize((width, height))
        data = _to_framebuffer_bytes(image, bits, stride)
        with open(FB_DEVICE, 'wb') as fb:
            fb.write(data)
        return True
    except Exception as e:
        logger.warning(f'Could not show the message on screen: {e}')
        return False

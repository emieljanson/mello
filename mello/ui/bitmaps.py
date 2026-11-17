"""
Pictures and text sizes for the screen, made with Pillow.

Icons, the logo, QR codes and the grey cover placeholder are prepared here
once (scaled, tinted or turned for the sideways panel) and then drawn by Slint.
Text is drawn by Slint too, in the same font; its size is measured here so the
renderer can size buttons to their label and know where a touch lands.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

from ..config import COLORS

logger = logging.getLogger(__name__)

FONT_FILE = Path(__file__).with_name('fonts') / 'FreeSansBold.ttf'

# Text sizes in pixels. (The same as the old Pygame screen: it asked for
# 42/32/24 and drew its built-in font at 0.6875 of that.)
FONT_LARGE = 28
FONT_MEDIUM = 22
FONT_SMALL = 16

ICON_FILES = {
    'play': 'play-fill.png',
    'pause': 'pause-fill.png',
    'prev': 'skip-back-fill.png',
    'next': 'skip-forward-fill.png',
    'volume_none': 'speaker-none-fill.png',
    'volume_low': 'speaker-low-fill.png',
    'volume_high': 'speaker-high-fill.png',
    'plus': 'plus-circle-fill.png',
    'minus': 'minus-circle-fill.png',
    'headphone': 'headphone.png',
    'close': 'close.png',
    'back': 'back.png',
    'logo': 'mello-logo.png',
}

QR_SIZE = 210       # edge of the white square, including its margin
QR_MARGIN = 16


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_FILE), size)


def text_size(text: str, size: int) -> Tuple[int, int]:
    """Width and line height of one line of text, before it is turned."""
    font = _font(size)
    ascent, descent = font.getmetrics()
    return int(round(font.getlength(text))), ascent + descent


class Bitmaps:
    """Prepares the screen's pictures. A picture is named by a tuple:

    ('icon', name)                  as the file is (icons are drawn pre-turned)
    ('icon', name, size)            scaled to size x size
    ('icon', name, size, rgb)       ... and coloured
    ('logo', width)                 the Mello logo, scaled and turned
    ('qr', matrix)                  a code to scan on a white rounded square
    ('placeholder', size)           grey rounded square for a missing cover
    """

    def __init__(self, icons_dir: Path):
        self.icons_dir = Path(icons_dir)
        self._images: Dict[tuple, Image.Image] = {}

    def image(self, key: tuple) -> Image.Image:
        image = self._images.get(key)
        if image is None:
            image = self._make(key)
            # QR codes change with every new link: keep only the latest few.
            if key[0] == 'qr':
                for old in [k for k in self._images if k[0] == 'qr'][:-2]:
                    del self._images[old]
            self._images[key] = image
        return image

    def size(self, key: tuple) -> Tuple[int, int]:
        return self.image(key).size

    def _icon_file(self, name: str) -> Image.Image:
        try:
            return Image.open(self.icons_dir / ICON_FILES[name]).convert('RGBA')
        except Exception as e:
            logger.warning(f'Failed to load icon {name}: {e}')
            return Image.new('RGBA', (1, 1))

    def _make(self, key: tuple) -> Image.Image:
        kind = key[0]
        if kind == 'icon':
            image = self._icon_file(key[1])
            if len(key) > 2:
                image = image.resize((key[2], key[2]), Image.LANCZOS)
            if len(key) > 3:
                # Multiply, like Pygame's BLEND_RGB_MULT: white becomes the colour.
                r, g, b, a = image.split()
                tint = key[3]
                r, g, b = (band.point(lambda v, t=t: v * t // 255) for band, t in zip((r, g, b), tint))
                image = Image.merge('RGBA', (r, g, b, a))
            return image
        if kind == 'logo':
            image = self._icon_file('logo')
            width = key[1]
            height = int(image.height * width / image.width)
            # Turned a quarter to the right, like all text on the sideways panel.
            return image.resize((width, height), Image.LANCZOS).rotate(-90, expand=True)
        if kind == 'qr':
            return _qr_image(key[1])
        if kind == 'placeholder':
            size = key[1]
            image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(image).rounded_rectangle(
                (0, 0, size - 1, size - 1), radius=max(12, size // 25), fill=COLORS['bg_elevated'])
            return image
        raise ValueError(f'unknown picture {key!r}')


def _qr_image(matrix) -> Image.Image:
    """A QR code on a white rounded square. A QR code reads the same in any
    orientation, so it is not turned (rows run along the panel's x)."""
    size, margin = QR_SIZE, QR_MARGIN
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=(255, 255, 255, 255))
    module = max(1, (size - 2 * margin) // len(matrix))
    origin = (size - module * len(matrix)) // 2
    for row_index, row in enumerate(matrix):
        for col_index, dark in enumerate(row):
            if dark:
                x = origin + row_index * module
                y = origin + col_index * module
                draw.rectangle((x, y, x + module - 1, y + module - 1), fill=(0, 0, 0, 255))
    return image

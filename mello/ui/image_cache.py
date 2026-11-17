"""
Image Cache - finds the album cover files to show.

Images are stored on disk in 4 pre-scaled, pre-turned variants (made by
catalog.py when a cover is downloaded):
- {hash}.png           - 410px normal
- {hash}_small.png     - 307px normal
- {hash}_dim.png       - 410px dimmed
- {hash}_small_dim.png - 307px dimmed

Slint loads them straight from disk (ui/slint_display.py).
"""
import logging
from pathlib import Path
from typing import Optional

from ..config import COVER_SIZE_SMALL

logger = logging.getLogger(__name__)


class ImageCache:
    """Knows which file holds which cover variant."""

    def __init__(self, images_dir: Path):
        self.images_dir = images_dir
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def variant_path(self, image_path: str, size: int, dimmed: bool = False) -> Optional[Path]:
        """The file with this cover at this size, or None when it is missing."""
        if not image_path or not image_path.startswith('/images/'):
            return None

        filename = image_path.replace('/images/', '')
        base = filename.replace('.png', '').replace('.jpg', '')

        if size == COVER_SIZE_SMALL:
            suffix = '_small_dim' if dimmed else '_small'
        else:
            suffix = '_dim' if dimmed else ''

        variant_path = self.images_dir / f'{base}{suffix}.png'
        if variant_path.exists():
            return variant_path

        logger.warning(f'Image not found: {base}{suffix}.png')
        return None

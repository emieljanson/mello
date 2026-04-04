#!/usr/bin/env python3
"""
Rotate assets 90° CW for portrait display mode.

Usage:
    python -m berry.scripts.rotate_assets --icons    # Rotate icons only
    python -m berry.scripts.rotate_assets --images   # Rotate cover images only
    python -m berry.scripts.rotate_assets --all      # Rotate everything
"""
import sys
from pathlib import Path

# Add parent to path for imports when run directly
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from PIL import Image


def rotate_directory(directory: Path, name: str) -> tuple[int, int]:
    """Rotate all PNG files in a directory 90° CW. Returns (rotated, skipped)."""
    if not directory.exists():
        print(f"Directory not found: {directory}")
        return 0, 0
    
    png_files = list(directory.glob('*.png'))
    print(f"Found {len(png_files)} {name} to rotate")
    
    rotated = 0
    skipped = 0
    
    for path in png_files:
        try:
            img = Image.open(path)
            rotated_img = img.transpose(Image.Transpose.ROTATE_270)
            rotated_img.save(path, 'PNG')
            rotated += 1
            print(f"  ✓ {path.name}")
        except Exception as e:
            print(f"  ✗ {path.name}: {e}")
            skipped += 1
    
    return rotated, skipped


def main():
    root = Path(__file__).parent.parent.parent
    icons_dir = root / 'icons'
    images_dir = root / 'data' / 'images'
    
    do_icons = '--icons' in sys.argv or '--all' in sys.argv
    do_images = '--images' in sys.argv or '--all' in sys.argv
    
    if not do_icons and not do_images:
        print("Usage: python -m berry.scripts.rotate_assets [--icons] [--images] [--all]")
        print("\nOptions:")
        print("  --icons   Rotate icons in /icons")
        print("  --images  Rotate cover images in /data/images")
        print("  --all     Rotate everything")
        sys.exit(1)
    
    total_rotated = 0
    total_skipped = 0
    
    if do_icons:
        print("\n📁 Icons:")
        r, s = rotate_directory(icons_dir, "icons")
        total_rotated += r
        total_skipped += s
    
    if do_images:
        print("\n📁 Cover images:")
        r, s = rotate_directory(images_dir, "images")
        total_rotated += r
        total_skipped += s
    
    print(f"\n✓ Done! Rotated {total_rotated}, skipped {total_skipped}")


if __name__ == '__main__':
    main()


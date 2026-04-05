"""
Catalog Manager - Unified catalog operations.

Handles:
- Loading/saving catalog items
- Image download and deduplication  
- Playlist cover collection
- Progress tracking for resume
"""
import json
import time
import hashlib
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Callable
from io import BytesIO

import requests
from PIL import Image, ImageDraw

from ..models import CatalogItem
from ..config import PROGRESS_EXPIRY_HOURS, COVER_SIZE, COVER_SIZE_SMALL

logger = logging.getLogger(__name__)


def apply_rounded_corners_pil(img: Image.Image, radius: int) -> Image.Image:
    """Apply rounded corners to a PIL image with transparency."""
    size = img.size[0]
    mask = Image.new('L', (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=radius, fill=255)
    result = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def apply_dimming(img: Image.Image, alpha: int = 115) -> Image.Image:
    """Apply dark overlay to image (45% dimming by default)."""
    overlay = Image.new('RGBA', img.size, (0, 0, 0, alpha))
    return Image.alpha_composite(img, overlay)


class CatalogManager:
    """
    Unified catalog manager for albums and playlists.
    
    Handles save/delete, image dedup, progress tracking, and playlist covers.
    """
    
    def __init__(self, catalog_path: Path, images_path: Path, mock_mode: bool = False,
                 progress_path: Optional[Path] = None,
                 get_progress_expiry: Optional[Callable] = None):
        self.catalog_path = catalog_path
        self.images_path = images_path
        self.progress_path = progress_path or catalog_path.parent / 'progress.json'
        self.mock_mode = mock_mode
        self._get_progress_expiry = get_progress_expiry or (lambda: PROGRESS_EXPIRY_HOURS)
        
        # Thread locks for file operations
        self._catalog_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        
        # Ensure images directory exists
        self.images_path.mkdir(parents=True, exist_ok=True)
        
        # Hash -> local_path for deduplication
        self.image_hashes: Dict[str, str] = {}
        
        # Playlist covers collection: {context_uri: {hash: local_path}}
        self.playlist_covers: Dict[str, Dict[str, str]] = {}
        
        # Track tried URLs to avoid repeated downloads (with max size to prevent memory growth)
        self._tried_cover_urls: set = set()
        self._max_tried_urls = 500
        
        # Cached items
        self._items: List[CatalogItem] = []
        
        # Index existing images on startup
        self._index_existing_images()
    
    # ============================================
    # LOADING & SAVING
    # ============================================
    
    def _image_exists(self, image_path: Optional[str]) -> bool:
        """Check if an image file exists on disk."""
        if not image_path:
            return False
        # image_path format: "/images/abc12345.png"
        if image_path.startswith('/images/'):
            filename = image_path[8:]  # Remove "/images/" prefix
            return (self.images_path / filename).exists()
        return False
    
    def load(self) -> List[CatalogItem]:
        """Load catalog items from disk."""
        if self.mock_mode:
            self._items = self._load_mock_data()
            return self._items

        # Check for leftover temp file from crashed save
        temp_path = self.catalog_path.with_suffix('.json.tmp')
        if temp_path.exists():
            logger.warning(f'Found leftover temp file from crashed save: {temp_path}')
            try:
                # Try to recover - if temp file is valid JSON, use it
                temp_data = json.loads(temp_path.read_text())
                if isinstance(temp_data, dict) and 'items' in temp_data:
                    logger.info('Recovering from temp file...')
                    import os
                    os.replace(temp_path, self.catalog_path)
                    logger.info('Recovery successful')
                else:
                    logger.warning('Temp file invalid, removing')
                    temp_path.unlink()
            except (json.JSONDecodeError, IOError, OSError) as e:
                logger.warning(f'Could not recover from temp file: {e}')
                temp_path.unlink()

        try:
            logger.info(f'Loading catalog from {self.catalog_path}')
            if self.catalog_path.exists():
                data = json.loads(self.catalog_path.read_text())
                items_data = data.get('items', []) if isinstance(data, dict) else []
                self._items = []
                for item in items_data:
                    if not isinstance(item, dict) or item.get('type') == 'track':
                        continue
                    
                    # Check if image file exists, clear if not
                    image_path = item.get('image')
                    if image_path and not self._image_exists(image_path):
                        logger.warning(f'Image missing for {item.get("name")}: {image_path}')
                        image_path = None
                    
                    self._items.append(CatalogItem(
                        id=item.get('id', ''),
                        uri=item.get('uri', ''),
                        name=item.get('name', ''),
                        type=item.get('type', 'album'),
                        artist=item.get('artist'),
                        image=image_path,
                        images=item.get('images'),
                    ))
                self._populate_current_tracks()
                logger.info(f'Loaded {len(self._items)} items')
            else:
                logger.warning(f'Catalog not found at {self.catalog_path}')
                self._items = []
        except json.JSONDecodeError as e:
            logger.error(f'Invalid JSON in catalog file: {e}', exc_info=True)
            self._items = []
        except (IOError, OSError) as e:
            logger.error(f'Cannot read catalog file: {e}', exc_info=True)
            self._items = []
        except Exception as e:
            logger.error(f'Unexpected error loading catalog: {e}', exc_info=True)
            self._items = []
        
        return self._items
    
    @property
    def items(self) -> List[CatalogItem]:
        """Get cached catalog items."""
        return self._items
    
    def _load_raw(self) -> dict:
        """Load raw catalog.json (thread-safe)."""
        with self._catalog_lock:
            try:
                if self.catalog_path.exists():
                    return json.loads(self.catalog_path.read_text())
                return {'items': []}
            except json.JSONDecodeError as e:
                logger.warning(f'Invalid JSON in catalog: {e}')
                return {'items': []}
            except (IOError, OSError) as e:
                logger.error(f'Cannot read catalog file: {e}', exc_info=True)
                return {'items': []}
            except Exception as e:
                logger.error(f'Unexpected error loading catalog: {e}', exc_info=True)
                return {'items': []}
    
    def _save_raw(self, catalog: dict):
        """Save raw catalog.json atomically (thread-safe).

        Uses temp file + atomic rename to prevent corruption on crash.
        """
        with self._catalog_lock:
            temp_path = self.catalog_path.with_suffix('.json.tmp')
            try:
                # Write to temp file
                temp_path.write_text(json.dumps(catalog, indent=2))
                # Atomic rename (os.replace is atomic on POSIX)
                import os
                os.replace(temp_path, self.catalog_path)
            except Exception:
                # Clean up temp file on error
                if temp_path.exists():
                    temp_path.unlink()
                raise
    
    def _load_mock_data(self) -> List[CatalogItem]:
        """Load mock data for UI testing."""
        return [
            CatalogItem(
                id='1', uri='spotify:album:mock1',
                name='Abbey Road', type='album',
                artist='The Beatles',
                image='https://i.scdn.co/image/ab67616d0000b273dc30583ba717007b00cceb25',
                current_track={'name': 'Come Together', 'artist': 'The Beatles'}
            ),
            CatalogItem(
                id='2', uri='spotify:album:mock2',
                name='Dark Side of the Moon', type='album',
                artist='Pink Floyd',
                image='https://i.scdn.co/image/ab67616d0000b273ea7caaff71dea1051d49b2fe',
            ),
            CatalogItem(
                id='3', uri='spotify:album:mock3',
                name='Rumours', type='album',
                artist='Fleetwood Mac',
                image='https://i.scdn.co/image/ab67616d0000b273e52a59a28efa4773dd2bfe1b',
            ),
            CatalogItem(
                id='4', uri='spotify:album:mock4',
                name='Back in Black', type='album',
                artist='AC/DC',
                image='https://i.scdn.co/image/ab67616d0000b2734809adfae9bd679cffadd3a3',
            ),
            CatalogItem(
                id='5', uri='spotify:album:mock5',
                name='Thriller', type='album',
                artist='Michael Jackson',
                image='https://i.scdn.co/image/ab67616d0000b27334bfb69e00898660fc3c3ab3',
            ),
        ]
    
    # ============================================
    # IMAGE HANDLING
    # ============================================
    
    def _index_existing_images(self):
        """Index existing images by extracting hash from filename.
        
        Handles both old format (timestamp-hash.png) and new format (hash.png).
        Only indexes the main variant (not _small, _dim variants).
        """
        try:
            for file in self.images_path.iterdir():
                if file.suffix not in ('.jpg', '.png'):
                    continue
                
                # Skip variant files (only index main files)
                if '_small' in file.name or '_dim' in file.name:
                    continue
                
                # Extract hash from filename
                # New format: "abc12345.png" or "abc12345_composite.png"
                # Old format: "1767089701460-6aa1f146.png"
                name = file.stem  # Without extension
                
                # Handle composite images
                if '_composite' in name:
                    # abc12345_composite -> abc12345
                    hash_part = name.replace('_composite', '')
                elif '-' in name:
                    # Old format: timestamp-hash -> hash
                    hash_part = name.split('-')[-1]
                else:
                    # New format: hash directly
                    hash_part = name
                
                # Remove temp_ prefix if present
                if hash_part.startswith('temp_'):
                    hash_part = hash_part[5:]
                
                if len(hash_part) == 8:  # Valid 8-char hash
                    self.image_hashes[hash_part] = f'/images/{file.name}'
            
            logger.info(f'Indexed {len(self.image_hashes)} images')
        except (IOError, OSError) as e:
            logger.warning(f'Error indexing images: {e}', exc_info=True)
        except Exception as e:
            logger.warning(f'Unexpected error indexing images: {e}', exc_info=True)
    
    def _download_and_hash_image(self, image_url: str) -> tuple:
        """Download image and return (hash, PIL Image).
        
        Returns the raw RGBA image without resizing - variants are generated at save time.
        """
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()
        buffer = response.content
        hash_full = hashlib.md5(buffer).hexdigest()
        hash_short = hash_full[:8]  # Use first 8 chars like backend
        
        # Load as RGBA but don't resize - variants generated at save time
        img = Image.open(BytesIO(buffer)).convert('RGBA')
        
        return (hash_short, img)
    
    def _save_image(self, hash_short: str, img: Image.Image, temp: bool = False) -> str:
        """Save all image variants (4 files) and return base local path.
        
        Generates 4 variants for fast runtime loading:
        - {hash}.png          - 410px normal (pre-rotated 90° CW)
        - {hash}_small.png    - 307px normal (pre-rotated 90° CW)
        - {hash}_dim.png      - 410px dimmed (pre-rotated 90° CW)
        - {hash}_small_dim.png - 307px dimmed (pre-rotated 90° CW)
        
        Images are pre-rotated for portrait display mode to avoid
        runtime rotation overhead.
        """
        # Check if already exists
        if hash_short in self.image_hashes:
            return self.image_hashes[hash_short]
        
        # Rotate 90° CW for portrait display mode (done once at save time)
        img = img.transpose(Image.Transpose.ROTATE_270)
        
        prefix = 'temp_' if temp else ''
        base_name = f'{prefix}{hash_short}'
        
        # Generate all 4 variants
        sizes = [
            (COVER_SIZE, ''),            # 410px, no suffix
            (COVER_SIZE_SMALL, '_small') # 307px
        ]
        
        for size, suffix in sizes:
            # Resize to target size
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            
            # Apply rounded corners
            radius = max(12, size // 25)
            processed = apply_rounded_corners_pil(resized, radius)
            
            # Save normal version
            filename = f'{base_name}{suffix}.png'
            processed.save(self.images_path / filename, 'PNG')
            
            # Save dimmed version
            dimmed = apply_dimming(processed)
            dimmed.save(self.images_path / f'{base_name}{suffix}_dim.png', 'PNG')
        
        # Return path to main variant (410px normal)
        local_path = f'/images/{base_name}.png'
        self.image_hashes[hash_short] = local_path
        logger.info(f'Saved {"temp " if temp else ""}image variants: {local_path} (4 files)')
        return local_path
    
    def download_temp_image(self, image_url: str) -> Optional[str]:
        """Download and process image temporarily for temp items.
        
        Returns local path to processed image, or None on error.
        """
        if not image_url or not image_url.startswith('http'):
            return None
        
        try:
            hash_short, img = self._download_and_hash_image(image_url)
            local_path = self._save_image(hash_short, img, temp=True)
            return local_path
        except requests.RequestException as e:
            logger.debug(f'Error downloading temp image: {e}')
            return None
        except Exception as e:
            logger.warning(f'Unexpected error downloading temp image: {e}', exc_info=True)
            return None
    
    # ============================================
    # PLAYLIST COVER COLLECTION
    # ============================================
    
    def collect_cover_for_playlist(self, context_uri: str, cover_url: str) -> bool:
        """Collect album cover URL for playlist composite (max 4 unique).
        
        Stores URLs for later composite creation. Returns True if a new URL was added.
        """
        if 'playlist' not in context_uri or not cover_url:
            return False
        
        if context_uri not in self.playlist_covers:
            self.playlist_covers[context_uri] = {}
        
        covers = self.playlist_covers[context_uri]
        if len(covers) >= 4:
            return False  # Already have 4 covers
        
        # Skip if we've already tried this URL recently
        url_key = f'{context_uri}:{cover_url}'
        if url_key in self._tried_cover_urls:
            return False
        
        # Cleanup if cache is too large (prevent memory growth)
        if len(self._tried_cover_urls) > self._max_tried_urls:
            logger.debug(f'Clearing tried URLs cache ({len(self._tried_cover_urls)} entries)')
            self._tried_cover_urls.clear()
        
        self._tried_cover_urls.add(url_key)
        
        try:
            # Download to get hash for deduplication
            response = requests.get(cover_url, timeout=10)
            response.raise_for_status()
            buffer = response.content
            hash_full = hashlib.md5(buffer).hexdigest()
            hash_short = hash_full[:8]
            
            # Skip if already have this hash for this context
            if hash_short in covers:
                logger.debug(f'Cover already collected (same album): {len(covers)}/4')
                return False
            
            # Store URL and buffer for later composite creation
            covers[hash_short] = {'url': cover_url, 'buffer': buffer}
            logger.info(f'Collected cover {len(covers)}/4 for playlist')
            
            # Create composite if we have enough covers
            if len(covers) >= 4:
                self._update_playlist_covers_if_needed(context_uri)
            
            return True
            
        except requests.RequestException as e:
            logger.debug(f'Error downloading cover image: {e}')
            return False
        except Exception as e:
            logger.warning(f'Error collecting cover: {e}', exc_info=True)
            return False
    
    def _create_composite_from_collected(self, context_uri: str) -> Optional[str]:
        """Create composite image from collected covers and save all variants to disk.
        
        Generates 4 variants like regular images for fast runtime loading.
        """
        if context_uri not in self.playlist_covers:
            return None
        
        covers = self.playlist_covers[context_uri]
        if not covers:
            return None
        
        try:
            # Get cover buffers
            cover_buffers = [c['buffer'] for c in covers.values()]
            
            # Pad to 4 by repeating
            while len(cover_buffers) < 4 and cover_buffers:
                cover_buffers.append(cover_buffers[len(cover_buffers) % len(covers)])
            
            # Generate hash from all buffers combined
            combined = b''.join(cover_buffers)
            hash_short = hashlib.md5(combined).hexdigest()[:8]
            
            # Check if already exists
            if hash_short in self.image_hashes:
                return self.image_hashes[hash_short]
            
            base_name = f'{hash_short}_composite'
            
            # Generate all 4 variants
            sizes = [
                (COVER_SIZE, ''),            # 410px
                (COVER_SIZE_SMALL, '_small') # 307px
            ]
            
            for size, suffix in sizes:
                half_size = size // 2
                composite = Image.new('RGBA', (size, size), (0, 0, 0, 0))
                positions = [(0, 0), (half_size, 0), (0, half_size), (half_size, half_size)]
                
                for i, (buffer, pos) in enumerate(zip(cover_buffers, positions)):
                    try:
                        img = Image.open(BytesIO(buffer)).convert('RGBA')
                        img = img.resize((half_size, half_size), Image.Resampling.LANCZOS)
                        composite.paste(img, pos)
                    except Exception as e:
                        logger.debug(f'Error processing cover {i}: {e}')
                        draw = ImageDraw.Draw(composite)
                        draw.rectangle([pos, (pos[0] + half_size, pos[1] + half_size)], fill=(40, 40, 40))
                
                # Apply rounded corners
                radius = max(12, size // 25)
                composite = apply_rounded_corners_pil(composite, radius)
                
                # Rotate 90° CW for portrait display mode (like regular covers)
                composite = composite.transpose(Image.Transpose.ROTATE_270)
                
                # Save normal version
                filename = f'{base_name}{suffix}.png'
                composite.save(self.images_path / filename, 'PNG')
                
                # Save dimmed version
                dimmed = apply_dimming(composite)
                dimmed.save(self.images_path / f'{base_name}{suffix}_dim.png', 'PNG')
            
            local_path = f'/images/{base_name}.png'
            self.image_hashes[hash_short] = local_path
            logger.info(f'Created composite image variants: {local_path} (4 files)')
            return local_path
            
        except Exception as e:
            logger.warning(f'Error creating composite: {e}', exc_info=True)
            return None
    
    def _update_playlist_covers_if_needed(self, context_uri: str):
        """Update saved playlist with composite when we have enough covers.
        
        Will update existing composites if new unique covers are collected.
        """
        covers = self.playlist_covers.get(context_uri, {})
        if len(covers) < 4:
            return  # Wait until we have 4 covers
        
        try:
            catalog = self._load_raw()
            item = next((i for i in catalog['items'] if i['uri'] == context_uri), None)
            
            if not item or item.get('type') != 'playlist':
                return
            
            # Create composite (returns existing path if same covers)
            composite_path = self._create_composite_from_collected(context_uri)
            if composite_path:
                current_image = item.get('image', '')
                # Only update if composite changed
                if composite_path != current_image:
                    item['image'] = composite_path
                    # Remove old images array if present
                    if 'images' in item:
                        del item['images']
                    self._save_raw(catalog)
                    logger.info(f'Updated playlist with new composite image')
                
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning(f'Error updating playlist covers: {e}', exc_info=True)
        except Exception as e:
            logger.warning(f'Unexpected error updating playlist covers: {e}', exc_info=True)
    
    def get_collected_covers(self, context_uri: str) -> Optional[List[str]]:
        """Get collected cover image paths for a playlist."""
        if context_uri in self.playlist_covers:
            return list(self.playlist_covers[context_uri].values())
        return None
    
    # ============================================
    # SAVE & DELETE
    # ============================================
    
    def save_item(self, item_data: dict) -> bool:
        """Save item to catalog with image download and deduplication."""
        if self.mock_mode:
            return True
        
        try:
            catalog = self._load_raw()
            
            # Check for duplicates
            uri = item_data.get('uri')
            if any(i['uri'] == uri for i in catalog['items']):
                logger.warning(f'Item already in catalog: {item_data.get("name")}')
                return False
            
            local_image = None
            image_url = item_data.get('image')
            
            # Check if we already have a temp image (from temp item) - rename to permanent
            if image_url and image_url.startswith('/images/'):
                image_filename = image_url.replace('/images/', '')
                if image_filename.startswith('temp_'):
                    # Extract hash from filename: temp_7b86d360.png -> 7b86d360
                    hash_short = image_filename.replace('temp_', '').replace('.png', '')
                    
                    # Rename all 4 variant files from temp to permanent
                    variants_renamed = 0
                    for suffix in ['', '_small', '_dim', '_small_dim']:
                        old_variant = self.images_path / f'temp_{hash_short}{suffix}.png'
                        new_variant = self.images_path / f'{hash_short}{suffix}.png'
                        if old_variant.exists():
                            old_variant.rename(new_variant)
                            variants_renamed += 1
                    
                    if variants_renamed > 0:
                        local_image = f'/images/{hash_short}.png'
                        self.image_hashes[hash_short] = local_image
                        logger.info(f'Renamed temp image to permanent: {local_image} ({variants_renamed} files)')
                else:
                    # Already permanent image, reuse it
                    local_image = image_url
            
            # For playlists: create composite from collected covers
            if not local_image and item_data.get('type') == 'playlist' and uri in self.playlist_covers:
                covers = self.playlist_covers[uri]
                if covers:
                    local_image = self._create_composite_from_collected(uri)
                    if local_image:
                        logger.info(f'Created composite from {len(covers)} collected covers')
            
            # Download single image if no composite or temp image (albums or playlists without collected covers)
            if not local_image and image_url and image_url.startswith('http'):
                try:
                    hash_short, img = self._download_and_hash_image(image_url)
                    local_image = self._save_image(hash_short, img)
                except requests.RequestException as e:
                    logger.warning(f'Error downloading image from {image_url[:50]}...: {e}')
                    local_image = image_url  # Fallback to URL
                except Exception as e:
                    logger.warning(f'Unexpected error downloading image: {e}', exc_info=True)
                    local_image = image_url  # Fallback to URL
            
            # Build new item (no images array, just single image)
            new_item = {
                'id': str(int(time.time() * 1000)),
                'type': item_data.get('type', 'album'),
                'uri': uri,
                'name': item_data.get('name'),
                'artist': item_data.get('artist'),
                'album': item_data.get('album'),
                'image': local_image or item_data.get('image'),
                'originalImage': item_data.get('image'),
                'addedAt': datetime.now().isoformat(),
            }
            
            catalog['items'].append(new_item)
            self._save_raw(catalog)
            logger.info(f'Saved to catalog: {new_item["name"]}')
            return True
            
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(f'Error saving to catalog: {e}', exc_info=True)
            return False
        except Exception as e:
            logger.error(f'Unexpected error saving to catalog: {e}', exc_info=True)
            return False
    
    def delete_item(self, item_id: str) -> bool:
        """Delete item from catalog."""
        if self.mock_mode:
            return True
        
        try:
            catalog = self._load_raw()
            
            index = next((i for i, item in enumerate(catalog['items']) 
                         if item['id'] == item_id), None)
            if index is None:
                logger.warning(f'Item not found: {item_id}')
                return False
            
            removed = catalog['items'].pop(index)
            self._save_raw(catalog)
            logger.info(f'Deleted from catalog: {removed.get("name")}')
            return True
            
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(f'Error deleting from catalog: {e}', exc_info=True)
            return False
        except Exception as e:
            logger.error(f'Unexpected error deleting from catalog: {e}', exc_info=True)
            return False
    
    # ============================================
    # PROGRESS TRACKING (stored in progress.json)
    # ============================================

    def _load_progress_data(self) -> dict:
        """Load progress.json (thread-safe). Returns {context_uri: {...}}."""
        with self._progress_lock:
            try:
                if self.progress_path.exists():
                    return json.loads(self.progress_path.read_text())
            except (json.JSONDecodeError, IOError, OSError) as e:
                logger.warning(f'Error reading progress file: {e}')
            return {}

    def _save_progress_data(self, data: dict):
        """Save progress.json atomically (thread-safe)."""
        with self._progress_lock:
            temp_path = self.progress_path.with_suffix('.json.tmp')
            try:
                temp_path.write_text(json.dumps(data, indent=2))
                import os
                os.replace(temp_path, self.progress_path)
            except Exception:
                if temp_path.exists():
                    temp_path.unlink()
                raise

    def _populate_current_tracks(self):
        """Populate in-memory items with progress data for UI display."""
        progress_data = self._load_progress_data()
        for item in self._items:
            entry = progress_data.get(item.uri)
            if entry:
                item.current_track = entry

    def save_progress(self, context_uri: str, track_uri: str,
                      position: int, track_name: str = None, artist: str = None):
        """Save playback progress to progress.json."""
        if self.mock_mode or not context_uri or not track_uri:
            return

        try:
            progress_data = self._load_progress_data()
            position = max(0, int(position or 0))
            existing = progress_data.get(context_uri) if isinstance(progress_data, dict) else None

            if isinstance(existing, dict) and existing.get('uri') == track_uri:
                existing_position = max(0, int(existing.get('position', 0) or 0))
                regressed = existing_position - position
                if regressed > 2000:
                    # Reject stale regressions (especially accidental 0s) for same track.
                    logger.info(
                        'progress_write_rejected | reason=position_regression | '
                        f'context_uri={context_uri[:40]} | track_uri={track_uri[:40]} | '
                        f'old_pos={existing_position // 1000}s | new_pos={position // 1000}s'
                    )
                    return

            entry = {
                'uri': track_uri,
                'position': position,
                'name': track_name,
                'artist': artist,
                'updatedAt': datetime.now().isoformat()
            }
            progress_data[context_uri] = entry
            self._save_progress_data(progress_data)

            for mem_item in self.items:
                if mem_item.uri == context_uri:
                    mem_item.current_track = entry
                    break

            logger.debug(f'Saved progress: {track_name} @ {position // 1000}s')

        except Exception as e:
            logger.warning(f'Error saving progress: {e}', exc_info=True)

    def get_progress(self, context_uri: str) -> Optional[dict]:
        """Get saved progress if not expired."""
        if self.mock_mode:
            return None

        try:
            progress_data = self._load_progress_data()
            entry = progress_data.get(context_uri)
            if not entry:
                return None

            updated_at = entry.get('updatedAt')
            if updated_at:
                updated = datetime.fromisoformat(updated_at)
                age_hours = (datetime.now() - updated).total_seconds() / 3600
                if age_hours > self._get_progress_expiry():
                    logger.debug(f'Progress expired ({age_hours:.1f}h old)')
                    self.clear_progress(context_uri)
                    return None

            logger.info(f'Resume: "{entry.get("name")}" @ {entry.get("position", 0) // 1000}s')
            return entry

        except Exception as e:
            logger.warning(f'Error getting progress: {e}', exc_info=True)
            return None

    def clear_progress(self, context_uri: str):
        """Clear saved progress for a context."""
        if self.mock_mode or not context_uri:
            return

        try:
            progress_data = self._load_progress_data()
            if context_uri in progress_data:
                del progress_data[context_uri]
                self._save_progress_data(progress_data)
                for mem_item in self.items:
                    if mem_item.uri == context_uri:
                        mem_item.current_track = None
                        break
                logger.debug(f'Cleared progress for: {context_uri[:40]}')

        except Exception as e:
            logger.warning(f'Error clearing progress: {e}', exc_info=True)

    def clear_all_progress(self):
        """Delete the progress file entirely (used by library reset)."""
        try:
            if self.progress_path.exists():
                self.progress_path.unlink()
                logger.info('All progress cleared')
        except Exception as e:
            logger.warning(f'Error clearing all progress: {e}', exc_info=True)
    
    # ============================================
    # CLEANUP
    # ============================================
    
    def cleanup_unused_images(self) -> int:
        """Delete images not referenced in catalog. Returns count deleted.
        
        Handles all 4 variants per image - if base image is used, keep all variants.
        """
        if self.mock_mode:
            return 0
        
        try:
            catalog = self._load_raw()
            
            # Collect base names of used images (without variants)
            # /images/abc12345.png -> abc12345
            # /images/abc12345_composite.png -> abc12345_composite
            used_bases = set()
            for item in catalog['items']:
                img_path = item.get('image') or ''
                if img_path.startswith('/images/'):
                    filename = img_path.replace('/images/', '')
                    # Extract base name (remove .png and any variant suffix)
                    base = filename.replace('.png', '')
                    # Remove variant suffixes to get true base
                    for suffix in ['_small_dim', '_small', '_dim']:
                        if base.endswith(suffix):
                            base = base[:-len(suffix)]
                            break
                    used_bases.add(base)
            
            # Find and delete unused (check if file's base is in used_bases)
            deleted = 0
            for file in self.images_path.iterdir():
                if file.suffix not in ('.jpg', '.png'):
                    continue
                
                # Extract base name from file
                base = file.stem
                for suffix in ['_small_dim', '_small', '_dim']:
                    if base.endswith(suffix):
                        base = base[:-len(suffix)]
                        break
                
                if base not in used_bases:
                    file.unlink()
                    deleted += 1
            
            # Rebuild hash index
            if deleted:
                self.image_hashes.clear()
                self._index_existing_images()
                logger.info(f'Cleanup: {deleted} unused image files deleted')
            
            return deleted
            
        except (IOError, OSError) as e:
            logger.warning(f'Error cleaning up images: {e}', exc_info=True)
            return 0
        except Exception as e:
            logger.warning(f'Unexpected error cleaning up images: {e}', exc_info=True)
            return 0


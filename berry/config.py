"""
Berry Configuration - All constants and settings.
"""
import os
import sys
import json
from pathlib import Path

# ============================================
# SCREEN & DISPLAY (Portrait mode - pre-rotated UI)
# ============================================

# Physical screen dimensions (portrait panel)
# User holds device with left side up to see landscape
SCREEN_WIDTH = 720
SCREEN_HEIGHT = 1280

# From user's perspective when holding landscape (left side up):
# - User's "horizontal" (left-right) = Physical Y (0-1280)
# - User's "vertical" (top-bottom) = Physical X (720-0, inverted)

# ============================================
# NETWORK ENDPOINTS
# ============================================

LIBRESPOT_URL = os.environ.get('LIBRESPOT_URL', 'http://localhost:3678')
LIBRESPOT_WS = os.environ.get('LIBRESPOT_WS', 'ws://localhost:3678/events')

# ============================================
# PATHS
# ============================================

# Use data folder (shared catalog & images)
DATA_DIR = Path(__file__).parent.parent / 'data'
CATALOG_PATH = DATA_DIR / 'catalog.json'
IMAGES_DIR = DATA_DIR / 'images'
ICONS_DIR = Path(__file__).parent.parent / 'icons'

# Logging directory
LOG_DIR = Path.home() / 'berry' / 'logs'
LOG_FILE = LOG_DIR / 'berry.log'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB per file
LOG_BACKUP_COUNT = 10  # Keep 10 backup files (~50MB total)

# ============================================
# COMMAND LINE FLAGS
# ============================================

MOCK_MODE = '--mock' in sys.argv or '-m' in sys.argv
FULLSCREEN = '--fullscreen' in sys.argv or '-f' in sys.argv
PROFILE_MODE = '--profile' in sys.argv or os.environ.get('BERRY_PROFILE') == '1'

# ============================================
# COLORS (Design specs from web version)
# ============================================

COLORS = {
    'bg_primary': (13, 13, 13),
    'bg_secondary': (26, 26, 26),
    'bg_elevated': (40, 40, 40),
    'accent': (189, 101, 252),  # Purple #BD65FC
    'accent_hover': (205, 130, 255),
    'text_primary': (255, 255, 255),
    'text_secondary': (160, 160, 160),
    'text_muted': (96, 96, 96),
    'success': (29, 185, 84),
    'error': (232, 80, 80),
}

# ============================================
# LAYOUT & SIZES (Portrait mode)
# ============================================
# 
# User holds device with left side up (landscape view).
# Physical portrait coordinates (720 x 1280) map to user's view:
# - Physical X (0-720) → User's vertical (bottom to top)
# - Physical Y (0-1280) → User's horizontal (left to right)
#
# Layout uses physical X for "vertical" positioning from user's POV:
# - Small X = user's bottom
# - Large X = user's top

# Cover sizes (same as before)
COVER_SIZE = 410
COVER_SIZE_SMALL = int(COVER_SIZE * 0.75)  # ~307
COVER_SPACING = 20

# Layout positions (physical X axis = user's vertical)
# X=0 is user's bottom, X=720 is user's top
# Layout: | 25px | Buttons | 50px | Cover 410px | 50px | TrackInfo | 25px |
TRACK_INFO_X = 675   # Center of track info text
CAROUSEL_X = 185     # Start of cover, centered between buttons and track info
CONTROLS_X = 85      # Center of play button (25px margin + 60px radius)

# For carousel center along physical Y (user's horizontal): Y = 640 (center of 1280)
CAROUSEL_CENTER_Y = 640
CAROUSEL_Y = CAROUSEL_CENTER_Y - COVER_SIZE // 2  # Top of carousel for touch detection
CONTROLS_Y = CAROUSEL_CENTER_Y  # Center of buttons (same as carousel center)

# Button sizes
BTN_SIZE = 100
PLAY_BTN_SIZE = 120

# Button spacing along physical Y (user's horizontal)
BTN_SPACING = (COVER_SIZE - BTN_SIZE) // 2  # 155px

# Progress bar (now vertical on physical screen)
PROGRESS_BAR_WIDTH = 8

# ============================================
# VOLUME
# ============================================

# Volume levels with separate speaker/headphone values
# Speaker needs higher gain, headphones need lower to avoid being too loud
VOLUME_LEVELS = [
    {'speaker': 88, 'headphone': 70, 'icon': 'volume_none'},
    {'speaker': 94, 'headphone': 60, 'icon': 'volume_low'},
    {'speaker': 98, 'headphone': 80, 'icon': 'volume_high'},
]

# ============================================
# TIMING
# ============================================

SLEEP_TIMEOUT = 120.0  # 2 minutes of inactivity
PLAY_TIMER_DELAY = 1.0  # seconds before auto-play
SYNC_COOLDOWN = 3.0  # Block sync for 3s after play timer fires
PROGRESS_SAVE_INTERVAL = 10  # Save progress every 10 seconds
PROGRESS_EXPIRY_HOURS = 24  # Expire saved progress after 24 hours

# ============================================
# TOUCH & GESTURES
# ============================================

SWIPE_THRESHOLD = 50      # Minimum distance for swipe
SWIPE_VELOCITY = 0.3      # Minimum velocity (pixels/ms)
LONG_PRESS_TIME = 1.0     # Time for long press (seconds)
ADMIN_HOLD_TIME = 10.0    # Time for admin menu hold (seconds)

# ============================================
# AUTO-PAUSE (prevents music playing forever)
# ============================================

AUTO_PAUSE_TIMEOUT = 30 * 60  # 30 minutes in seconds
AUTO_PAUSE_FADE_DURATION = 5.0  # Fade out over 5 seconds

# ============================================
# PERFORMANCE
# ============================================

PERF_LOG_INTERVAL = 5.0   # Log performance every 5 seconds
PERF_SAMPLE_SIZE = 60     # Average over 60 frames
IMAGE_CACHE_MAX_SIZE = 200  # Maximum cached images

# ============================================
# LIBRESPOT CONFIG
# ============================================

LIBRESPOT_CONFIG_DIR = Path(os.path.expanduser('~/.config/go-librespot'))
LIBRESPOT_STATE_FILE = LIBRESPOT_CONFIG_DIR / 'state.json'


def has_spotify_credentials() -> bool:
    """Check if Spotify credentials are configured in go-librespot."""
    if MOCK_MODE:
        return True
    
    if LIBRESPOT_STATE_FILE.exists():
        try:
            with open(LIBRESPOT_STATE_FILE) as f:
                state = json.load(f)
                return bool(state.get('credentials'))
        except (json.JSONDecodeError, IOError):
            pass
    return False


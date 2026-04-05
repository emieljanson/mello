"""
Mello Configuration - All constants and settings.
"""
import os
import sys
from pathlib import Path

# Load .env file (secrets stay out of git)
_env_path = Path(__file__).parent.parent / '.env'
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip())

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
PROGRESS_PATH = DATA_DIR / 'progress.json'
SETTINGS_PATH = DATA_DIR / 'settings.json'
IMAGES_DIR = DATA_DIR / 'images'
ICONS_DIR = Path(__file__).parent.parent / 'icons'
LIBRESPOT_STATE_PATH = Path.home() / '.config' / 'go-librespot' / 'state.json'

# Logging directory
LOG_DIR = Path.home() / 'mello' / 'logs'
LOG_FILE = LOG_DIR / 'mello.log'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB per file
LOG_BACKUP_COUNT = 10  # Keep 10 backup files (~50MB total)

# ============================================
# COMMAND LINE FLAGS
# ============================================

MOCK_MODE = '--mock' in sys.argv or '-m' in sys.argv
FULLSCREEN = '--fullscreen' in sys.argv or '-f' in sys.argv

# ============================================
# COLORS (Design specs from web version)
# ============================================

COLORS = {
    'bg_primary': (13, 13, 13),
    'bg_secondary': (26, 26, 26),
    'bg_elevated': (40, 40, 40),
    'accent': (189, 101, 252),  # Purple #BD65FC
    'text_primary': (255, 255, 255),
    'text_secondary': (160, 160, 160),
    'text_muted': (96, 96, 96),
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

# Default volume levels (speaker = ALSA speaker, bt = PipeWire bluetooth sink)
DEFAULT_VOLUME_LEVELS = [
    {'speaker': 88, 'bt': 20, 'icon': 'volume_none'},
    {'speaker': 94, 'bt': 40, 'icon': 'volume_low'},
    {'speaker': 98, 'bt': 65, 'icon': 'volume_high'},
]

# Valid ranges for volume adjustment (+/- 1% per tap)
VOLUME_RANGE = {'speaker': (50, 100), 'bt': (5, 100)}

# ============================================
# BLUETOOTH
# ============================================

# WM8960 PipeWire sink name (constant on this hardware)
WM8960_SINK = 'alsa_output.platform-soc_sound.stereo-fallback'

# How often to poll BT connection state (seconds)
BT_MONITOR_INTERVAL = 5.0

# How long to scan for new devices (seconds)
BT_SCAN_DURATION = 20.0

# ============================================
# TIMING
# ============================================

SLEEP_TIMEOUT = 120.0  # 2 minutes of inactivity
PLAY_TIMER_DELAY = 1.0  # seconds before auto-play
SYNC_COOLDOWN = 5.0  # Block sync for 5s after play timer fires
PROGRESS_SAVE_INTERVAL = 10  # Save progress every 10 seconds
PROGRESS_EXPIRY_HOURS = 96  # Expire saved progress after 96 hours
CONTEXT_SWITCH_WATCHDOG_TIMEOUT = 60.0  # Hard failsafe for stuck context-switch loading

# ============================================
# TOUCH & GESTURES
# ============================================

SWIPE_THRESHOLD = 50      # Minimum distance for swipe
SWIPE_VELOCITY = 0.3      # Minimum velocity (pixels/ms)
LONG_PRESS_TIME = 1.0     # Time for long press (seconds)
CAROUSEL_TOUCH_MARGIN = 50  # Extra pixels beyond cover for touch zone
MAX_SWIPE_JUMP = 5          # Max items to skip in one swipe
VELOCITY_THRESHOLDS = (1.0, 2.0, 3.5)  # Velocity breakpoints for swipe bonus
ACTION_DEBOUNCE = 0.3     # Seconds between button actions
BUTTON_PRESS_DURATION = 0.15  # Seconds to show pressed state
MENU_HOLD_TIME = 3.0      # Seconds to hold volume button to open setup menu

# ============================================
# AUTO-PAUSE (prevents music playing forever)
# ============================================

AUTO_PAUSE_TIMEOUT = 30 * 60  # 30 minutes in seconds
AUTO_PAUSE_FADE_DURATION = 5.0  # Fade out over 5 seconds

# ============================================
# ANALYTICS (PostHog)
# ============================================

# Shared write-only ingest key for anonymous usage data.
# PostHog ingest keys are write-only by design and safe to embed in client code.
# Users who run their own PostHog project can override via .env.
POSTHOG_SHARED_API_KEY = 'phc_REPLACE_WITH_YOUR_POSTHOG_INGEST_KEY'

POSTHOG_API_KEY = os.environ.get('POSTHOG_API_KEY', '') or POSTHOG_SHARED_API_KEY
POSTHOG_HOST = os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')
ANALYTICS_DISTINCT_ID = os.environ.get('ANALYTICS_DISTINCT_ID', '').strip()
ANALYTICS_INCLUDE_CONTENT = os.environ.get('ANALYTICS_INCLUDE_CONTENT', '0').lower() in ('1', 'true', 'yes')
ANALYTICS_USE_MACHINE_ID = os.environ.get('ANALYTICS_USE_MACHINE_ID', '0').lower() in ('1', 'true', 'yes')

# ============================================
# PERFORMANCE
# ============================================

PERF_LOG_INTERVAL = 5.0   # Log performance every 5 seconds
PERF_SAMPLE_SIZE = 60     # Average over 60 frames
IMAGE_CACHE_MAX_SIZE = 200  # Maximum cached images



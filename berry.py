#!/usr/bin/env python3
"""
🍓 Berry Native - Pygame UI for Raspberry Pi

This file is a backwards-compatible wrapper.
The actual implementation is now in the berry/ package.

Usage:
    python berry.py              # Windowed (development)
    python berry.py --fullscreen # Fullscreen (Pi)
    python berry.py --mock       # Mock mode (UI testing)
"""
from berry.main import main

if __name__ == '__main__':
    main()

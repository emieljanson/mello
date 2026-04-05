#!/usr/bin/env python3
"""
Mello - Pygame UI for Raspberry Pi

This file is a backwards-compatible wrapper.
The actual implementation is now in the mello/ package.

Usage:
    python mello.py              # Windowed (development)
    python mello.py --fullscreen # Fullscreen (Pi)
    python mello.py --mock       # Mock mode (UI testing)
"""
from mello.main import main

if __name__ == '__main__':
    main()

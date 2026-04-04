# Berry

A simple, screen-based music player for kids — built on a Raspberry Pi with a touchscreen.

Kids swipe through album covers and tap to play. Parents control the music library from Spotify on their phone.

<!-- TODO: Add photo of Berry device here -->

## Features

- **Spotify Connect** — Add albums and playlists from your Spotify app, Berry plays them
- **Album carousel** — Large cover art with smooth swipe navigation
- **Simple controls** — Play, pause, skip. That's it
- **Auto-sleep** — Screen turns off after 2 minutes of inactivity
- **Auto-pause** — Music stops after 30 minutes (configurable) to prevent all-day playback
- **Progress memory** — Remembers where each album left off for up to 96 hours
- **Bluetooth** — Connect wireless headphones or speakers
- **WiFi setup** — Creates a hotspot for easy configuration if WiFi drops
- **Auto-updates** — Pulls latest changes from GitHub nightly
- **No account needed on the device** — Authentication happens via Spotify on your phone

## Hardware

My setup:

- Raspberry Pi 3B (newer models should work too)
- [Raspberry Pi Touch Display 2](https://www.raspberrypi.com/products/raspberry-pi-touch-display-2/) (5")
- [WM8960 Audio HAT](https://www.waveshare.com/wm8960-audio-hat.htm)
- SD card (16GB+)

See [pi/README.md](pi/README.md) for detailed setup instructions.

## Quick Start

### 1. Flash Raspberry Pi OS

Use the [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
- Choose **Raspberry Pi OS Lite (64-bit)**
- Choose a hostname and username (e.g. `berry` / `berry`)
- Configure WiFi and enable SSH

### 2. Install Berry

```bash
ssh <your-user>@<your-hostname>.local
curl -sSL https://raw.githubusercontent.com/emieljanson/berry/main/install.sh | bash
sudo reboot
```

### 3. Connect Spotify

1. Open Spotify on your phone
2. Tap the speaker icon
3. Select "Berry"
4. Start playing — it shows up on the touchscreen

## How It Works

Berry is a Python app using Pygame for the UI and [go-librespot](https://github.com/devgianlu/go-librespot) as a Spotify Connect receiver. When you select Berry as a speaker in Spotify and play an album, go-librespot handles the audio stream while Berry displays the album art and provides touch controls.

```
Your phone (Spotify app)
    │
    ▼
go-librespot (Spotify Connect daemon)
    │
    ▼
Berry (Pygame UI + touch input)
    │
    ▼
Touchscreen + Speaker
```

Albums and playlists you play are automatically saved to the device. Kids can then browse and play them independently from the touchscreen.

## Development

### Local (no Pi needed)

```bash
git clone https://github.com/emieljanson/berry.git
cd berry
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./run.sh  # Runs in mock mode with simulated playback
```

### On a Pi

```bash
./dev-pi.sh  # Syncs changes to Pi over SSH and streams logs
```

### Tests

```bash
pytest tests/ -v
```

## Settings Menu

> **How to open:** Press and hold the volume button for 3 seconds. There's no gear icon or visible button — the long-press on the volume button is the only way in.

Once open, you'll see a scrollable menu with these sections:

### Connections
- **WiFi** — View saved networks, connect to a new one, or switch. If WiFi drops, Berry creates a "Berry-Setup" hotspot you can connect to from your phone
- **Bluetooth** — Pair and connect wireless headphones or speakers. Shows paired devices and nearby discoverable devices
- **Volume levels** — Set separate volume levels (low/mid/high) for the built-in speaker and Bluetooth output

### Playback settings
- **Auto-pause** — How long Berry plays before automatically pausing (15, 30, 60, or 120 minutes). Tap to cycle through options. Default: 30 minutes
- **Remember progress** — How long Berry remembers where each album left off (12, 24, 48, or 96 hours). Tap to cycle. Default: 96 hours

### System
- **Check for updates** — Manually check for and install updates (Berry also updates automatically each night)
- **Reset** — Factory reset: clears all albums, WiFi, Bluetooth, Spotify credentials, and settings. Requires a second tap to confirm

To close the menu, tap the **✕** in the top-right corner.

### Usage Data

During installation, Berry asks if you'd like to share anonymous usage data. This helps improve the project. Only session-level events are collected (play/pause, sleep/wake) — no personal data or music choices. The choice is made once during setup.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## Security

See [SECURITY.md](SECURITY.md) for the security policy and responsible disclosure.

## License

[MIT](LICENSE)

## Acknowledgments

- [go-librespot](https://github.com/devgianlu/go-librespot) — Spotify Connect implementation
- [Pygame](https://www.pygame.org/) — UI framework
- [PostHog](https://posthog.com/) — Anonymous usage analytics

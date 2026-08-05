# Mello

A distraction free Spotify speaker for kids

Kids swipe through album covers and tap to play. Parents control the music library from Spotify on their phone.

### Build Video

<a href="https://youtu.be/4tn8OtKkvs8"><img src="assets/videothumb.png" width="440" alt="Build video"></a>

## Features

- **Spotify Connect** — Add albums and playlists from your Spotify app, Mello plays them
- **Album carousel** — Large cover art with smooth swipe navigation
- **Simple controls** — Play, pause, skip. That's it
- **Track list** — Tap the list button on the cover to see every song, and tap one to jump to it
- **What's next** — The song before and after the current one, so skipping isn't a guess
- **Auto-sleep** — Screen dims to a clock after 2 minutes of inactivity
- **Auto-pause** — Music stops after 30 minutes (configurable), with a visible warning before it does
- **Quiet hours** — Set a bedtime and the device stays asleep until morning
- **OK to wake** — Moon on the sleep clock means stay in bed, sun means you're allowed up
- **Bedtime album** — Allow one record to stay playable at bedtime, and nothing else
- **Progress memory** — Remembers where each album left off for up to 96 hours
- **Bluetooth** — Connect wireless headphones or speakers
- **WiFi setup** — Creates a hotspot for easy configuration if WiFi drops
- **Auto-updates** — Pulls latest changes from GitHub nightly
- **No account needed on the device** — Authentication happens via Spotify on your phone

## Hardware

Print the case from [MakerWorld](https://makerworld.com/en/models/2692843-distraction-free-spotify-player-for-kids).

| Part | Link |
|------|------|
| Raspberry Pi 3 Model B | [Amazon](https://www.amazon.com/dp/B07BDR5PDW) |
| Raspberry Pi Touch Display 2 (5") | [Amazon](https://www.amazon.com/dp/B0FMYFKDLZ) |
| WM8960 Audio HAT | [Amazon](https://www.amazon.com/dp/B07KN8424G) |
| 5.1V 3A USB-C Power Supply | [Amazon](https://www.amazon.com/dp/B0CLV6WB4L) |
| USB-C Panel Mount Bushing | [Amazon](https://www.amazon.com/dp/B0CDC1X4BY) |
| Micro SD Card (16GB+) | — |

## Quick Start

### 1. Flash Raspberry Pi OS

Use the [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
- Choose **Raspberry Pi OS Lite (64-bit)**
- Choose a hostname and username (e.g. `mello` / `mello`)
- Configure WiFi and enable SSH

### 2. Install Mello

```bash
ssh <your-user>@<your-hostname>.local
curl -sSL https://raw.githubusercontent.com/jeremypele/mello/main/install.sh | bash
sudo reboot
```

To install without anonymous usage analytics:

```bash
curl -sSL https://raw.githubusercontent.com/jeremypele/mello/main/install.sh | bash -s -- --no-analytics
```

### 3. Connect Spotify

1. Open Spotify on your phone
2. Tap the speaker icon
3. Select "Mello"
4. Start playing — it shows up on the touchscreen

## How It Works

Mello is a Python app using Pygame for the UI and [go-librespot](https://github.com/devgianlu/go-librespot) as a Spotify Connect receiver. When you select Mello as a speaker in Spotify and play an album, go-librespot handles the audio stream while Mello displays the album art and provides touch controls.

```
Your phone (Spotify app)
    │
    ▼
go-librespot (Spotify Connect daemon)
    │
    ▼
Mello (Pygame UI + touch input)
    │
    ▼
Touchscreen + Speaker
```

Albums and playlists you play are automatically saved to the device. Kids can then browse and play them independently from the touchscreen.

## Settings Menu

> **How to open:** Press and hold the volume button for 3 seconds. There's no gear icon or visible button — the long-press on the volume button is the only way in.

Once open, you'll see a scrollable menu with these sections:

### Connections
- **WiFi** — View saved networks, connect to a new one, or switch. If WiFi drops, Mello creates a "Mello-Setup" hotspot you can connect to from your phone
- **Bluetooth** — Pair and connect wireless headphones or speakers. Shows paired devices and nearby discoverable devices
- **Volume levels** — Set separate volume levels (low/mid/high) for the built-in speaker and Bluetooth output

### Playback settings
- **Auto-pause** — How long Mello plays before automatically pausing (15, 30, 60, or 120 minutes). Tap to cycle through options. Default: 30 minutes. An amber bar appears along the top of the cover for the last 5 minutes, shrinking as the time runs out, so the music stopping isn't a surprise
- **Remember progress** — How long Mello remembers where each album left off (12, 24, 48, or 96 hours). Tap to cycle. Default: 96 hours
- **Bedtime** — When quiet hours start (18:30 through 21:00, or Off). Tap to cycle. Default: Off
- **Wake** — When quiet hours end (06:00 through 08:00). Only shown once a bedtime is set. Tap to cycle. Default: 07:00
- **Bedtime album** — Pick one album that stays playable during quiet hours, or None. Only shown once a bedtime is set. Default: None

### Track list

While something is playing, the line above the cover shows the **previous** and **next** song, so Previous/Next stop being a lottery. The current song is already the title above it.

Tap the **list button** in the cover's top corner (diagonally opposite the `+`) to see every track in the album or playlist, with the current one highlighted. Tap any song to jump straight to it.

The list comes from Spotify's Web API. Mello borrows an access token from go-librespot's own session, so there's nothing to register or configure — but go-librespot ships a client ID shared by every installation, so Spotify throttles it often. Mello handles that by honouring Spotify's retry delay (usually seconds) and **caching each list on disk forever**, in `data/tracks/`. An album only has to succeed once; after that the list is instant and works offline.

The practical effect: the very first time you open a brand-new album's list it may be empty for a few seconds. Play it, wait a moment, and it fills in. The list button only appears for the album that's actually playing, so what you see always matches the cover in front of you.

### Quiet hours

With a bedtime set, Mello pauses the music at that time and keeps the screen asleep on its dim clock until the wake time. Taps are ignored, so there's no play button for a child to find.

Three deliberate exceptions:

- **Press and hold the screen for 3 seconds** to override until morning — the parent's way in.
- **Casting from Spotify on your phone still works**, so you can start music yourself.
- **A bedtime album**, if you set one. Then taps *do* wake the screen, but the carousel holds nothing except that one record — so a lullaby album still works at bedtime while everything else stays out of reach. If that album is already playing when bedtime arrives, Mello leaves it alone.

### OK to wake

While the screen is asleep, the clock shows a **moon** during quiet hours and a **sun** for 90 minutes after the wake time. For a child too young to read a clock, that's the whole point: moon means stay in bed, sun means you're allowed up. Outside those windows (a midday nap, say) the clock shows no symbol at all.

Quiet hours needs a correct clock. The Pi has no battery-backed clock, so it gets the time from the network at boot — if it can't reach the network, quiet hours stays off rather than guessing, and the sleep clock hides itself instead of showing a wrong time. Make sure the Pi's timezone is right (`sudo raspi-config` → Localisation Options).

### System
- **Check for updates** — Manually check for and install updates (Mello also updates automatically each night)
- **Reset** — Factory reset: clears all albums, WiFi, Bluetooth, Spotify credentials, and settings. Requires a second tap to confirm

To close the menu, tap the **✕** in the top-right corner.

### Usage Data

During installation, Mello asks if you'd like to share anonymous usage data. This helps improve the project. Only session-level events are collected (play/pause, sleep/wake) — no personal data or music choices. The choice is made once during setup.

## Known Issues

**Spotify "audio key error" — tracks skip without playing.** This is an upstream issue in librespot (the library that handles Spotify Connect). It affects some Spotify accounts but not others, and there's no fix yet. Mello uses [go-librespot](https://github.com/devgianlu/go-librespot) which is affected by the same problem. Track the issue here: [librespot-org/librespot#1649](https://github.com/librespot-org/librespot/issues/1649)

## Show Off Your Build

Built a Mello? I'd love to see it! Share a photo on Twitter/X and tag [@emieljanson](https://x.com/emieljanson).

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

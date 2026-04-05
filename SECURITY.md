# Security Policy

## Reporting a Vulnerability

If you discover a security issue, please report it privately rather than opening a public issue.

**Email:** emiel@janson.dev

You can expect an initial response within 48 hours.

## Security Design

### Spotify Credentials

Mello uses [go-librespot](https://github.com/devgianlu/go-librespot) for Spotify Connect. Authentication happens via the Spotify app on your phone (device pairing), not through Mello itself. Session credentials are stored locally on each Pi at `~/.config/go-librespot/state.json` and are never transmitted.

### Analytics

Mello includes a PostHog write-only ingest key in the source code. This is intentional and safe — PostHog ingest keys can only write events, not read data. The key is used for anonymous usage analytics (session counts, sleep/wake events) when users opt in during installation.

### Local Data

All user data (saved albums, playback progress, settings) is stored locally in the `data/` directory on each Pi. Nothing is synced or uploaded except anonymous analytics events when enabled.

# Contributing to Mello

Thanks for your interest in contributing! Mello is a simple music player for kids, and we want to keep it that way — simple, reliable, and fun.

## Development Setup

You need a Raspberry Pi with Mello installed (see the [README](README.md)). Edit code on your machine, then sync and test on the Pi:

```bash
./dev-pi.sh --host user@host  # Syncs changes to Pi over SSH and streams logs
```

## Running Tests

```bash
pytest tests/ -v
```

## Making Changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run the tests: `pytest tests/ -v`
4. Test on the Pi
5. Open a pull request

### What makes a good PR

- **Small and focused** — one feature or fix per PR
- **Tested** — add or update tests for logic changes
- **Descriptive** — explain what changed and why in the PR description

## Code Style

- Python 3.11+
- No linter enforced yet, but keep it consistent with existing code
- Type hints where they help readability
- Keep modules focused — managers manage one concern each

## Project Structure

```
mello/
├── api/          # Spotify & catalog APIs
├── handlers/     # Touch & event input
├── managers/     # Feature controllers (sleep, carousel, analytics, etc.)
├── controllers/  # System-level (volume, playback)
├── ui/           # Pygame renderer & helpers
├── config.py     # All constants
└── app.py        # Main application class
```

## Questions?

Open an issue — happy to help you get started.

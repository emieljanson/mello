# Contributing to Berry

Thanks for your interest in contributing! Berry is a simple music player for kids, and we want to keep it that way — simple, reliable, and fun.

## Development Setup

### Local (Mac, no Pi required)

```bash
git clone https://github.com/emieljanson/berry.git
cd berry
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./run.sh  # Runs in mock mode (no Spotify needed)
```

Mock mode simulates playback and album art so you can work on the UI without a Pi or Spotify account.

### With a Raspberry Pi

```bash
./dev-pi.sh
```

This syncs your local changes to the Pi over SSH and streams logs back. Edit locally, test on the Pi.

## Running Tests

```bash
pytest tests/ -v
```

Tests run without pygame or hardware dependencies. All managers, models, and API logic are covered.

## Making Changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run the tests: `pytest tests/ -v`
4. Test on a Pi if your change touches hardware (touch, display, audio, Bluetooth)
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
berry/
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

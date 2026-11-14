"""
Log lines must reach the SD card once, not twice.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import mello.main as main


def handlers_after_setup(monkeypatch, tmp_path, isatty):
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(main, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(main, 'LOG_FILE', tmp_path / 'mello.log')
    monkeypatch.setattr(main.sys.stdout, 'isatty', lambda: isatty, raising=False)
    try:
        main.setup_logging()
        added = [h for h in root.handlers if h not in before]
        return [type(h).__name__ for h in added]
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()


def test_as_a_service_lines_go_to_the_log_file_only(monkeypatch, tmp_path):
    # systemd appends stdout to a second, never-trimmed file on the SD card.
    assert handlers_after_setup(monkeypatch, tmp_path, isatty=False) == ['RotatingFileHandler']


def test_at_a_terminal_lines_are_also_printed(monkeypatch, tmp_path):
    assert sorted(handlers_after_setup(monkeypatch, tmp_path, isatty=True)) == ['RotatingFileHandler', 'StreamHandler']

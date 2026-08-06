"""
mello-login.py — the one-time Spotify login.

Only the parts that can silently corrupt something are tested: writing the
token into an .env that already has other keys in it (clobbering the client
secret would be an unrecoverable mess for anyone running this), and the state
check that stops a code minted for someone else's account being accepted.
The browser round-trip itself is left to the one manual run it takes.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Hyphen in the filename: not importable as a module name.
_spec = importlib.util.spec_from_file_location('mello_login', ROOT / 'mello-login.py')
assert _spec and _spec.loader
mello_login = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mello_login)


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    monkeypatch.setattr(mello_login, 'ENV_PATH', path)
    return path


class TestSaveRefreshToken:

    def test_appends_to_an_existing_env(self, env):
        env.write_text('SPOTIFY_CLIENT_ID=abc\nSPOTIFY_CLIENT_SECRET=def\n')
        mello_login.save_refresh_token('refresh-1')
        text = env.read_text()
        assert 'SPOTIFY_REFRESH_TOKEN=refresh-1' in text
        assert 'SPOTIFY_CLIENT_ID=abc' in text, 'must not clobber the app key'
        assert 'SPOTIFY_CLIENT_SECRET=def' in text

    def test_replaces_rather_than_duplicating(self, env):
        """Logging in twice must not leave two lines — .env takes the first."""
        env.write_text('SPOTIFY_REFRESH_TOKEN=old\nSPOTIFY_MARKET=FR\n')
        mello_login.save_refresh_token('refresh-2')
        lines = env.read_text().splitlines()
        assert lines.count('SPOTIFY_REFRESH_TOKEN=refresh-2') == 1
        assert not any(line.endswith('=old') for line in lines)
        assert 'SPOTIFY_MARKET=FR' in lines

    def test_creates_the_file_when_absent(self, env):
        mello_login.save_refresh_token('refresh-3')
        assert env.read_text() == 'SPOTIFY_REFRESH_TOKEN=refresh-3\n'

    def test_comments_survive(self, env):
        env.write_text('# my key\nSPOTIFY_CLIENT_ID=abc\n')
        mello_login.save_refresh_token('refresh-4')
        assert '# my key' in env.read_text()

    def test_is_not_world_readable(self, env):
        """It's a credential, sitting next to another credential."""
        mello_login.save_refresh_token('refresh-5')
        assert env.stat().st_mode & 0o077 == 0


class TestRedirectHandling:
    """The state parameter is the entire CSRF defence for a loopback flow."""

    def test_a_mismatched_state_is_refused(self, monkeypatch):
        monkeypatch.setattr(mello_login, 'SPOTIFY_CLIENT_ID', 'id')
        monkeypatch.setattr(mello_login, 'SPOTIFY_CLIENT_SECRET', 'secret')
        monkeypatch.setattr(mello_login, '_redirect_query',
                            {'code': 'c', 'state': 'not-the-one-we-sent'})

        class _Server:
            def handle_request(self):
                pass

        monkeypatch.setattr(mello_login.http.server, 'HTTPServer',
                            lambda *a, **k: _Server())
        exchanged = []
        monkeypatch.setattr(mello_login, 'exchange_code', exchanged.append)

        with pytest.raises(SystemExit) as exit_info:
            mello_login.main()
        assert 'State' in str(exit_info.value)
        assert exchanged == [], 'a foreign code must never be exchanged'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))

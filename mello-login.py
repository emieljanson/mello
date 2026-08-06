#!/usr/bin/env python3
"""
One-time Spotify login, so Mello can read playlist track lists.

Spotify gates /playlists/{id}/tracks behind the playlist-read-private scope for
*every* playlist — public ones included. An app-only (client credentials) token
carries no scopes at all, so it can never read a playlist's tracks no matter
who owns it or how public it is. Albums and shows are unaffected.

That scope needs a real login. This does it once and stores the refresh token in
.env; after that Mello refreshes silently and you never see this again.

Spotify only accepts a loopback redirect over plain http, so the browser you log
in with has to land on 127.0.0.1 *of this machine*. Tunnel it from your laptop:

    ssh -N -L 8080:127.0.0.1:8080 <user>@<this-pi>

then run this script here and open the URL it prints, in your laptop's browser.
"""
import base64
import http.server
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mello.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET  # also loads .env

PORT = 8080
# Must match the dashboard entry byte for byte. docs/spotify-api.md already has
# you register exactly this, so logging in needs no dashboard change.
REDIRECT_URI = f'http://127.0.0.1:{PORT}'
SCOPE = 'playlist-read-private'
ENV_PATH = Path(__file__).parent / '.env'


_redirect_query: dict = {}


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        _redirect_query.update(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))
        body = ('<h2>Mello is logged in.</h2><p>You can close this tab.</p>'
                if 'code' in _redirect_query
                else '<h2>Login failed.</h2><p>Check the terminal on the Pi.</p>')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass   # the prompts below are the only output worth reading


def exchange_code(code: str) -> str:
    """Swap the one-shot code for a refresh token that lasts."""
    basic = base64.b64encode(
        f'{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}'.encode()).decode()
    request = urllib.request.Request(
        'https://accounts.spotify.com/api/token',
        data=urllib.parse.urlencode({
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI,
        }).encode(),
        headers={'Authorization': f'Basic {basic}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:200]
        sys.exit(f'Token exchange failed ({e.code}): {detail}')
    except (urllib.error.URLError, ValueError) as e:
        sys.exit(f'Token exchange failed: {e}')

    token = payload.get('refresh_token')
    if not token:
        sys.exit('Spotify returned no refresh token.')
    return token


def save_refresh_token(token: str):
    """Into .env beside the client key — same file, same gitignore, same habits."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    entry = f'SPOTIFY_REFRESH_TOKEN={token}'
    for i, line in enumerate(lines):
        if line.strip().startswith('SPOTIFY_REFRESH_TOKEN='):
            lines[i] = entry
            break
    else:
        lines.append(entry)
    ENV_PATH.write_text('\n'.join(lines) + '\n')
    ENV_PATH.chmod(0o600)


def main():
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        sys.exit('Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first '
                 '— see docs/spotify-api.md.')

    state = secrets.token_urlsafe(16)
    auth_url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
        'client_id': SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
        'state': state,
    })

    try:
        server = http.server.HTTPServer(('127.0.0.1', PORT), _Callback)
    except OSError as e:
        sys.exit(f'Cannot listen on 127.0.0.1:{PORT} ({e}). Something else is '
                 f'using it — stop that first, or close an old tunnel.')

    print(f"""
Mello Spotify login
===================

1. On your laptop, in another terminal:

     ssh -N -L {PORT}:127.0.0.1:{PORT} <user>@<this-pi>

2. Then open this in your laptop's browser:

{auth_url}

3. Log in, press Agree. This finishes on its own.

Waiting for the redirect...""")

    server.handle_request()
    result = _redirect_query

    # The state check is the whole CSRF defence for a loopback flow: without it
    # any page you visit could hand us a code minted for someone else's account.
    if result.get('state') != state:
        sys.exit('State did not match — ignoring that response. Try again.')
    if 'code' not in result:
        sys.exit(f"Spotify refused: {result.get('error', 'no code returned')}")

    save_refresh_token(exchange_code(result['code']))
    print('\nLogged in. Restart Mello to pick it up:\n\n'
          '    sudo systemctl restart mello-native\n')


if __name__ == '__main__':
    main()

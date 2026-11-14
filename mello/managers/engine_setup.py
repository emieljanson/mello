"""
Engine setup - lets the owner switch this speaker to Spotify Soloist.

Soloist needs an API key that every owner requests from Spotify themselves.
A key is far too long to type on the speaker, so while the setup menu's
Spotify screen is open the speaker serves one small web page on the home
network where the key can be pasted from a phone or computer. Being at the
speaker with the menu open is the permission: the page does not exist at any
other time.

Saving a key hands over to pi/soloist/soloist-setup.sh, which downloads
Soloist, switches the speaker's Spotify engine and restarts the app.
"""
import html
import logging
import os
import platform
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

_REPO_DIR = Path(__file__).resolve().parent.parent.parent
SETUP_SCRIPT = _REPO_DIR / 'pi' / 'soloist' / 'soloist-setup.sh'

# Spotify does not document the key's format; this only rejects what cannot
# be a key at all (empty, a sentence, a pasted page).
_KEY_PATTERN = re.compile(r'^[A-Za-z0-9._~+/=-]{16,256}$')
_MAX_BODY = 4096

# Where an owner gets a key (Spotify's own steps, from its Soloist "getting started").
KEY_DASHBOARD_URL = 'https://developer.spotify.com/dashboard/soloist'

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mello</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0d;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;padding:32px 20px;min-height:100vh;-webkit-font-smoothing:antialiased;max-width:30em;margin:0 auto}}
h1{{font-size:20px;font-weight:600;letter-spacing:-.02em;margin-bottom:6px}}
.sub{{font-size:13px;color:#666;margin-bottom:28px}}
label{{display:block;font-size:12px;font-weight:500;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}}
input[type=text]{{width:100%;background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:13px 14px;font-size:15px;color:#fff;outline:none;margin-bottom:20px}}
input[type=text]:focus{{border-color:#bd65fc}}
input::placeholder{{color:#444}}
button{{width:100%;background:#bd65fc;border:none;border-radius:10px;padding:13px;font-size:15px;font-weight:600;color:#fff;cursor:pointer}}
button.plain{{background:#1c1c1c;color:#aaa;border:1px solid #2a2a2a;font-weight:500}}
.steps{{background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:14px 16px;margin-bottom:24px;font-size:14px;line-height:1.5;color:#bbb}}
.steps b{{color:#fff;font-weight:600;display:block;margin-bottom:6px}}
.steps ol{{padding-left:20px}}
.steps a{{color:#bd65fc}}
.note{{margin-bottom:24px;padding:13px 14px;border-radius:10px;font-size:14px;background:#1a2e1a;color:#4ade80;border:1px solid #264d26}}
.note.error{{background:#2e1a1a;color:#f87171;border-color:#4d2626}}
.lead{{font-size:14px;color:#bbb;line-height:1.5;margin-bottom:24px}}
.small{{font-size:12px;color:#666;line-height:1.5;margin-top:16px}}
hr{{border:0;border-top:1px solid #222;margin:28px 0 20px}}
</style></head><body>
<h1>Mello</h1>
{body}
</body></html>"""

_HOW_TO = """<div class="steps"><b>Get your key</b><ol>
<li>Open <a href="{dashboard}" target="_blank" rel="noopener">developer.spotify.com/dashboard/soloist</a> (needs Spotify Premium).</li>
<li>Log in, accept the terms and generate an API key.</li>
<li>Paste it here.</li>
</ol></div>""".format(dashboard=KEY_DASHBOARD_URL)

_FORM = """<p class="sub">Now playing through {engine}</p>
<p class="lead">Spotify Soloist is Spotify's own player for speakers. Play, pause and skip respond faster.</p>
{message}
""" + _HOW_TO + """
<form method="post" action="/key">
 <label for="key">Soloist API key</label>
 <input type="text" id="key" name="key" placeholder="Paste your key" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" required>
 <button type="submit">Save</button>
</form>
<p class="small">Your key stays on this speaker.</p>
{back}"""

_BACK = """<hr><form method="post" action="/librespot">
 <button type="submit" class="plain">Switch back to go-librespot</button>
</form>"""

_BUSY = """<p class="note">{text}</p>
<p class="lead">Mello restarts in about a minute. Then open Spotify, tap the speaker icon and pick Mello once.</p>"""


def valid_key(key: str) -> bool:
    return bool(_KEY_PATTERN.match(key))


def qr_matrix(text: str) -> Optional[List[List[bool]]]:
    """QR code for `text` as rows of dark/light modules, or None if unavailable."""
    try:
        import qrcode
    except ImportError:
        return None  # dependency not installed yet: the address is shown as text instead
    code = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def lan_address() -> Optional[str]:
    """This speaker's address on the home network (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 9))
            return probe.getsockname()[0]
    except OSError:
        return None


class EngineSetup:
    """Key entry page plus the hand-over to the switch script."""

    def __init__(
        self,
        engine: str,
        soloist_home: Path,
        port: int = 8080,
        script: Path = SETUP_SCRIPT,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.engine = engine
        self.soloist_home = Path(soloist_home)
        self.port = port
        self.script = Path(script)
        self._on_change = on_change or (lambda: None)

        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        # 'waiting' | 'switching' | 'failed'
        self.state = 'waiting'
        self.error: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None
        self._qr_url: Optional[str] = None
        self._qr: Optional[List[List[bool]]] = None

    # ------------------------------------------------------------------
    # Web page lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> Optional[str]:
        address = lan_address()
        return f'http://{address}:{self.port}' if address else None

    def start(self) -> bool:
        """Serve the page. Returns False when the port cannot be opened."""
        if self._server:
            return True
        setup = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                setup._respond(self, setup._render())

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                if length > _MAX_BODY:
                    self.send_error(413)
                    return
                fields = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
                if self.path == '/key':
                    setup.submit_key((fields.get('key') or [''])[0])
                elif self.path == '/librespot':
                    setup.switch_back()
                else:
                    self.send_error(404)
                    return
                setup._respond(self, setup._render())

            def log_message(self, *_args):
                pass  # never log requests: a key may be in them

        try:
            self._server = ThreadingHTTPServer(('0.0.0.0', self.port), Handler)
        except OSError as e:
            logger.error(f'Engine setup page could not start on port {self.port}: {e}')
            return False
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        logger.info(f'Engine setup page started on port {self.port}')
        return True

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
            logger.info('Engine setup page stopped')

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, body: str):
        payload = _PAGE.format(body=body).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(payload)))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(payload)

    def _render(self) -> str:
        with self._lock:
            state, error = self.state, self.error
        if state == 'switching':
            return _BUSY.format(text='Switching to Soloist&hellip;')
        message = ''
        if state == 'failed' and error:
            message = f'<p class="note error">{html.escape(error)}</p>'
        return _FORM.format(
            engine='Spotify Soloist' if self.engine == 'soloist' else 'go-librespot',
            message=message,
            back=_BACK if self.engine == 'soloist' else '',
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def submit_key(self, key: str) -> bool:
        """Store the owner's key and switch the speaker to Soloist."""
        key = (key or '').strip()
        if not valid_key(key):
            self._set('failed', 'That does not look like a Soloist API key. Paste the key exactly as Spotify shows it.')
            return False
        try:
            self._store_key(key)
        except OSError as e:
            logger.error(f'Could not store Soloist key: {e}')
            self._set('failed', 'The key could not be saved on the speaker.')
            return False
        logger.info('Soloist API key saved')
        return self._run_script('activate')

    def switch_back(self) -> bool:
        return self._run_script('deactivate')

    def _store_key(self, key: str):
        self.soloist_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.soloist_home / 'key'
        tmp = path.with_name('key.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(key + '\n')
        os.replace(tmp, path)

    def _run_script(self, command: str) -> bool:
        with self._lock:
            if self.state == 'switching':
                return False
            self.state, self.error = 'switching', None
        self._on_change()
        try:
            self._process = subprocess.Popen(
                ['bash', str(self.script), command],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        except OSError as e:
            logger.error(f'Could not start {self.script.name}: {e}')
            self._set('failed', 'The speaker could not start the switch.')
            return False
        threading.Thread(target=self._follow, args=(self._process, command), daemon=True).start()
        return True

    def _follow(self, process: subprocess.Popen, command: str):
        """Log the script's output; it ends by restarting this app when it succeeds."""
        last = ''
        for line in process.stdout:
            line = line.rstrip()
            if line:
                last = line
                logger.info(f'soloist-setup {command}: {line}')
        code = process.wait()
        if code != 0:
            logger.error(f'soloist-setup {command} failed (code={code})')
            reason = last.split('ERROR: ', 1)[1] if 'ERROR: ' in last else 'something went wrong'
            self._set('failed', f'Switching did not work: {reason}.')

    def _set(self, state: str, error: Optional[str] = None):
        with self._lock:
            self.state, self.error = state, error
        self._on_change()

    # ------------------------------------------------------------------
    # What the speaker's own screen shows
    # ------------------------------------------------------------------

    def can_offer_soloist(self) -> bool:
        """Whether this speaker could switch and has not been set up for it yet."""
        return (
            self.engine != 'soloist'
            and platform.machine() in ('aarch64', 'arm64')   # the only Soloist build Mello knows
            and not (self.soloist_home / 'key').exists()
        )

    def screen_items(self) -> List[tuple]:
        """What the speaker's Spotify screen shows.

        ('text', line), ('spacer',) or ('qr', matrix) where matrix is rows of
        booleans. A phone camera opens the key page straight from the code.
        """
        with self._lock:
            state, error = self.state, self.error
        if state == 'switching':
            return [('text', 'Switching to Soloist...'), ('text', 'Mello restarts by itself')]
        url = self.url if self.running else None
        if not url:
            return [('text', 'Connect Mello to WiFi first')]

        if self.engine == 'soloist':
            items = [('text', 'Spotify Soloist is in use'), ('spacer',)]
            hint = 'Scan to change it'
        else:
            items = [('text', 'Faster play, pause and skip'), ('text', 'with Spotify Soloist'), ('spacer',)]
            hint = 'Scan to set it up'
        if url != self._qr_url:
            # Asked for every frame the screen is drawn; build it once per address.
            self._qr_url, self._qr = url, qr_matrix(url)
        matrix = self._qr
        if matrix:
            items.append(('qr', matrix))
            items.append(('spacer',))
        else:
            hint = 'Open on your phone'
        items += [('text', hint), ('text', url.replace('http://', ''))]
        if state == 'failed' and error:
            items += [('spacer',), ('text', 'Last try did not work')]
        return items

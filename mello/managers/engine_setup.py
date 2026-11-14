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

The same page also backs the optional key field on the WiFi setup page
(portal/index.html). The speaker has no internet at that moment, so the key
is only stored and marked "pending"; the switch happens once it is online.
"""
import html
import logging
import os
import re
import socket
import subprocess
import threading
import time
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

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mello - Spotify</title>
<style>
 body{{font-family:-apple-system,system-ui,sans-serif;max-width:30em;margin:2em auto;padding:0 1em;color:#222}}
 input[type=text]{{width:100%;box-sizing:border-box;font-size:1em;padding:.7em;margin:.5em 0 1em}}
 button{{font-size:1em;padding:.7em 1.2em;border:0;border-radius:.5em;background:#7c5cff;color:#fff}}
 button.plain{{background:#ddd;color:#222}}
 .note{{background:#f3f0ff;padding:.8em 1em;border-radius:.5em}}
 .error{{background:#ffe9e9}}
 small{{color:#666}}
</style></head><body>
<h1>Mello</h1>
{body}
</body></html>"""

_FORM = """<p>This speaker now plays Spotify through <b>{engine}</b>.</p>
{message}
<form method="post" action="/key">
 <label for="key">Spotify Soloist API key</label>
 <input type="text" id="key" name="key" autocomplete="off" autocapitalize="off" spellcheck="false" required>
 <button type="submit">Save and switch to Soloist</button>
</form>
<p><small>Request your own key at developer.spotify.com (Soloist). It stays on
this speaker. After switching, choose the speaker once in your Spotify app.</small></p>
{back}"""

_BACK = """<hr><form method="post" action="/librespot">
 <button type="submit" class="plain">Switch back to go-librespot</button>
</form>"""

_SAVED = """<p class="note">Key saved. Mello switches to Soloist as soon as it is online.</p>"""

# How long to wait for internet after WiFi was set up before giving up for now
# (the nightly update tries again).
_ONLINE_WAIT = 180
_DOWNLOAD_HOST = ('soloist-builds.spotifycdn.com', 443)

_BUSY = """<p class="note">{text}</p>
<p>The speaker restarts by itself when it is ready (about a minute). You can close this page.</p>"""


def valid_key(key: str) -> bool:
    return bool(_KEY_PATTERN.match(key))


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
        # True while serving next to the WiFi setup page: no internet yet,
        # so a key is stored for later instead of acted on.
        self._defer = False
        self._lock = threading.Lock()
        # 'waiting' | 'saved' | 'switching' | 'failed'
        self.state = 'waiting'
        self.error: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None

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

    @property
    def pending_file(self) -> Path:
        return self.soloist_home / 'activate-pending'

    def start(self, defer: bool = False) -> bool:
        """Serve the page. Returns False when the port cannot be opened.

        Args:
            defer: store a submitted key for later instead of switching now
                   (used while the speaker is its own WiFi hotspot).
        """
        self._defer = defer
        if self._server:
            return True
        setup = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                setup._respond(self, setup._render())

            def do_OPTIONS(self):
                # The WiFi setup page lives on another port of this speaker.
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'POST')
                self.end_headers()

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
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.end_headers()
        handler.wfile.write(payload)

    def _render(self) -> str:
        with self._lock:
            state, error = self.state, self.error
        if state == 'switching':
            return _BUSY.format(text='Switching the speaker over&hellip;')
        if state == 'saved':
            return _SAVED
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
        if self._defer:
            self.pending_file.touch()
            self._set('saved')
            return True
        return self._run_script('activate')

    def activate_pending_when_online(self):
        """Finish a switch whose key was entered during WiFi setup.

        Safe to call any time (app start, WiFi just connected): does nothing
        unless a key is waiting. Waits in the background for internet.
        """
        if not self.pending_file.exists():
            return

        def _wait_and_switch():
            deadline = time.time() + _ONLINE_WAIT
            while time.time() < deadline:
                try:
                    socket.create_connection(_DOWNLOAD_HOST, timeout=5).close()
                except OSError:
                    time.sleep(5)
                    continue
                if self.pending_file.exists():
                    logger.info('Online: switching to Soloist with the key entered during WiFi setup')
                    self._run_script('activate')
                return
            logger.warning('Soloist switch still pending: no internet yet (the nightly update retries)')

        threading.Thread(target=_wait_and_switch, daemon=True).start()

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

    def screen_lines(self) -> List[str]:
        """Text for the setup menu's Spotify screen ('' = blank line)."""
        with self._lock:
            state, error = self.state, self.error
        if state == 'switching':
            return ['Switching over...', '', 'Mello restarts by itself', 'when it is ready.']
        url = self.url if self.running else None
        if not url:
            return ['Connect Mello to WiFi first.']
        lines = [
            'Soloist' if self.engine == 'soloist' else 'go-librespot',
            'is in use.',
            '',
            'To change it, open on',
            'your phone or computer:',
            url.replace('http://', ''),
        ]
        if state == 'failed' and error:
            lines += ['', 'Last try did not work.']
        return lines

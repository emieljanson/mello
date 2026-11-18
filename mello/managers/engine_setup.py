"""
Engine setup - lets the owner switch this speaker to Spotify Soloist.

Soloist needs an API key that every owner requests from Spotify themselves.
A key is far too long to type on the speaker, so while the setup menu's
Spotify screen is open (or a new Mello's home screen is asking to connect
Spotify) the speaker serves one small web page on the home network where the
key can be pasted from a phone or computer. Seeing the code on the speaker's
screen is the permission: the page does not exist at any other time.

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
import sys
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

# Where an owner gets a key (Spotify's own steps, from its Soloist "getting started").
KEY_DASHBOARD_URL = 'https://developer.spotify.com/dashboard/soloist'

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mello</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0d;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;padding:32px 20px;min-height:100vh;-webkit-font-smoothing:antialiased;max-width:30em;margin:0 auto}}
h1{{font-size:22px;font-weight:600;letter-spacing:-.02em;margin-bottom:12px}}
.sub{{font-size:13px;color:#666;margin-bottom:28px}}
input[type=text]{{width:100%;background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:13px 14px;font-size:15px;color:#fff;outline:none;margin-bottom:20px}}
input[type=text]:focus{{border-color:#bd65fc}}
input::placeholder{{color:#444}}
button,a.button{{display:block;width:100%;background:#bd65fc;border:none;border-radius:10px;padding:13px;font-size:15px;font-weight:600;color:#fff;cursor:pointer;text-align:center;text-decoration:none}}
button.plain{{background:#1c1c1c;color:#aaa;border:1px solid #2a2a2a;font-weight:500}}
.steps{{background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:14px 16px;margin-bottom:24px;font-size:14px;line-height:1.5;color:#bbb}}
.steps ol{{padding-left:20px}}
.steps a{{color:#bd65fc}}
.note{{margin-bottom:24px;padding:13px 14px;border-radius:10px;font-size:14px;background:#1a2e1a;color:#4ade80;border:1px solid #264d26}}
.note.error{{background:#2e1a1a;color:#f87171;border-color:#4d2626}}
.lead{{font-size:14px;color:#bbb;line-height:1.5;margin-bottom:24px}}
.lead.after{{margin-top:20px}}
.small{{font-size:12px;color:#666;line-height:1.5;margin-top:16px}}
.center{{text-align:center}}
.eyebrow{{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#bd65fc;margin-bottom:10px}}
.ben{{display:flex;gap:12px;align-items:flex-start;margin-bottom:14px;font-size:14px;color:#bbb;line-height:1.45}}
.ben span{{flex:none;color:#bd65fc;font-weight:700}}
.ben b{{color:#fff;font-weight:600;display:block}}
hr{{border:0;border-top:1px solid #222;margin:28px 0 20px}}
</style></head><body>
{body}
</body></html>"""

_HOW_TO = """<div class="steps"><ol>
<li>Open <a href="{dashboard}" target="_blank" rel="noopener">Spotify's setup page</a> and log in.</li>
<li>Accept the terms and tap &ldquo;Generate an API key&rdquo;.</li>
<li>Copy the code and paste it here.</li>
</ol></div>""".format(dashboard=KEY_DASHBOARD_URL)

# First page after scanning the offer: why, what you get, what it takes.
_INTRO = """{eyebrow}<h1>A faster Mello</h1>
<p class="lead">{reason}</p>
<div class="ben"><span>1</span><div><b>Starts sooner</b>Music begins faster after a tap.</div></div>
<div class="ben"><span>2</span><div><b>Skips faster</b>Next, previous and pause react right away.</div></div>
<div class="ben"><span>3</span><div><b>Fewer hiccups</b>Music keeps playing, also after a break.</div></div>
<p class="lead after">Spotify asks you for a personal code, once. It takes about 2 minutes.</p>
<a class="button" href="/code">Turn it on</a>
<p class="small center">Not now? Mello keeps working like before.</p>"""

_FORM = """{eyebrow}<h1>{title}</h1>
{lead}{message}
""" + _HOW_TO + """
<form method="post" action="/key">
 <input type="text" id="key" name="key" aria-label="Code from Spotify" placeholder="Paste the code" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" required>
 <button type="submit">{button}</button>
</form>
<p class="small center">You need Spotify Premium.<br>The code is only saved on your Mello.</p>"""



# The same words as on Mello's own screen while it switches.
_BUSY = """<h1>One moment&hellip;</h1>
<p class="lead">Mello restarts by itself.</p>"""

# The offer on Mello's own screen (see SetupMenu.show_spotify_prompt).
PROMPT_TITLE_UPDATE = 'New: a faster Mello'
PROMPT_TITLE_NEW_OWNER = 'Make Mello faster'
PROMPT_BENEFITS = ('Starts sooner', 'Skips faster', 'Fewer hiccups')
PROMPT_BUTTON_LATER = 'Hold to do it later'
PROMPT_BUTTON_LAST = 'Hold to skip this'
PROMPT_SCAN_HINT = 'Scan to turn it on'

# The same question on the home screen of a new Mello, once it is online.
SETUP_SPOTIFY_TITLE = 'Connect Spotify'          # heading of the phone page
SETUP_LINES = ('Scan with a phone on the same WiFi', 'to connect Spotify')
# Under Settings > Spotify, for a Mello that was offered it and once it is in use.
SETTINGS_OFFER_LINES = ('Scan with a phone on the same WiFi', 'to make Mello faster')
SETTINGS_CONNECTED_LINES = ('Faster playback is on', 'Scan the code to change it')

# What the owner reads when it did not work; the technical reason is in the log.
_TRY_AGAIN = "That didn't work. Check that Mello is connected to WiFi and try again."
_NOT_ON_THIS_MELLO = "This Mello can't use faster playback yet."


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
        # A Mello that was set up new is not told this "came with an update".
        self.new_owner = False
        # Asked as part of setting up a new Mello (True) or as an offer (False).
        self.setup_step = False
        # When a phone last asked for the page: someone is busy with it.
        self.last_request = 0.0

        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        # One code submission at a time, from check to the switch starting.
        self._submit_lock = threading.Lock()
        # 'waiting' | 'switching' | 'failed'
        self.state = 'waiting'
        self.error: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None
        self._address: Optional[str] = None
        self._address_at = float('-inf')
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
        # Asked for every frame a screen with the code is drawn: look the
        # address up at most once a second.
        now = time.monotonic()
        if now - self._address_at > 1.0:
            self._address, self._address_at = lan_address(), now
        return f'http://{self._address}:{self.port}' if self._address else None

    def start(self) -> bool:
        """Serve the page. Returns False when the port cannot be opened."""
        if self._server:
            return True
        setup = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                setup.last_request = time.time()
                setup._respond(self, setup._render(self.path))

            def do_POST(self):
                setup.last_request = time.time()
                length = int(self.headers.get('Content-Length') or 0)
                if length > _MAX_BODY:
                    self.send_error(413)
                    return
                fields = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
                if self.path == '/key':
                    setup.submit_key((fields.get('key') or [''])[0])
                else:
                    self.send_error(404)
                    return
                setup._respond(self, setup._render('/code'))

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

    def stop(self, wait: bool = True):
        """Take the page down. `wait=False` for callers that draw the screen:
        shutting the server down takes up to half a second."""
        server, self._server = self._server, None
        if not server:
            return

        def shut():
            server.shutdown()
            server.server_close()
            logger.info('Engine setup page stopped')

        if wait:
            shut()
        else:
            threading.Thread(target=shut, daemon=True).start()

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, body: str):
        payload = _PAGE.format(body=body).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(payload)))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(payload)

    def _render(self, path: str = '/') -> str:
        with self._lock:
            state, error = self.state, self.error
        if state == 'switching':
            return _BUSY
        on_soloist = self.engine == 'soloist'
        setting_up = self.new_owner and self.setup_step and not on_soloist
        if not on_soloist and not setting_up and state == 'waiting' and not path.startswith('/code'):
            # Straight from the QR code: first say what this is and why.
            if self.new_owner:
                eyebrow = ''
                reason = "Spotify has its own player for speakers. Mello can use it, and that makes everything quicker."
            else:
                eyebrow = '<p class="eyebrow">New in this update</p>\n'
                reason = "Spotify just released its own player for speakers. Mello can now use it, and that makes everything quicker."
            return _INTRO.format(eyebrow=eyebrow, reason=reason)
        message = ''
        if state == 'failed' and error:
            message = f'<p class="note error">{html.escape(error)}</p>'
        lead = eyebrow = ''
        title = 'Get your code from Spotify'
        if on_soloist:
            title = 'Faster playback is on'
            lead = '<p class="lead">Got a new code from Spotify? Paste it below.</p>\n'
        elif setting_up:
            # Part of setting up a new Mello: no talk of faster or new.
            title = SETUP_SPOTIFY_TITLE
            lead = ('<p class="lead">Spotify asks every speaker owner for a personal code. '
                    'You only do this once, it takes about 2 minutes.</p>\n')
        return _FORM.format(
            eyebrow=eyebrow,
            title=title,
            lead=lead,
            button='Save' if on_soloist else 'Connect',
            message=message,
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def submit_key(self, key: str) -> bool:
        """Store the owner's key and switch the speaker to Soloist."""
        with self._submit_lock:
            return self._submit_key(key)

    def _submit_key(self, key: str) -> bool:
        key = (key or '').strip()
        with self._lock:
            if self.state == 'switching':
                return False   # the running switch keeps the code it started with
        if not valid_key(key):
            self._set('failed', "That code doesn't look right. Copy it again from Spotify and paste it here.")
            return False
        try:
            self._store_key(key)
        except OSError as e:
            logger.error(f'Could not store Soloist key: {e}')
            self._set('failed', "Mello couldn't save the code. Please try again.")
            return False
        logger.info('Soloist API key saved')
        return self._run_script('activate')

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
            self._set('failed', _TRY_AGAIN)
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
            if command == 'activate' and self.engine != 'soloist':
                # A code that did not get the speaker switched must not stay
                # behind: with it Mello would count as "already set up" and
                # never ask again, while the page says "try again".
                try:
                    (self.soloist_home / 'key').unlink()
                except OSError:
                    pass
            self._set('failed', _NOT_ON_THIS_MELLO if 'no Soloist build' in last else _TRY_AGAIN)

    def _set(self, state: str, error: Optional[str] = None):
        with self._lock:
            self.state, self.error = state, error
        self._on_change()

    # ------------------------------------------------------------------
    # What the speaker's own screen shows
    # ------------------------------------------------------------------

    @staticmethod
    def can_run_soloist() -> bool:
        """Spotify only builds Soloist for 64-bit ARM. A 32-bit Raspberry Pi
        OS on a 64-bit kernel also reports aarch64, so the programs' own
        bitness (this Python's) decides."""
        return platform.machine() in ('aarch64', 'arm64') and sys.maxsize > 2**32

    def can_offer_soloist(self) -> bool:
        """Whether this speaker could switch and has not been set up for it yet."""
        return (
            self.engine != 'soloist'
            and self.can_run_soloist()
            and not (self.soloist_home / 'key').exists()
        )

    def screen_items(self) -> List[tuple]:
        """What the speaker's connect-Spotify screen shows besides its title.

        ('text', line) or ('qr', matrix) where matrix is rows of booleans. A
        phone camera opens the key page straight from the code.
        """
        with self._lock:
            state = self.state
        if state == 'switching':
            return [('text', 'One moment...'), ('text', 'Mello restarts by itself')]
        url = self.url if self.running else None
        if not url:
            return [('text', 'Connect Mello to WiFi first')]
        if url != self._qr_url:
            # Asked for every frame the screen is drawn; build it once per address.
            self._qr_url, self._qr = url, qr_matrix(url)
        if self._qr:
            return [('qr', self._qr)]
        # No code to scan: the address is the only way in.
        return [('text', 'Open on your phone'), ('text', url.replace('http://', ''))]

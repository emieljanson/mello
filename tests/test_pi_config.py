"""
Static checks for Pi deployment files.
"""
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).parent.parent


def _migration_015_body() -> str:
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    return migrate.split('_migrate_015() {', 1)[1].split('\n}', 1)[0]


def _run_migration_015(sudoers_dir: Path):
    script = textwrap.dedent(
        f'''\
        set -euo pipefail
        MELLO_SUDOERS_DIR={str(sudoers_dir)!r}
        log() {{ :; }}
        sudo() {{
          if [ "$1" = visudo ]; then return 0; fi
          "$@"
        }}
        _migrate_015() {{
        {_migration_015_body()}
        }}
        _migrate_015
        '''
    )
    return subprocess.run(
        ['bash'],
        input=script,
        text=True,
        capture_output=True,
    )


def test_soloist_service_is_not_part_of_ui_service():
    service = (ROOT / 'pi/systemd/mello-soloist.service.template').read_text()

    assert 'PartOf=mello-native.service' not in service


def test_migration_014_registered_for_librespot_dependency_update():
    migrate = (ROOT / 'pi/migrate.sh').read_text()

    assert '_migrate_014()' in migrate
    assert 'run_migration "014" "Keep librespot independent of UI sleep/restarts"' in migrate


def test_librespot_recovery_permission_is_installed_and_migrated():
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    setup = (ROOT / 'pi/setup.sh').read_text()
    migration_015 = _migration_015_body()

    assert '/bin/systemctl restart mello-soloist' in setup
    assert '/bin/systemctl restart mello-librespot' in migration_015
    assert '_migrate_015()' in migrate
    assert 'run_migration "015" "Allow automatic librespot recovery"' in migrate


def test_migration_015_updates_current_sudoers_rule(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    sudoers.write_text('berry ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    result = _run_migration_015(tmp_path)

    assert result.returncode == 0, result.stderr
    assert sudoers.read_text().count('/bin/systemctl restart mello-librespot') == 1


def test_migration_015_updates_legacy_sudoers_rule(tmp_path):
    sudoers = tmp_path / 'berry-wifi'
    sudoers.write_text('berry ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    result = _run_migration_015(tmp_path)

    assert result.returncode == 0, result.stderr
    assert '/bin/systemctl restart mello-librespot' in sudoers.read_text()


def test_migration_015_is_idempotent(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    sudoers.write_text('berry ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    assert _run_migration_015(tmp_path).returncode == 0
    assert _run_migration_015(tmp_path).returncode == 0

    assert sudoers.read_text().count('/bin/systemctl restart mello-librespot') == 1


def test_migration_015_rejects_unexpected_rule_format(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    original = 'berry ALL=(ALL) NOPASSWD: /bin/systemctl restart mello-native\n'
    sudoers.write_text(original)

    result = _run_migration_015(tmp_path)

    assert result.returncode != 0
    assert sudoers.read_text() == original


# --- Spotify Soloist as an opt-in second engine --------------------------------

SUDOERS_AFTER_015 = (
    'berry ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, '
    '/bin/systemctl stop mello-librespot, /bin/systemctl start mello-librespot, '
    '/bin/systemctl restart mello-librespot, /bin/systemctl restart mello-native, '
    '/bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up\n'
)


def _run_migration_016(sudoers_dir: Path, home: Path):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    body = migrate.split('_migrate_016() {', 1)[1].split('\n}', 1)[0]
    script = textwrap.dedent(
        f'''\
        set -euo pipefail
        HOME={str(home)!r}
        MELLO_SUDOERS_DIR={str(sudoers_dir)!r}
        log() {{ :; }}
        sudo() {{
          if [ "$1" = visudo ]; then return 0; fi
          if [ "$1" = systemctl ]; then echo "$@" >> {str(home / 'systemctl.log')!r}; return 0; fi
          "$@"
        }}
        _migrate_016() {{
        {body}
        }}
        _migrate_016
        '''
    )
    return subprocess.run(['bash'], input=script, text=True, capture_output=True)


def test_migration_016_lets_the_app_switch_engines_and_keeps_the_current_one_enabled(tmp_path):
    sudoers = tmp_path / 'sudoers.d'
    sudoers.mkdir()
    (sudoers / 'mello-wifi').write_text(SUDOERS_AFTER_015)
    (tmp_path / 'mello').mkdir()

    result = _run_migration_016(sudoers, tmp_path)

    assert result.returncode == 0, result.stderr
    rule = (sudoers / 'mello-wifi').read_text()
    for command in (
        'restart mello-soloist', 'enable mello-soloist', 'disable mello-soloist',
        'stop mello-soloist', 'start mello-soloist',
        'enable mello-librespot', 'disable mello-librespot',
    ):
        assert f'/bin/systemctl {command}' in rule
    assert rule.count('\n') == 1                      # still one rule line
    assert '/bin/systemctl restart bluetooth' in rule  # nothing lost
    # The app no longer pulls go-librespot in by itself, so it must be enabled.
    assert (tmp_path / 'systemctl.log').read_text().split('\n')[0] == 'systemctl enable mello-librespot'

    again = _run_migration_016(sudoers, tmp_path)
    assert again.returncode == 0
    assert (sudoers / 'mello-wifi').read_text() == rule


def test_migration_016_keeps_soloist_enabled_on_a_speaker_already_using_it(tmp_path):
    sudoers = tmp_path / 'sudoers.d'
    sudoers.mkdir()
    (sudoers / 'mello-wifi').write_text(SUDOERS_AFTER_015)
    (tmp_path / 'mello').mkdir()
    (tmp_path / 'mello' / '.env').write_text('MELLO_SPOTIFY_ENGINE=soloist\n')

    assert _run_migration_016(sudoers, tmp_path).returncode == 0
    assert 'systemctl enable mello-soloist' in (tmp_path / 'systemctl.log').read_text()


def test_app_service_does_not_start_an_engine_by_itself():
    # Each engine runs only when enabled; a Wants= here would start
    # go-librespot next to Soloist and both would announce the speaker.
    service = (ROOT / 'pi/systemd/mello-native.service.template').read_text()
    assert 'Wants=mello-librespot' not in service
    assert 'Wants=mello-soloist' not in service


def test_soloist_service_template_is_rendered_with_the_other_services():
    service = (ROOT / 'pi/systemd/mello-soloist.service.template').read_text()
    assert 'ExecStart=__HOME__/mello/pi/soloist/run-soloist.sh' in service
    assert 'PartOf=mello-native.service' not in service


def test_nightly_update_renews_soloist_even_without_a_mello_update():
    update = (ROOT / 'pi/auto-update.sh').read_text()
    renew = update.index('soloist-setup.sh refresh')
    no_update_exit = update.index('NO_UPDATE=true\n    ;;')
    assert renew < no_update_exit


def _run_setup_script(tmp_path, command, fake_age_days=None, engine=None, key=True):
    """Run soloist-setup.sh against a fake home with a fake Soloist binary."""
    home = tmp_path / 'home'
    soloist = home / 'soloist'
    soloist.mkdir(parents=True, exist_ok=True)
    if key:
        (soloist / 'key').write_text('k' * 37 + '\n')
    if fake_age_days is not None:
        binary = soloist / 'soloist'
        binary.write_text(
            '#!/bin/bash\n'
            f'echo "soloist 1.0 build $(( $(date +%s) - {fake_age_days} * 86400 )) (x) (linux/aarch64)"\n'
        )
        binary.chmod(0o755)
    env = {
        'HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
        'MELLO_SOLOIST_HOME': str(soloist),
        # Unreachable on purpose: these tests must never download anything.
        'MELLO_SOLOIST_URL_BASE': 'http://127.0.0.1:9',
    }
    return subprocess.run(
        ['bash', str(ROOT / 'pi/soloist/soloist-setup.sh'), *command],
        env=env, text=True, capture_output=True,
    )


def test_refresh_does_nothing_on_a_speaker_without_a_key(tmp_path):
    result = _run_setup_script(tmp_path, ['refresh'], key=False)
    assert result.returncode == 0
    assert result.stdout == ''


def test_refresh_leaves_a_recent_build_alone(tmp_path):
    result = _run_setup_script(tmp_path, ['refresh'], fake_age_days=10)
    assert result.returncode == 0
    assert 'Renewing' not in result.stdout


def test_refresh_renews_a_build_past_half_its_life(tmp_path):
    result = _run_setup_script(tmp_path, ['refresh'], fake_age_days=50)
    # The (unreachable) download fails, which proves a renewal was attempted;
    # the old binary must still be there to keep playing meanwhile.
    assert 'Renewing Soloist (current build: 50 days old)' in result.stdout
    assert result.returncode != 0
    assert (tmp_path / 'home' / 'soloist' / 'soloist').exists()


def test_activate_refuses_without_a_key(tmp_path):
    result = _run_setup_script(tmp_path, ['activate'], key=False)
    assert result.returncode == 2
    assert 'no API key' in result.stdout


# --- run-soloist.sh: Soloist only logs in when it starts ------------------------

import http.server
import threading
import time


class _Internet:
    """A local stand-in for "Spotify is reachable"."""

    def __init__(self):
        self.server = None
        probe = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
        self.port = probe.server_address[1]
        probe.server_close()
        self.url = f'http://127.0.0.1:{self.port}/'

    def up(self):
        class Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), Quiet)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def down(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def _soloist_home(tmp_path, paired=True, logged_in='no'):
    """A fake ~/soloist: the "binary" notes each start and reports a login state from a file."""
    home = tmp_path / 'soloist'
    (home / 'data' / 'cache').mkdir(parents=True)
    (home / 'key').write_text('k' * 37 + '\n')
    (home / 'login').write_text(logged_in)
    if paired:
        (home / 'data' / 'cache' / 'Users').mkdir()
        (home / 'data' / 'cache' / 'Users' / 'someone-user').mkdir()
    binary = home / 'soloist'
    binary.write_text(
        '#!/bin/bash\n'
        'here="$(cd "$(dirname "$0")" && pwd)"\n'
        'if [ "$1" = ctl ]; then echo "  logged in: $(cat "$here/login")"; exit 0; fi\n'
        'date +%s.%N >> "$here/starts"\n'
        'trap "exit 0" TERM\n'
        'while true; do sleep 0.2; done\n'
    )
    binary.chmod(0o755)
    return home


def _start_wrapper(home, internet, boot_wait=6):
    env = {
        'HOME': str(home.parent), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
        'MELLO_SOLOIST_HOME': str(home),
        'MELLO_SOLOIST_CHECK_URL': internet.url,
        'MELLO_SOLOIST_BOOT_WAIT': str(boot_wait),
        'MELLO_SOLOIST_LOGIN_CHECK_EVERY': '1',
    }
    return subprocess.Popen(
        ['bash', str(ROOT / 'pi/soloist/run-soloist.sh')],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _starts(home):
    path = home / 'starts'
    return len(path.read_text().split()) if path.exists() else 0


def _wait_until(condition, timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def _stop(process):
    process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        raise


def test_soloist_is_not_started_before_the_internet_is_there(tmp_path):
    # Seen on a speaker after a power cut: Soloist started while WiFi was
    # still connecting, its one login attempt failed, and it stayed logged out.
    home = _soloist_home(tmp_path, logged_in='yes')
    internet = _Internet()
    process = _start_wrapper(home, internet, boot_wait=20)
    try:
        time.sleep(3)
        assert _starts(home) == 0                # still waiting for WiFi
        internet.up()
        assert _wait_until(lambda: _starts(home) == 1)
    finally:
        assert _stop(process) == 0               # being stopped is not a failure
        internet.down()


def test_soloist_is_started_again_when_it_is_logged_out_on_a_paired_speaker(tmp_path):
    home = _soloist_home(tmp_path, logged_in='no')
    internet = _Internet()
    process = _start_wrapper(home, internet, boot_wait=2)    # gives up waiting: starts offline
    try:
        assert _wait_until(lambda: _starts(home) == 1)
        time.sleep(3)
        assert _starts(home) == 1                # offline: restarting would not help
        internet.up()                            # WiFi comes back
        assert _wait_until(lambda: _starts(home) == 2)
        (home / 'login').write_text('yes')       # this time the login works
        time.sleep(3)
        assert _starts(home) == 2                # and it is left alone
    finally:
        _stop(process)
        internet.down()


def test_new_speaker_waiting_for_its_owner_is_not_restarted(tmp_path):
    home = _soloist_home(tmp_path, paired=False, logged_in='no')
    internet = _Internet()
    internet.up()
    process = _start_wrapper(home, internet)
    try:
        assert _wait_until(lambda: _starts(home) == 1)
        time.sleep(4)
        assert _starts(home) == 1
    finally:
        _stop(process)
        internet.down()


def test_login_that_keeps_failing_is_not_retried_for_ever(tmp_path):
    home = _soloist_home(tmp_path, logged_in='no')           # e.g. a revoked key
    internet = _Internet()
    internet.up()
    process = _start_wrapper(home, internet)
    try:
        assert _wait_until(lambda: _starts(home) == 6, timeout=30)   # first start + 5 retries
        time.sleep(4)
        assert _starts(home) == 6
    finally:
        _stop(process)
        internet.down()



def test_menu_update_runs_outside_mello_so_the_restart_does_not_cut_it_off(monkeypatch, tmp_path):
    import mello.managers.setup_menu as sm
    monkeypatch.setattr(sm.shutil, 'which', lambda name: '/usr/bin/systemd-run')
    cmd = sm._update_command(tmp_path / 'mello-update.log')
    assert cmd[:3] == ['systemd-run', '--user', '--wait']   # a job of its own, result still reported
    assert f'StandardOutput=append:{tmp_path}/mello-update.log' in cmd
    assert cmd[-2:] == ['bash', f'{sm._REPO_DIR}/pi/auto-update.sh']


def test_menu_update_still_runs_without_systemd_run(monkeypatch, tmp_path):
    import mello.managers.setup_menu as sm
    monkeypatch.setattr(sm.shutil, 'which', lambda name: None)
    cmd = sm._update_command(tmp_path / 'u.log')
    assert cmd[0] == 'bash' and 'auto-update.sh' in cmd[-1] and str(tmp_path / 'u.log') in cmd[-1]


def test_wifi_power_save_is_switched_off_on_new_and_existing_speakers(tmp_path):
    setup = (ROOT / 'pi/setup.sh').read_text()
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    conf = (ROOT / 'pi/networkmanager/99-mello-wifi-powersave.conf').read_text()
    assert 'wifi.powersave = 2' in conf
    assert '99-mello-wifi-powersave.conf /etc/NetworkManager/conf.d/' in setup
    assert 'run_migration "017"' in migrate

    body = migrate.split('_migrate_017() {', 1)[1].split('\n}', 1)[0]
    home = tmp_path / 'home'
    (home / 'mello/pi/networkmanager').mkdir(parents=True)
    (home / 'mello/pi/networkmanager/99-mello-wifi-powersave.conf').write_text(conf)
    conf_dir = tmp_path / 'nm'
    script = textwrap.dedent(f'''\
        set -euo pipefail
        HOME={str(home)!r}
        MELLO_NM_CONF_DIR={str(conf_dir)!r}
        log() {{ :; }}
        sudo() {{ case "$1" in *iw) return 0;; esac; "$@"; }}
        _migrate_017() {{
        {body}
        }}
        _migrate_017
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (conf_dir / '99-mello-wifi-powersave.conf').read_text() == conf


def _run_migration_018(tmp_path, cloud_status, exit_code=0):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    body = migrate.split('_migrate_018() {', 1)[1].split('\n}', 1)[0]
    cloud_dir = tmp_path / 'cloud'
    cloud_dir.mkdir()
    script = textwrap.dedent(f'''\
        set -euo pipefail
        MELLO_CLOUD_DIR={str(cloud_dir)!r}
        log() {{ :; }}
        sudo() {{ "$@"; }}
        cloud-init() {{ echo {cloud_status!r}; return {exit_code}; }}
        _migrate_018() {{
        {body}
        }}
        _migrate_018
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return (cloud_dir / 'cloud-init.disabled').exists()


def test_cloud_init_is_skipped_on_later_boots(tmp_path):
    assert 'run_migration "018"' in (ROOT / 'pi/migrate.sh').read_text()
    assert 'cloud-init.disabled' in (ROOT / 'pi/setup.sh').read_text()
    assert _run_migration_018(tmp_path, 'status: done') is True


def test_cloud_init_first_boot_is_never_cut_short(tmp_path):
    assert _run_migration_018(tmp_path, 'status: running') is False


def test_cloud_init_done_with_warnings_counts_as_done(tmp_path):
    # Real Pi: "status: done" with exit code 2 (degraded done)
    assert _run_migration_018(tmp_path, 'status: done', exit_code=2) is True


def _run_migration_019(tmp_path, installed):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    assert 'run_migration "019"' in migrate
    body = migrate.split('_migrate_019() {', 1)[1].split('\n}', 1)[0]
    calls = tmp_path / 'calls'
    script = textwrap.dedent(f'''\
        set -euo pipefail
        log() {{ :; }}
        dpkg() {{ return {0 if installed else 1}; }}
        sudo() {{ echo "$*" >> {str(calls)!r}; }}
        _migrate_019() {{
        {body}
        }}
        _migrate_019
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return calls.read_text() if calls.exists() else ''


def test_slint_libraries_are_installed(tmp_path):
    calls = _run_migration_019(tmp_path, installed=False)
    assert 'apt-get update' in calls
    assert 'apt-get install -y -q libinput10' in calls
    assert 'libinput10' in (ROOT / 'pi/setup.sh').read_text()


def test_slint_libraries_already_there(tmp_path):
    assert _run_migration_019(tmp_path, installed=True) == ''


def test_slint_package_only_where_it_has_wheels():
    req = (ROOT / 'requirements.txt').read_text()
    line = next(l for l in req.splitlines() if l.startswith('slint') and 'armv7l' in l)
    assert '==' in line
    # 64-bit and 32-bit Pi OS (a 32-bit system on a 64-bit kernel says aarch64 or armv8l)
    assert "platform_machine in 'aarch64 armv7l armv8l'" in line
    # Its wheels need Python 3.12+: older systems skip it instead of failing the update
    assert "python_version >= '3.12'" in line


def test_nightly_update_catches_up_and_cannot_hang_forever():
    timer = (ROOT / 'pi/systemd/mello-update.timer').read_text()
    service = (ROOT / 'pi/systemd/mello-update.service.template').read_text()
    assert 'OnCalendar=*-*-* 03:00:00' in timer and 'Persistent=true' in timer
    assert 'TimeoutStartSec=45min' in service and 'auto-update.sh' in service
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    assert 'run_migration "021"' in migrate
    # cron is only dropped after the timer is confirmed running
    body = migrate.split('_migrate_021() {', 1)[1].split('\n}\n', 1)[0]
    assert body.index('is-active --quiet mello-update.timer') < body.index('crontab -')
    # the update itself keeps timer units linked, like the services
    assert '*.timer' in (ROOT / 'pi/auto-update.sh').read_text()


def test_manual_update_is_stopped_if_it_hangs(tmp_path, monkeypatch):
    from mello.managers import setup_menu as sm
    monkeypatch.setattr(sm.shutil, 'which', lambda name: '/usr/bin/systemd-run')
    assert 'RuntimeMaxSec=45min' in sm._update_command(tmp_path / 'u.log')


def test_no_pygame_left():
    assert 'pygame' not in (ROOT / 'requirements.txt').read_text().lower()
    setup = (ROOT / 'pi/setup.sh').read_text()
    assert 'python3-pygame' not in setup and 'libsdl2' not in setup
    for path in (ROOT / 'mello').rglob('*.py'):
        assert 'import pygame' not in path.read_text(), path


def test_setup_and_migration_install_the_same_screen_libraries():
    def libs(path):
        line = next(l for l in (ROOT / path).read_text().splitlines() if l.startswith('SLINT_LIBS='))
        return set(line.split('=', 1)[1].strip('"').split())
    assert libs('pi/setup.sh') == libs('pi/migrate.sh')
    assert {'libinput10', 'libgbm1', 'libegl1', 'libgles2', 'libfontconfig1'} <= libs('pi/setup.sh')


def _run_migration_020(tmp_path, installed):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    assert 'run_migration "020"' in migrate
    body = migrate.split('_migrate_020() {', 1)[1].split('\n}', 1)[0]
    calls = tmp_path / 'calls'
    script = textwrap.dedent(f'''\
        set -euo pipefail
        log() {{ :; }}
        SLINT_LIBS="libinput10 libgbm1"
        dpkg() {{ [ "$2" = libinput10 ] || return {0 if installed else 1}; }}
        sudo() {{ echo "$*" >> {str(calls)!r}; }}
        _migrate_020() {{
        {body}
        }}
        _migrate_020
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return calls.read_text() if calls.exists() else ''


def test_missing_screen_libraries_are_installed(tmp_path):
    calls = _run_migration_020(tmp_path, installed=False)
    assert 'apt-get install -y -q libgbm1' in calls
    assert 'libinput10' not in calls.split('apt-get install')[1]


def test_screen_libraries_already_there(tmp_path):
    assert _run_migration_020(tmp_path, installed=True) == ''


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                   env={'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
                        'GIT_COMMITTER_EMAIL': 't@t', 'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'})
    return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True).stdout.strip()


def _speaker(tmp_path, slint_works: bool, python='3.11.2'):
    """A speaker checkout: an old version with Pygame's screen, then the one without."""
    repo = tmp_path / 'mello'
    (repo / 'mello/ui').mkdir(parents=True)
    _git(repo, 'init', '-q', '-b', 'main')
    (repo / 'mello/ui/helpers.py').write_text('# pygame drawing\n')
    (repo / 'requirements.txt').write_text('requests\n')
    old = _git(repo, 'add', '-A') and _git(repo, 'commit', '-qm', 'Pygame screen')
    (repo / 'mello/ui/helpers.py').unlink()
    _git(repo, 'add', '-A')
    new = _git(repo, 'commit', '-qm', 'Slint only')
    (repo / 'venv/bin').mkdir(parents=True)
    python_stub = repo / 'venv/bin/python'
    python_stub.write_text(textwrap.dedent(f'''\
        #!/bin/bash
        case "$*" in
          *"import slint"*) exit {0 if slint_works else 1} ;;
          *version_info*) [ "{python}" \\< "3.12" ] && exit 1; exit 0 ;;
          *python_version*) echo {python} ;;
        esac
        '''))
    python_stub.chmod(0o755)
    pip_stub = repo / 'venv/bin/pip'
    pip_stub.write_text(f'#!/bin/bash\necho "$*" >> {tmp_path}/pip_calls\n')
    pip_stub.chmod(0o755)
    return repo, old, new


def _run_hold_check(tmp_path, repo):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    assert 'slint_hold_check "$CODE_ROOT"' in migrate
    body = migrate.split('slint_hold_check() {', 1)[1].split('\n}', 1)[0]
    script = textwrap.dedent(f'''\
        set -euo pipefail
        log() {{ echo "$*"; }}
        HOME={str(tmp_path)!r}
        SLINT_HOLD_FILE="$HOME/.mello-slint-hold"
        slint_hold_check() {{
        {body}
        }}
        slint_hold_check {str(repo)!r}
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    return result.stdout, head


def test_speaker_without_slint_stays_on_the_last_pygame_version(tmp_path):
    repo, old, new = _speaker(tmp_path, slint_works=False, python='3.11.2')
    out, head = _run_hold_check(tmp_path, repo)
    assert head == old
    assert 'HOLD' in out and 'Python 3.11.2' in out and 'Trixie' in out
    assert 'kept on' in (tmp_path / '.mello-slint-hold').read_text()
    assert not (tmp_path / 'pip_calls').exists()       # nothing to install on Python 3.11


def test_speaker_with_slint_moves_on(tmp_path):
    repo, old, new = _speaker(tmp_path, slint_works=True, python='3.13.5')
    (tmp_path / '.mello-slint-hold').write_text('old hold')
    out, head = _run_hold_check(tmp_path, repo)
    assert head == new
    assert not (tmp_path / '.mello-slint-hold').exists()


def test_trixie_speaker_without_the_package_gets_it_installed_first(tmp_path):
    repo, old, new = _speaker(tmp_path, slint_works=False, python='3.13.5')
    out, head = _run_hold_check(tmp_path, repo)
    assert 'install' in (tmp_path / 'pip_calls').read_text()
    # (the stub still cannot import it, so it is held back after trying)
    assert head == old


import pytest


# --- soloist_hold_check: a speaker not on Soloist never runs a version without librespot ---

def _librespot_speaker(tmp_path, engine_line):
    """main: a librespot version, then (merged with a merge commit) the removal."""
    repo = tmp_path / 'mello'
    (repo / 'pi').mkdir(parents=True)
    _git(repo, 'init', '-q', '-b', 'main')
    (repo / 'a.txt').write_text('a')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'with librespot')
    _git(repo, 'checkout', '-qb', 'remove')
    (repo / 'pi/needs-soloist').write_text('')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'librespot removed')
    _git(repo, 'checkout', '-q', 'main')
    (repo / 'b.txt').write_text('b')
    _git(repo, 'add', '-A')
    last = _git(repo, 'commit', '-qm', 'last with librespot')
    _git(repo, 'merge', '-q', '--no-ff', '-m', 'merge removal', 'remove')
    new = _git(repo, 'rev-parse', 'HEAD')
    if engine_line is not None:
        (repo / '.env').write_text(engine_line + '\n')
    return repo, last, new


def _run_soloist_hold(tmp_path, repo, returncode=0):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    assert 'if soloist_hold_check "$(cd' in migrate
    # It runs before the migrations: a held speaker must not get the new ones.
    assert migrate.index('if soloist_hold_check "$(cd') < migrate.index('run_migration "001"')
    # Held: the older version's own migrations run, not this file's.
    assert 'exec bash "$(cd' in migrate.split('if soloist_hold_check "$(cd', 1)[1].split('\nfi', 1)[0]
    body = migrate.split('soloist_hold_check() {', 1)[1].split('\n}', 1)[0]
    script = textwrap.dedent(f'''\
        set -euo pipefail
        log() {{ echo "$*"; }}
        HOME={str(tmp_path)!r}
        SOLOIST_HOLD_FILE="$HOME/.mello-soloist-hold"
        soloist_hold_check() {{
        {body}
        }}
        if soloist_hold_check {str(repo)!r}; then echo HELD; fi
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == returncode, result.stderr
    return result.stdout, _git(repo, 'rev-parse', 'HEAD')


def test_hold_fails_closed_when_the_old_version_is_unknown(tmp_path):
    # The marker is there but not in the history: stop before any migration
    # (024 would remove go-librespot) instead of carrying on.
    repo = tmp_path / 'mello'
    (repo / 'pi').mkdir(parents=True)
    _git(repo, 'init', '-q', '-b', 'main')
    (repo / 'a.txt').write_text('a')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'some version')
    (repo / 'pi/needs-soloist').write_text('')
    out, head = _run_soloist_hold(tmp_path, repo, returncode=1)
    assert 'HELD' not in out and 'stopping' in out


@pytest.mark.parametrize('engine_line', [None, 'MELLO_SPOTIFY_ENGINE=librespot',
                                         'MELLO_SPOTIFY_ENGINE=soloist-disabled'])
def test_speaker_not_on_soloist_goes_back_to_the_last_librespot_version(tmp_path, engine_line):
    repo, last, new = _librespot_speaker(tmp_path, engine_line)
    out, head = _run_soloist_hold(tmp_path, repo)
    assert 'HELD' in out and 'HOLD' in out
    assert head == last
    assert 'kept on' in (tmp_path / '.mello-soloist-hold').read_text()


def test_speaker_on_soloist_moves_on(tmp_path):
    repo, last, new = _librespot_speaker(tmp_path, 'MELLO_SPOTIFY_ENGINE=soloist')
    (tmp_path / '.mello-soloist-hold').write_text('old hold')
    out, head = _run_soloist_hold(tmp_path, repo)
    assert 'HELD' not in out
    assert head == new
    assert not (tmp_path / '.mello-soloist-hold').exists()


def test_new_speakers_are_set_up_on_soloist_and_64_bit_only():
    setup = (ROOT / 'pi/setup.sh').read_text()
    assert 'MELLO_SPOTIFY_ENGINE=soloist' in setup        # or the update gate would hold it
    assert 'aarch64-64|arm64-64) ;;' in setup
    assert 'librespot' not in setup


def test_deactivate_keeps_the_speaker_on_soloist(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'sudo').write_text('#!/bin/bash\nexit 0\n')
    (bin_dir / 'sudo').chmod(0o755)
    script = (ROOT / 'pi/soloist/soloist-setup.sh').read_text()
    copy = tmp_path / 'repo/pi/soloist/soloist-setup.sh'
    copy.parent.mkdir(parents=True)
    copy.write_text(script)
    (tmp_path / 'repo/.env').write_text('MELLO_SPOTIFY_ENGINE=soloist\n')
    result = subprocess.run(['bash', str(copy), 'deactivate'], text=True, capture_output=True,
                            env={'HOME': str(tmp_path), 'PATH': f'{bin_dir}:/usr/bin:/bin'})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'repo/.env').read_text() == 'MELLO_SPOTIFY_ENGINE=soloist\n'


# --- every run: screen libraries and the checks after a failed migration ---

def _run_ensure_screen_libs(tmp_path, missing):
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    body = migrate.split('ensure_screen_libs() {', 1)[1].split('\n}', 1)[0]
    calls = tmp_path / 'calls'
    script = textwrap.dedent(f'''\
        set -euo pipefail
        log() {{ :; }}
        SLINT_LIBS="libinput10 libgbm1 libegl1"
        dpkg() {{ case " {' '.join(missing)} " in *" $2 "*) return 1 ;; esac; }}
        sudo() {{ echo "$*" >> {str(calls)!r}; }}
        ensure_screen_libs() {{
        {body}
        }}
        ensure_screen_libs
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return calls.read_text() if calls.exists() else ''


def test_missing_libinput_is_installed_even_after_its_migration_ran(tmp_path):
    # Migrations 019/020 have their markers, but libinput10 is gone (the report
    # that led to this check: the screen said "needs a newer Raspberry Pi OS").
    calls = _run_ensure_screen_libs(tmp_path, ['libinput10'])
    assert 'apt-get install -y -q libinput10' in calls
    assert 'libgbm1' not in calls


def test_screen_libraries_present_means_no_apt(tmp_path):
    assert _run_ensure_screen_libs(tmp_path, []) == ''


def _run_migration_list(tmp_path, failing_id):
    """The bottom of migrate.sh with stub migrations, one of which fails."""
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    tail = migrate[migrate.index('MIGRATIONS_OK=true'):]
    script = textwrap.dedent(f'''\
        set -euo pipefail
        log() {{ echo "$*"; }}
        run_migration() {{ echo "migration $1"; [ "$1" != "{failing_id}" ]; }}
        ensure_screen_libs() {{ echo "screen libs checked"; }}
        slint_hold_check() {{ echo "slint checked"; }}
        ''') + tail
    return subprocess.run(['bash'], input=script, text=True, capture_output=True)


def test_failed_migration_does_not_skip_the_screen_checks(tmp_path):
    result = _run_migration_list(tmp_path, '003')
    assert result.returncode == 1
    out = result.stdout
    assert 'migration 003' in out and 'migration 004' not in out   # order kept
    assert 'screen libs checked' in out and 'slint checked' in out


def test_all_migrations_ok(tmp_path):
    result = _run_migration_list(tmp_path, 'none')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'migration 024' in result.stdout and 'screen libs checked' in result.stdout


def test_speaker_already_on_the_pygame_version_is_not_moved_further_back(tmp_path):
    repo, old, new = _speaker(tmp_path, slint_works=False, python='3.11.2')
    _git(repo, 'checkout', '-q', '--detach', old)
    out, head = _run_hold_check(tmp_path, repo)
    assert head == old and 'HOLD' not in out


def _soloist_files(tmp_path, binary=True):
    soloist = tmp_path / 'soloist'
    soloist.mkdir()
    (soloist / 'key').write_text('k' * 37 + '\n')
    if binary:
        (soloist / 'soloist').write_text('#!/bin/bash\n')
        (soloist / 'soloist').chmod(0o755)


def test_soloist_key_without_engine_setting_is_not_sent_back(tmp_path):
    repo, last, new = _librespot_speaker(tmp_path, 'MELLO_UPDATE_CHANNEL=beta')
    _soloist_files(tmp_path)
    out, head = _run_soloist_hold(tmp_path, repo)
    assert 'HELD' not in out and head == new
    assert (repo / '.env').read_text().splitlines() == [
        'MELLO_UPDATE_CHANNEL=beta', 'MELLO_SPOTIFY_ENGINE=soloist']


def test_explicit_other_engine_setting_is_left_alone(tmp_path):
    repo, last, new = _librespot_speaker(tmp_path, 'MELLO_SPOTIFY_ENGINE=librespot')
    _soloist_files(tmp_path)
    out, head = _run_soloist_hold(tmp_path, repo)
    assert 'HELD' in out and head == last


def test_soloist_key_without_the_program_is_still_held(tmp_path):
    repo, last, new = _librespot_speaker(tmp_path, None)
    _soloist_files(tmp_path, binary=False)
    out, head = _run_soloist_hold(tmp_path, repo)
    assert 'HELD' in out and head == last

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


def test_librespot_service_is_not_part_of_ui_service():
    service = (ROOT / 'pi/systemd/mello-librespot.service.template').read_text()

    assert 'PartOf=mello-native.service' not in service


def test_migration_014_registered_for_librespot_dependency_update():
    migrate = (ROOT / 'pi/migrate.sh').read_text()

    assert '_migrate_014()' in migrate
    assert 'run_migration "014" "Keep librespot independent of UI sleep/restarts"' in migrate


def test_librespot_recovery_permission_is_installed_and_migrated():
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    setup = (ROOT / 'pi/setup.sh').read_text()
    migration_015 = _migration_015_body()

    assert '/bin/systemctl restart mello-librespot' in setup
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
    no_update_exit = update.index('exit 0  # No updates')
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
    line = next(l for l in req.splitlines() if l.startswith('slint'))
    assert "platform_machine == 'aarch64'" in line and '==' in line

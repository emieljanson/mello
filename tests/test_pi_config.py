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


def test_refresh_finishes_a_switch_that_was_waiting_for_internet(tmp_path):
    home = tmp_path / 'home'
    (home / 'soloist').mkdir(parents=True)
    (home / 'soloist' / 'activate-pending').touch()
    result = _run_setup_script(tmp_path, ['refresh'], fake_age_days=5)
    # It went for the switch (and stops at the first thing this fake home lacks).
    assert 'mello-soloist service is not installed' in result.stdout or result.returncode != 0

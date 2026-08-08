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

"""
Runs pi/auto-update.sh against a throwaway git "GitHub" and fake systemd, to
check which version a speaker ends up on: beta vs stable channel, never going
backwards, and rolling back an update whose services do not stay up.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'pi' / 'auto-update.sh'
TARGET_SCRIPT = ROOT / 'pi' / 'update-target.sh'

pytestmark = pytest.mark.skipif(
    shutil.which('flock') is None, reason='auto-update.sh needs flock (Linux)')

# systemd stand-in: services "crash" while the checked-out code has a CRASH file.
SYSTEMCTL = '''#!/bin/bash
case "$1" in
  is-active) [ -f "$HOME/mello/CRASH" ] && exit 3; exit 0 ;;
  show)
    n=$(cat "$HOME/nrestarts" 2>/dev/null || echo 0)
    if [ -f "$HOME/mello/CRASH" ]; then n=$((n + 1)); echo "$n" > "$HOME/nrestarts"; fi
    echo "$n" ;;
  *) exit 0 ;;
esac
'''
# Only systemctl goes through (to the fake one); everything else run with
# sudo (unit files, boot splash) is skipped so the test never touches the host.
SUDO = '''#!/bin/bash
if [ "$1" = systemctl ]; then exec "$@"; fi
if [ "$1" = tee ]; then cat > /dev/null; fi
exit 0
'''


class Speaker:
    def __init__(self, tmp_path):
        self.home = tmp_path / 'home'
        self.home.mkdir()
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        for name, body in (('systemctl', SYSTEMCTL), ('sudo', SUDO), ('sleep', '#!/bin/bash\n')):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'PATH': f'{bin_dir}:{os.environ["PATH"]}',
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
            'GIT_CONFIG_GLOBAL': '/dev/null',
        }
        # "GitHub": the URL must look like emieljanson/mello for the script.
        self.origin = tmp_path / 'emieljanson' / 'mello.git'
        self.work = tmp_path / 'dev'
        self.git('init', '-q', '-b', 'main', str(self.work), cwd=tmp_path)
        (self.work / 'pi').mkdir()
        shutil.copy(TARGET_SCRIPT, self.work / 'pi' / 'update-target.sh')
        self.first = self.commit('first', push=False)
        self.git('clone', '-q', '--bare', str(self.work), str(self.origin), cwd=tmp_path)
        self.git('remote', 'add', 'origin', str(self.origin))
        self.git('clone', '-q', str(self.origin), str(self.home / 'mello'), cwd=tmp_path)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.work, env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, name, crash=False, push=True):
        (self.work / f'{name}.txt').write_text(name)
        crash_file = self.work / 'CRASH'
        if crash:
            crash_file.write_text('boom')
        elif crash_file.exists():
            crash_file.unlink()
        self.git('add', '-A')
        self.git('commit', '-q', '-m', name)
        if push:
            self.git('push', '-q', 'origin', 'main')
        return self.git('rev-parse', 'HEAD')

    def release(self, tag, commit):
        self.git('tag', tag, commit)
        self.git('push', '-q', 'origin', tag)

    def set_channel(self, channel):
        (self.home / 'mello' / '.env').write_text(f'MELLO_UPDATE_CHANNEL={channel}\n')

    def update(self):
        result = subprocess.run(['bash', str(SCRIPT)], env=self.env,
                                capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr

    def check(self):
        """What "Check for updates" in the settings menu sees."""
        return subprocess.run(['bash', str(self.home / 'mello' / 'pi' / 'update-target.sh')],
                              env=self.env, capture_output=True, text=True,
                              check=True).stdout.split()

    @property
    def version(self):
        return self.git('rev-parse', 'HEAD', cwd=self.home / 'mello')


@pytest.fixture
def speaker(tmp_path):
    return Speaker(tmp_path)


def test_stable_follows_main_until_the_first_release(speaker):
    newest = speaker.commit('second')
    assert speaker.update()[0] == 0
    assert speaker.version == newest


def test_stable_goes_to_the_newest_release_not_main(speaker):
    second = speaker.commit('second')
    third = speaker.commit('third')
    speaker.commit('fourth')
    speaker.release('v2026.11.1', second)
    speaker.release('v2026.11.2', third)
    assert speaker.update()[0] == 0
    assert speaker.version == third


def test_beta_follows_main_even_with_releases(speaker):
    second = speaker.commit('second')
    newest = speaker.commit('third')
    speaker.release('v2026.11.1', second)
    speaker.set_channel('beta')
    assert speaker.update()[0] == 0
    assert speaker.version == newest


def test_stable_never_goes_back_to_an_older_release(speaker):
    newest = speaker.commit('second')
    speaker.update()                      # still follows main: no release yet
    assert speaker.version == newest
    speaker.release('v2026.11.1', speaker.first)
    code, out = speaker.update()
    assert code == 0
    assert speaker.version == newest
    assert 'older than the running code' in out


def test_broken_update_is_rolled_back_and_not_retried(speaker):
    speaker.set_channel('beta')
    good = speaker.version
    bad = speaker.commit('broken', crash=True)

    code, out = speaker.update()
    assert code == 1
    assert 'ROLLBACK' in out and 'Rollback complete' in out
    assert speaker.version == good
    assert (speaker.home / '.mello-bad-update').read_text().strip() == bad

    code, out = speaker.update()          # next night: same broken version
    assert code == 0
    assert speaker.version == good
    assert 'failed its health check before' in out

    fixed = speaker.commit('fixed')       # the fix is merged
    code, out = speaker.update()
    assert code == 0, out
    assert speaker.version == fixed
    assert not (speaker.home / '.mello-bad-update').exists()


def test_check_for_updates_agrees_with_the_nightly_update(speaker):
    assert speaker.check()[:3] == ['current', 'stable', 'origin/main']
    second = speaker.commit('second')
    speaker.commit('third')
    speaker.release('v2026.11.1', second)
    assert speaker.check() == ['update', 'stable', 'v2026.11.1', second]
    speaker.set_channel('beta')
    assert speaker.check()[:3] == ['update', 'beta', 'origin/main']

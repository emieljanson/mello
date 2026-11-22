#!/bin/bash
# Mello Auto-Update Script
# Runs nightly via mello-update.timer (cron on installs from before it),
# and from "Check for updates" in the settings menu
#
# The entire script body is wrapped in main() so bash reads it fully into
# memory before executing.  This prevents corruption when git pull updates
# this file while it is running.

main() {
set -Eeuo pipefail

# Support transitional directory names (berry → tomo → mello)
if [ -d ~/mello ]; then
  cd ~/mello
elif [ -d ~/tomo ]; then
  cd ~/tomo
elif [ -d ~/berry ]; then
  cd ~/berry
else
  exit 1
fi

# Load install environment (username, home, uid)
# Support all transitional env file names
if [ -f ~/mello/.mello-env ]; then
  source ~/mello/.mello-env
elif [ -f ~/tomo/.tomo-env ]; then
  source ~/tomo/.tomo-env
  MELLO_USER="${TOMO_USER:-$USER}"
  MELLO_HOME="${TOMO_HOME:-$HOME}"
  MELLO_UID="${TOMO_UID:-$(id -u)}"
elif [ -f ~/berry/.berry-env ]; then
  source ~/berry/.berry-env
  MELLO_USER="${BERRY_USER:-$USER}"
  MELLO_HOME="${BERRY_HOME:-$HOME}"
  MELLO_UID="${BERRY_UID:-$(id -u)}"
else
  MELLO_USER="$USER"
  MELLO_HOME="$HOME"
  MELLO_UID="$(id -u)"
fi

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [auto-update] $*"
}

# Avoid overlapping updates from cron/manual invocations.
LOCK_FILE="/tmp/mello-auto-update.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Skip: lock file present ($LOCK_FILE)"
  exit 0
fi

# How the last run ended, for the settings menu (mello/update_status.py):
#   time=<when>  result=ok|no-internet|failed  message=<why>  last_ok=<when>
# Without it a speaker that cannot update looks exactly like one that is
# up to date, to its owner and to us.
STATUS_FILE="$HOME/.mello-update-status"
STATUS_WRITTEN=false
write_status() {
  local last_ok now
  now=$(date +%s)
  last_ok=$(sed -n 's/^last_ok=//p' "$STATUS_FILE" 2>/dev/null || true)
  if [ "$1" = ok ]; then
    last_ok=$now
  fi
  printf 'time=%s\nresult=%s\nmessage=%s\nlast_ok=%s\n' \
    "$now" "$1" "$2" "$last_ok" > "$STATUS_FILE.tmp" \
    && mv "$STATUS_FILE.tmp" "$STATUS_FILE" || true
  STATUS_WRITTEN=true
}
# A run that stops on an unexpected error (set -e) still says so.
LAST_FAILED_COMMAND=""
trap 'LAST_FAILED_COMMAND=$BASH_COMMAND' ERR
on_exit() {
  local code=$?
  if [ "$STATUS_WRITTEN" = false ]; then
    if [ "$code" = 0 ]; then
      write_status ok ""
    else
      log "ERROR: update stopped (exit $code) at: $LAST_FAILED_COMMAND"
      write_status failed "stopped at: $LAST_FAILED_COMMAND"
    fi
  fi
}
trap on_exit EXIT

REPO_URL="https://github.com/emieljanson/mello.git"

# Ensure we have a healthy git repo.  If .git is missing or fetch fails for
# non-network reasons (corrupt repo, no remote, manual SCP deploy, etc.),
# re-clone from scratch while preserving user data.
_ensure_git_repo() {
  # Quick health-check: is this a valid git repo with the right remote?
  if git rev-parse --git-dir >/dev/null 2>&1 \
     && git remote get-url origin 2>/dev/null | grep -q "emieljanson/mello"; then
    return 0  # repo looks fine
  fi

  log "Git repo missing or broken — re-cloning from $REPO_URL"
  local parent
  parent="$(dirname "$(pwd)")"
  local current
  current="$(basename "$(pwd)")"

  # Backup user data
  local data_backup="/tmp/mello-reclone-data.$$"
  if [ -d data ] && [ "$(ls -A data/ 2>/dev/null)" ]; then
    cp -a data "$data_backup"
  fi

  # Backup env file (support transitional names)
  local env_backup="/tmp/mello-reclone-env.$$"
  for env_file in .mello-env .tomo-env .berry-env; do
    if [ -f "$env_file" ]; then
      cp "$env_file" "$env_backup"
      break
    fi
  done

  # Backup .env (POSTHOG_API_KEY and any other user secrets)
  local dotenv_backup="/tmp/mello-reclone-dotenv.$$"
  if [ -f .env ]; then
    cp .env "$dotenv_backup"
  fi

  # Re-clone into a temp dir, then swap
  local tmp_clone="/tmp/mello-reclone-repo.$$"
  local clone_err
  if ! clone_err=$(git clone --quiet --depth 1 "$REPO_URL" "$tmp_clone" 2>&1); then
    clone_err=$(echo "$clone_err" | tr '\n' ' ')
    log "Re-clone failed, will retry next run: $clone_err"
    if echo "$clone_err" | grep -qiE "could not resolve|unable to access|timed out|network is unreachable"; then
      write_status no-internet "re-clone: $clone_err"
    else
      write_status failed "re-clone: $clone_err"
    fi
    rm -rf "$tmp_clone" "$data_backup" "$env_backup" "$dotenv_backup"
    exit 0
  fi

  # Swap: move broken dir out, move clone in
  cd "$parent"
  mv "$current" "/tmp/mello-broken-backup.$$"
  if ! mv "$tmp_clone" "$current"; then
    log "Swap failed — restoring original directory"
    mv "/tmp/mello-broken-backup.$$" "$current"
    rm -rf "$tmp_clone" "$data_backup" "$env_backup" "$dotenv_backup"
    exit 1
  fi
  cd "$current"

  # Unshallow so future fetches work normally
  git fetch --unshallow origin main 2>/dev/null || true

  # Restore user data and env
  if [ -d "$data_backup" ]; then
    mkdir -p data
    cp -a "$data_backup"/. data/ 2>/dev/null || true
    rm -rf "$data_backup"
  fi
  if [ -f "$env_backup" ]; then
    cp "$env_backup" .mello-env
    rm -f "$env_backup"
  fi
  if [ -f "$dotenv_backup" ]; then
    cp "$dotenv_backup" .env
    rm -f "$dotenv_backup"
  fi

  # Recreate venv (--system-site-packages: older versions use apt's
  # python3-pygame, which a speaker held on such a version still needs).
  log "Recreating venv"
  rm -rf venv
  python3 -m venv --system-site-packages venv
  source venv/bin/activate
  pip install -q --disable-pip-version-check -r requirements.txt
  deactivate

  # Clean up broken backup (keep for a bit in case of issues)
  rm -rf "/tmp/mello-broken-backup.$$"

  RECLONED=true
  log "Re-clone complete, repo is healthy"
}

RECLONED=false
_ensure_git_repo

# Which version should run here (beta/stable channel, see update-target.sh).
DECISION=$(bash pi/update-target.sh 2>"$HOME/.mello-update-target.err") || {
  code=$?
  reason=$(tr '\n' ' ' < "$HOME/.mello-update-target.err")
  if [ "$code" = 2 ]; then
    log "GitHub not reachable, will retry next run: $reason"
    write_status no-internet "$reason"
  else
    log "ERROR: could not decide the version (exit $code): $reason"
    write_status failed "update-target: $reason"
  fi
  exit 0
}
read -r STATUS CHANNEL TARGET_NAME TARGET <<< "$DECISION"
LOCAL=$(git rev-parse HEAD)
BAD_UPDATE_FILE="$HOME/.mello-bad-update"

# Speakers on Spotify Soloist: its binary stops working 90 days after it was
# built, so renew it in time. Runs every night, also when Mello itself has no
# update. A no-op for speakers that have no key yet.
if [ -f pi/soloist/soloist-setup.sh ]; then
  bash pi/soloist/soloist-setup.sh refresh || log "Soloist renewal failed, will retry next run"
fi

# No new version to apply: the run still finishes what an earlier run left
# undone (see catch_up below), so it does not exit right away.
NO_UPDATE=false
case "$STATUS" in
  current)
    NO_UPDATE=true
    ;;
  older)
    # A fresh clone starts on main; a stable speaker must still go to its
    # release, even though that is older.
    if [ "$RECLONED" = true ]; then
      log "Fresh clone: moving to $CHANNEL target $TARGET_NAME"
    else
      log "Skip: $CHANNEL target $TARGET_NAME ($TARGET) is older than the running code ($LOCAL)"
      NO_UPDATE=true
    fi
    ;;
  failed)
    log "Skip: $TARGET_NAME ($TARGET) failed its health check before, waiting for a newer version"
    write_status failed "waiting for a fix: $TARGET_NAME failed here before"
    NO_UPDATE=true
    ;;
esac

if [ "$NO_UPDATE" = false ]; then
  log "Updates found on $CHANNEL channel, applying $TARGET_NAME (local=$LOCAL target=$TARGET)"
fi

# Remember current code directory (may be ~/tomo or ~/berry during transition)
CODE_DIR="$(pwd)"

# The commit pi/migrate.sh last finished for without errors. A run that
# failed (or never ran: an update that stopped halfway) runs again on the next
# night, also when there is no new version, instead of never.
MIGRATED_FILE="$HOME/.mello-migrated"
# Written by the app while it cannot draw its screen (mello/main.py).
SCREEN_ERROR_FILE="$HOME/.mello-screen-error"

run_migrations() {
  if [ ! -f "$CODE_DIR/pi/migrate.sh" ] || bash "$CODE_DIR/pi/migrate.sh"; then
    git -C "$CODE_DIR" rev-parse HEAD > "$MIGRATED_FILE" 2>/dev/null || true
    return 0
  fi
  return 1
}

# Spotify's player, Soloist, runs once the owner has entered an API key; a
# new Mello that is still waiting for one has none (see soloist-setup.sh).
engine_service() {
  if [ -s "${MELLO_SOLOIST_HOME:-$HOME/soloist}/key" ]; then
    echo mello-soloist
  fi
}

# Render templated services with install-time user/home/uid, symlink the rest.
sync_systemd_units() {
  local tmpl name f
  for tmpl in "$CODE_DIR/pi/systemd/"*.service.template; do
    [ -f "$tmpl" ] || continue
    name=$(basename "$tmpl" .template)
    sed -e "s|__USER__|$MELLO_USER|g" \
        -e "s|__HOME__|$MELLO_HOME|g" \
        -e "s|__UID__|$MELLO_UID|g" \
        "$tmpl" | sudo tee "/etc/systemd/system/$name" > /dev/null || return 1
  done
  for f in "$CODE_DIR/pi/systemd/"*.service "$CODE_DIR/pi/systemd/"*.timer; do
    [ -f "$f" ] || continue
    sudo ln -sf "$f" "/etc/systemd/system/$(basename "$f")" || return 1
  done
  sudo systemctl daemon-reload
}

restart_services() {
  # A fresh start says for itself whether it can draw (see health_check).
  rm -f "$SCREEN_ERROR_FILE"
  # shellcheck disable=SC2046  # no engine yet: just the app
  sudo systemctl restart $(engine_service) mello-native
}

# Healthy = both services still running a minute after the restart, without
# systemd having had to restart them (a crash loop keeps flipping between
# "activating" and "active", so is-active alone is not enough).
HEALTH_WAIT=60
health_check() {
  local engine before_native before_engine after_native after_engine
  engine=$(engine_service)
  engine=${engine:-mello-native}   # no engine yet: only the app to check
  before_native=$(systemctl show -p NRestarts --value mello-native)
  before_engine=$(systemctl show -p NRestarts --value "$engine")
  sleep "$HEALTH_WAIT"
  after_native=$(systemctl show -p NRestarts --value mello-native)
  after_engine=$(systemctl show -p NRestarts --value "$engine")
  if ! systemctl is-active --quiet mello-native; then
    log "Health check: mello-native is not running"
    return 1
  fi
  if ! systemctl is-active --quiet "$engine"; then
    log "Health check: $engine is not running"
    return 1
  fi
  # The app does not crash when it cannot draw: it shows a message and waits.
  if [ -f "$SCREEN_ERROR_FILE" ]; then
    log "Health check: the app cannot draw the screen: $(head -c 300 "$SCREEN_ERROR_FILE")"
    return 1
  fi
  if [ "$after_native" != "$before_native" ]; then
    log "Health check: mello-native restarted $((after_native - before_native))x within ${HEALTH_WAIT}s"
    return 1
  fi
  if [ "$after_engine" != "$before_engine" ]; then
    log "Health check: $engine restarted $((after_engine - before_engine))x within ${HEALTH_WAIT}s"
    return 1
  fi
  return 0
}

# Put the previous version back and remember the failed one, so the speaker
# keeps working instead of crash-looping until the next fix is merged.
# Migrations are not undone: they are written to be safe for older code too.
rollback() {
  log "ROLLBACK: $1 — returning to $LOCAL"
  echo "$TARGET" > "$BAD_UPDATE_FILE"
  git reset --hard "$LOCAL"
  local restored=true
  if git diff --name-only "$LOCAL" "$TARGET" | grep -q "^requirements\.txt$"; then
    log "Rollback: restoring Python dependencies"
    "$CODE_DIR/venv/bin/pip" install -q --disable-pip-version-check -r requirements.txt \
      || { log "Rollback: pip install failed"; restored=false; }
  fi
  sync_systemd_units || { log "Rollback: restoring systemd units failed"; restored=false; }
  restart_services || { log "Rollback: restarting services failed"; restored=false; }
  if ! health_check; then
    restored=false
  fi
  if [ "$restored" = true ]; then
    log "Rollback complete, running $LOCAL again"
  else
    log "ERROR: still unhealthy after rollback"
  fi
  write_status failed "rolled back: $1"
  exit 1
}

# No new version: finish what is still undone on the running one. The
# migration script runs again when it did not finish for this commit (it
# failed, or an earlier update stopped before it), or when the app cannot draw
# its screen (pi/migrate.sh installs missing screen libraries on every run).
catch_up() {
  local head screen_error=false
  head=$(git rev-parse HEAD)
  [ -f "$SCREEN_ERROR_FILE" ] && screen_error=true
  if [ "$(cat "$MIGRATED_FILE" 2>/dev/null)" = "$head" ] && [ "$screen_error" = false ]; then
    return 0
  fi
  if [ "$screen_error" = true ]; then
    log "Repair: the app cannot draw its screen, running the migration script"
  else
    log "Catch-up: the migration script has not finished for $head yet, running it"
  fi
  if ! run_migrations; then
    log "WARNING: migration failed, will try again next run"
    write_status failed "migration failed, will try again next run"
  fi
  if [ -d ~/mello ]; then CODE_DIR=~/mello; cd "$CODE_DIR"; fi
  if [ "$(git rev-parse HEAD)" != "$head" ]; then
    # A hold moved the code to another version: start that one.
    log "Catch-up: now on $(git rev-parse HEAD), restarting"
    sync_systemd_units || log "WARNING: installing systemd units failed"
    restart_services || log "WARNING: restarting services failed"
  elif [ "$screen_error" = true ]; then
    # (The app removes SCREEN_ERROR_FILE once it draws, or writes it again:
    # if this restart fails, the next run still sees it and tries again.)
    log "Repair: restarting the app"
    sudo systemctl restart mello-native || log "WARNING: restarting the app failed"
  fi
}

if [ "$NO_UPDATE" = true ]; then
  catch_up
  exit 0
fi

# Backup user data that may still be tracked by git (e.g. data/catalog.json
# was committed early on but later gitignored; git rm --cached will cause
# git pull to delete it from disk).
DATA_BACKUP="/tmp/mello-data-backup.$$"
if [ -d data ] && [ "$(ls -A data/ 2>/dev/null)" ]; then
  cp -a data "$DATA_BACKUP"
fi

# Hard reset to the target — always converge to the target state
git checkout main 2>/dev/null || true
git reset --hard "$TARGET"

# Restore any data files that git pull deleted
if [ -d "$DATA_BACKUP" ]; then
  cp -a "$DATA_BACKUP"/. data/ 2>/dev/null || true
  rm -rf "$DATA_BACKUP"
fi

# ============================================
# 1. Update Python dependencies if requirements.txt changed
# ============================================
if git diff --name-only "$LOCAL" "$TARGET" | grep -q "^requirements\.txt$"; then
  log "Updating Python dependencies"
  cd "$CODE_DIR"
  if ! "$CODE_DIR/venv/bin/pip" install -q --disable-pip-version-check -r requirements.txt; then
    rollback "pip install failed"
  fi
fi

# ============================================
# 2. Run migration script BEFORE service restart
#    (may install packages, change configs, update sudoers)
#    Migration may move $CODE_DIR (e.g. ~/tomo → ~/mello)
# ============================================
if [ -f "$CODE_DIR/pi/migrate.sh" ]; then
  log "Running migration script"
  if ! run_migrations; then
    log "WARNING: migration failed, continuing with service restart (next run tries again)"
  fi
fi

# After migration, code may have moved to ~/mello
if [ -d ~/mello ]; then
  CODE_DIR=~/mello
  cd "$CODE_DIR"
fi

# Reload env after migration (may have been renamed)
if [ -f "$CODE_DIR/.mello-env" ]; then
  source "$CODE_DIR/.mello-env"
fi

# ============================================
# 3. Sync Plymouth boot splash theme (if installed)
# ============================================
if [ -d /usr/share/plymouth/themes/mello ]; then
  PLYMOUTH_CHANGED=false
  for f in mello-logo-boot.png mello.script mello.plymouth; do
    if ! diff -q "$CODE_DIR/pi/plymouth/$f" "/usr/share/plymouth/themes/mello/$f" &>/dev/null; then
      PLYMOUTH_CHANGED=true
      break
    fi
  done
  if [ "$PLYMOUTH_CHANGED" = true ]; then
    log "Updating Plymouth boot splash theme"
    sudo cp "$CODE_DIR/pi/plymouth/"* /usr/share/plymouth/themes/mello/
    sudo update-initramfs -u
  fi
fi

# ============================================
# 4. Update systemd services
# ============================================
log "Updating systemd services"
if ! sync_systemd_units; then
  rollback "installing systemd units failed"
fi

# ============================================
# 5. Restart services and check they stay up
# ============================================
log "Restarting services"
if ! restart_services; then
  rollback "restarting services failed"
fi

if ! health_check; then
  rollback "service health check failed after update"
fi

rm -f "$BAD_UPDATE_FILE"
log "Update complete ($CHANNEL channel, now at $TARGET_NAME)"
}

main "$@"

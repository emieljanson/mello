#!/bin/bash
# Mello Auto-Update Script
# Runs via cron, checks GitHub and applies ALL changes
#
# The entire script body is wrapped in main() so bash reads it fully into
# memory before executing.  This prevents corruption when git pull updates
# this file while it is running.

main() {
set -euo pipefail

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

# Never auto-update a dirty tree; leave it for manual intervention.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  log "Skip: working tree has local changes"
  exit 0
fi

# Get current branch name (could be main or master)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Check if there are updates
if ! git fetch origin "$BRANCH" 2>/dev/null; then
  log "Fetch failed (network issue?), will retry next run"
  exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0  # No updates
fi

log "Updates found on $BRANCH, applying"

# Remember current code directory (may be ~/tomo or ~/berry during transition)
CODE_DIR="$(pwd)"

# Backup user data that may still be tracked by git (e.g. data/catalog.json
# was committed early on but later gitignored; git rm --cached will cause
# git pull to delete it from disk).
DATA_BACKUP="/tmp/mello-data-backup.$$"
if [ -d data ] && [ "$(ls -A data/ 2>/dev/null)" ]; then
  cp -a data "$DATA_BACKUP"
fi

# Pull changes (fast-forward only to avoid accidental merge commits)
git pull --ff-only origin "$BRANCH" || exit 1

# Restore any data files that git pull deleted
if [ -d "$DATA_BACKUP" ]; then
  cp -an "$DATA_BACKUP"/. data/ 2>/dev/null || true
  rm -rf "$DATA_BACKUP"
fi

# ============================================
# 1. Update Python dependencies if requirements.txt changed
# ============================================
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "^requirements\.txt$"; then
  log "Updating Python dependencies"
  cd "$CODE_DIR"
  source venv/bin/activate
  pip install -q --disable-pip-version-check -r requirements.txt
fi

# ============================================
# 2. Run migration script BEFORE service restart
#    (may install packages, change configs, update sudoers)
#    Migration may move $CODE_DIR (e.g. ~/tomo → ~/mello)
# ============================================
if [ -f "$CODE_DIR/pi/migrate.sh" ]; then
  log "Running migration script"
  if ! bash "$CODE_DIR/pi/migrate.sh"; then
    log "WARNING: migration failed, continuing with service restart"
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
# 3. Update systemd services
# ============================================
log "Updating systemd services"
# Render templated services with install-time user/home/uid
for tmpl in "$CODE_DIR/pi/systemd/"*.service.template; do
  [ -f "$tmpl" ] || continue
  name=$(basename "$tmpl" .template)
  sed -e "s|__USER__|$MELLO_USER|g" \
      -e "s|__HOME__|$MELLO_HOME|g" \
      -e "s|__UID__|$MELLO_UID|g" \
      "$tmpl" | sudo tee "/etc/systemd/system/$name" > /dev/null
done
# Symlink non-templated services
for f in "$CODE_DIR/pi/systemd/"*.service; do
  [ -f "$f" ] || continue
  sudo ln -sf "$f" "/etc/systemd/system/$(basename "$f")"
done
sudo systemctl daemon-reload

# ============================================
# 4. Restart services
# ============================================
log "Restarting services"
sudo systemctl restart mello-librespot mello-native

# Basic post-update health check to catch hard failures quickly.
sleep 2
if ! systemctl is-active --quiet mello-librespot || ! systemctl is-active --quiet mello-native; then
  log "ERROR: service health check failed after restart"
  exit 1
fi

log "Update complete"
}

main "$@"

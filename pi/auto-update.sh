#!/bin/bash
# Berry Auto-Update Script
# Runs via cron, checks GitHub and applies ALL changes

set -euo pipefail

cd ~/berry || exit 1

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [auto-update] $*"
}

# Avoid overlapping updates from cron/manual invocations.
LOCK_FILE="/tmp/berry-auto-update.lock"
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
git fetch origin "$BRANCH" 2>/dev/null || exit 0

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0  # No updates
fi

log "Updates found on $BRANCH, applying"

# Backup user data that may still be tracked by git (e.g. data/catalog.json
# was committed early on but later gitignored; git rm --cached will cause
# git pull to delete it from disk).
DATA_BACKUP="/tmp/berry-data-backup.$$"
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
  cd ~/berry
  source venv/bin/activate
  pip install -q --disable-pip-version-check -r requirements.txt
fi

# ============================================
# 2. Run migration script BEFORE service restart
#    (may install packages, change configs, update sudoers)
# ============================================
if [ -f ~/berry/pi/migrate.sh ]; then
  log "Running migration script"
  bash ~/berry/pi/migrate.sh
fi

# ============================================
# 3. Update systemd services
# ============================================
log "Updating systemd services"
for f in ~/berry/pi/systemd/*.service; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  sudo ln -sf "$f" "/etc/systemd/system/$name"
done
sudo systemctl daemon-reload

# ============================================
# 4. Restart services
# ============================================
log "Restarting services"
sudo systemctl restart berry-librespot berry-native

# Basic post-update health check to catch hard failures quickly.
sleep 2
if ! systemctl is-active --quiet berry-librespot || ! systemctl is-active --quiet berry-native; then
  log "ERROR: service health check failed after restart"
  exit 1
fi

log "Update complete"

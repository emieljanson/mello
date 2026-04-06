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

  # Re-clone into a temp dir, then swap
  local tmp_clone="/tmp/mello-reclone-repo.$$"
  if ! git clone --depth 1 "$REPO_URL" "$tmp_clone" 2>/dev/null; then
    log "Re-clone failed (network issue?), will retry next run"
    rm -rf "$tmp_clone" "$data_backup" "$env_backup"
    exit 0
  fi

  # Swap: move broken dir out, move clone in
  cd "$parent"
  mv "$current" "/tmp/mello-broken-backup.$$"
  if ! mv "$tmp_clone" "$current"; then
    log "Swap failed — restoring original directory"
    mv "/tmp/mello-broken-backup.$$" "$current"
    rm -rf "$tmp_clone" "$data_backup" "$env_backup"
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

  # Recreate venv (pygame is installed via apt, so --system-site-packages
  # is required for the venv to see it).
  log "Recreating venv"
  rm -rf venv
  python3 -m venv --system-site-packages venv
  source venv/bin/activate
  pip install -q --disable-pip-version-check -r requirements.txt
  deactivate

  # Clean up broken backup (keep for a bit in case of issues)
  rm -rf "/tmp/mello-broken-backup.$$"

  log "Re-clone complete, repo is healthy"
}

_ensure_git_repo

# Always converge to origin/main regardless of local state.
if ! git fetch origin main 2>/dev/null; then
  log "Fetch failed (network issue?), will retry next run"
  exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0  # No updates
fi

log "Updates found, applying (local=$LOCAL remote=$REMOTE)"

# Remember current code directory (may be ~/tomo or ~/berry during transition)
CODE_DIR="$(pwd)"

# Backup user data that may still be tracked by git (e.g. data/catalog.json
# was committed early on but later gitignored; git rm --cached will cause
# git pull to delete it from disk).
DATA_BACKUP="/tmp/mello-data-backup.$$"
if [ -d data ] && [ "$(ls -A data/ 2>/dev/null)" ]; then
  cp -a data "$DATA_BACKUP"
fi

# Hard reset to origin/main — always converge to the target state
git checkout main 2>/dev/null || true
git reset --hard origin/main

# Restore any data files that git pull deleted
if [ -d "$DATA_BACKUP" ]; then
  cp -a "$DATA_BACKUP"/. data/ 2>/dev/null || true
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
# 5. Restart services
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

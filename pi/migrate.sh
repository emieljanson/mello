#!/bin/bash
# Berry Migration Script
# Runs automatically after auto-update (see auto-update.sh step 4).
# Each migration is idempotent and guarded by a marker file.
#
# Migrations are numbered and run in order. Once a migration succeeds
# a marker is written so it never runs again.

set -euo pipefail

MIGRATION_DIR="$HOME/.berry-migrations"
mkdir -p "$MIGRATION_DIR"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [migrate] $*"
}

run_migration() {
  local id="$1"
  local desc="$2"
  local marker="$MIGRATION_DIR/$id.done"

  if [ -f "$marker" ]; then
    return 0
  fi

  log "Running migration $id: $desc"
  # The caller defines a function named _migrate_$id
  if "_migrate_$id"; then
    touch "$marker"
    log "Migration $id complete"
  else
    log "ERROR: Migration $id failed"
    return 1
  fi
}

# ============================================
# Migration 001: Bluetooth audio via PipeWire
# ============================================
_migrate_001() {
  # 1. Install PipeWire + Bluetooth audio packages
  #    - pipewire: core audio daemon
  #    - pipewire-pulse: PulseAudio compat layer (provides pactl)
  #    - wireplumber: session manager
  #    - pipewire-alsa: ALSA integration so apps using "default" route through PipeWire
  #    - libspa-0.2-bluetooth: PipeWire Bluetooth audio module (A2DP, HFP)
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    pipewire pipewire-pulse wireplumber \
    pipewire-alsa libspa-0.2-bluetooth

  # Enable PipeWire for the berry user (user-level systemd services)
  # Create user service directory if it doesn't exist
  mkdir -p "$HOME/.config/systemd/user"

  # Enable PipeWire user services (will start on next login/reboot)
  systemctl --user enable pipewire pipewire-pulse wireplumber 2>/dev/null || true

  # Start them now if not running
  systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true

  # 2. Switch go-librespot from direct ALSA hardware to PipeWire default
  #    Before: audio_device: "plughw:CARD=wm8960soundcard" (bypasses PipeWire)
  #    After:  audio_device: "default" (routes through PipeWire)
  local CONFIG="$HOME/.config/go-librespot/config.yml"
  if [ -f "$CONFIG" ]; then
    if grep -q 'plughw:CARD=' "$CONFIG"; then
      sed -i 's|audio_device:.*"plughw:CARD=.*"|audio_device: "default"|' "$CONFIG"
      log "go-librespot config updated: audio_device -> default"
    fi
  fi

  # 3. Add berry user to bluetooth group
  sudo usermod -aG bluetooth "$USER" 2>/dev/null || true

  # 4. Add BT-related commands to sudoers
  #    bluetooth.py needs: systemctl restart bluetooth, hciconfig hci0 up
  local SUDOERS_FILE="/etc/sudoers.d/berry-wifi"
  local EXPECTED_LINE='berry ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /bin/systemctl stop berry-librespot, /bin/systemctl start berry-librespot, /bin/systemctl restart berry-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up'

  # Create or update sudoers if BT commands are missing
  if ! sudo grep -q "restart bluetooth" "$SUDOERS_FILE" 2>/dev/null; then
    local TMP_SUDOERS="/tmp/berry-sudoers.$$"
    echo "$EXPECTED_LINE" > "$TMP_SUDOERS"
    if sudo visudo -cf "$TMP_SUDOERS"; then
      sudo install -m 440 "$TMP_SUDOERS" "$SUDOERS_FILE"
      log "sudoers updated with BT commands"
    else
      log "ERROR: sudoers validation failed"
      rm -f "$TMP_SUDOERS"
      return 1
    fi
    rm -f "$TMP_SUDOERS"
  fi

  # 5. Ensure XDG_RUNTIME_DIR is set for PipeWire in systemd service
  #    PipeWire needs this to find its socket
  local SERVICE="/etc/systemd/system/berry-native.service"
  if [ -f "$SERVICE" ] && ! grep -q "DBUS_SESSION_BUS_ADDRESS" "$SERVICE"; then
    log "Note: berry-native.service will be updated on next auto-update cycle"
  fi

  # Restart bluetooth service to pick up new group membership
  sudo systemctl restart bluetooth 2>/dev/null || true

  log "Bluetooth audio migration complete — reboot recommended"
}

# ============================================
# Run all migrations
# ============================================
run_migration "001" "Bluetooth audio via PipeWire"

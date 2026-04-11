#!/bin/bash
# Mello Migration Script
# Runs automatically after auto-update (see auto-update.sh step 4).
# Each migration is idempotent and guarded by a marker file.
#
# Migrations are numbered and run in order. Once a migration succeeds
# a marker is written so it never runs again.

set -euo pipefail

# Load install environment (username, home, uid)
# Support old (.berry-env), intermediate (.tomo-env), and new (.mello-env) locations
if [ -f "$HOME/mello/.mello-env" ]; then
  source "$HOME/mello/.mello-env"
elif [ -f "$HOME/tomo/.tomo-env" ]; then
  source "$HOME/tomo/.tomo-env"
  # Map Tomo variable names to Mello
  MELLO_USER="${TOMO_USER:-$USER}"
  MELLO_HOME="${TOMO_HOME:-$HOME}"
  MELLO_UID="${TOMO_UID:-$(id -u)}"
elif [ -f "$HOME/berry/.berry-env" ]; then
  source "$HOME/berry/.berry-env"
  # Map Berry variable names to Mello
  MELLO_USER="${BERRY_USER:-$USER}"
  MELLO_HOME="${BERRY_HOME:-$HOME}"
  MELLO_UID="${BERRY_UID:-$(id -u)}"
else
  MELLO_USER="$USER"
  MELLO_HOME="$HOME"
  MELLO_UID="$(id -u)"
fi

# Support old, intermediate, and new migration dirs for transition
MIGRATION_DIR="$HOME/.mello-migrations"
if [ ! -d "$MIGRATION_DIR" ] && [ -d "$HOME/.tomo-migrations" ]; then
  mv "$HOME/.tomo-migrations" "$MIGRATION_DIR"
elif [ ! -d "$MIGRATION_DIR" ] && [ -d "$HOME/.berry-migrations" ]; then
  mv "$HOME/.berry-migrations" "$MIGRATION_DIR"
fi
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
  #    - pipewire-pulse: PulseAudio compat layer
  #    - pulseaudio-utils: provides pactl CLI (not bundled with pipewire-pulse on Trixie)
  #    - wireplumber: session manager
  #    - pipewire-alsa: ALSA integration so apps using "default" route through PipeWire
  #    - libspa-0.2-bluetooth: PipeWire Bluetooth audio module (A2DP, HFP)
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    pipewire pipewire-pulse wireplumber \
    pipewire-alsa libspa-0.2-bluetooth \
    pulseaudio-utils

  # Enable PipeWire for the mello user (user-level systemd services)
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

  # 3. Add mello user to bluetooth group
  sudo usermod -aG bluetooth "$USER" 2>/dev/null || true

  # 4. Add BT-related commands to sudoers
  #    bluetooth.py needs: systemctl restart bluetooth, hciconfig hci0 up
  local SUDOERS_FILE="/etc/sudoers.d/mello-wifi"
  # Also check old name for transition
  if [ ! -f "$SUDOERS_FILE" ] && [ -f "/etc/sudoers.d/berry-wifi" ]; then
    SUDOERS_FILE="/etc/sudoers.d/berry-wifi"
  fi
  local EXPECTED_LINE="$MELLO_USER ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /bin/systemctl stop mello-librespot, /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up"

  # Create or update sudoers if BT commands are missing
  if ! sudo grep -q "restart bluetooth" "$SUDOERS_FILE" 2>/dev/null; then
    local TMP_SUDOERS="/tmp/mello-sudoers.$$"
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
  local SERVICE="/etc/systemd/system/mello-native.service"
  # Check old name too
  [ -f "$SERVICE" ] || SERVICE="/etc/systemd/system/berry-native.service"
  if [ -f "$SERVICE" ] && ! grep -q "DBUS_SESSION_BUS_ADDRESS" "$SERVICE"; then
    log "Note: mello-native.service will be updated on next auto-update cycle"
  fi

  # Restart bluetooth service to pick up new group membership
  sudo systemctl restart bluetooth 2>/dev/null || true

  log "Bluetooth audio migration complete — reboot recommended"
}

# ============================================
# Migration 002: Install pactl (missing from 001 on Trixie)
# ============================================
_migrate_002() {
  # pulseaudio-utils provides the pactl CLI needed for BT audio routing.
  # On Debian Trixie, pipewire-pulse does NOT bundle pactl (unlike Ubuntu).
  # Migration 001 missed this; devices that already ran 001 need this fix.
  if command -v pactl &>/dev/null; then
    log "pactl already available, skipping"
    return 0
  fi
  sudo apt-get update -qq
  sudo apt-get install -y -qq pulseaudio-utils
  if command -v pactl &>/dev/null; then
    log "pactl installed successfully"
  else
    log "ERROR: pactl still not found after install"
    return 1
  fi
}

# ============================================
# Migration 003: Dynamic username support
# ============================================
_migrate_003() {
  # Create .mello-env if it doesn't exist (existing installs used user "berry")
  # Check both old and new locations
  local CODE_DIR="$HOME/mello"
  [ -d "$CODE_DIR" ] || CODE_DIR="$HOME/berry"

  if [ ! -f "$CODE_DIR/.mello-env" ] && [ ! -f "$CODE_DIR/.berry-env" ]; then
    cat > "$CODE_DIR/.mello-env" << EOF
MELLO_USER=$MELLO_USER
MELLO_HOME=$MELLO_HOME
MELLO_UID=$MELLO_UID
EOF
    log "Created .mello-env (user=$MELLO_USER)"
  fi

  # Re-render service templates (replaces old symlinks with rendered copies)
  for tmpl in "$CODE_DIR/pi/systemd/"*.service.template; do
    [ -f "$tmpl" ] || continue
    local name
    name=$(basename "$tmpl" .template)
    sed -e "s|__USER__|$MELLO_USER|g" \
        -e "s|__HOME__|$MELLO_HOME|g" \
        -e "s|__UID__|$MELLO_UID|g" \
        "$tmpl" | sudo tee "/etc/systemd/system/$name" > /dev/null
    log "Rendered $name"
  done
  sudo systemctl daemon-reload

  # Update sudoers if it still has hardcoded "berry" username
  local SUDOERS_FILE="/etc/sudoers.d/mello-wifi"
  [ -f "$SUDOERS_FILE" ] || SUDOERS_FILE="/etc/sudoers.d/berry-wifi"
  if sudo grep -q "^berry " "$SUDOERS_FILE" 2>/dev/null && [ "$MELLO_USER" != "berry" ]; then
    local TMP_SUDOERS="/tmp/mello-sudoers.$$"
    echo "$MELLO_USER ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /bin/systemctl stop mello-librespot, /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up" > "$TMP_SUDOERS"
    if sudo visudo -cf "$TMP_SUDOERS"; then
      sudo install -m 440 "$TMP_SUDOERS" "$SUDOERS_FILE"
      log "sudoers updated for user $MELLO_USER"
    fi
    rm -f "$TMP_SUDOERS"
  fi
}

# ============================================
# Migration 004: Berry → Mello rebrand
# ============================================
_migrate_004() {
  log "Starting Berry → Mello rebrand migration"

  # 1. Stop old services
  sudo systemctl stop berry-native berry-librespot 2>/dev/null || true

  # 2. Move code directory ~/berry → ~/mello
  if [ -d "$HOME/berry" ] && [ ! -d "$HOME/mello" ]; then
    mv "$HOME/berry" "$HOME/mello"
    log "Moved ~/berry → ~/mello"
  elif [ -d "$HOME/berry" ] && [ -d "$HOME/mello" ]; then
    log "Both ~/berry and ~/mello exist — skipping directory move"
  fi

  local CODE_DIR="$HOME/mello"

  # 3. Rename .berry-env → .mello-env and update variable names
  if [ -f "$CODE_DIR/.berry-env" ]; then
    sed -e 's/^BERRY_USER=/MELLO_USER=/' \
        -e 's/^BERRY_HOME=/MELLO_HOME=/' \
        -e 's/^BERRY_UID=/MELLO_UID=/' \
        "$CODE_DIR/.berry-env" > "$CODE_DIR/.mello-env"
    rm -f "$CODE_DIR/.berry-env"
    log "Migrated .berry-env → .mello-env"
  fi

  # Reload env from new file
  if [ -f "$CODE_DIR/.mello-env" ]; then
    source "$CODE_DIR/.mello-env"
  fi

  # 4. Remove old systemd services, install new ones
  sudo systemctl disable berry-native berry-librespot berry-touch-fix 2>/dev/null || true
  sudo rm -f /etc/systemd/system/berry-native.service
  sudo rm -f /etc/systemd/system/berry-librespot.service
  sudo rm -f /etc/systemd/system/berry-touch-fix.service

  # Render and install new service templates
  for tmpl in "$CODE_DIR/pi/systemd/"*.service.template; do
    [ -f "$tmpl" ] || continue
    local name
    name=$(basename "$tmpl" .template)
    sed -e "s|__USER__|$MELLO_USER|g" \
        -e "s|__HOME__|$MELLO_HOME|g" \
        -e "s|__UID__|$MELLO_UID|g" \
        "$tmpl" | sudo tee "/etc/systemd/system/$name" > /dev/null
    log "Installed $name"
  done

  # Symlink non-templated services
  for f in "$CODE_DIR/pi/systemd/"*.service; do
    [ -f "$f" ] || continue
    sudo ln -sf "$f" "/etc/systemd/system/$(basename "$f")"
  done

  sudo systemctl daemon-reload
  sudo systemctl enable mello-librespot mello-native mello-touch-fix

  # 5. Update cron job
  ( (crontab -l 2>/dev/null || true) | grep -v "berry/pi/auto-update\|mello/pi/auto-update" || true
    echo "0 3 * * * bash ~/mello/pi/auto-update.sh >> ~/mello-update.log 2>&1"
  ) | crontab -
  log "Cron job updated"

  # 6. Rename system group berry → mello
  if getent group berry >/dev/null 2>&1; then
    if ! getent group mello >/dev/null 2>&1; then
      sudo groupmod -n mello berry
      log "Renamed group berry → mello"
    else
      # mello group already exists, just add user
      sudo usermod -aG mello "$MELLO_USER" 2>/dev/null || true
    fi
  fi

  # 7. Update udev rules
  if [ -f /etc/udev/rules.d/99-berry-drm.rules ]; then
    sudo mv /etc/udev/rules.d/99-berry-drm.rules /etc/udev/rules.d/99-mello-drm.rules
  fi
  if [ -f /etc/udev/rules.d/99-berry-power.rules ]; then
    sudo mv /etc/udev/rules.d/99-mello-power.rules 2>/dev/null || true
    sudo mv /etc/udev/rules.d/99-berry-power.rules /etc/udev/rules.d/99-mello-power.rules
  fi
  # Update backlight rule to use mello group
  if [ -f /etc/udev/rules.d/99-backlight.rules ]; then
    sudo sed -i 's/chgrp berry/chgrp mello/g' /etc/udev/rules.d/99-backlight.rules
  fi
  # Update power rules to use mello group
  if [ -f /etc/udev/rules.d/99-mello-power.rules ]; then
    sudo sed -i 's/chgrp berry/chgrp mello/g' /etc/udev/rules.d/99-mello-power.rules
  fi
  sudo udevadm control --reload-rules 2>/dev/null || true
  sudo udevadm trigger 2>/dev/null || true

  # 8. Update sudoers
  if [ -f /etc/sudoers.d/berry-wifi ]; then
    local TMP_SUDOERS="/tmp/mello-sudoers.$$"
    echo "$MELLO_USER ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /bin/systemctl stop mello-librespot, /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up" > "$TMP_SUDOERS"
    if sudo visudo -cf "$TMP_SUDOERS"; then
      sudo install -m 440 "$TMP_SUDOERS" /etc/sudoers.d/mello-wifi
      sudo rm -f /etc/sudoers.d/berry-wifi
      log "Sudoers migrated to mello-wifi"
    fi
    rm -f "$TMP_SUDOERS"
  fi

  # 9. Update go-librespot device name
  local CONFIG="$HOME/.config/go-librespot/config.yml"
  if [ -f "$CONFIG" ]; then
    sed -i 's/device_name:.*"Berry"/device_name: "Mello"/' "$CONFIG"
    log "go-librespot device name updated to Mello"
  fi

  # 10. Update portal UI
  sudo cp "$CODE_DIR/portal/index.html" /usr/local/share/wifi-connect/ui/index.html 2>/dev/null || true

  # 11. Start new services
  sudo systemctl start mello-librespot mello-native

  log "Berry → Mello rebrand migration complete"
}

# ============================================
# Migration 005: Tomo → Mello rebrand
# ============================================
# Handles devices that were running the intermediate Tomo version.
# Similar to 004 but replaces tomo references instead of berry.
_migrate_005() {
  # Skip if no tomo artifacts exist (device came straight from berry via 004)
  if [ ! -d "$HOME/tomo" ] && ! systemctl list-unit-files tomo-native.service &>/dev/null; then
    log "No Tomo artifacts found, skipping"
    return 0
  fi

  log "Starting Tomo → Mello rebrand migration"

  # 1. Stop old services
  sudo systemctl stop tomo-native tomo-librespot 2>/dev/null || true

  # 2. Move code directory ~/tomo → ~/mello
  if [ -d "$HOME/tomo" ] && [ ! -d "$HOME/mello" ]; then
    mv "$HOME/tomo" "$HOME/mello"
    log "Moved ~/tomo → ~/mello"
  elif [ -d "$HOME/tomo" ] && [ -d "$HOME/mello" ]; then
    log "Both ~/tomo and ~/mello exist — skipping directory move"
  fi

  local CODE_DIR="$HOME/mello"

  # 3. Rename .tomo-env → .mello-env and update variable names
  if [ -f "$CODE_DIR/.tomo-env" ]; then
    sed -e 's/^TOMO_USER=/MELLO_USER=/' \
        -e 's/^TOMO_HOME=/MELLO_HOME=/' \
        -e 's/^TOMO_UID=/MELLO_UID=/' \
        "$CODE_DIR/.tomo-env" > "$CODE_DIR/.mello-env"
    rm -f "$CODE_DIR/.tomo-env"
    log "Migrated .tomo-env → .mello-env"
  fi

  # Reload env from new file
  if [ -f "$CODE_DIR/.mello-env" ]; then
    source "$CODE_DIR/.mello-env"
  fi

  # 4. Remove old systemd services, install new ones
  sudo systemctl disable tomo-native tomo-librespot tomo-touch-fix 2>/dev/null || true
  sudo rm -f /etc/systemd/system/tomo-native.service
  sudo rm -f /etc/systemd/system/tomo-librespot.service
  sudo rm -f /etc/systemd/system/tomo-touch-fix.service

  # Render and install new service templates
  for tmpl in "$CODE_DIR/pi/systemd/"*.service.template; do
    [ -f "$tmpl" ] || continue
    local name
    name=$(basename "$tmpl" .template)
    sed -e "s|__USER__|$MELLO_USER|g" \
        -e "s|__HOME__|$MELLO_HOME|g" \
        -e "s|__UID__|$MELLO_UID|g" \
        "$tmpl" | sudo tee "/etc/systemd/system/$name" > /dev/null
    log "Installed $name"
  done

  # Symlink non-templated services
  for f in "$CODE_DIR/pi/systemd/"*.service; do
    [ -f "$f" ] || continue
    sudo ln -sf "$f" "/etc/systemd/system/$(basename "$f")"
  done

  sudo systemctl daemon-reload
  sudo systemctl enable mello-librespot mello-native mello-touch-fix

  # 5. Update cron job
  ( (crontab -l 2>/dev/null || true) | grep -v "tomo/pi/auto-update\|mello/pi/auto-update" || true
    echo "0 3 * * * bash ~/mello/pi/auto-update.sh >> ~/mello-update.log 2>&1"
  ) | crontab -
  log "Cron job updated"

  # 6. Rename system group tomo → mello
  if getent group tomo >/dev/null 2>&1; then
    if ! getent group mello >/dev/null 2>&1; then
      sudo groupmod -n mello tomo
      log "Renamed group tomo → mello"
    else
      sudo usermod -aG mello "$MELLO_USER" 2>/dev/null || true
    fi
  fi

  # 7. Update udev rules
  if [ -f /etc/udev/rules.d/99-tomo-drm.rules ]; then
    sudo mv /etc/udev/rules.d/99-tomo-drm.rules /etc/udev/rules.d/99-mello-drm.rules
  fi
  if [ -f /etc/udev/rules.d/99-tomo-power.rules ]; then
    sudo mv /etc/udev/rules.d/99-tomo-power.rules /etc/udev/rules.d/99-mello-power.rules
  fi
  # Update rules to use mello group
  if [ -f /etc/udev/rules.d/99-backlight.rules ]; then
    sudo sed -i 's/chgrp tomo/chgrp mello/g' /etc/udev/rules.d/99-backlight.rules
  fi
  if [ -f /etc/udev/rules.d/99-mello-power.rules ]; then
    sudo sed -i 's/chgrp tomo/chgrp mello/g' /etc/udev/rules.d/99-mello-power.rules
  fi
  sudo udevadm control --reload-rules 2>/dev/null || true
  sudo udevadm trigger 2>/dev/null || true

  # 8. Update sudoers
  if [ -f /etc/sudoers.d/tomo-wifi ]; then
    local TMP_SUDOERS="/tmp/mello-sudoers.$$"
    echo "$MELLO_USER ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /bin/systemctl stop mello-librespot, /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up" > "$TMP_SUDOERS"
    if sudo visudo -cf "$TMP_SUDOERS"; then
      sudo install -m 440 "$TMP_SUDOERS" /etc/sudoers.d/mello-wifi
      sudo rm -f /etc/sudoers.d/tomo-wifi
      log "Sudoers migrated from tomo-wifi to mello-wifi"
    fi
    rm -f "$TMP_SUDOERS"
  fi

  # 9. Update go-librespot device name
  local CONFIG="$HOME/.config/go-librespot/config.yml"
  if [ -f "$CONFIG" ]; then
    sed -i 's/device_name:.*"Tomo"/device_name: "Mello"/' "$CONFIG"
    log "go-librespot device name updated to Mello"
  fi

  # 10. Update portal UI
  sudo cp "$CODE_DIR/portal/index.html" /usr/local/share/wifi-connect/ui/index.html 2>/dev/null || true

  # 11. Clean up old tomo update log
  rm -f "$HOME/tomo-update.log"

  # 12. Start new services
  sudo systemctl start mello-librespot mello-native

  log "Tomo → Mello rebrand migration complete"
}

# ============================================
# Migration 006: Plymouth boot splash (plain black)
# ============================================
_migrate_006() {
  local CODE_DIR="$HOME/mello"

  # 1. Install Plymouth
  sudo apt-get update -qq
  sudo apt-get install -y -qq plymouth plymouth-themes

  # 2. Copy theme files (plain black screen) to system directory
  local THEME_DIR="/usr/share/plymouth/themes/mello"
  sudo mkdir -p "$THEME_DIR"
  sudo cp "$CODE_DIR/pi/plymouth/"* "$THEME_DIR/"

  # 3. Set Mello as the default Plymouth theme
  sudo plymouth-set-default-theme mello

  # 4. Configure cmdline.txt
  local BOOT_CMDLINE="/boot/firmware/cmdline.txt"
  [ -f "$BOOT_CMDLINE" ] || BOOT_CMDLINE="/boot/cmdline.txt"
  if [ -f "$BOOT_CMDLINE" ]; then
    # Prevent Plymouth from disabling itself on serial console setups
    if ! grep -q "plymouth.ignore-serial-consoles" "$BOOT_CMDLINE"; then
      sudo sed -i 's/$/ plymouth.ignore-serial-consoles/' "$BOOT_CMDLINE"
    fi
    # Move kernel console off tty1 so the display stays clean
    if grep -q "console=tty1" "$BOOT_CMDLINE"; then
      sudo sed -i 's/console=tty1/console=tty3/' "$BOOT_CMDLINE"
    fi
  fi

  # 5. Keep Plymouth splash on framebuffer until the app renders over it
  sudo mkdir -p /etc/systemd/system/plymouth-quit.service.d
  cat <<'DROPEOF' | sudo tee /etc/systemd/system/plymouth-quit.service.d/retain-splash.conf > /dev/null
[Service]
ExecStart=
ExecStart=-/usr/bin/plymouth quit --retain-splash
DROPEOF
  sudo systemctl daemon-reload

  # 6. Update initramfs to include Plymouth
  if ls /boot/initrd* &>/dev/null || ls /boot/firmware/initramfs* &>/dev/null; then
    sudo update-initramfs -u
  else
    sudo update-initramfs -c -k "$(uname -r)"
  fi

  log "Plymouth boot splash installed — takes effect on next reboot"
}

# ============================================
# Migration 007: Mask getty@tty1 (missed by older installs)
# ============================================
_migrate_007() {
  # setup.sh masks getty@tty1 so mello-native can own /dev/tty1, but devices
  # installed before that line was added still have it enabled.  When an
  # auto-update restarts mello-native, getty can race for the TTY and block
  # the service's ExecStartPre from completing (timeout → restart loop).
  if systemctl is-enabled getty@tty1.service &>/dev/null; then
    sudo systemctl stop getty@tty1.service 2>/dev/null || true
    sudo systemctl mask getty@tty1.service 2>/dev/null || true
    log "Masked getty@tty1.service"
  else
    log "getty@tty1 already masked, skipping"
  fi
}

# ============================================
# Migration 008: Route go-librespot audio through PipeWire
# ============================================
_migrate_008() {
  # Migration 001 changed audio_device from "plughw:CARD=..." to "default",
  # but /etc/asound.conf routes ALSA "default" to dmix → hw:wm8960soundcard,
  # bypassing PipeWire entirely. Audio must go through PipeWire so that
  # pactl set-default-sink can route it to Bluetooth headphones.
  # Catches all non-pipewire values (default, plughw:CARD=..., hw:..., etc).
  local CONFIG="$HOME/.config/go-librespot/config.yml"
  if [ -f "$CONFIG" ]; then
    if grep -q 'audio_device:.*"pipewire"' "$CONFIG"; then
      log "go-librespot already using pipewire, skipping"
    else
      sed -i 's|audio_device:.*|audio_device: "pipewire"|' "$CONFIG"
      log "go-librespot config updated: audio_device -> pipewire"
    fi
  fi
}

# ============================================
# Migration 009: Update sudoers for hciconfig down+up (both paths)
# ============================================
_migrate_009() {
  local SUDOERS="/etc/sudoers.d/mello-wifi"
  if [ ! -f "$SUDOERS" ]; then
    log "sudoers file not found, skipping"
    return
  fi
  # Already has rfkill → fully migrated
  if sudo grep -q 'rfkill' "$SUDOERS"; then
    log "sudoers already has rfkill, skipping"
    return
  fi
  # Replace the hciconfig entries with both /usr/bin and /usr/sbin paths for up+down + rfkill
  local TMP="/tmp/mello-sudoers-009.$$"
  sudo sed 's|/usr/bin/hciconfig hci0 up.*|/usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up, /usr/bin/hciconfig hci0 down, /usr/sbin/hciconfig hci0 down, /usr/sbin/rfkill unblock bluetooth|' "$SUDOERS" > "$TMP"
  if sudo visudo -cf "$TMP"; then
    sudo install -m 440 "$TMP" "$SUDOERS"
    log "sudoers updated: added hciconfig down, /usr/sbin paths, rfkill"
  else
    log "ERROR: sudoers validation failed, skipping"
  fi
  rm -f "$TMP"
}

# ============================================
# Migration 010: Remove go-librespot audio_device (use default sink)
# ============================================
_migrate_010() {
  # Migration 008 set audio_device to "pipewire", but this is an ALSA PCM name
  # that only works with audio_backend: "alsa". Some devices ended up with
  # audio_backend: "pulseaudio" where "pipewire" is not a valid sink name,
  # causing go-librespot to get stuck in buffering state.
  # Removing audio_device entirely lets go-librespot use the system default
  # sink, which the Bluetooth manager already configures correctly.
  local CONFIG="$HOME/.config/go-librespot/config.yml"
  if [ -f "$CONFIG" ]; then
    if grep -q 'audio_device:' "$CONFIG"; then
      sed -i '/^audio_device:/d' "$CONFIG"
      log "go-librespot config: removed audio_device (will use default sink)"
    else
      log "go-librespot config: audio_device already absent, skipping"
    fi
  fi
}

# ============================================
# Run all migrations
# ============================================
run_migration "001" "Bluetooth audio via PipeWire"
run_migration "002" "Install pactl (missing from 001 on Trixie)"
run_migration "003" "Dynamic username support"
run_migration "004" "Berry to Mello rebrand"
run_migration "005" "Tomo to Mello rebrand"
run_migration "006" "Plymouth boot splash"
run_migration "007" "Mask getty@tty1 (missed by older installs)"
run_migration "008" "Route go-librespot audio through PipeWire"
run_migration "009" "Update sudoers for hciconfig down+up"
run_migration "010" "Remove go-librespot audio_device (use default sink)"

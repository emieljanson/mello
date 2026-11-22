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

# What Slint needs to draw the screen (keep in sync with pi/setup.sh)
SLINT_LIBS="libinput10 libgbm1 libegl1 libgles2 libegl-mesa0 libgl1-mesa-dri libxkbcommon0 libudev1 libfontconfig1 libfreetype6"

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
# Migration 011: Converge Raspberry Pi Touch Display 2 boot config
# ============================================
_migrate_011() {
  local BOOT_CONFIG=""
  if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
  elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG="/boot/config.txt"
  else
    log "Boot config not found, skipping display config migration"
    return 0
  fi

  local changed=false

  if grep -q "^display_auto_detect=1" "$BOOT_CONFIG" 2>/dev/null; then
    sudo sed -i 's/^display_auto_detect=1/#display_auto_detect=1/' "$BOOT_CONFIG"
    log "Commented display_auto_detect=1 in $BOOT_CONFIG"
    changed=true
  fi

  if grep -q "^disable_splash=" "$BOOT_CONFIG" 2>/dev/null; then
    if ! grep -q "^disable_splash=1" "$BOOT_CONFIG" 2>/dev/null; then
      sudo sed -i 's/^disable_splash=.*/disable_splash=1/' "$BOOT_CONFIG"
      log "Set disable_splash=1 in $BOOT_CONFIG"
      changed=true
    fi
  else
    echo "disable_splash=1" | sudo tee -a "$BOOT_CONFIG" > /dev/null
    log "Added disable_splash=1 to $BOOT_CONFIG"
    changed=true
  fi

  if grep -q "^dtoverlay=vc4-kms-dsi-ili9881-5inch" "$BOOT_CONFIG" 2>/dev/null; then
    if ! grep -q "^dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90" "$BOOT_CONFIG" 2>/dev/null; then
      sudo sed -i 's/^dtoverlay=vc4-kms-dsi-ili9881-5inch.*/dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90/' "$BOOT_CONFIG"
      log "Set Raspberry Pi Touch Display 2 overlay rotation=90 in $BOOT_CONFIG"
      changed=true
    fi
  else
    {
      echo ""
      echo "# Mello: Raspberry Pi Touch Display 2 (5\", landscape)"
      echo "dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90"
    } | sudo tee -a "$BOOT_CONFIG" > /dev/null
    log "Added Raspberry Pi Touch Display 2 overlay to $BOOT_CONFIG"
    changed=true
  fi

  if [ "$changed" = true ]; then
    log "Display boot config changed — reboot required (not rebooting automatically)"
  else
    log "Display boot config already converged"
  fi
}

# ============================================
# Migration 012: Reboot after display boot config changes
# ============================================
_migrate_012() {
  local BOOT_CONFIG=""
  if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
  elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG="/boot/config.txt"
  else
    log "Boot config not found, skipping display reboot check"
    return 0
  fi

  # Migration 011 changes boot-time display configuration. Field testing showed
  # the DSI panel can stay wedged until the Pi actually boots with that config.
  # Reboot only when the boot config was modified after the current boot.
  local boot_time
  local config_time
  boot_time=$(awk '/^btime / {print $2}' /proc/stat)
  config_time=$(stat -c %Y "$BOOT_CONFIG")

  if [ "$config_time" -le "$boot_time" ]; then
    log "Display boot config already applied by current boot"
    return 0
  fi

  if ! grep -q "^dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90" "$BOOT_CONFIG" 2>/dev/null; then
    log "Touch Display 2 overlay not active in boot config, skipping reboot"
    return 0
  fi

  log "Display boot config changed after current boot — rebooting now"
  sudo systemctl reboot --no-wall
}

# ============================================
# Migration 013: Disable Spotify suggested autoplay
# ============================================
_migrate_013() {
  # Keep albums/playlists inside their selected context. Mello also sets
  # repeat_context at runtime, but this config prevents Spotify suggestions if
  # repeat state is lost after a librespot restart.
  local CONFIG="$HOME/.config/go-librespot/config.yml"
  if [ ! -f "$CONFIG" ]; then
    log "go-librespot config not found, skipping disable_autoplay"
    return 0
  fi

  if grep -q '^disable_autoplay:' "$CONFIG"; then
    if grep -q '^disable_autoplay: true' "$CONFIG"; then
      log "go-librespot config: disable_autoplay already true"
    else
      sed -i 's/^disable_autoplay:.*/disable_autoplay: true/' "$CONFIG"
      log "go-librespot config: set disable_autoplay -> true"
    fi
    return 0
  fi

  if grep -q '^bitrate:' "$CONFIG"; then
    sed -i '/^bitrate:/a disable_autoplay: true' "$CONFIG"
  else
    printf '\ndisable_autoplay: true\n' >> "$CONFIG"
  fi
  log "go-librespot config: added disable_autoplay: true"
}

# ============================================
# Migration 014: Keep librespot independent of UI sleep/restarts
# ============================================
_migrate_014() {
  local CODE_DIR="$HOME/mello"
  [ -d "$CODE_DIR" ] || CODE_DIR="$HOME/tomo"
  [ -d "$CODE_DIR" ] || CODE_DIR="$HOME/berry"

  local SERVICE="/etc/systemd/system/mello-librespot.service"
  local TEMPLATE="$CODE_DIR/pi/systemd/mello-librespot.service.template"

  if [ -f "$TEMPLATE" ]; then
    local name
    name=$(basename "$TEMPLATE" .template)
    sed -e "s|__USER__|$MELLO_USER|g" \
        -e "s|__HOME__|$MELLO_HOME|g" \
        -e "s|__UID__|$MELLO_UID|g" \
        "$TEMPLATE" | sudo tee "/etc/systemd/system/$name" > /dev/null
    log "Rendered $name without PartOf=mello-native.service"
  elif [ -f "$SERVICE" ]; then
    sudo sed -i '/^PartOf=mello-native\.service$/d' "$SERVICE"
    log "Removed PartOf=mello-native.service from installed mello-librespot.service"
  else
    log "mello-librespot service not found, skipping"
    return 0
  fi

  sudo systemctl daemon-reload
  log "systemd reloaded after librespot dependency update"
}

# ============================================
# Migration 015: Allow Mello to recover a hung Spotify engine
# ============================================
_migrate_015() {
  local SUDOERS_DIR="${MELLO_SUDOERS_DIR:-/etc/sudoers.d}"
  local SUDOERS_FILE="$SUDOERS_DIR/mello-wifi"
  [ -f "$SUDOERS_FILE" ] || SUDOERS_FILE="$SUDOERS_DIR/berry-wifi"

  if [ ! -f "$SUDOERS_FILE" ]; then
    log "Mello sudoers file not found, skipping librespot recovery permission"
    return 0
  fi
  if sudo grep -q '/bin/systemctl restart mello-librespot' "$SUDOERS_FILE"; then
    log "Librespot recovery permission already installed"
    return 0
  fi

  local TMP_SUDOERS="/tmp/mello-sudoers.$$"
  sudo sed \
    's|/bin/systemctl start mello-librespot,|/bin/systemctl start mello-librespot, /bin/systemctl restart mello-librespot,|' \
    "$SUDOERS_FILE" > "$TMP_SUDOERS"
  if ! grep -q '/bin/systemctl restart mello-librespot' "$TMP_SUDOERS"; then
    log "ERROR: existing sudoers rule has an unexpected format"
    rm -f "$TMP_SUDOERS"
    return 1
  fi
  if sudo visudo -cf "$TMP_SUDOERS"; then
    sudo install -m 440 "$TMP_SUDOERS" "$SUDOERS_FILE"
    log "Installed librespot recovery permission"
  else
    log "ERROR: librespot recovery sudoers validation failed"
    rm -f "$TMP_SUDOERS"
    return 1
  fi
  rm -f "$TMP_SUDOERS"
}

# ============================================
# Migration 016: Let a speaker switch to Spotify Soloist
# ============================================
_migrate_016() {
  # Soloist is an opt-in second Spotify engine (the owner enters an API key
  # in the setup menu). Nothing is switched here; this only makes switching
  # possible:
  #  1. The app starts no longer pull in go-librespot by themselves
  #     (mello-native lost its Wants=), so each engine runs only when enabled.
  #     Make sure the engine this speaker uses is enabled.
  #  2. The app may enable/disable/stop/start/restart either engine.
  local CODE_DIR="$HOME/mello"
  local ENGINE_SERVICE=mello-librespot
  if grep -qE '^MELLO_SPOTIFY_ENGINE=soloist' "$CODE_DIR/.env" 2>/dev/null; then
    ENGINE_SERVICE=mello-soloist
  fi
  if ! sudo systemctl enable "$ENGINE_SERVICE"; then
    # Without this the speaker would come back from a reboot with no engine.
    log "ERROR: could not enable $ENGINE_SERVICE"
    return 1
  fi

  local SUDOERS_DIR="${MELLO_SUDOERS_DIR:-/etc/sudoers.d}"
  local SUDOERS_FILE="$SUDOERS_DIR/mello-wifi"
  if [ ! -f "$SUDOERS_FILE" ]; then
    log "Mello sudoers file not found, skipping engine switch permission"
    return 0
  fi
  if sudo grep -q '/bin/systemctl enable mello-soloist' "$SUDOERS_FILE"; then
    log "Engine switch permission already installed"
    return 0
  fi

  local EXTRA="/bin/systemctl stop mello-soloist, /bin/systemctl start mello-soloist, /bin/systemctl restart mello-soloist, /bin/systemctl enable mello-soloist, /bin/systemctl disable mello-soloist, /bin/systemctl enable mello-librespot, /bin/systemctl disable mello-librespot"
  local TMP_SUDOERS="/tmp/mello-sudoers.$$"
  sudo sed "s|/bin/systemctl restart mello-native,|/bin/systemctl restart mello-native, $EXTRA,|" \
    "$SUDOERS_FILE" > "$TMP_SUDOERS"
  if ! grep -q '/bin/systemctl enable mello-soloist' "$TMP_SUDOERS"; then
    log "ERROR: existing sudoers rule has an unexpected format"
    rm -f "$TMP_SUDOERS"
    return 1
  fi
  if sudo visudo -cf "$TMP_SUDOERS"; then
    sudo install -m 440 "$TMP_SUDOERS" "$SUDOERS_FILE"
    log "Installed engine switch permission"
  else
    log "ERROR: engine switch sudoers validation failed"
    rm -f "$TMP_SUDOERS"
    return 1
  fi
  rm -f "$TMP_SUDOERS"
}

# ============================================
# Migration 017: Keep the WiFi chip awake
# ============================================
_migrate_017() {
  # With WiFi power save on, the Pi's WiFi chip dozes and the router drops it
  # (wpa_supplicant "CTRL-EVENT-DISCONNECTED reason=6/7"), several times a day.
  # The NetworkManager setting makes it stick across reconnects and reboots;
  # iw switches it off right away without dropping the current connection.
  local CODE_DIR="$HOME/mello"
  local CONF_DIR="${MELLO_NM_CONF_DIR:-/etc/NetworkManager/conf.d}"
  local SRC="$CODE_DIR/pi/networkmanager/99-mello-wifi-powersave.conf"
  if [ ! -f "$SRC" ]; then
    log "ERROR: $SRC missing"
    return 1
  fi
  sudo mkdir -p "$CONF_DIR"
  sudo install -m 644 "$SRC" "$CONF_DIR/99-mello-wifi-powersave.conf"
  log "Installed WiFi power save config"
  local IW
  IW="$(command -v iw || echo /sbin/iw)"
  if [ -x "$IW" ]; then
    sudo "$IW" dev wlan0 set power_save off 2>/dev/null || log "Could not switch WiFi power save off now (applies on next connect)"
  fi
}

# ============================================
# Migration 018: Faster boot, skip cloud-init after the first boot
# ============================================
_migrate_018() {
  # cloud-init applies Raspberry Pi Imager's settings on the first boot only,
  # but keeps running on every boot in front of Mello (~8 s on a Pi 3).
  # WiFi lives in NetworkManager's own files, so nothing depends on it.
  local CLOUD_DIR="${MELLO_CLOUD_DIR:-/etc/cloud}"
  if [ ! -d "$CLOUD_DIR" ]; then
    log "No cloud-init on this speaker"
    return 0
  fi
  # "cloud-init status" exits 2 when its first boot finished with warnings
  # ("degraded done"); with pipefail that would read as "not finished".
  local STATUS
  STATUS="$(cloud-init status 2>/dev/null || true)"
  if ! grep -qE 'status: (done|error|disabled)' <<<"$STATUS"; then
    # Never cut a first boot short. (A speaker runs updates only after
    # install.sh, long after its first boot, so this is a safety net.)
    log "cloud-init has not finished its first boot, leaving it on"
    return 0
  fi
  sudo touch "$CLOUD_DIR/cloud-init.disabled"
  log "cloud-init disabled for later boots"
}

# ============================================
# Migration 019: Libraries for the Slint screen (MELLO_UI=slint)
# ============================================
_migrate_019() {
  # Slint's display backend loads libinput at start. (The slint package
  # itself comes from requirements.txt.)
  if dpkg -s libinput10 >/dev/null 2>&1; then
    log "libinput10 already installed"
    return 0
  fi
  # The package index can be outdated (a 404 was seen on the test speaker).
  sudo apt-get update -q || true
  sudo apt-get install -y -q libinput10
  log "Installed libinput10 for the Slint screen"
}

# ============================================
# Migration 021: Nightly update via a systemd timer instead of cron
# ============================================
# Cron skips the night when the speaker is off at 03:00 and never catches
# up; a hung run holds the update lock until a reboot. The timer runs a
# missed night at boot, and the service stops a run after 45 minutes.
# (020 is taken by the Slint screen.)
_migrate_021() {
  local code_dir="$MELLO_HOME/mello"
  sed -e "s|__USER__|$MELLO_USER|g" \
      -e "s|__HOME__|$MELLO_HOME|g" \
      -e "s|__UID__|$MELLO_UID|g" \
      "$code_dir/pi/systemd/mello-update.service.template" \
    | sudo tee /etc/systemd/system/mello-update.service > /dev/null || return 1
  # run_migration calls this inside an `if`, where set -e is off: every
  # step is checked by hand.
  sudo ln -sf "$code_dir/pi/systemd/mello-update.timer" /etc/systemd/system/mello-update.timer || return 1
  sudo systemctl daemon-reload || return 1
  sudo systemctl enable mello-update.timer || return 1
  sudo systemctl start mello-update.timer || return 1
  # Only drop cron once the timer is really waiting, so a speaker never
  # ends up with neither.
  if ! systemctl is-active --quiet mello-update.timer; then
    log "mello-update.timer did not start, keeping the cron job"
    return 1
  fi
  ( (crontab -l 2>/dev/null || true) | grep -v "pi/auto-update.sh" || true ) | crontab - || return 1
  log "Nightly update now runs from mello-update.timer"
}

# ============================================
# Migration 022: Make the speaker PipeWire's default output
# ============================================
# Soloist plays to PipeWire's default output. PipeWire ranked the Pi's
# headphone jack equal to the speaker and picked the jack on speakers that
# never used Bluetooth: "playing", but silent. The rule ranks the speaker first.
_migrate_022() {
  local code_dir="$MELLO_HOME/mello"
  local conf_dir="$MELLO_HOME/.config/wireplumber/wireplumber.conf.d"
  mkdir -p "$conf_dir" || return 1
  ln -sf "$code_dir/pi/wireplumber/50-mello-speaker.conf" "$conf_dir/50-mello-speaker.conf" || return 1
  # Picks up the rule now; a playing stream moves to the speaker by itself.
  XDG_RUNTIME_DIR="/run/user/$MELLO_UID" systemctl --user restart wireplumber \
    || log "wireplumber restart failed, the rule applies after the next reboot"
  log "Speaker is PipeWire's preferred output"
}

# ============================================
# Migration 023: WiFi watchdog outside the app
# ============================================
# A setup hotspot left up (app stopped mid-setup, or nobody finished setup)
# kept the speaker offline until someone pulled the plug. The watchdog runs
# every 2 minutes as root, independent of the app (pi/wifi-watchdog.sh).
_migrate_023() {
  local code_dir="$MELLO_HOME/mello"
  sed -e "s|__USER__|$MELLO_USER|g" \
      -e "s|__HOME__|$MELLO_HOME|g" \
      -e "s|__UID__|$MELLO_UID|g" \
      "$code_dir/pi/systemd/mello-wifi-watchdog.service.template" \
    | sudo tee /etc/systemd/system/mello-wifi-watchdog.service > /dev/null || return 1
  sudo ln -sf "$code_dir/pi/systemd/mello-wifi-watchdog.timer" /etc/systemd/system/mello-wifi-watchdog.timer || return 1
  sudo systemctl daemon-reload || return 1
  sudo systemctl enable mello-wifi-watchdog.timer || return 1
  sudo systemctl start mello-wifi-watchdog.timer || return 1
  log "WiFi watchdog runs every 2 minutes"
}

# ============================================
# Migration 020: Everything Slint needs to draw the screen
# ============================================
_migrate_020() {
  # Slint is now the only way Mello draws its screen. These libraries used to
  # come in with Pygame's SDL packages; install them by name so the screen
  # keeps working if those are ever removed.
  local missing=()
  for pkg in $SLINT_LIBS; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    log "Screen libraries already installed"
    return 0
  fi
  sudo apt-get update -q || true
  sudo apt-get install -y -q "${missing[@]}"
  log "Installed screen libraries: ${missing[*]}"
}

# ============================================
# Every update: are Slint's screen libraries there?
# ============================================
# Migrations 019/020 installed them once, but a marker says nothing about
# today: a speaker reinstalled with its home folder kept, or an in-place
# upgrade of Raspberry Pi OS, can have the marker and still miss them. Slint
# loads libinput only when it opens the screen, so "import slint" works and
# the app then fails to draw. A dpkg check per package is cheap, so check
# every run and install what is missing.
ensure_screen_libs() {
  local missing=() pkg
  for pkg in $SLINT_LIBS; do
    # (dpkg -s also succeeds for a removed package that left its config)
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed' \
      || missing+=("$pkg")
  done
  [ ${#missing[@]} -eq 0 ] && return 0
  log "Screen libraries missing: ${missing[*]}; installing"
  sudo apt-get update -q || true
  if sudo apt-get install -y -q "${missing[@]}"; then
    log "Installed screen libraries: ${missing[*]}"
  else
    log "ERROR: could not install screen libraries: ${missing[*]}"
    return 1
  fi
}

# ============================================
# Every update: can this speaker run the Slint screen?
# ============================================
# Slint needs Raspberry Pi OS Trixie or newer (Python 3.12+, glibc 2.35+). A
# speaker on an older system cannot show this version's screen, so it stays on
# the last version that drew its screen with Pygame (the commit before Pygame
# was removed) and keeps working, until it is reinstalled with Trixie. This
# runs after every update, not once: the nightly update moves the code to the
# newest version first.
SLINT_HOLD_FILE="$HOME/.mello-slint-hold"

slint_hold_check() {
  local code_dir="$1"
  local py="$code_dir/venv/bin/python"
  [ -x "$py" ] || return 0
  # Already on a version that draws with Pygame: nothing to hold back from
  # (and the check below would otherwise step back one more version).
  [ -f "$code_dir/mello/ui/helpers.py" ] && return 0
  if "$py" -c 'import slint' >/dev/null 2>&1; then
    rm -f "$SLINT_HOLD_FILE"
    return 0
  fi
  # A Trixie speaker that never got the package (say a 32-bit one, from
  # before 32-bit was supported): install it now.
  if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    "$code_dir/venv/bin/pip" install -q --disable-pip-version-check -r "$code_dir/requirements.txt" || true
    if "$py" -c 'import slint' >/dev/null 2>&1; then
      rm -f "$SLINT_HOLD_FILE"
      log "Installed Slint for the screen"
      return 0
    fi
  fi
  local pyver glibc why
  pyver=$("$py" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)
  glibc=$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}' || true)
  why="Slint cannot run here: Python $pyver, $(uname -m), glibc ${glibc:-unknown}; it needs Raspberry Pi OS Trixie or newer"
  # The commit that removed Pygame's screen; the one before it still has it.
  local removed
  removed=$(git -C "$code_dir" rev-list -1 HEAD -- mello/ui/helpers.py 2>/dev/null || true)
  if [ -z "$removed" ] || ! git -C "$code_dir" cat-file -e "$removed^:mello/ui/helpers.py" 2>/dev/null; then
    log "WARNING: $why, and no earlier version to stay on was found"
    return 0
  fi
  local keep
  keep=$(git -C "$code_dir" rev-parse "$removed^")
  log "HOLD: $why. Staying on $(git -C "$code_dir" log -1 --format='%h %s' "$keep") until this speaker is reinstalled with Trixie."
  echo "$(date '+%Y-%m-%d %H:%M:%S') $why; kept on $keep" > "$SLINT_HOLD_FILE"
  git -C "$code_dir" checkout -q --detach "$keep"
}

# ============================================
# Migration 024: go-librespot is gone
# ============================================
_migrate_024() {
  # Spotify plays through Soloist only. Only speakers that use it get this
  # far (soloist_hold_check keeps the others on the last version with
  # go-librespot), so the old player can go: service, program and its login.
  sudo systemctl disable --now mello-librespot 2>/dev/null || true
  sudo rm -f /etc/systemd/system/mello-librespot.service
  sudo systemctl daemon-reload
  sudo rm -f /usr/local/bin/go-librespot
  rm -rf "$MELLO_HOME/.config/go-librespot"
  log "Removed go-librespot"
}

# ============================================
# Before any migration: is this speaker on Soloist?
# ============================================
# From the version that added pi/needs-soloist on, Spotify plays through
# Soloist only. A speaker still on go-librespot normally never gets here: the
# update gate in pi/update-target.sh keeps it back. A speaker whose code was
# older than that gate (a stable speaker on an early release) can still jump
# here, so this puts it back on the last version with go-librespot, where it
# keeps playing and its settings menu says what to do.
SOLOIST_HOLD_FILE="$HOME/.mello-soloist-hold"

soloist_hold_check() {
  local code_dir="$1" added keep
  local soloist_home="${MELLO_SOLOIST_HOME:-$HOME/soloist}"
  if ! grep -qE "^[[:space:]]*MELLO_SPOTIFY_ENGINE[[:space:]]*=" "$code_dir/.env" 2>/dev/null \
     && [ -s "$soloist_home/key" ] && [ -x "$soloist_home/soloist" ]; then
    # The owner entered a key and Soloist is installed (the app plays through
    # it whenever the key is there), but .env has no engine setting at all,
    # for example a new .env after a reinstall that kept the home folder.
    # Write it back instead of sending the speaker back to go-librespot and
    # asking for the key again. (An explicit other setting is left alone.)
    # (finish a last line without a newline first)
    if [ -s "$code_dir/.env" ] && [ -n "$(tail -c1 "$code_dir/.env")" ]; then echo >> "$code_dir/.env"; fi
    echo "MELLO_SPOTIFY_ENGINE=soloist" >> "$code_dir/.env"
    log "Soloist key found: MELLO_SPOTIFY_ENGINE=soloist written back to .env"
  fi
  if grep -qE "^[[:space:]]*MELLO_SPOTIFY_ENGINE[[:space:]]*=[[:space:]]*[\"']?soloist[\"']?[[:space:]]*$" "$code_dir/.env" 2>/dev/null; then
    rm -f "$SOLOIST_HOLD_FILE"
    return 1
  fi
  [ -f "$code_dir/pi/needs-soloist" ] || return 1
  added=$(git -C "$code_dir" log -1 --first-parent --diff-filter=A --format=%H HEAD -- pi/needs-soloist 2>/dev/null || true)
  if [ -z "$added" ]; then
    # Fail closed: without the old version, stop rather than remove go-librespot
    log "ERROR: not on Soloist, and no earlier version with go-librespot was found; stopping"
    exit 1
  fi
  keep=$(git -C "$code_dir" rev-parse "$added^")
  log "HOLD: this speaker is not on Soloist yet. Staying on $(git -C "$code_dir" log -1 --format='%h %s' "$keep") until it is."
  echo "$(date '+%Y-%m-%d %H:%M:%S') not on Soloist; kept on $keep" > "$SOLOIST_HOLD_FILE"
  if ! git -C "$code_dir" checkout -q --detach "$keep"; then
    log "ERROR: cannot go back to the version with go-librespot; stopping"
    exit 1
  fi
}

if soloist_hold_check "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; then
  # The speaker may come from code older than that version, so run its own
  # migrations (that migrate.sh has no Soloist hold, so this does not loop).
  exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pi/migrate.sh"
fi

# ============================================
# Run all migrations
# ============================================
# Migrations run in order and stop at the first that fails (a later one may
# build on it); it runs again on the next update. The every-run checks below
# still run either way: a speaker must not miss its screen libraries because
# an unrelated migration failed (with set -e this script used to stop here).
MIGRATIONS_OK=true
{
  run_migration "001" "Bluetooth audio via PipeWire" &&
  run_migration "002" "Install pactl (missing from 001 on Trixie)" &&
  run_migration "003" "Dynamic username support" &&
  run_migration "004" "Berry to Mello rebrand" &&
  run_migration "005" "Tomo to Mello rebrand" &&
  run_migration "006" "Plymouth boot splash" &&
  run_migration "007" "Mask getty@tty1 (missed by older installs)" &&
  run_migration "008" "Route go-librespot audio through PipeWire" &&
  run_migration "009" "Update sudoers for hciconfig down+up" &&
  run_migration "010" "Remove go-librespot audio_device (use default sink)" &&
  run_migration "011" "Converge Raspberry Pi Touch Display 2 boot config" &&
  run_migration "012" "Reboot after display boot config changes" &&
  run_migration "013" "Disable Spotify suggested autoplay" &&
  run_migration "014" "Keep librespot independent of UI sleep/restarts" &&
  run_migration "015" "Allow automatic librespot recovery" &&
  run_migration "016" "Let a speaker switch to Spotify Soloist" &&
  run_migration "017" "Keep the WiFi chip awake (no power save)" &&
  run_migration "018" "Faster boot: skip cloud-init after the first boot" &&
  run_migration "019" "Libraries for the Slint screen" &&
  run_migration "020" "Everything Slint needs to draw the screen" &&
  run_migration "021" "Nightly update via a systemd timer instead of cron" &&
  run_migration "022" "Make the speaker PipeWire's default output" &&
  run_migration "023" "WiFi watchdog outside the app" &&
  run_migration "024" "go-librespot is gone"
} || MIGRATIONS_OK=false

CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKS_OK=true
ensure_screen_libs || CHECKS_OK=false
slint_hold_check "$CODE_ROOT"

if [ "$MIGRATIONS_OK" != true ] || [ "$CHECKS_OK" != true ]; then
  log "ERROR: not everything is in place yet; the next update run tries again"
  exit 1
fi


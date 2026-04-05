#!/bin/bash
# Mello First-Time Setup Script
# Run this ONCE on a new Raspberry Pi (Lite or Desktop)

set -euo pipefail

# Detect installing user (used for systemd services, permissions, sudoers)
MELLO_USER="$USER"
MELLO_HOME="$HOME"
MELLO_UID="$(id -u)"

# Render a .service.template file and install it to systemd
install_service() {
  local template="$1"
  local name
  name=$(basename "$template" .template)
  sed -e "s|__USER__|$MELLO_USER|g" \
      -e "s|__HOME__|$MELLO_HOME|g" \
      -e "s|__UID__|$MELLO_UID|g" \
      "$template" | sudo tee "/etc/systemd/system/$name" > /dev/null
}

echo "Mello Setup Starting..."
echo ""

# ============================================
# 1. Install system packages (needed by later steps)
# ============================================
echo "Installing system packages..."
sudo apt-get update
sudo apt-get install -y \
  curl git \
  python3-venv python3-pip python3-dev python3-pygame \
  libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev \
  network-manager \
  pipewire pipewire-pulse wireplumber pipewire-alsa libspa-0.2-bluetooth

# ============================================
# 2. Configure boot settings (display + audio + quiet boot)
# ============================================
echo "Configuring boot settings..."

# Detect boot config location (Bookworm+ uses /boot/firmware/)
if [ -f /boot/firmware/config.txt ]; then
  BOOT_CONFIG="/boot/firmware/config.txt"
  BOOT_CMDLINE="/boot/firmware/cmdline.txt"
elif [ -f /boot/config.txt ]; then
  BOOT_CONFIG="/boot/config.txt"
  BOOT_CMDLINE="/boot/cmdline.txt"
else
  echo "Could not find boot config"
  BOOT_CONFIG=""
fi

BOOT_CHANGED=false

if [ -n "$BOOT_CONFIG" ]; then
  # Disable display_auto_detect (conflicts with specific display overlay)
  if grep -q "^display_auto_detect=1" "$BOOT_CONFIG" 2>/dev/null; then
    sudo sed -i 's/^display_auto_detect=1/#display_auto_detect=1/' "$BOOT_CONFIG"
    BOOT_CHANGED=true
  fi

  # Add display overlay for Raspberry Pi Touch Display 2 (5")
  if ! grep -q "vc4-kms-dsi-ili9881-5inch" "$BOOT_CONFIG" 2>/dev/null; then
    {
      echo ""
      echo "# Mello: Raspberry Pi Touch Display 2 (5\", landscape)"
      echo "disable_splash=1"
      echo "dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90"
    } | sudo tee -a "$BOOT_CONFIG" > /dev/null
    echo "  Display overlay added"
    BOOT_CHANGED=true
  else
    echo "  Display overlay already configured"
  fi

  # Quiet boot (hide kernel text during startup)
  if [ -f "$BOOT_CMDLINE" ] && ! grep -q "quiet" "$BOOT_CMDLINE" 2>/dev/null; then
    sudo sed -i 's/$/ logo.nologo quiet splash loglevel=0 vt.global_cursor_default=0/' "$BOOT_CMDLINE"
    echo "  Quiet boot configured"
    BOOT_CHANGED=true
  fi
fi

# ============================================
# 3. Install WM8960 Audio HAT driver
# ============================================
if ! aplay -l 2>/dev/null | grep -q "wm8960"; then
  echo "Installing WM8960 Audio HAT driver..."
  if git clone https://github.com/waveshare/WM8960-Audio-HAT.git /tmp/wm8960 2>/dev/null; then
    cd /tmp/wm8960
    sudo ./install.sh && echo "  WM8960 driver installed" || echo "  WM8960 install script failed"
    cd ~/mello
    rm -rf /tmp/wm8960
    BOOT_CHANGED=true
  else
    echo "  Could not download WM8960 driver - skipping"
  fi
else
  echo "WM8960 Audio HAT driver already installed"
fi

# ============================================
# 4. Install go-librespot (Spotify Connect daemon)
# ============================================
if ! command -v go-librespot &> /dev/null; then
  echo "Installing go-librespot..."
  ARCH=$(dpkg --print-architecture)
  LATEST=$(curl -sL https://api.github.com/repos/devgianlu/go-librespot/releases/latest \
    | grep "browser_download_url.*linux_${ARCH}" | head -1 | cut -d '"' -f 4)

  if [ -z "$LATEST" ]; then
    echo "Could not find go-librespot release for $ARCH"
    echo "   Install manually: https://github.com/devgianlu/go-librespot/releases"
  else
    curl -L "$LATEST" -o /tmp/go-librespot.tar.gz
    sudo tar -xzf /tmp/go-librespot.tar.gz -C /usr/local/bin go-librespot
    rm -f /tmp/go-librespot.tar.gz
    echo "  go-librespot installed"
  fi
else
  echo "go-librespot already installed"
fi

# ============================================
# 5. Configure go-librespot
# ============================================
mkdir -p ~/.config/go-librespot

if [ ! -f ~/.config/go-librespot/config.yml ]; then
  cat > ~/.config/go-librespot/config.yml << 'EOF'
device_name: "Mello"
device_type: "speaker"
audio_backend: "alsa"
audio_device: "default"
external_volume: true
initial_volume: 100
bitrate: 320
server:
  enabled: true
  port: 3678
credentials:
  type: "zeroconf"
  zeroconf:
    persist_credentials: true
EOF
  echo "go-librespot config created"
fi

# ============================================
# 6. Install WiFi Connect (captive portal for WiFi setup)
# ============================================
if [ ! -f /usr/local/bin/wifi-connect ]; then
  echo "Installing WiFi Connect..."
  ARCH=$(dpkg --print-architecture)

  case $ARCH in
    arm64|aarch64) WC_TRIPLE="aarch64-unknown-linux-gnu" ;;
    armhf) WC_TRIPLE="armv7-unknown-linux-gnueabihf" ;;
    *) WC_TRIPLE="aarch64-unknown-linux-gnu" ;;
  esac

  WC_URL=$(curl -sL https://api.github.com/repos/balena-os/wifi-connect/releases/latest \
    | grep "browser_download_url.*${WC_TRIPLE}\.tar\.gz" | head -1 | cut -d '"' -f 4)

  if [ -z "$WC_URL" ]; then
    echo "  Could not find WiFi Connect release - skipping"
  else
    if curl -fL "$WC_URL" -o /tmp/wifi-connect.tar.gz 2>/dev/null \
        && file /tmp/wifi-connect.tar.gz | grep -q "gzip\|tar"; then
      sudo tar -xzf /tmp/wifi-connect.tar.gz -C /usr/local/bin
      echo "  WiFi Connect binary installed"
    else
      echo "  WiFi Connect download failed - skipping (not required for basic operation)"
    fi
    rm -f /tmp/wifi-connect.tar.gz

    # Download UI assets (separate package)
    WC_UI_URL=$(curl -sL https://api.github.com/repos/balena-os/wifi-connect/releases/latest \
      | grep "browser_download_url.*wifi-connect-ui\.tar\.gz" | head -1 | cut -d '"' -f 4)
    # Install Mello custom portal UI (overrides default wifi-connect UI)
    sudo mkdir -p /usr/local/share/wifi-connect/ui
    sudo cp ~/mello/portal/index.html /usr/local/share/wifi-connect/ui/index.html
    echo "  Mello portal UI installed"
  fi
else
  echo "WiFi Connect already installed"
  # Always update Mello portal UI (may have changed)
  sudo mkdir -p /usr/local/share/wifi-connect/ui
  sudo cp ~/mello/portal/index.html /usr/local/share/wifi-connect/ui/index.html
  echo "Mello portal UI updated"
fi

# ============================================
# 7. Setup Python virtual environment
# ============================================
echo "Setting up Python environment..."
cd ~/mello

if [ ! -d "venv" ]; then
  python3 -m venv --system-site-packages venv
fi

source venv/bin/activate

# Remove pip-built pygame if present — it bundles an older SDL that breaks kmsdrm.
# The system python3-pygame (apt) uses SDL 2.32+ and must be used instead.
pip show pygame 2>/dev/null | grep -q "Location.*venv" && pip uninstall -y pygame 2>/dev/null || true

pip install -q -r requirements.txt

mkdir -p data/images

# ============================================
# 8. Anonymous usage data
# ============================================
echo ""
echo "Mello collects anonymous usage data (play/pause sessions,"
echo "   sleep/wake events) to help improve the project."
echo "   No personal data or music choices are shared."
echo ""
read -rp "   Share anonymous usage data? [Y/n] " ANALYTICS_CHOICE
ANALYTICS_CHOICE="${ANALYTICS_CHOICE:-Y}"

if [[ "$ANALYTICS_CHOICE" =~ ^[Nn] ]]; then
  SHARE_USAGE=false
  echo "  Usage data sharing disabled"
else
  SHARE_USAGE=true
  echo "  Usage data sharing enabled — thank you!"
fi

# Write to settings.json (merge with existing if present)
SETTINGS_FILE=~/mello/data/settings.json
mkdir -p ~/mello/data
if [ -f "$SETTINGS_FILE" ]; then
  # Update existing settings file
  python3 -c "
import json, sys
with open('$SETTINGS_FILE') as f:
    data = json.load(f)
data['share_usage_data'] = $SHARE_USAGE
with open('$SETTINGS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || echo "{\"share_usage_data\": $SHARE_USAGE}" > "$SETTINGS_FILE"
else
  echo "{\"share_usage_data\": $SHARE_USAGE}" > "$SETTINGS_FILE"
fi

# Save install environment (used by auto-update and migrations)
cat > ~/mello/.mello-env << EOF
MELLO_USER=$MELLO_USER
MELLO_HOME=$MELLO_HOME
MELLO_UID=$MELLO_UID
EOF

# ============================================
# 9. Setup systemd services
# ============================================
echo "Setting up systemd services..."
for tmpl in ~/mello/pi/systemd/*.service.template; do
  install_service "$tmpl"
done
sudo ln -sf ~/mello/pi/systemd/mello-touch-fix.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable mello-wifi 2>/dev/null || true
sudo rm -f /etc/systemd/system/mello-wifi.service
sudo systemctl enable mello-librespot mello-native mello-touch-fix

# Enable PipeWire user services (Bluetooth audio routing)
sudo -u "$MELLO_USER" XDG_RUNTIME_DIR=/run/user/$MELLO_UID systemctl --user enable pipewire pipewire-pulse wireplumber 2>/dev/null || true

# ============================================
# 10. Setup permissions (display, audio, touch, backlight)
# ============================================
echo "Setting up permissions..."

# Ensure dedicated runtime group exists for Mello-controlled hardware nodes.
if ! getent group mello >/dev/null; then
  sudo groupadd --system mello
fi

# Add installing user to required groups.
sudo usermod -aG video,audio,input,bluetooth,mello "$MELLO_USER" 2>/dev/null || true

# Backlight control (for sleep mode) — udev rule + apply immediately
echo 'SUBSYSTEM=="backlight", RUN+="/bin/chgrp mello /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power", RUN+="/bin/chmod 660 /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power"' \
  | sudo tee /etc/udev/rules.d/99-backlight.rules > /dev/null
sudo chgrp mello /sys/class/backlight/*/bl_power /sys/class/backlight/*/brightness 2>/dev/null || true
sudo chmod 660 /sys/class/backlight/*/bl_power /sys/class/backlight/*/brightness 2>/dev/null || true

# DRM/KMS access for pygame kmsdrm driver (card + render nodes)
cat << 'UDEV' | sudo tee /etc/udev/rules.d/99-mello-drm.rules > /dev/null
SUBSYSTEM=="drm", GROUP="video", MODE="0660"
SUBSYSTEM=="video4linux", GROUP="video", MODE="0660"
UDEV
sudo chmod 660 /dev/dri/card* /dev/dri/render* 2>/dev/null || true

# CPU governor + LED control (for sleep mode energy saving)
cat << 'UDEV' | sudo tee /etc/udev/rules.d/99-mello-power.rules > /dev/null
SUBSYSTEM=="cpu", KERNEL=="cpu0", RUN+="/bin/chgrp mello /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", RUN+="/bin/chmod 660 /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
SUBSYSTEM=="leds", KERNEL=="ACT", RUN+="/bin/chgrp mello /sys/class/leds/ACT/trigger /sys/class/leds/ACT/brightness", RUN+="/bin/chmod 660 /sys/class/leds/ACT/trigger /sys/class/leds/ACT/brightness"
UDEV
sudo udevadm control --reload-rules 2>/dev/null || true
sudo udevadm trigger 2>/dev/null || true

# Disable getty on tty1 so Mello can own the display (kmsdrm requires a free VT)
sudo systemctl mask getty@tty1.service 2>/dev/null || true
sudo systemctl stop getty@tty1.service 2>/dev/null || true

# Allow Mello app to run wifi-connect, nmcli, and librespot service management
# without a password prompt (needed for the setup menu)
TMP_SUDOERS="/tmp/mello-wifi.$$"
cat > "$TMP_SUDOERS" << EOF
$MELLO_USER ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /bin/systemctl stop mello-librespot, /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up
EOF
sudo visudo -cf "$TMP_SUDOERS"
sudo install -m 440 "$TMP_SUDOERS" /etc/sudoers.d/mello-wifi
rm -f "$TMP_SUDOERS"

# ============================================
# 11. Setup auto-update cron job
# ============================================
echo "Setting up auto-updates..."
chmod +x ~/mello/pi/auto-update.sh
# crontab -l exits 1 when empty; with pipefail that would abort the whole script
( (crontab -l 2>/dev/null || true) | grep -v "mello/pi/auto-update" || true
  echo "0 3 * * * bash ~/mello/pi/auto-update.sh >> ~/mello-update.log 2>&1"
) | crontab -

# ============================================
# 12. CPU power management (energy saving)
# ============================================
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
  echo "ondemand" | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null 2>&1 || true
  echo "CPU governor set to 'ondemand'"
fi

# ============================================
# Done!
# ============================================
echo ""
echo "============================================"
echo "Mello setup complete!"
echo "============================================"
echo ""

if [ "$BOOT_CHANGED" = true ]; then
  echo "Boot config was changed — reboot required!"
  echo ""
  echo "  sudo reboot"
  echo ""
  echo "After reboot, Mello starts automatically."
else
  echo "Starting services..."
  sudo systemctl start mello-librespot mello-native
  echo ""
  echo "Mello is running!"
fi

echo ""
echo "Next steps:"
echo "  1. Open Spotify on your phone"
echo "  2. Tap the speaker icon"
echo "  3. Connect to 'Mello'"
echo ""
echo "If WiFi disconnects, Mello creates a"
echo "'Mello-Setup' hotspot to reconfigure."
echo ""

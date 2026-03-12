#!/bin/bash
# Berry First-Time Setup Script
# Run this ONCE on a new Raspberry Pi

set -e
echo "🍓 Berry Setup Starting..."
echo ""

# ============================================
# 1. Check/Install go-librespot
# ============================================
if ! command -v go-librespot &> /dev/null; then
  echo "📦 Installing go-librespot..."
  # Download latest release
  ARCH=$(dpkg --print-architecture)
  LATEST=$(curl -s https://api.github.com/repos/devgianlu/go-librespot/releases/latest | grep "browser_download_url.*linux_${ARCH}" | cut -d '"' -f 4)
  curl -L "$LATEST" -o /tmp/go-librespot.tar.gz
  sudo tar -xzf /tmp/go-librespot.tar.gz -C /usr/local/bin go-librespot
  rm /tmp/go-librespot.tar.gz
  echo "✅ go-librespot installed"
else
  echo "✅ go-librespot already installed"
fi

# ============================================
# 2. Configure go-librespot
# ============================================
mkdir -p ~/.config/go-librespot

# Create default config if not exists
if [ ! -f ~/.config/go-librespot/config.toml ]; then
  cat > ~/.config/go-librespot/config.toml << 'EOF'
[server]
enabled = true
port = 3678

[player]
device_name = "Berry"
device_type = "speaker"
EOF
  echo "✅ go-librespot config created"
fi

# Note: Spotify credentials will be set up via the Berry app
# The app shows a setup screen prompting users to connect via Spotify

# ============================================
# 3. Install system packages
# ============================================
echo "📦 Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev network-manager

# ============================================
# 3b. Install WiFi Connect (captive portal for WiFi setup)
# ============================================
if [ ! -f /usr/local/bin/wifi-connect ]; then
  echo "📶 Installing WiFi Connect..."
  ARCH=$(dpkg --print-architecture)

  # Map Debian arch to wifi-connect asset name
  case $ARCH in
    arm64|aarch64) WC_ARCH="aarch64-unknown-linux-gnu" ;;
    armhf) WC_ARCH="armv7-unknown-linux-gnueabihf" ;;
    *) WC_ARCH="aarch64-unknown-linux-gnu" ;;
  esac

  # Download latest wifi-connect release
  WC_URL="https://github.com/balena-os/wifi-connect/releases/latest/download/wifi-connect-${WC_ARCH}.tar.gz"
  echo "Downloading from: $WC_URL"
  curl -fL "$WC_URL" -o /tmp/wifi-connect.tar.gz
  if [ $? -eq 0 ] && [ -s /tmp/wifi-connect.tar.gz ]; then
    sudo tar -xzf /tmp/wifi-connect.tar.gz -C /usr/local/bin
    rm /tmp/wifi-connect.tar.gz
    echo "✅ WiFi Connect installed"
  else
    echo "⚠️ WiFi Connect download failed, skipping (captive portal won't be available)"
    rm -f /tmp/wifi-connect.tar.gz
  fi
else
  echo "✅ WiFi Connect already installed"
fi

# ============================================
# 4. Setup Python virtual environment
# ============================================
echo "🐍 Setting up Python environment..."
cd ~/berry

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

# Rebuild pygame from source to link against system SDL2 (with kmsdrm support)
# The pip wheel bundles its own SDL2 without kmsdrm, so Berry can't render to the display
echo "🔧 Building pygame with kmsdrm support (this takes a few minutes)..."
pip install --no-binary pygame pygame --force-reinstall -q

# Create data directory
mkdir -p data/images

# ============================================
# 5. Setup systemd services (symlinks)
# ============================================
echo "⚙️ Setting up systemd services..."
chmod +x ~/berry/pi/wifi-check.sh
sudo ln -sf ~/berry/pi/systemd/berry-wifi.service /etc/systemd/system/
sudo ln -sf ~/berry/pi/systemd/berry-librespot.service /etc/systemd/system/
sudo ln -sf ~/berry/pi/systemd/berry-native.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable berry-wifi berry-librespot berry-native

# ============================================
# 6. Setup backlight permissions (for sleep mode)
# ============================================
echo "💡 Setting up backlight permissions..."
sudo usermod -aG video $USER 2>/dev/null || true
# Works for any touchscreen (rpi_backlight, 10-0045, etc.)
echo 'SUBSYSTEM=="backlight", RUN+="/bin/chmod 666 /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power"' | sudo tee /etc/udev/rules.d/99-backlight.rules > /dev/null

# ============================================
# 7. Setup auto-update cron job
# ============================================
echo "🔄 Setting up auto-updates..."
chmod +x ~/berry/pi/auto-update.sh
(crontab -l 2>/dev/null | grep -v "berry/pi/auto-update"; echo "0 * * * * ~/berry/pi/auto-update.sh >> ~/berry-update.log 2>&1") | crontab -

# ============================================
# 8. CPU power management (energy saving)
# ============================================
echo "⚡ Configuring CPU power management..."
# Use 'ondemand' governor for automatic frequency scaling
# CPU scales down to 600MHz in idle, up to 1.5GHz under load
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
  echo "ondemand" | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null
  echo "✅ CPU governor set to 'ondemand'"
  
  # Make it persistent across reboots
  if ! grep -q "scaling_governor" /etc/rc.local 2>/dev/null; then
    sudo sed -i '/^exit 0/i echo "ondemand" | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null' /etc/rc.local 2>/dev/null || true
  fi
else
  echo "⚠️ CPU frequency scaling not available (VM or unsupported kernel)"
fi

# ============================================
# 9. Configure display (5" DSI touchscreen)
# ============================================
echo "🖥️ Configuring display..."

# Add display overlay to config.txt if not already present
if ! grep -q "vc4-kms-dsi-ili9881-5inch" /boot/firmware/config.txt 2>/dev/null; then
  echo "" | sudo tee -a /boot/firmware/config.txt > /dev/null
  echo "# Berry 5-inch DSI touchscreen" | sudo tee -a /boot/firmware/config.txt > /dev/null
  echo "dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90" | sudo tee -a /boot/firmware/config.txt > /dev/null
  echo "✅ Display overlay added to config.txt"
else
  echo "✅ Display overlay already configured"
fi

# Add panel orientation to cmdline.txt if not already present
if ! grep -q "panel_orientation" /boot/firmware/cmdline.txt 2>/dev/null; then
  # Append to the single line in cmdline.txt (no newline)
  sudo sed -i 's/$/ video=DSI-1:panel_orientation=left_side_up/' /boot/firmware/cmdline.txt
  echo "✅ Panel orientation added to cmdline.txt"
else
  echo "✅ Panel orientation already configured"
fi

# Disable console blanking (prevents screen going black)
if ! grep -q "consoleblank=0" /boot/firmware/cmdline.txt 2>/dev/null; then
  sudo sed -i 's/$/ consoleblank=0/' /boot/firmware/cmdline.txt
  echo "✅ Console blanking disabled"
fi

# ============================================
# 10. Start services
# ============================================
echo "🚀 Starting services..."
sudo systemctl start berry-librespot berry-native

echo ""
echo "============================================"
echo "✅ Berry setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Reboot: sudo reboot"
echo "  2. Berry starts automatically in fullscreen"
echo "  3. Open Spotify on your phone"
echo "  4. Connect to 'Berry' speaker"
echo ""
echo "If WiFi disconnects:"
echo "  Berry creates a 'Berry-Setup' hotspot"
echo "  Connect to configure WiFi"
echo ""

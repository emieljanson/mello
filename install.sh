#!/bin/bash
# Mello One-Line Installer
# Usage: curl -sSL https://raw.githubusercontent.com/emieljanson/mello/main/install.sh | bash
# Options: --no-analytics  Disable anonymous usage data

set -e

# Parse flags to pass through to setup.sh
SETUP_FLAGS=""
for arg in "$@"; do
  case "$arg" in
    --no-analytics) SETUP_FLAGS="$SETUP_FLAGS --no-analytics" ;;
  esac
done

echo ""
echo "Mello Installer"
echo "=================="
echo ""

# Check if already installed
if [ -d ~/mello ]; then
  echo "Mello is already installed in ~/mello"
  echo "   For updates: cd ~/mello && git pull"
  exit 1
fi

# Install git if needed
if ! command -v git &> /dev/null; then
  echo "Installing git..."
  sudo apt-get update
  sudo apt-get install -y git
fi

# Clone repository
echo "Downloading Mello..."
git clone https://github.com/emieljanson/mello.git ~/mello

# Run setup
echo ""
echo "Running setup..."
cd ~/mello/pi
chmod +x setup.sh
./setup.sh $SETUP_FLAGS

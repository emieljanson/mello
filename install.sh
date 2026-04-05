#!/bin/bash
# Mello One-Line Installer
# Usage: curl -sSL https://raw.githubusercontent.com/emieljanson/mello/main/install.sh | bash

set -e

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
./setup.sh

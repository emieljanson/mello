#!/bin/bash
# Berry One-Line Installer
# Usage: curl -sSL https://raw.githubusercontent.com/emieljanson/berry/main/install.sh | bash

set -e

echo ""
echo "🍓 Berry Installer"
echo "=================="
echo ""

# Check if already installed
if [ -d ~/berry ]; then
  echo "⚠️  Berry is already installed in ~/berry"
  echo "   For updates: cd ~/berry && git pull"
  exit 1
fi

# Install git if needed
if ! command -v git &> /dev/null; then
  echo "📦 Installing git..."
  sudo apt-get update
  sudo apt-get install -y git
fi

# Clone repository
echo "📥 Downloading Berry..."
git clone https://github.com/emieljanson/berry.git ~/berry

# Run setup
echo ""
echo "🚀 Running setup..."
cd ~/berry/pi
chmod +x setup.sh
./setup.sh

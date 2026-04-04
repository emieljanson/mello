#!/bin/bash
# Berry Native - Development launcher
# Starts go-librespot (if needed) and the Pygame app

set -e
cd "$(dirname "$0")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🍓 Berry Native${NC}"
echo ""

# Check if go-librespot is installed
if ! command -v go-librespot &> /dev/null; then
    echo -e "${RED}❌ go-librespot not found${NC}"
    echo ""
    echo "Install it with:"
    echo "  brew tap devgianlu/tap"
    echo "  brew install go-librespot"
    echo ""
    echo "Then create config:"
    echo "  mkdir -p ~/.config/go-librespot"
    echo '  cat > ~/.config/go-librespot/config.toml << EOF'
    echo '[server]'
    echo 'enabled = true'
    echo 'port = 3678'
    echo ''
    echo '[player]'
    echo 'device_name = "Berry Dev"'
    echo 'device_type = "speaker"'
    echo 'EOF'
    exit 1
fi

# Check if go-librespot is running
if ! curl -s http://localhost:3678/status > /dev/null 2>&1; then
    echo -e "${YELLOW}Starting go-librespot...${NC}"
    
    # Create config if not exists
    if [ ! -f ~/.config/go-librespot/config.toml ]; then
        mkdir -p ~/.config/go-librespot
        cat > ~/.config/go-librespot/config.toml << 'EOF'
[server]
enabled = true
port = 3678

[player]
device_name = "Berry Dev"
device_type = "speaker"
EOF
        echo "Created default config"
    fi
    
    # Start librespot in background
    go-librespot --config_dir ~/.config/go-librespot > /tmp/librespot.log 2>&1 &
    LIBRESPOT_PID=$!
    echo "Started go-librespot (PID: $LIBRESPOT_PID)"
    
    # Wait for it to be ready
    for i in {1..10}; do
        if curl -s http://localhost:3678/status > /dev/null 2>&1; then
            echo -e "${GREEN}✓ go-librespot ready${NC}"
            break
        fi
        sleep 0.5
    done
else
    echo -e "${GREEN}✓ go-librespot already running${NC}"
fi

# Create virtual environment if needed
if [ ! -d "venv" ]; then
    echo ""
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate and install dependencies
source venv/bin/activate
pip install -q -r requirements.txt

# Create data directory
mkdir -p data/images

echo ""
echo -e "${GREEN}Starting Berry...${NC}"
echo "Connect Spotify to 'Berry Dev' to play music"
echo ""

# Run the app (pass any arguments like --fullscreen)
python berry.py "$@"

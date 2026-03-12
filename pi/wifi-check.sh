#!/bin/bash
# Berry WiFi Check Script
# Starts a captive portal if no WiFi connection is available
#
# This script is run at boot by the berry-wifi.service
# If WiFi is connected, it exits immediately
# If not, it starts a hotspot "Berry-Setup" where users can configure WiFi
#
# IMPORTANT: No set -e — errors are handled explicitly to avoid blocking boot

# Check if we have a WiFi connection
SSID=$(iwgetid -r 2>/dev/null || true)

if [ -n "$SSID" ]; then
    echo "WiFi connected to: $SSID"
    exit 0
fi

echo "No WiFi connection detected"

# Check if wifi-connect is installed
if [ ! -f /usr/local/bin/wifi-connect ]; then
    echo "wifi-connect not installed, skipping captive portal"
    exit 0
fi

echo "Starting WiFi setup portal..."
echo "Connect to 'Berry-Setup' hotspot to configure WiFi"

# Start the captive portal with retry logic
# Try up to 2 times in case of transient failures
for attempt in 1 2; do
    /usr/local/bin/wifi-connect \
        --portal-ssid "Berry-Setup" \
        --portal-passphrase "" \
        --portal-listening-port 80 \
        --activity-timeout 300
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        echo "WiFi configured successfully"
        exit 0
    fi

    echo "wifi-connect attempt $attempt failed (exit code: $exit_code)"

    if [ $attempt -lt 2 ]; then
        echo "Retrying in 5 seconds..."
        sleep 5
    fi
done

# All attempts failed — exit 0 anyway to not block boot
echo "WiFi setup portal failed, continuing boot without WiFi"
exit 0

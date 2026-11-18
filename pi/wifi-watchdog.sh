#!/bin/bash
# Mello WiFi watchdog: gets the speaker back online without the app.
#
# Runs every 2 minutes as root (mello-wifi-watchdog.timer). The Mello app sets
# up WiFi through the "Mello-Setup" hotspot; while that runs the speaker is
# offline on purpose. If the app is gone (stopped, crashed mid-setup) or the
# hotspot has been up for half an hour with nobody finishing setup, nothing in
# the app brings the speaker back: it then stayed on its hotspot until someone
# pulled the plug (2026-11-17). This puts the speaker back on its known
# network instead.
#
# It also keeps a copy of the known WiFi networks while online: retyping the
# password of a known network on the phone makes wifi-connect delete the old
# entry first, so a setup that is abandoned can leave the speaker with no
# known network at all. That copy is put back when the hotspot is cleaned up.
set -u

AP_NAME="Mello-Setup"
HOTSPOT_MAX_SECONDS=${HOTSPOT_MAX_SECONDS:-1800}
STATE=/run/mello-wifi-watchdog.hotspot-since
# The app touches this every 20 s while its WiFi setup screen is up (also
# while it joins the network picked on the phone, with the hotspot down).
SETUP_MARKER=/tmp/mello-wifi-setup
BACKUP=/var/lib/mello/wifi-backup
NM_DIR=/etc/NetworkManager/system-connections

log() { echo "mello-wifi-watchdog: $*"; }

hotspot_up() {
  nmcli -t -f NAME,TYPE con show --active 2>/dev/null | grep -qx "$AP_NAME:802-11-wireless"
}

wifi_files() {
  # Keyfiles of the WiFi networks the owner set up (not the hotspot).
  grep -l -E '^type=(wifi|802-11-wireless)$' "$NM_DIR"/*.nmconnection 2>/dev/null \
    | while read -r f; do grep -qx "id=$AP_NAME" "$f" || echo "$f"; done
}

back_up_networks() {
  local files
  files=$(wifi_files)
  [ -n "$files" ] || return 0
  mkdir -p "$BACKUP" && chmod 700 "$BACKUP"
  rm -f "$BACKUP"/*.nmconnection
  while read -r f; do cp -p "$f" "$BACKUP/"; done <<< "$files"
}

put_back_networks() {
  local f uuid ssid
  for f in "$BACKUP"/*.nmconnection; do
    [ -e "$f" ] || continue
    uuid=$(sed -n 's/^uuid=//p' "$f")
    ssid=$(sed -n 's/^ssid=//p' "$f")
    nmcli -t -f UUID con show | grep -qx "$uuid" && continue
    # The phone may have saved the same network anew: then it is not missing.
    if [ -n "$ssid" ] && grep -lqx "ssid=$ssid" $(wifi_files) /dev/null 2>/dev/null; then continue; fi
    install -m 600 "$f" "$NM_DIR/" && nmcli con load "$NM_DIR/$(basename "$f")" \
      && log "put back WiFi network $(sed -n 's/^id=//p' "$f")"
  done
}

leave_hotspot() {
  log "stopping the setup hotspot ($1) and rejoining a known network"
  pkill -f /usr/local/bin/wifi-connect
  pkill -x dnsmasq
  sleep 2
  nmcli con delete "$AP_NAME" >/dev/null 2>&1
  put_back_networks
  nmcli device set wlan0 autoconnect yes
  nmcli --wait 60 device connect wlan0 >/dev/null 2>&1 || log "rejoin not done yet; NetworkManager keeps trying"
  rm -f "$STATE"
}

now=$(date +%s)
if hotspot_up; then
  [ -f "$STATE" ] || echo "$now" > "$STATE"
  since=$(cat "$STATE")
  if ! systemctl is-active --quiet mello-native; then
    leave_hotspot "the Mello app is not running"
  elif [ $((now - since)) -ge "$HOTSPOT_MAX_SECONDS" ]; then
    leave_hotspot "up for $(( (now - since) / 60 )) minutes"
  fi
  exit 0
fi
rm -f "$STATE"

setup_running() {
  systemctl is-active --quiet mello-native && [ -f "$SETUP_MARKER" ] \
    && [ $((now - $(stat -c %Y "$SETUP_MARKER"))) -lt 120 ]
}

# The app turns autoconnect off while its hotspot runs. Left off (app stopped
# halfway), NetworkManager never rejoins by itself.
if ! setup_running && ! pgrep -f /usr/local/bin/wifi-connect >/dev/null \
   && nmcli -g GENERAL.AUTOCONNECT device show wlan0 2>/dev/null | grep -qx no; then
  log "autoconnect was left off with no setup running; turning it back on"
  nmcli device set wlan0 autoconnect yes
  nmcli --wait 60 device connect wlan0 >/dev/null 2>&1 || true
fi

if nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -qx 'wlan0:connected'; then
  back_up_networks
fi
exit 0

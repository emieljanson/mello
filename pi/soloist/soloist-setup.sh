#!/bin/bash
# Spotify Soloist on a Mello: fetch, refresh and switch the speaker to it.
#
#   soloist-setup.sh refresh      nightly (from auto-update.sh): renew the
#                                 binary well before its 90-day expiry
#   soloist-setup.sh activate     owner entered an API key: fetch the binary
#                                 and make Soloist this speaker's Spotify engine
#   soloist-setup.sh deactivate   key removed (factory reset): switch Soloist
#                                 off until the next owner enters theirs
#   soloist-setup.sh status       one line for humans and for the app
#
# Spotify does not allow redistributing the binary, and every owner needs
# their own API key, so neither lives in this repo: the binary is downloaded
# from Spotify, the key is typed in by the owner (see mello/managers/engine_setup.py).
set -euo pipefail

SOLOIST_HOME="${MELLO_SOLOIST_HOME:-$HOME/soloist}"
CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$CODE_DIR/.env"
URL_BASE="${MELLO_SOLOIST_URL_BASE:-https://soloist-builds.spotifycdn.com}"
# Builds stop working 90 days after they were made. Renewing at half that
# leaves a speaker that is offline for weeks plenty of time to catch up.
RENEW_AFTER_DAYS="${MELLO_SOLOIST_RENEW_AFTER_DAYS:-45}"
EXPIRES_AFTER_DAYS=90

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [soloist] $*"; }

engine() {
  local value
  value=$(grep -E '^MELLO_SPOTIFY_ENGINE=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)
  echo "${value:-soloist}"
}

set_engine() {
  touch "$ENV_FILE"
  if grep -qE '^MELLO_SPOTIFY_ENGINE=' "$ENV_FILE"; then
    sed -i "s|^MELLO_SPOTIFY_ENGINE=.*|MELLO_SPOTIFY_ENGINE=$1|" "$ENV_FILE"
  else
    echo "MELLO_SPOTIFY_ENGINE=$1" >> "$ENV_FILE"
  fi
}

has_key() { [ -s "$SOLOIST_HOME/key" ]; }

# Age of a Soloist binary in whole days, from the build time it reports itself.
build_age_days() {
  local binary="$1" built
  built=$("$binary" --version 2>/dev/null | sed -n 's/.* build \([0-9]\{9,\}\) .*/\1/p' | head -1)
  [ -n "$built" ] || return 1
  echo $(( ( $(date +%s) - built ) / 86400 ))
}

download_url() {
  # A 32-bit system on a 64-bit kernel also says aarch64: LONG_BIT tells them apart.
  case "$(uname -m)-$(getconf LONG_BIT)" in
    aarch64-64|arm64-64) echo "$URL_BASE/soloist_release_arm64.tar.gz" ;;
    *) return 1 ;;
  esac
}

# Download the current build, check that it runs and is not itself expired,
# then swap it in. The running daemon keeps its old file until it restarts.
fetch_binary() {
  local tmp status=0
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/soloist-fetch.XXXXXX")
  _fetch_into "$tmp" || status=$?
  rm -rf "$tmp"
  return "$status"
}

_fetch_into() {
  local tmp="$1" url age
  if ! url=$(download_url); then
    log "ERROR: no Soloist build for $(uname -m); this speaker needs a 64-bit system"
    return 1
  fi
  log "Downloading $url"
  if ! curl -fsSL --retry 2 --max-time 300 -o "$tmp/soloist.tar.gz" "$url"; then
    log "ERROR: download failed"
    return 1
  fi
  if ! tar -xzf "$tmp/soloist.tar.gz" -C "$tmp" || [ ! -f "$tmp/soloist" ]; then
    log "ERROR: download is not a Soloist archive"
    return 1
  fi
  chmod 755 "$tmp/soloist"
  if ! age=$(build_age_days "$tmp/soloist"); then
    log "ERROR: downloaded binary does not run on this speaker"
    return 1
  fi
  if [ "$age" -ge "$EXPIRES_AFTER_DAYS" ]; then
    log "ERROR: downloaded build is already $age days old"
    return 1
  fi
  mkdir -p "$SOLOIST_HOME"
  chmod 700 "$SOLOIST_HOME"
  # Rename within the same folder: the running daemon keeps its old file
  # until it restarts, and a power cut never leaves half a binary.
  cp "$tmp/soloist" "$SOLOIST_HOME/soloist.new"
  mv -f "$SOLOIST_HOME/soloist.new" "$SOLOIST_HOME/soloist"
  log "Installed $("$SOLOIST_HOME/soloist" --version 2>/dev/null | head -1) ($age days old)"
}

cmd_refresh() {
  # Only speakers whose owner entered a key use Soloist at all.
  has_key || return 0
  local age=""
  if [ -x "$SOLOIST_HOME/soloist" ] && age=$(build_age_days "$SOLOIST_HOME/soloist") \
     && [ "$age" -lt "$RENEW_AFTER_DAYS" ] && [ "${1:-}" != "--force" ]; then
    return 0
  fi
  log "Renewing Soloist (current build: ${age:-none} days old)"
  fetch_binary || return 1
  if [ "$(engine)" = "soloist" ] && [ "${1:-}" != "--no-restart" ]; then
    sudo systemctl restart mello-soloist
    log "Restarted mello-soloist on the new build"
  fi
}

cmd_activate() {
  if ! has_key; then
    log "ERROR: no API key in $SOLOIST_HOME/key"
    return 2
  fi
  chmod 600 "$SOLOIST_HOME/key"
  local age=""
  if ! { [ -x "$SOLOIST_HOME/soloist" ] && age=$(build_age_days "$SOLOIST_HOME/soloist") \
         && [ "$age" -lt "$RENEW_AFTER_DAYS" ]; }; then
    fetch_binary || return 1
  fi
  if [ ! -f /etc/systemd/system/mello-soloist.service ]; then
    log "ERROR: mello-soloist service is not installed yet (update Mello first)"
    return 3
  fi
  sudo systemctl enable mello-soloist
  sudo systemctl restart mello-soloist
  set_engine soloist
  log "Soloist is now this speaker's Spotify engine"
  sudo systemctl restart mello-native
}

cmd_deactivate() {
  # The engine setting stays soloist: there is no other one any more, and
  # the update gate (pi/update-target.sh) reads it.
  sudo systemctl disable mello-soloist
  sudo systemctl stop mello-soloist
  set_engine soloist
  log "Soloist is off until a new API key is entered"
  sudo systemctl restart mello-native
}

cmd_status() {
  local age="?"
  [ -x "$SOLOIST_HOME/soloist" ] && age=$(build_age_days "$SOLOIST_HOME/soloist" || echo "?")
  echo "engine=$(engine) key=$(has_key && echo yes || echo no) build_age_days=$age expires_after_days=$EXPIRES_AFTER_DAYS"
}

case "${1:-}" in
  refresh)    shift; cmd_refresh "$@" ;;
  activate)   cmd_activate ;;
  deactivate) cmd_deactivate ;;
  status)     cmd_status ;;
  *) echo "usage: $0 refresh [--force|--no-restart] | activate | deactivate | status" >&2; exit 64 ;;
esac

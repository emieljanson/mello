#!/bin/bash
# Starts Spotify Soloist for Mello (see soloist-setup.sh for how it gets here).
#
#   ~/soloist/soloist   binary, downloaded from Spotify (expires after 90 days)
#   ~/soloist/key       the owner's API key, one line, mode 600
#   ~/soloist/data      Spotify login + device identity (kept across updates)
#   ~/soloist/cache     audio cache
SOLOIST_HOME="${MELLO_SOLOIST_HOME:-$HOME/soloist}"
SETUP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/soloist-setup.sh"
cd "$SOLOIST_HOME" || exit 1

if [ ! -s ./key ]; then
  echo "Soloist API key missing in $SOLOIST_HOME" >&2
  exit 1
fi
[ -x ./soloist ] || "$SETUP" refresh --force --no-restart || exit 1

run() {
  # Volume stays at 100: Mello owns the volume through the speaker itself.
  ./soloist \
    --device-name "${MELLO_SOLOIST_NAME:-Mello}" \
    --api-key "$(tr -d '\n' < key)" \
    --data-dir "$SOLOIST_HOME/data" \
    --cache-dir "$SOLOIST_HOME/cache" \
    --cache-size 200 \
    --initial-volume 100 \
    --ws "${MELLO_SOLOIST_WS_ADDR:-127.0.0.1:9090}" \
    ${MELLO_SOLOIST_VERBOSE:+--verbose} &
  child=$!
  trap 'stopping=1; kill -TERM "$child" 2>/dev/null' TERM INT
  wait "$child"
}

stopping=0
run
code=$?
# Being stopped by systemd is not a failure.
[ "$stopping" -eq 1 ] && exit 0
if [ "$code" -eq 10 ]; then
  # Exit code 10 = this build has expired (the nightly renewal did not get
  # to it, e.g. the speaker was off for months). Fetch a new one and go on.
  echo "Soloist build expired, fetching a new one" >&2
  "$SETUP" refresh --force --no-restart && { run; code=$?; }
fi
exit "$code"

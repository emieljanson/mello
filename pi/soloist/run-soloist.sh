#!/bin/bash
# Starts Spotify Soloist for Mello and keeps it logged in
# (see soloist-setup.sh for how the binary and key get here).
#
#   ~/soloist/soloist   binary, downloaded from Spotify (expires after 90 days)
#   ~/soloist/key       the owner's API key, one line, mode 600
#   ~/soloist/data      Spotify login + device identity (kept across updates)
#   ~/soloist/cache     audio cache
SOLOIST_HOME="${MELLO_SOLOIST_HOME:-$HOME/soloist}"
SETUP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/soloist-setup.sh"
# What Soloist must be able to reach to log in.
CHECK_URL="${MELLO_SOLOIST_CHECK_URL:-https://apresolve.spotify.com/}"
BOOT_WAIT="${MELLO_SOLOIST_BOOT_WAIT:-90}"
LOGIN_CHECK_EVERY="${MELLO_SOLOIST_LOGIN_CHECK_EVERY:-30}"
cd "$SOLOIST_HOME" || exit 1

if [ ! -s ./key ]; then
  echo "Soloist API key missing in $SOLOIST_HOME" >&2
  exit 1
fi
[ -x ./soloist ] || "$SETUP" refresh --force --no-restart || exit 1

online() {
  # Any answer counts, also an error page: the point is that Spotify is reachable.
  curl -s -o /dev/null --connect-timeout 4 --max-time 6 "$CHECK_URL"
}

# Someone has paired this speaker before (Soloist keeps the account here).
has_stored_login() {
  [ -n "$(ls -A "$SOLOIST_HOME/data/cache/Users" 2>/dev/null)" ]
}

logged_out() {
  ./soloist ctl status -D "$SOLOIST_HOME/data" 2>/dev/null | grep -q 'logged in: no'
}

# A login that keeps failing while online (a revoked key, say) is not going
# to be fixed by restarting for ever.
MAX_LOGIN_RETRIES=5
login_retries=0
stopping=0
child=""
trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT

# Soloist logs in once, when it starts, and does not try again. After a power
# cut it used to start before WiFi was up (seen on a speaker: "login failed",
# then logged out until someone restarted it). So: wait for the internet first.
waited=0
while [ "$stopping" -eq 0 ] && [ "$waited" -lt "$BOOT_WAIT" ] && ! online; do
  sleep 2
  waited=$((waited + 2))
done
[ "$waited" -gt 0 ] && echo "Waited ${waited}s for the internet before starting Soloist" >&2

while [ "$stopping" -eq 0 ]; do
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

  # And if it still ended up logged out (started offline, WiFi came back
  # later): start it again once the internet is there. Only for a speaker
  # that was paired before; a new one is simply waiting for its owner.
  retry_login=0
  while kill -0 "$child" 2>/dev/null; do
    sleep "$LOGIN_CHECK_EVERY" &
    wait $! 2>/dev/null
    [ "$stopping" -eq 1 ] && break
    kill -0 "$child" 2>/dev/null || break
    if ! logged_out; then
      login_retries=0
    elif [ "$login_retries" -lt "$MAX_LOGIN_RETRIES" ] && has_stored_login && online; then
      login_retries=$((login_retries + 1))
      echo "Soloist is logged out although this speaker was paired: starting it again ($login_retries/$MAX_LOGIN_RETRIES)" >&2
      retry_login=1
      kill -TERM "$child" 2>/dev/null
      break
    fi
  done

  wait "$child"
  code=$?
  child=""
  # Being stopped by systemd is not a failure.
  [ "$stopping" -eq 1 ] && exit 0
  [ "$retry_login" -eq 1 ] && continue

  if [ "$code" -eq 10 ]; then
    # Exit code 10 = this build has expired (the nightly renewal did not get
    # to it, e.g. the speaker was off for months). Fetch a new one and go on.
    echo "Soloist build expired, fetching a new one" >&2
    "$SETUP" refresh --force --no-restart && continue
  fi
  exit "$code"
done
exit 0

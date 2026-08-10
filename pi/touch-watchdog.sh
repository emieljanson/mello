#!/bin/sh
# The evdev node can remain present while Goodix is returning I2C errors. Mello
# cannot observe that state itself, so watch the kernel's authoritative errors
# and recover the controller without rebooting the Pi.
set -eu

SYSTEMCTL="${MELLO_SYSTEMCTL:-/bin/systemctl}"
TOUCH_FIX="${MELLO_TOUCH_FIX:-$(dirname "$0")/touch-fix.sh}"
VCGENCMD="${MELLO_VCGENCMD:-vcgencmd}"
JOURNALCTL="${MELLO_JOURNALCTL:-/bin/journalctl}"
COOLDOWN_SECONDS="${MELLO_TOUCH_RECOVERY_COOLDOWN:-30}"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [touch-watchdog] $*"
}

recover_touch() {
  log "Goodix I2C failure confirmed; waking display and recovering touch"
  power_state=$("$VCGENCMD" get_throttled 2>&1 || true)
  log "Power state: $power_state"

  "$SYSTEMCTL" stop mello-native
  "$TOUCH_FIX"
  "$SYSTEMCTL" start mello-native

  log "Touch recovery complete; Mello restarted on the rebound input device"
}

if [ "${1:-}" = "--recover-once" ]; then
  recover_touch
  exit 0
fi

log "Watching kernel messages for Goodix runtime I2C failures"
error_count=0
error_window_started=0
last_recovery=0

"$JOURNALCTL" -k -f -n 0 -o cat | while IFS= read -r line; do
  case "$line" in
    *Goodix-TS*Error\ reading*0x814e*|*Goodix-TS*Error\ writing*0x814e*)
      now=$(date +%s)
      if [ $((now - last_recovery)) -lt "$COOLDOWN_SECONDS" ]; then
        continue
      fi
      if [ "$error_window_started" -eq 0 ] || [ $((now - error_window_started)) -gt 2 ]; then
        error_window_started=$now
        error_count=1
      else
        error_count=$((error_count + 1))
      fi
      # A single I2C retry can be harmless. A burst proves the controller is
      # wedged; the field failure produces dozens per second.
      if [ "$error_count" -ge 3 ]; then
        recover_touch
        last_recovery=$now
        error_count=0
        error_window_started=0
      fi
      ;;
  esac
done

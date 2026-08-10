#!/bin/sh
# Wake the display and rebind the Goodix controller. This is safe both during
# boot and after a runtime I2C failure.
set -eu

GOODIX_DEVICE="${MELLO_GOODIX_DEVICE:-/sys/bus/i2c/drivers/Goodix-TS/10-005d}"
GOODIX_DEVICE_ID="${GOODIX_DEVICE##*/}"
GOODIX_BIND="${MELLO_GOODIX_BIND:-/sys/bus/i2c/drivers/Goodix-TS/bind}"
GOODIX_UNBIND="${MELLO_GOODIX_UNBIND:-/sys/bus/i2c/drivers/Goodix-TS/unbind}"
BACKLIGHT_POWER="${MELLO_BACKLIGHT_POWER_PATH:-}"
I2C_DEVICE="${MELLO_I2C_DEVICE:-/dev/i2c-10}"
RECOVERY_SLEEP="${MELLO_RECOVERY_SLEEP_SECONDS:-1}"

if [ -z "$BACKLIGHT_POWER" ]; then
  for candidate in /sys/class/backlight/*/bl_power; do
    if [ -e "$candidate" ]; then
      BACKLIGHT_POWER="$candidate"
      break
    fi
  done
fi

# Give immediate visual feedback, even if rebinding fails.
if [ -n "$BACKLIGHT_POWER" ] && [ -w "$BACKLIGHT_POWER" ]; then
  printf '0\n' > "$BACKLIGHT_POWER"
  echo "Display backlight forced on: $BACKLIGHT_POWER"
fi

# Touch Display 2 MCU register 0x03 controls PWM. Restoring full brightness
# before probing prevents Goodix from being rebound while the display MCU is
# still in its dark state.
if [ -e "$I2C_DEVICE" ]; then
  MELLO_I2C_DEVICE="$I2C_DEVICE" python3 - <<'PY' || true
import fcntl
import os

I2C_SLAVE_FORCE = 0x0706
fd = os.open(os.environ['MELLO_I2C_DEVICE'], os.O_RDWR)
try:
    fcntl.ioctl(fd, I2C_SLAVE_FORCE, 0x45)
    os.write(fd, bytes([0x03, 0x80 | 31]))
finally:
    os.close(fd)
PY
fi

if [ ! -w "$GOODIX_BIND" ]; then
  echo "Goodix bind path unavailable: $GOODIX_BIND"
  exit 0
fi

if (printf '%s\n' "$GOODIX_DEVICE_ID" > "$GOODIX_UNBIND") 2>/dev/null; then
  sleep "$RECOVERY_SLEEP"
fi

attempt=1
while [ "$attempt" -le 5 ]; do
  echo "Goodix bind attempt $attempt"
  if printf '%s\n' "$GOODIX_DEVICE_ID" > "$GOODIX_BIND" 2>/dev/null; then
    sleep "$RECOVERY_SLEEP"
    if [ -e "$GOODIX_DEVICE" ]; then
      echo "Goodix touchscreen bound"
      exit 0
    fi
  else
    sleep "$RECOVERY_SLEEP"
  fi
  attempt=$((attempt + 1))
done

echo "Goodix touchscreen bind failed after retries; display remains on"
exit 0

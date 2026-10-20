#!/usr/bin/env bash
# Flash esp32/agribot_esp32 onto the ESP32 attached to the Pi, with checks.
# Run by hand, operator present:   bash esp32/flash.sh [serial-port]
#
# Refuses to flash unless: the port exists and is free, the operator confirms
# servo power is off (or the arm is clear), and esptool identifies a plain ESP32
# (the firmware is built for "ESP32 Dev Module", FQBN esp32:esp32:esp32).
# Afterwards it reads the boot line to confirm the firmware version.
# Flashing resets the board; it never enables the Pi's ESP32 link or arm motion.
set -euo pipefail

SKETCH_DIR="$(cd "$(dirname "$0")/agribot_esp32" && pwd)"
FQBN="esp32:esp32:esp32"
EXPECT_FW_PREFIX="agribot-esp32/"
EXPECT_PROTO=3
PORT="${1:-/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0}"
ARDUINO_CLI="${ARDUINO_CLI:-$HOME/.local/bin/arduino-cli}"
ESPTOOL="${ESPTOOL:-$(ls -d "$HOME"/.arduino15/packages/esp32/tools/esptool_py/*/esptool 2>/dev/null | sort -V | tail -1)}"
PYTHON="${PYTHON:-/opt/agribot/venv/bin/python}"

die() { echo "!! $*" >&2; exit 1; }

[[ -x "$ARDUINO_CLI" ]] || die "arduino-cli not found at $ARDUINO_CLI"
[[ -n "$ESPTOOL" && -x "$ESPTOOL" ]] || die "esptool not found (install the esp32 core: arduino-cli core install esp32:esp32)"
[[ -e "$PORT" ]] || die "serial port $PORT not present (is the ESP32 plugged in?)"
if command -v fuser >/dev/null && fuser "$PORT" >/dev/null 2>&1; then
  die "$PORT is in use. If ESP32_ENABLED=true the AgriBot service holds it: sudo systemctl stop agribot (wheels stop too)"
fi

echo "== Building $SKETCH_DIR for $FQBN"
"$ARDUINO_CLI" compile --fqbn "$FQBN" "$SKETCH_DIR"

cat <<'EOF'

Before flashing (the board resets; servo outputs float during the reset):
  * servo power supply OFF, or the arm mechanically clear of everything
  * nobody touching the arm or the wheels
EOF
read -r -p "Type FLASH to continue: " answer
[[ "$answer" == "FLASH" ]] || die "not confirmed; nothing flashed"

echo "== Identifying the board (puts it into the bootloader briefly)"
chip_log="$(mktemp)"
"$ESPTOOL" --port "$PORT" chip-id 2>&1 | tee "$chip_log"
# esptool 5.x prints "Connected to ESP32 on <port>:" (S2/S3/C3... print ESP32-S3 etc.);
# esptool 4.x printed "Detecting chip type... ESP32".
grep -Eq '^Connected to ESP32 on |Detecting chip type\.\.\. ESP32[[:space:]]*$' "$chip_log" ||
  die "board is not a plain ESP32 (see above); this firmware targets ESP32 Dev Module. Not flashing."

echo "== Uploading"
"$ARDUINO_CLI" upload --fqbn "$FQBN" -p "$PORT" "$SKETCH_DIR"

echo "== Reading the boot message"
"$PYTHON" - "$PORT" "$EXPECT_FW_PREFIX" "$EXPECT_PROTO" <<'PY'
import json, sys, time
import serial
port, prefix, proto = sys.argv[1], sys.argv[2], int(sys.argv[3])
ser = serial.Serial(port, 115200, timeout=0.3)
ser.dtr = False; ser.rts = True; time.sleep(0.1); ser.rts = False  # EN pulse: clean boot
deadline, seen = time.time() + 6, None
while time.time() < deadline and seen is None:
    for raw in ser.read(4096).split(b"\n"):
        raw = raw.strip()
        if raw.startswith(b"{"):
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("ev") == "boot":
                seen = msg
ser.close()
if not seen:
    sys.exit("!! no boot message seen; check the port and baud rate")
print("boot:", seen)
if not str(seen.get("fw", "")).startswith(prefix) or seen.get("proto") != proto:
    sys.exit(f"!! unexpected firmware/protocol (want {prefix}* proto {proto})")
print("OK: AgriBot firmware running. Servos are stopped and disarmed until the Pi arms them.")
PY

#!/usr/bin/env bash
# Live DHT22 diagnosis on GPIO24 with the kernel dht11 driver. NO permanent changes:
# config.txt is not touched, the overlay is removed again, and agribot is ALWAYS
# restarted at the end (also on failure or Ctrl+C).
#
# Run:  sudo bash /opt/agribot/pi/tools/dht22_live_test.sh
#
# Steps
#   1. Safety: wheels must be stopped (0/0) and the arm disabled, else abort.
#   2. POST /stop, then stop agribot.service (it holds GPIO24 for its own DHT reader).
#   3. Wiring probe with GPIO24 free: read the line with the internal pull-DOWN
#      enabled. HIGH means an external pull-up on a powered module is connected
#      to this pin; LOW means nothing powered is pulling it up (wrong pin, no
#      power/GND, or no pull-up resistor).
#   4. Load the overlay live (dtoverlay dht11 gpiopin=24), read the IIO device
#      8 times, 3 s apart (DHT22 needs >= 2 s between reads).
#   5. Always: remove the overlay, start agribot, wait for /health.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
PIN=24
URL=http://127.0.0.1:8000
LOG=/home/rayyanshk/dht22_live_test-$(date +%Y%m%d-%H%M%S).log
exec > >(tee "$LOG") 2>&1
echo "AgriBot DHT22 live test $(date)  (log: $LOG)"

overlay_loaded=0
service_stopped=0
restore() {
  echo; echo "== Restoring original state"
  if [[ $overlay_loaded == 1 ]]; then
    dtoverlay -r dht11 && echo "overlay dht11 removed" || echo "!! could not remove overlay (check: dtoverlay -l)"
  fi
  if [[ $service_stopped == 1 ]]; then
    systemctl start agribot.service
    for _ in $(seq 1 30); do
      curl -s --max-time 2 "$URL/health" >/dev/null && break
      sleep 1
    done
  fi
  echo "agribot: $(systemctl is-active agribot)  $(curl -s --max-time 3 $URL/health)"
  echo "overlays loaded now: $(dtoverlay -l 2>&1 | tr '\n' ' ')"
  echo "config.txt untouched: $(grep -c '^dtoverlay=dht11' /boot/firmware/config.txt) dht11 line(s) in config.txt"
}
trap restore EXIT

echo "== 1. Safety checks"
drive=$(curl -s --max-time 5 "$URL/drive")
arm=$(curl -s --max-time 5 "$URL/arm")
echo "drive: $drive"
python3 - "$drive" "$arm" <<'EOF' || { echo "!! Not safe to continue (wheels moving or arm not disabled). Aborting."; exit 1; }
import json, sys
d = json.loads(sys.argv[1])["data"]
a = json.loads(sys.argv[2])["data"]
print("arm:", {k: a.get(k) for k in ("mode", "status", "operation")})
sys.exit(0 if d["left"] == 0 and d["right"] == 0 and a["mode"] == "disabled" and not a["operation"] else 1)
EOF
curl -s -X POST --max-time 5 "$URL/stop" >/dev/null && echo "stop sent"

echo "== 2. Stopping agribot (frees GPIO$PIN)"
systemctl show agribot -p MainPID -p NRestarts -p ActiveEnterTimestamp
systemctl stop agribot.service && service_stopped=1
sleep 1
gpioinfo -c gpiochip0 | grep -E "line +$PIN:"

echo "== 3. Wiring probe (internal pull-down on, GPIO$PIN as input)"
level=$(gpioget -c gpiochip0 -b pull-down --numeric $PIN 2>&1)
echo "GPIO$PIN with pull-down: $level"
if [[ $level == "1" ]]; then
  echo "=> HIGH: something powered is pulling the data line up (module connected to this pin with power)."
else
  echo "=> LOW: no external pull-up on GPIO$PIN. Check: data wire on PHYSICAL PIN 18, module VCC=3.3V and GND, pull-up resistor."
fi

echo "== 4. Loading kernel driver live"
if dtoverlay dht11 gpiopin=$PIN; then overlay_loaded=1; else echo "!! dtoverlay failed"; exit 1; fi
sleep 2
dmesg | tail -5 | grep -i -E 'dht|iio|overlay' || true
dev=""
for n in /sys/bus/iio/devices/iio:device*/name; do
  [[ -f $n ]] && grep -qE '^dht(11|22)(@|$)' "$n" && dev=$(dirname "$n")
done
[[ -n $dev ]] || { echo "!! no dht IIO device appeared"; exit 1; }
echo "IIO device: $dev ($(cat $dev/name))"

ok=0
for i in $(seq 1 8); do
  sleep 3
  t=$(cat "$dev/in_temp_input" 2>&1); h=$(cat "$dev/in_humidityrelative_input" 2>&1)
  if [[ $t =~ ^-?[0-9]+$ && $h =~ ^[0-9]+$ ]]; then
    tc=$(awk "BEGIN{printf \"%.1f\", $t/1000}"); hp=$(awk "BEGIN{printf \"%.1f\", $h/1000}")
    if awk "BEGIN{exit !($t>=-40000 && $t<=80000 && $h>=0 && $h<=100000)}"; then
      ok=$((ok+1)); echo "  $i: OK   temp=${tc} C  humidity=${hp} %"
    else
      echo "  $i: OUT OF RANGE temp=${tc} humidity=${hp}"
    fi
  else
    echo "  $i: FAIL temp: $t | humidity: $h"
  fi
done
echo "== Result: $ok/8 valid kernel-driver readings"
dmesg | tail -10 | grep -i dht || true
[[ $ok -ge 3 ]] && echo "KERNEL DRIVER WORKS" || echo "KERNEL DRIVER DID NOT GET VALID READINGS"

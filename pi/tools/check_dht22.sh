#!/usr/bin/env bash
# Read-only DHT22 / GPIO24 check (no sudo, no restarts, does not touch the pin).
# Reads through the running agribot service, which owns the pin; a separate
# DHT script would just get "GPIO busy".
PIN=${1:-24}
URL=${AGRIBOT_URL:-http://127.0.0.1:8000}

echo "== GPIO$PIN owner (expected: none, or \"lg\" = AgriBot service)"
gpioinfo -c gpiochip0 2>/dev/null | grep -E "line +$PIN:" || gpioinfo gpiochip0 | grep -E "line +$PIN:"

echo "== LCD drivers (expected: nothing)"
lsmod | grep -E 'fb_ili9486|ads7846|fbtft' || echo "none loaded"

echo "== LCD overlays active in config.txt (expected: nothing)"
grep -nE '^dtoverlay=(waveshare35a|mpi3501-custom)' /boot/firmware/config.txt || echo "none active"

echo "== Service"
systemctl is-active agribot
curl -s --max-time 3 "$URL/health"; echo

echo "== DHT22 readings from the service (5 samples, ~6 s apart)"
ok=0
for i in 1 2 3 4 5; do
  line=$(curl -s --max-time 15 "$URL/sensors/dht22" | python3 -c '
import json,sys
try:
    r=json.load(sys.stdin)["data"]["reading"]
except Exception as e:
    print("ERROR no response:", e); sys.exit()
print(r.get("status"), "temp=%s C" % r.get("temperatureC"), "hum=%s %%" % r.get("humidity"), "at", r.get("at"), "|", r.get("source"))')
  echo "  $i: $line"
  [[ $line == ok* ]] && ok=$((ok+1))
  [[ $i -lt 5 ]] && sleep 6
done
echo "== Result: $ok/5 fresh hardware readings"
if [[ $ok -ge 3 ]]; then echo "DHT22 WORKING"; else echo "DHT22 NOT VERIFIED (see journalctl -u agribot -b | grep -i dht)"; exit 1; fi

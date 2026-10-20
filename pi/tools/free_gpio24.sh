#!/usr/bin/env bash
# Frees GPIO24 for the DHT22 by disabling the leftover 3.5" SPI LCD config.
# Run with: sudo bash /opt/agribot/pi/tools/free_gpio24.sh   (does NOT reboot)
# See docs/COMMISSIONING.md section 2. Idempotent: lines already commented are left alone.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
CFG=/boot/firmware/config.txt
BACKUP=/boot/firmware/config.txt.bak-lcd-$STAMP

# 1. Backups (config.txt kept on the boot partition and in the home folder).
cp -a "$CFG" "$BACKUP"
cp -a "$CFG" "/home/rayyanshk/config.txt.bak-lcd-$STAMP"
cp -a /etc/rc.local "/etc/rc.local.bak-lcd-$STAMP"
echo "Backups: $BACKUP  /home/rayyanshk/config.txt.bak-lcd-$STAMP  /etc/rc.local.bak-lcd-$STAMP"

# 2. Disable only the two LCD overlays that claim GPIO24 (exact whole-line matches).
sed -i -E 's/^(dtoverlay=waveshare35a)$/# Disabled \1 (no LCD fitted; it claimed GPIO24 needed by the DHT22)/' "$CFG"
sed -i -E 's/^(dtoverlay=mpi3501-custom)$/# Disabled \1 (no LCD fitted; it claimed GPIO24 needed by the DHT22)/' "$CFG"

# 3. LCD-only helpers in rc.local (framebuffer copy / console mapping to the LCD).
sed -i -E 's/^(fbcp &)$/# \1  # disabled: no LCD fitted/; s/^(con2fbmap 1 0)$/# \1  # disabled: no LCD fitted/' /etc/rc.local

# 4. LCD touchscreen bridge service (reads only the ADS7846 LCD touch device).
systemctl disable tft-touch-bridge.service || true

echo; echo "== config.txt changes"; diff "$BACKUP" "$CFG" || true
echo; echo "== rc.local changes"; diff "/etc/rc.local.bak-lcd-$STAMP" /etc/rc.local || true
echo; echo "== AgriBot DHT settings (unchanged)"; grep -E '^(DHT_PIN|DHT_BACKEND)=' /etc/agribot.env || echo "DHT_PIN not set: code default is 24"
echo; echo "Done. Nothing rebooted. GPIO24 is released only after: sudo reboot"

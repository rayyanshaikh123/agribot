#!/usr/bin/env bash
# Installs the AgriBot Pi service. Run on the Pi from this folder:  sudo ./install.sh
set -euo pipefail

if [[ $(uname -s) != Linux ]] || ! command -v raspi-config >/dev/null || ! command -v apt-get >/dev/null; then
  echo "Install on a Raspberry Pi running Raspberry Pi OS Bookworm or newer (64-bit)."
  exit 1
fi
if [[ $(dpkg --print-architecture) != arm64 ]]; then
  echo "The ONNX runtime requires 64-bit Raspberry Pi OS (arm64). Reimage the Pi with the 64-bit OS."
  exit 1
fi
if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo ./install.sh"; exit 1; fi

PI_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$PI_DIR")"

echo "==> Installing system packages"
apt-get update
apt-get install -y python3-venv python3-numpy python3-opencv python3-gpiozero python3-spidev \
  python3-picamera2 python3-dbus python3-gi python3-lgpio python3-evdev bluez network-manager

echo "==> Linking checkout to /opt/agribot"
mkdir -p /opt/agribot /var/lib/agribot
ln -sfn "$PI_DIR" /opt/agribot/pi
ln -sfn "$REPO_DIR/ai" /opt/agribot/ai

echo "==> Creating virtualenv (with system packages for picamera2/opencv/dbus)"
python3 -m venv --system-site-packages /opt/agribot/venv
/opt/agribot/venv/bin/pip install --upgrade pip
/opt/agribot/venv/bin/pip install -r "$PI_DIR/requirements.txt"

if [[ ! -f /etc/agribot.env ]]; then
  if [[ -f "$PI_DIR/agribot.env" ]]; then cp "$PI_DIR/agribot.env" /etc/agribot.env
  else cp "$PI_DIR/agribot.env.example" /etc/agribot.env; fi
  chmod 600 /etc/agribot.env
  echo "==> Created /etc/agribot.env - edit ROBOT_INGEST_KEY before relying on it"
fi
# The checkout's relative MODEL_PATH resolves from /opt/agribot/pi.
sed -i 's#^MODEL_PATH=\.\./ai/#MODEL_PATH=/opt/agribot/ai/#' /etc/agribot.env
if grep -q '^ROBOT_INGEST_KEY=replace-with' /etc/agribot.env; then
  echo "!! Set ROBOT_INGEST_KEY in /etc/agribot.env (sudo nano /etc/agribot.env), then: sudo systemctl restart agribot"
fi

if [[ ! -f "$REPO_DIR/ai/models/weights/best.onnx" ]]; then
  echo "!! Model missing: copy best.onnx to $REPO_DIR/ai/models/weights/ (vision stays disabled until then)"
fi

echo "==> Enabling SPI and the service"
raspi-config nonint do_spi 0 || true
install -m 644 "$PI_DIR/agribot.service" /etc/systemd/system/agribot.service
systemctl disable --now agribot-sensors.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable agribot.service
systemctl restart agribot.service
systemctl --no-pager --lines=10 status agribot.service
echo "==> Done. Logs: journalctl -u agribot -f"

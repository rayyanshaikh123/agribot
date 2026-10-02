#!/usr/bin/env python3
"""AgriBot Raspberry Pi service.

One lightweight process that:
  * reads the field sensors and publishes them to the backend,
  * runs the tomato-leaf disease model on camera captures,
  * polls the backend for app commands (scan now, refresh readings),
  * drives the motors from a PS5 controller or the app (local HTTP),
  * advertises a Bluetooth LE service so the app can share Wi-Fi,
  * exposes a small local HTTP API for diagnostics.

Every subsystem is optional: missing hardware is reported, not fatal.
"""

import json
import queue
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agribot.backend import BackendClient, BackendError
from agribot.ble import BleProvisioner
from agribot.config import load_config
from agribot.drive import Drive, arcade
from agribot.network import WifiManager
from agribot.sensors import SensorHub
from agribot.state import StateStore
from agribot.vision import Camera, Detector, decode_image, encode_jpeg, jpeg_data_uri, summarize

VERSION = "2.1.0"


class AgriBot:
    def __init__(self, config):
        self.cfg = config
        self.state = StateStore(config.state_file)
        self.sensors = SensorHub(config)
        self.detector = Detector(config)
        self.camera = Camera(config)
        self.drive = Drive(config)
        self.wifi = WifiManager(config.wifi_interface)
        self.backend = BackendClient(self.backend_url, config.robot_key)
        self.ble = BleProvisioner(config, self.wifi, self.ble_status, self.set_backend_url)
        self.stop = threading.Event()
        self.jobs = queue.Queue()
        self.latest_scan = None
        self.latest_jpeg = None
        self.backend_ok_at = 0
        self.backend_error = None
        self.internet = None
        self._internet_checked = 0
        self._publish_now = threading.Event()
        self._frame = (0.0, None)
        self._frame_lock = threading.Lock()

    # --- configuration ------------------------------------------------------
    def backend_url(self):
        return self.state.get("backendUrl") or self.cfg.backend_url

    def set_backend_url(self, url):
        if url != self.backend_url():
            print(f"[service] Backend URL set from phone: {url}")
        self.state.set("backendUrl", url)

    # --- status -------------------------------------------------------------
    def capabilities(self):
        return {
            "camera": self.camera.kind,
            "model": self.detector.ready,
            "sensors": self.sensors.mode,
            "ble": self.ble.running,
            "drive": self.drive.ready,
            "gamepad": self.drive.gamepad,
        }

    def status(self):
        wifi = self.wifi.status()
        return {
            "version": VERSION,
            "capabilities": self.capabilities(),
            "errors": {k: v for k, v in {"camera": self.camera.error, "model": self.detector.error, "sensors": self.sensors.error, "ble": self.ble.error, "drive": self.drive.error}.items() if v},
            "network": {**wifi, "internet": self.internet},
            "backend": {"url": self.backend_url(), "connected": time.time() - self.backend_ok_at < 15, "error": self.backend_error},
            "lastScan": self.latest_scan and {k: self.latest_scan[k] for k in ("summary", "at", "inferenceMs")},
        }

    def ble_status(self):
        wifi = self.wifi.status()
        return {
            "wifi": {"state": wifi["state"], "ssid": wifi["ssid"], "ip": wifi["ip"], "error": wifi["error"], "target": wifi["target"]},
            "internet": self.internet,
            "backend": {"url": self.backend_url(), "ok": time.time() - self.backend_ok_at < 15},
            "camera": self.camera.ready,
            "model": self.detector.ready,
        }

    # --- actions ------------------------------------------------------------
    def publish_reading(self):
        reading = self.sensors.read_all()
        payload = {
            "sensors": reading["sensors"],
            "robot": {"status": "online"},
            "location": {"latitude": self.cfg.latitude, "longitude": self.cfg.longitude},
        }
        self.backend.request("POST", "/api/robot/data", payload, timeout=30)
        return reading

    def scan(self, image=None, source="camera", publish=True):
        if image is None:
            image = self.camera.capture()
        detections, elapsed = self.detector.detect(image)
        annotated = self.detector.annotate(image, detections)
        jpeg = encode_jpeg(annotated)
        result = {
            "summary": summarize(detections),
            "detections": detections,
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "inferenceMs": elapsed,
            "source": source,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.latest_scan, self.latest_jpeg = result, jpeg
        print(f"[vision] {result['summary']['label']} ({len(detections)} detections, {elapsed} ms)")
        if publish and self.backend.configured:
            self.backend.request("POST", "/api/robot/detections", {**result, "image": jpeg_data_uri(jpeg)}, timeout=60)
        return result

    def live_frame(self):
        """Small JPEG for the app's drive view, shared between viewers (~12 fps max)."""
        with self._frame_lock:
            at, jpeg = self._frame
            if jpeg is None or time.monotonic() - at > 0.08:
                jpeg = encode_jpeg(self.camera.capture(), max_side=640, quality=60)
                self._frame = (time.monotonic(), jpeg)
            return jpeg

    def run_command(self, command):
        kind, command_id = command.get("type"), command.get("id")
        ok, message = True, None
        try:
            if kind == "scan":
                summary = self.scan()["summary"]
                message = summary["label"]
            elif kind == "publish":
                self.publish_reading()
                message = "Readings updated"
            else:
                raise ValueError(f"Unknown command '{kind}'")
        except Exception as error:
            ok, message = False, str(error)
            print(f"[command] {kind} failed: {error}")
        if command_id:
            try:
                self.backend.request("POST", f"/api/robot/commands/{command_id}/result", {"ok": ok, "message": message})
            except BackendError as error:
                print(f"[command] Could not report result: {error}")

    # --- background loops ---------------------------------------------------
    def _worker(self):
        while not self.stop.is_set():
            try:
                command = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            self.run_command(command)

    def _heartbeat(self):
        while not self.stop.is_set():
            if self.backend.configured:
                try:
                    response = self.backend.request("POST", "/api/robot/heartbeat", self.status(), timeout=15)
                    if not self.backend_ok_at:
                        print(f"[service] Connected to backend {self.backend_url()}")
                    self.backend_ok_at, self.backend_error = time.time(), None
                    for command in response.get("commands", []):
                        self.jobs.put(command)
                except BackendError as error:
                    if self.backend_error != str(error):
                        print(f"[service] {error}")
                    self.backend_error = str(error)
            if time.time() - self._internet_checked > 15:
                self.internet = self.wifi.has_internet()
                self._internet_checked = time.time()
            self.stop.wait(self.cfg.heartbeat_interval)

    def _publisher(self):
        while not self.stop.is_set():
            if self.backend.configured:
                try:
                    self.publish_reading()
                except Exception as error:
                    print(f"[sensors] Publish failed, retrying later: {error}")
            self.stop.wait(self.cfg.publish_interval)

    def _auto_scanner(self):
        while not self.stop.wait(self.cfg.auto_scan_interval):
            if self.camera.ready and self.detector.ready:
                self.jobs.put({"type": "scan"})

    def start(self):
        self.ble.start()
        self.drive.start(self.stop)
        loops = [self._worker, self._heartbeat, self._publisher]
        if self.cfg.auto_scan_interval > 0:
            loops.append(self._auto_scanner)
        for loop in loops:
            threading.Thread(target=loop, name=loop.__name__, daemon=True).start()

    def close(self):
        self.stop.set()
        self.drive.close()
        self.camera.close()
        self.sensors.close()


def make_handler(bot):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AgriBotPi/{VERSION}"

        def log_message(self, fmt, *args):
            print(f"[http] {self.client_address[0]} {fmt % args}")

        def _send(self, status, payload, content_type="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            get, post = self.command == "GET", self.command == "POST"
            if get and path == "/health":
                return self._send(200, {"success": True, "service": "agribot-pi", "version": VERSION})
            if get and path == "/status":
                return self._send(200, {"success": True, "data": bot.status()})
            if get and path == "/sensors":
                return self._send(200, {"success": True, "data": bot.sensors.read_all()})
            if get and path.startswith("/sensors/"):
                return self._send(200, {"success": True, "data": bot.sensors.read_one(path.rsplit("/", 1)[-1])})
            if post and path == "/sensors/publish":
                return self._send(200, {"success": True, "data": bot.publish_reading()})
            if post and path == "/vision/scan":
                return self._send(200, {"success": True, "data": bot.scan()})
            if post and path == "/vision/detect":
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 15_000_000:
                    raise ValueError("POST a JPEG or PNG image body (max 15 MB).")
                publish = "publish=1" in self.path
                return self._send(200, {"success": True, "data": bot.scan(decode_image(self.rfile.read(length)), source="upload", publish=publish)})
            if get and path == "/vision/latest":
                return self._send(200, {"success": True, "data": bot.latest_scan})
            if get and path == "/vision/latest.jpg":
                if not bot.latest_jpeg:
                    return self._send(404, {"success": False, "message": "No scan yet."})
                return self._send(200, bot.latest_jpeg, "image/jpeg")
            if get and path == "/camera/frame.jpg":
                return self._send(200, bot.live_frame(), "image/jpeg")
            if get and path == "/drive":
                return self._send(200, {"success": True, "data": bot.drive.state()})
            if post and path == "/drive":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if 0 < length <= 1024 else {}
                if "throttle" in body or "turn" in body:
                    left, right = arcade(float(body.get("throttle", 0)), float(body.get("turn", 0)))
                else:
                    left, right = float(body.get("left", 0)), float(body.get("right", 0))
                return self._send(200, {"success": True, "data": bot.drive.set(left, right)})
            if post and path == "/drive/stop":
                bot.drive.stop()
                return self._send(200, {"success": True, "data": bot.drive.state()})
            if get and path == "/network":
                return self._send(200, {"success": True, "data": {**bot.wifi.status(), "networks": bot.wifi.scan()}})
            return self._send(404, {"success": False, "message": "Endpoint not found."})

        def _handle(self):
            try:
                self._route()
            except KeyError as error:
                self._send(404, {"success": False, "message": str(error).strip("'")})
            except (RuntimeError, ValueError, BackendError) as error:
                self._send(503, {"success": False, "message": str(error)})
            except Exception as error:
                print(f"[http] Unexpected error: {error!r}")
                self._send(500, {"success": False, "message": "Unexpected error."})

        do_GET = do_POST = _handle

    return Handler


def main():
    config = load_config()
    bot = AgriBot(config)
    bot.start()
    server = ThreadingHTTPServer((config.host, config.port), make_handler(bot))
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"[service] AgriBot Pi {VERSION} on http://{config.host}:{config.port} | capabilities: {bot.capabilities()}")
    if not bot.backend.configured:
        print("[service] Backend not configured yet: set AGRIBOT_BACKEND_URL or share the network from the app.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[service] Stopping...")
        bot.close()
        server.server_close()


if __name__ == "__main__":
    main()

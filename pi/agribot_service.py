#!/usr/bin/env python3
"""AgriBot Raspberry Pi service.

One lightweight process that:
  * reads the field sensors and publishes them to the backend,
  * runs the tomato-leaf disease model on camera captures,
  * polls the backend for app commands (scan now, refresh readings),
  * drives the motors from a PS5 controller or the app (local HTTP),
  * talks to the ESP32 (pH probe ADC + probe arm) over USB serial,
  * advertises a Bluetooth LE service so the app can share Wi-Fi,
  * exposes a small local HTTP API for diagnostics and commissioning.

Every subsystem is optional: missing hardware is reported, not fatal. Nothing
moves at startup: the wheels only follow the app/controller, and the arm only
moves on an explicit, authorised request in the ARM_MODE that allows it.
"""

import hmac
import json
import queue
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agribot.arm import Arm, ArmError
from agribot.backend import BackendClient, BackendError
from agribot.ble import BleProvisioner
from agribot.config import CONFIG_WARNINGS, load_config
from agribot.demo import DEMO_ROBOT_STATUS
from agribot.drive import Drive, DriveBlocked, arcade
from agribot.esp32 import Esp32Error, Esp32Link
from agribot.network import WifiManager
from agribot.ph import PhProbe
from agribot.sensors import SensorHub
from agribot.state import StateStore
from agribot.vision import Camera, Detector, decode_image, encode_jpeg, jpeg_data_uri, summarize

VERSION = "2.2.0"
MAX_JSON_BODY = 4096


class BadRequest(ValueError):
    pass


class Forbidden(PermissionError):
    pass


class AgriBot:
    def __init__(self, config):
        self.cfg = config
        self.state = StateStore(config.state_file)
        self.esp32 = Esp32Link(config, on_event=self._esp32_event, on_connect=self._esp32_connected)
        self.ph = PhProbe(config, self.esp32)
        self.drive = Drive(config)
        self.arm = Arm(config, self.esp32, self.ph, is_driving=lambda: self.drive.moving)
        self.drive.block_reason = self.arm.drive_block_reason
        self.sensors = SensorHub(config, ph=self.ph, link=self.esp32)
        self.detector = Detector(config)
        self.camera = Camera(config)
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
        self.publish_state = {"ok": None, "at": None, "blockedBy": [], "labels": None, "error": None}

    # --- ESP32 callbacks ------------------------------------------------------
    def _esp32_connected(self):
        self.arm.on_connect()

    def _esp32_event(self, event):
        self.arm.on_event(event)
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
            "esp32": self.esp32.connected,
            "arm": self.arm.summary(),
        }

    def status(self):
        wifi = self.wifi.status()
        errors = {
            "camera": self.camera.error, "model": self.detector.error, "sensors": self.sensors.error,
            "ble": self.ble.error, "drive": self.drive.error,
            "esp32": self.esp32.error if self.esp32.enabled else None,
            "ph": self.ph.error, "arm": self.arm.config_error,
        }
        return {
            "version": VERSION,
            "capabilities": self.capabilities(),
            "errors": {k: v for k, v in errors.items() if v},
            "network": {**wifi, "internet": self.internet},
            "backend": {"url": self.backend_url(), "connected": time.time() - self.backend_ok_at < 15, "error": self.backend_error},
            "lastScan": self.latest_scan and {k: self.latest_scan[k] for k in ("summary", "at", "inferenceMs")},
            "sensorStatus": self.sensors.read_all()["status"],
            "demoMode": self.sensors.demo is not None,
            "publish": self.publish_state,
            # Volatile counters stay out of the heartbeat so the backend only
            # re-broadcasts robot:status when something meaningful changes.
            "esp32": {k: v for k, v in self.esp32.state().items() if k not in ("lastMessageAgeSeconds", "stats")},
            "arm": self.arm.state(include_firmware=False),
            "configWarnings": list(CONFIG_WARNINGS),
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
        """Send the current readings to the backend, unless a required value is not real.

        Raises RuntimeError (with the reasons) instead of inventing numbers."""
        sensors, labels, blockers = self.sensors.publish_payload()
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if blockers:
            self.publish_state = {"ok": False, "at": at, "blockedBy": blockers, "labels": labels, "error": None}
            raise RuntimeError("Readings not published: " + "; ".join(blockers))
        demo = "simulated" in labels.values()
        payload = {
            "sensors": sensors,
            # The backend stores this string with every reading: demo records stay identifiable.
            "robot": {"status": DEMO_ROBOT_STATUS if demo else "online"},
            "location": {"latitude": self.cfg.latitude, "longitude": self.cfg.longitude},
        }
        try:
            self.backend.request("POST", "/api/robot/data", payload, timeout=30)
        except BackendError as error:
            self.publish_state = {"ok": False, "at": at, "blockedBy": [], "labels": labels, "error": str(error)}
            raise
        self.publish_state = {"ok": True, "at": at, "blockedBy": [], "labels": labels, "error": None}
        return self.sensors.read_all()

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
        last_message = None
        while not self.stop.is_set():
            if self.backend.configured:
                try:
                    self.publish_reading()
                    message = None
                except Exception as error:
                    message = str(error)
                if message != last_message:  # log changes only, not every cycle
                    print(f"[sensors] {message}; retrying every {self.cfg.publish_interval} s" if message else "[sensors] Publishing readings to the backend")
                    last_message = message
            self.stop.wait(self.cfg.publish_interval)

    def _auto_scanner(self):
        while not self.stop.wait(self.cfg.auto_scan_interval):
            if self.camera.ready and self.detector.ready:
                self.jobs.put({"type": "scan"})

    def emergency_stop(self, reason):
        """Stop every actuator. Safe to call from any thread, any number of times."""
        for action in (self.drive.stop, lambda: self.arm.stop(reason)):
            try:
                action()
            except Exception as error:
                print(f"[service] Stop error: {error!r}")

    def start(self):
        self.sensors.start(self.stop)
        self.esp32.start(self.stop)
        self.ble.start()
        self.drive.start(self.stop)
        loops = [self._worker, self._heartbeat, self._publisher]
        if self.cfg.auto_scan_interval > 0:
            loops.append(self._auto_scanner)
        for loop in loops:
            threading.Thread(target=loop, name=loop.__name__, daemon=True).start()

    def close(self):
        self.emergency_stop("service stopping")
        self.stop.set()
        for close in (self.drive.close, self.esp32.close, self.camera.close, self.sensors.close):
            try:
                close()
            except Exception as error:
                print(f"[service] Shutdown error: {error!r}")


def make_handler(bot):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AgriBotPi/{VERSION}"

        def log_message(self, fmt, *args):
            print(f"[http] {self.client_address[0]} {fmt % args}")

        def _body(self):
            """JSON object body ({} when empty). Raises BadRequest."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest("Invalid Content-Length.") from None
            if length <= 0:
                return {}
            if length > MAX_JSON_BODY:
                raise BadRequest("Request body too large.")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise BadRequest("Body must be JSON.") from None
            if not isinstance(body, dict):
                raise BadRequest("Body must be a JSON object.")
            return body

        def _is_loopback(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _authorize_motion(self):
            """Arm motion: loopback clients, or the AGRIBOT_LOCAL_TOKEN header."""
            if self._is_loopback():
                return
            token = bot.cfg.local_api_token
            given = self.headers.get("X-AgriBot-Token", "")
            if token and hmac.compare_digest(given.encode(), token.encode()):
                return
            raise Forbidden("Arm motion is only accepted from the Pi itself or with X-AgriBot-Token (AGRIBOT_LOCAL_TOKEN).")

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
            if post and path == "/stop":
                bot.emergency_stop("HTTP /stop")
                return self._send(200, {"success": True, "data": {"drive": bot.drive.state(), "arm": bot.arm.state(include_firmware=False)}})
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
                body = self._body()
                try:
                    if "throttle" in body or "turn" in body:
                        left, right = arcade(body.get("throttle", 0), body.get("turn", 0))
                    else:
                        left, right = body.get("left", 0), body.get("right", 0)
                    return self._send(200, {"success": True, "data": bot.drive.set(left, right)})
                except (TypeError, ValueError) as error:
                    bot.drive.stop()  # malformed input never leaves the wheels running
                    raise BadRequest(f"Invalid drive command: {error}") from None
            if post and path == "/drive/stop":
                bot.drive.stop()
                return self._send(200, {"success": True, "data": bot.drive.state()})
            if get and path == "/network":
                return self._send(200, {"success": True, "data": {**bot.wifi.status(), "networks": bot.wifi.scan()}})
            # --- ESP32, pH probe and arm ---
            if get and path == "/esp32":
                return self._send(200, {"success": True, "data": bot.esp32.state()})
            if get and path == "/ph":
                return self._send(200, {"success": True, "data": {**bot.ph.latest(), "calibrated": bot.ph.calibrated, "source": bot.cfg.ph_source}})
            if post and path == "/ph/read":  # voltage now, no motion
                return self._send(200, {"success": True, "data": bot.ph.read_live()})
            if post and path == "/ph/sample":  # probe placed by hand: sample and store, no motion
                if bot.arm.operation:
                    raise ArmError(f"Arm busy ({bot.arm.operation})")
                result = bot.ph.measure(bot.cfg.ph_samples, interval=bot.cfg.ph_sample_interval, method="manual")
                return self._send(200, {"success": True, "data": result})
            if get and path == "/arm":
                return self._send(200, {"success": True, "data": bot.arm.state()})
            if post and path == "/arm/stop":
                return self._send(200, {"success": True, "data": bot.arm.stop("HTTP /arm/stop")})
            if post and path in ("/arm/jog", "/arm/nudge", "/arm/confirm-raised", "/arm/neutral-test", "/arm/home", "/arm/retract", "/arm/insert", "/ph/measure"):
                self._authorize_motion()
                if path in ("/arm/nudge", "/arm/confirm-raised", "/arm/retract") and not self._is_loopback() and not bot.cfg.arm_app_control:
                    raise Forbidden("Arm control from the app is off (ARM_APP_CONTROL=false).")
                if path == "/arm/retract" and not self._is_loopback() and not bot.cfg.arm_commissioned:
                    raise ArmError("Retract from the app needs a commissioned arm (ARM_COMMISSIONED=true); use agribotctl.py on the Pi during commissioning.")
                body = self._body()
                if path == "/arm/confirm-raised":
                    source = "pi" if self._is_loopback() else f"app {self.client_address[0]}"
                    data = bot.arm.confirm_raised(body.get("confirm"), source=source)
                elif path == "/arm/nudge":
                    data = bot.arm.nudge(body.get("direction"))
                elif path == "/arm/jog":
                    data = bot.arm.jog(body.get("axis"), body.get("speed"), body.get("ms"))
                elif path == "/arm/neutral-test":
                    data = bot.arm.neutral_test(body.get("axis"), body.get("value"), body.get("ms"))
                elif path == "/arm/home":
                    data = bot.arm.home(body.get("axes", [1, 2]))
                elif path == "/arm/retract":
                    data = bot.arm.retract()
                elif path == "/arm/insert":
                    data = bot.arm.insert()
                else:
                    data = bot.arm.measure_ph()
                return self._send(202 if data.get("accepted") else 200, {"success": True, "data": data})
            return self._send(404, {"success": False, "message": "Endpoint not found."})

        def _handle(self):
            try:
                self._route()
            except BadRequest as error:
                self._send(400, {"success": False, "message": str(error)})
            except Forbidden as error:
                self._send(403, {"success": False, "message": str(error)})
            except (DriveBlocked, ArmError) as error:
                self._send(409, {"success": False, "message": str(error)})
            except KeyError as error:
                self._send(404, {"success": False, "message": str(error).strip("'")})
            except (RuntimeError, ValueError, BackendError, Esp32Error) as error:
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

    def on_signal(signum, _frame):
        bot.emergency_stop(f"signal {signum}")  # actuators first, then the slower shutdown
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, on_signal)
    print(f"[service] AgriBot Pi {VERSION} on http://{config.host}:{config.port} | capabilities: {bot.capabilities()}")
    print(f"[service] ESP32 link {'on' if config.esp32_enabled else 'off'}, pH source {config.ph_source}, arm mode {bot.arm.mode}, soil sensor {config.soil_sensor}")
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

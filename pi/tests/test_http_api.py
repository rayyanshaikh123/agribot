"""Local HTTP API: existing response shapes (used by the app) and new endpoints.

Runs the real request handler on an ephemeral loopback port with mocked
hardware. Nothing here touches GPIO, serial ports or the camera.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import agribot_service
from agribot import arm as arm_module
from agribot.arm import Arm
from agribot.drive import Drive
from agribot.ph import PhProbe

from fakes import FakeLink, make_config
from test_drive_backend_vision import FakeMotor

ENV = {"MODEL_PATH": "/nonexistent.onnx", "ESP32_ENABLED": "true", "ARM_MODE": "manual", "PH_CALIBRATION": "7:1500,4:1990"}


def build_bot(env=None):
    cfg = make_config({**ENV, **(env or {})})
    with mock.patch.object(agribot_service, "WifiManager") as wifi:
        wifi.return_value.status.return_value = {"state": "connected", "ssid": "test", "ip": "127.0.0.1", "error": None, "target": None}
        wifi.return_value.scan.return_value = []
        bot = agribot_service.AgriBot(cfg)
    link = FakeLink()
    bot.esp32 = link
    bot.ph = PhProbe(cfg, link)
    bot.drive = Drive(cfg, motors=(FakeMotor(), FakeMotor()))
    bot.arm = Arm(cfg, link, bot.ph, is_driving=lambda: bot.drive.moving)
    bot.drive.block_reason = bot.arm.drive_block_reason
    bot.sensors.ph, bot.sensors.link = bot.ph, link
    return bot


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patch = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        cls.patch.start()
        cls.bot = build_bot()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agribot_service.make_handler(cls.bot))
        cls.server.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.patch.stop()

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers.get("Content-Type")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers.get("Content-Type")

    def json_call(self, *args, **kwargs):
        status, body, _ = self.call(*args, **kwargs)
        return status, json.loads(body)

    def test_health_and_status_shape(self):
        status, body = self.json_call("GET", "/health")
        self.assertEqual((status, body["service"], body["success"]), (200, "agribot-pi", True))
        status, body = self.json_call("GET", "/status")
        data = body["data"]
        for key in ("version", "capabilities", "errors", "network", "backend", "lastScan"):
            self.assertIn(key, data)
        caps = data["capabilities"]
        self.assertIsInstance(caps["sensors"], str)  # app reads it as a String
        self.assertIsInstance(caps["model"], bool)
        self.assertIn(caps["camera"], (None, "picamera", "usb"))

    def test_sensors_shape(self):
        status, body = self.json_call("GET", "/sensors")
        self.assertEqual(status, 200)
        sensors = body["data"]["sensors"]
        self.assertEqual(set(sensors), {"soilMoisture", "temperature", "humidity", "rainfall", "ph"})
        for value in sensors.values():
            self.assertTrue(value is None or isinstance(value, (int, float)))
        self.assertIn("sources", body["data"])
        status, body = self.json_call("GET", "/sensors/dht22")
        self.assertEqual(body["data"]["sensor"], "dht22")
        status, body = self.json_call("GET", "/sensors/bogus")
        self.assertEqual(status, 404)

    def test_publish_refuses_fake_values(self):
        self.bot.backend._key = "k"
        self.bot.backend._url_provider = lambda: "http://127.0.0.1:9"
        status, body = self.json_call("POST", "/sensors/publish")
        self.assertEqual(status, 503)
        self.assertIn("not published", body["message"])

    def test_drive_contract(self):
        self.bot.drive._stopped_at = 0  # clear the stop latch left by other tests
        status, body = self.json_call("POST", "/drive", {"throttle": 0.5, "turn": 0})
        self.assertEqual(status, 200)
        for key in ("ready", "left", "right", "source", "gamepad", "error"):
            self.assertIn(key, body["data"])
        self.assertEqual(body["data"]["source"], "app")
        status, body = self.json_call("POST", "/drive/stop", {})
        self.assertEqual((status, body["data"]["left"]), (200, 0))
        status, body = self.json_call("GET", "/drive")
        self.assertEqual(status, 200)

    def test_drive_rejects_bad_input_and_stops(self):
        self.bot.drive._stopped_at = 0
        self.json_call("POST", "/drive", {"left": 0.3, "right": 0.3})
        for raw in (b'{"throttle": NaN}', b'{"throttle": "x"}', b"not json", b"[1]"):
            status, _ = self.json_call("POST", "/drive", raw=raw)
            self.assertEqual(status, 400, raw)
            self.assertFalse(self.bot.drive.moving)

    def test_global_stop(self):
        status, body = self.json_call("POST", "/stop")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["drive"]["left"], 0)

    def test_esp32_ph_arm_endpoints(self):
        self.assertEqual(self.json_call("GET", "/esp32")[0], 200)
        status, body = self.json_call("POST", "/ph/read")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["mv"], 1500.0)
        status, body = self.json_call("GET", "/ph")
        self.assertIsNone(body["data"]["value"])
        status, body = self.json_call("GET", "/arm")
        self.assertEqual(body["data"]["mode"], "manual")
        status, body = self.json_call("POST", "/arm/jog", {"axis": 1, "speed": 20, "ms": 100})
        self.assertEqual(status, 200, body)
        status, body = self.json_call("POST", "/arm/jog", {"axis": 3, "speed": 20, "ms": 100})
        self.assertEqual(status, 409)
        status, body = self.json_call("POST", "/ph/measure")
        self.assertEqual(status, 409)  # manual mode: no automatic insertion
        self.assertIn("auto", body["message"])
        self.assertEqual(self.json_call("POST", "/arm/stop")[0], 200)

    def test_drive_blocked_while_probe_down(self):
        self.bot.arm.probe = "inserted"
        try:
            self.bot.drive._stopped_at = 0
            status, body = self.json_call("POST", "/drive", {"throttle": 0.5, "turn": 0})
            self.assertEqual(status, 409)
            self.assertIn("probe", body["message"])
            self.assertEqual(self.json_call("POST", "/drive/stop", {})[0], 200)
        finally:
            self.bot.arm.probe = "unknown"

    def test_remote_arm_motion_needs_token(self):
        handler_cls = agribot_service.make_handler(self.bot)
        handler = handler_cls.__new__(handler_cls)
        handler.client_address = ("192.168.1.50", 5555)
        handler.headers = {}
        with self.assertRaises(agribot_service.Forbidden):
            handler._authorize_motion()
        bot2 = build_bot({"AGRIBOT_LOCAL_TOKEN": "s3cret"})
        handler_cls = agribot_service.make_handler(bot2)
        handler = handler_cls.__new__(handler_cls)
        handler.client_address = ("192.168.1.50", 5555)
        handler.headers = {"X-AgriBot-Token": "wrong"}
        with self.assertRaises(agribot_service.Forbidden):
            handler._authorize_motion()
        handler.headers = {"X-AgriBot-Token": "s3cret"}
        handler._authorize_motion()

    def test_unknown_endpoint_and_camera_errors(self):
        self.assertEqual(self.json_call("GET", "/nope")[0], 404)
        status, body = self.json_call("GET", "/camera/frame.jpg")
        self.assertEqual(status, 503)
        self.assertIn("camera", body["message"].lower())
        status, body = self.json_call("POST", "/vision/scan")
        self.assertEqual(status, 503)

    def test_heartbeat_payload_is_json_and_stable(self):
        first = json.dumps(self.bot.status(), sort_keys=True)
        second = json.dumps(self.bot.status(), sort_keys=True)
        self.assertEqual(first, second)  # no counters that would spam robot:status broadcasts


if __name__ == "__main__":
    unittest.main()

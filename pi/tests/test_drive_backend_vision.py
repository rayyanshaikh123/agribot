"""Drive safety logic, backend client and vision error handling (hardware mocked)."""

import io
import json
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import numpy as np

from agribot import drive as drive_module
from agribot.backend import BackendClient, BackendError
from agribot.drive import Drive, DriveBlocked, arcade

from fakes import make_config


class FakeMotor:
    def __init__(self):
        self.calls = []

    def forward(self, speed):
        self.calls.append(("forward", speed))

    def backward(self, speed):
        self.calls.append(("backward", speed))

    def stop(self):
        self.calls.append(("stop",))

    def close(self):
        self.calls.append(("close",))

    @property
    def last(self):
        return self.calls[-1]


def make_drive(**env):
    cfg = make_config({"DRIVE_MAX_SPEED": "0.5", "DRIVE_TIMEOUT_MS": "300", **env})
    left, right = FakeMotor(), FakeMotor()
    return Drive(cfg, motors=(left, right)), left, right


class DriveTests(unittest.TestCase):
    def test_directions_and_speed_limit(self):
        drive, left, right = make_drive()
        drive.set(1, 1)
        self.assertEqual((left.last, right.last), (("forward", 0.5), ("forward", 0.5)))
        drive.set(-1, -0.5)
        self.assertEqual((left.last, right.last), (("backward", 0.5), ("backward", 0.25)))
        drive.set(*arcade(0, 1))  # spin right: left forward, right backward
        self.assertEqual((left.last[0], right.last[0]), ("forward", "backward"))
        drive.set(0.01, -0.01)  # inside the dead band
        self.assertEqual((left.last, right.last), (("stop",), ("stop",)))

    def test_values_are_clamped(self):
        drive, left, _ = make_drive()
        drive.set(7, 0)
        self.assertEqual(left.last, ("forward", 0.5))

    def test_invalid_values_rejected(self):
        drive, left, _ = make_drive()
        for bad in (float("nan"), float("inf"), "fast", None, True, [1]):
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                drive.set(bad, 0)
        with self.assertRaises(ValueError):
            arcade(float("nan"), 0)
        self.assertNotIn("forward", [c[0] for c in left.calls])

    def test_watchdog_stops_quiet_app(self):
        drive, left, _ = make_drive()
        drive.set(1, 1)
        self.assertFalse(drive.check_watchdog())
        drive._last_command -= 1.0
        self.assertTrue(drive.check_watchdog())
        self.assertEqual(left.last, ("stop",))
        self.assertFalse(drive.moving)
        drive.set(1, 1)  # a watchdog stop does not latch: the app may resume at once
        self.assertTrue(drive.moving)

    def test_stop_wins_over_in_flight_motion(self):
        drive, left, _ = make_drive()
        drive.set(1, 1)
        drive.stop()
        drive.set(1, 1)  # request that was already in flight when stop arrived
        self.assertEqual(left.last, ("stop",))
        drive._stopped_at -= drive_module.STOP_LATCH_SECONDS + 0.01
        drive.set(1, 1)
        self.assertEqual(left.last, ("forward", 0.5))

    def test_gamepad_priority(self):
        drive, left, _ = make_drive()
        drive.set(1, 0, source="gamepad")
        drive.set(-1, -1, source="app")  # ignored while the controller is in use
        self.assertEqual(left.last, ("forward", 0.5))
        drive.set(0, 0, source="app")
        self.assertEqual(drive.source, "gamepad")

    def test_interlock_blocks_motion_but_not_stop(self):
        drive, left, _ = make_drive()
        drive.block_reason = lambda: "pH probe is inserted"
        with self.assertRaises(DriveBlocked):
            drive.set(1, 1)
        self.assertNotIn("forward", [c[0] for c in left.calls])
        drive.set(0, 0)  # zero is always fine
        drive.stop()

    def test_close_stops_motors(self):
        drive, left, right = make_drive()
        drive.set(1, 1)
        drive.close()
        self.assertIn(("stop",), left.calls)
        self.assertEqual(left.last, ("close",))
        self.assertEqual(right.last, ("close",))

    def test_disabled_drive(self):
        drive = Drive(make_config({"DRIVE_ENABLED": "false"}))
        self.assertFalse(drive.ready)
        self.assertEqual(drive.set(1, 1)["ready"], False)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BackendTests(unittest.TestCase):
    def test_request_construction(self):
        client = BackendClient(lambda: "https://example.test", "secret-key")
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b'{"success":true,"commands":[]}')) as urlopen:
            reply = client.request("POST", "/api/robot/heartbeat", {"a": 1}, timeout=7)
        request = urlopen.call_args[0][0]
        self.assertEqual(reply["success"], True)
        self.assertEqual(request.full_url, "https://example.test/api/robot/heartbeat")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-robot-key"), "secret-key")
        self.assertEqual(json.loads(request.data), {"a": 1})
        self.assertEqual(urlopen.call_args[1]["timeout"], 7)

    def test_not_configured(self):
        client = BackendClient(lambda: "", "")
        self.assertFalse(client.configured)
        with self.assertRaises(BackendError):
            client.request("GET", "/x")

    def test_http_error_message_without_secrets(self):
        client = BackendClient(lambda: "https://example.test", "secret-key")
        error = urllib.error.HTTPError("https://example.test/x", 401, "Unauthorized", {}, io.BytesIO(b'{"message":"Invalid robot key."}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(BackendError) as ctx:
                client.request("POST", "/x", {})
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("secret-key", str(ctx.exception))

    def test_network_outage(self):
        client = BackendClient(lambda: "https://example.test", "k")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaisesRegex(BackendError, "unreachable"):
                client.request("GET", "/x")


class VisionTests(unittest.TestCase):
    def test_missing_model_is_reported_not_raised(self):
        from agribot.vision import Detector

        detector = Detector(make_config({"MODEL_PATH": "/nonexistent/model.onnx"}))
        self.assertFalse(detector.ready)
        self.assertIn("not found", detector.error)
        with self.assertRaisesRegex(RuntimeError, "not available"):
            detector.detect(np.zeros((64, 64, 3), np.uint8))

    def test_invalid_upload(self):
        from agribot.vision import decode_image

        with self.assertRaises(ValueError):
            decode_image(b"not an image")

    def test_frame_validation(self):
        from agribot.vision import validate_frame

        self.assertEqual(validate_frame(np.zeros((32, 32, 4), np.uint8)).shape, (32, 32, 3))
        for bad in (None, np.zeros((0, 0, 3)), np.zeros((32, 32)), np.zeros((4, 4, 3)), np.zeros((32, 32, 2))):
            with self.assertRaises(RuntimeError):
                validate_frame(bad)

    def test_camera_disabled(self):
        from agribot.vision import Camera

        camera = Camera(make_config({"CAMERA": "none"}))
        self.assertFalse(camera.ready)
        with self.assertRaisesRegex(RuntimeError, "No camera"):
            camera.capture()

    def test_usb_camera_reopens_after_failure(self):
        from agribot import vision

        captures = []

        class FakeCapture:
            def __init__(self, *_):
                self.reads = 0
                self.released = False
                captures.append(self)

            def isOpened(self):
                return True

            def set(self, *_):
                return True

            def grab(self):
                return True

            def read(self):
                self.reads += 1
                if len(captures) == 1 and self.reads > 3:  # first device dies after warm-up
                    return False, None
                return True, np.full((48, 64, 3), 7, np.uint8)

            def release(self):
                self.released = True

        with mock.patch.object(vision.cv2, "VideoCapture", FakeCapture):
            camera = vision.Camera(make_config({"CAMERA": "usb"}))
            self.assertEqual(camera.kind, "usb")
            frame = camera.capture()
        self.assertEqual(frame.shape, (48, 64, 3))
        self.assertEqual(len(captures), 2)
        self.assertTrue(captures[0].released)
        camera.close()
        self.assertTrue(captures[1].released)

    def test_real_model_inference_runs(self):
        """Loads the shipped ONNX model if present. Shows the pipeline runs, NOT that it is accurate."""
        from agribot.vision import Detector, summarize

        model = Path(__file__).resolve().parents[2] / "ai" / "models" / "weights" / "best.onnx"
        if not model.exists():
            self.skipTest("model file not present")
        detector = Detector(make_config({"MODEL_PATH": str(model)}))
        self.assertTrue(detector.ready, detector.error)
        detections, elapsed = detector.detect(np.full((480, 640, 3), 128, np.uint8))
        self.assertIsInstance(detections, list)
        self.assertIn(summarize(detections)["status"], ("healthy", "disease", "none"))


if __name__ == "__main__":
    unittest.main()

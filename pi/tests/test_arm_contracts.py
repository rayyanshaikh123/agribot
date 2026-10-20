"""Cross-component contracts for the arm path: app <-> Pi <-> ESP32 firmware.

These read the real firmware sketch and Flutter source so a change on one side
that the other side does not know about fails here instead of on the robot.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from agribot import arm as arm_module
from agribot import esp32 as esp32_module
from agribot.arm import Arm, ArmError
from agribot.ph import PhProbe

from fakes import FakeFirmware, FakeLink, make_config

REPO = Path(__file__).resolve().parents[2]
SKETCH = REPO / "esp32" / "agribot_esp32" / "agribot_esp32.ino"
APP_ARM = REPO / "flutter_app" / "lib" / "core" / "arm_controls.dart"


def sketch_int(name):
    match = re.search(rf"static const (?:int|uint32_t) {name} = (-?\d+);", SKETCH.read_text())
    if not match:
        raise AssertionError(f"{name} not found in {SKETCH.name}")
    return int(match.group(1))


@unittest.skipUnless(SKETCH.exists(), "firmware sketch not in this checkout")
class FirmwareContractTests(unittest.TestCase):
    def test_every_pi_command_is_handled_by_the_firmware(self):
        handled = set(re.findall(r'!strcmp\(command, "([A-Z0-9]+)"\)', SKETCH.read_text()))
        self.assertEqual(esp32_module.COMMANDS - handled, set())

    def test_protocol_versions_match(self):
        self.assertEqual(sketch_int("PROTOCOL"), esp32_module.PROTOCOL_VERSION)
        self.assertRegex(SKETCH.read_text(), r'FIRMWARE = "agribot-esp32/[0-9.]+"')

    def test_pins_and_default_stop_values_match(self):
        cfg = make_config()
        self.assertEqual(sketch_int("SERVO1_PIN"), 26)  # elbow
        self.assertEqual(sketch_int("SERVO2_PIN"), 25)  # wrist
        self.assertEqual(sketch_int("SERVO1_STOP"), cfg.arm_servo1_stop)
        self.assertEqual(sketch_int("SERVO2_STOP"), cfg.arm_servo2_stop)
        self.assertEqual((sketch_int("TRIM_MIN"), sketch_int("TRIM_MAX")), (70, 110))  # = config._servo_stop range

    def test_motion_caps_match(self):
        self.assertEqual(sketch_int("MAX_MOVE_MS"), arm_module.MAX_STEP_MS)
        self.assertEqual(sketch_int("JOG_MAX_MS"), arm_module.JOG_MAX_MS)
        self.assertEqual(sketch_int("JOG_MAX_SPEED"), arm_module.JOG_MAX_SPEED_UNHOMED)
        self.assertEqual(sketch_int("RAW_MAX_OFFSET"), arm_module.RAW_MAX_OFFSET)
        cfg = make_config({"ARM_NUDGE_SPEED": "999", "ARM_NUDGE_MS": "99999"})
        self.assertLessEqual(cfg.arm_nudge_speed, sketch_int("JOG_MAX_SPEED"))
        self.assertLessEqual(cfg.arm_nudge_ms, sketch_int("JOG_MAX_MS"))

    def test_link_timeout_outlasts_keepalive(self):
        # Firmware stops/disarms after COMM_TIMEOUT_MS of silence; the Pi must talk more often.
        self.assertGreater(sketch_int("COMM_TIMEOUT_MS") / 1000, 2 * esp32_module.KEEPALIVE_SECONDS)
        self.assertLess(arm_module.POLL_SECONDS * 1000, sketch_int("COMM_TIMEOUT_MS"))


@unittest.skipUnless(APP_ARM.exists(), "Flutter source not in this checkout")
class AppContractTests(unittest.TestCase):
    def test_every_field_the_app_reads_is_reported(self):
        source = APP_ARM.read_text()
        control_keys = set(re.findall(r"controls\['(\w+)'\]", source))
        arm_keys = set(re.findall(r"arm\['(\w+)'\]", source))
        self.assertIn("nudgeAllowed", control_keys)
        cfg = make_config({"ESP32_ENABLED": "true", "ARM_MODE": "manual"})
        link = FakeLink()
        arm = Arm(cfg, link, PhProbe(cfg, link))
        state = arm.state()
        self.assertEqual(control_keys - set(state["controls"]), set())
        self.assertEqual(arm_keys - set(state), set())

    def test_every_status_the_pi_reports_is_known_to_the_app(self):
        source = APP_ARM.read_text()
        for status in ("esp32 disabled", "offline", "motion disabled", "config error"):
            self.assertIn(f"'{status}'", source)


class StopRaceTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stop_while_arming_prevents_the_move(self):
        cfg = make_config({"ESP32_ENABLED": "true", "ARM_MODE": "manual"})
        link = FakeLink(FakeFirmware())
        arm = Arm(cfg, link, PhProbe(cfg, link))
        arm.on_connect()
        original = link.request

        def request(command, *args, **kwargs):
            if command == "ARM" and args and args[0] == 1:
                arm.stop("release raced the start")  # stop lands while the Pi is arming
            return original(command, *args, **kwargs)

        link.request = request
        arm._stopped_at = 0
        with mock.patch.object(arm_module, "STOP_LATCH_SECONDS", 0):
            with self.assertRaisesRegex(ArmError, "stopped by request"):
                arm.nudge("up")
        self.assertEqual(link.fw.move2_log, [])  # no servo command went out
        self.assertFalse(link.fw.armed)


if __name__ == "__main__":
    unittest.main()

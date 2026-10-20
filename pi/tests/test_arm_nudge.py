"""Arm Up / Arm Down (elbow = servo 1, wrist = servo 2) with all hardware mocked."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import agribot_service
from agribot import arm as arm_module
from agribot import config as config_module
from agribot.arm import Arm, ArmError
from agribot.esp32 import COMMANDS, format_command
from agribot.ph import PhProbe

from fakes import FakeFirmware, FakeLink, make_config

MANUAL = {"ESP32_ENABLED": "true", "ARM_MODE": "manual"}


def make_arm(env=None, fw=None, driving=False):
    cfg = make_config({**MANUAL, **(env or {})})
    link = FakeLink(fw or FakeFirmware())
    arm = Arm(cfg, link, PhProbe(cfg, link), is_driving=lambda: driving)
    arm.on_connect()
    return arm, link.fw


class NudgeConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = make_config()
        self.assertEqual(cfg.arm_mode, "disabled")
        self.assertFalse(cfg.arm_app_control)
        self.assertEqual((cfg.arm_up_elbow_dir, cfg.arm_up_wrist_dir), (1, -1))  # opposite
        self.assertEqual((cfg.arm_nudge_speed, cfg.arm_nudge_ms), (20, 250))

    def test_nudge_capped_by_jog_limits(self):
        cfg = make_config({"ARM_NUDGE_SPEED": "40", "ARM_NUDGE_MS": "400", "ARM_JOG_MAX_SPEED": "15", "ARM_JOG_MAX_MS": "100"})
        self.assertEqual((cfg.arm_nudge_speed, cfg.arm_nudge_ms), (15, 100))
        self.assertTrue(any("capped" in w for w in config_module.CONFIG_WARNINGS))

    def test_same_direction_warns_and_bad_values_fall_back(self):
        make_config({"ARM_UP_WRIST_DIR": "1"})
        self.assertTrue(any("equal" in w for w in config_module.CONFIG_WARNINGS))
        cfg = make_config({"ARM_UP_ELBOW_DIR": "3", "ARM_UP_WRIST_DIR": "x"})
        self.assertEqual((cfg.arm_up_elbow_dir, cfg.arm_up_wrist_dir), (1, -1))

    def test_move2_is_a_valid_command(self):
        self.assertIn("MOVE2", COMMANDS)
        self.assertEqual(format_command(4, "move2", (20, -20, 250)), b"4 MOVE2 20 -20 250\n")


class NudgeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_up_moves_elbow_and_wrist_together_in_opposite_directions(self):
        arm, fw = make_arm()
        result = arm.nudge("up")
        self.assertTrue(result["ok"])
        self.assertEqual((result["elbowSpeed"], result["wristSpeed"], result["ms"]), (20, -20, 250))
        self.assertEqual(len(fw.move2_log), 1)  # one atomic command, not two separate moves
        (speeds, ms, _) = fw.move2_log[0]
        self.assertEqual((speeds, ms), ((20, -20), 250))
        self.assertFalse(fw.armed)  # disarmed again after the nudge
        self.assertFalse(any(" MOVE " in line for line in fw.log))

    def test_down_reverses_both(self):
        arm, fw = make_arm()
        result = arm.nudge("down")
        self.assertEqual((result["elbowSpeed"], result["wristSpeed"]), (-20, 20))
        self.assertEqual(fw.move2_log[0][0], (-20, 20))

    def test_direction_config_is_applied(self):
        arm, fw = make_arm({"ARM_UP_ELBOW_DIR": "-1", "ARM_UP_WRIST_DIR": "1", "ARM_NUDGE_SPEED": "10", "ARM_NUDGE_MS": "120"})
        arm.nudge("up")
        self.assertEqual(fw.move2_log[0][:2], ((-10, 10), 120))

    def test_disabled_by_default_and_bad_direction(self):
        arm, fw = make_arm({"ARM_MODE": "disabled"})
        with self.assertRaisesRegex(ArmError, "ARM_MODE=disabled"):
            arm.nudge("up")
        self.assertEqual(fw.move2_log, [])
        arm2, _ = make_arm()
        with self.assertRaisesRegex(ArmError, "direction"):
            arm2.nudge("sideways")

    def test_offline_and_esp32_disabled(self):
        cfg = make_config(MANUAL)
        offline = Arm(cfg, FakeLink(connected=False), None)
        with self.assertRaisesRegex(ArmError, "not connected"):
            offline.nudge("up")
        cfg = make_config({"ARM_MODE": "manual"})
        disabled = Arm(cfg, FakeLink(enabled=False), None)
        with self.assertRaisesRegex(ArmError, "ESP32_ENABLED=false"):
            disabled.nudge("up")

    def test_release_to_stop_interrupts_a_running_nudge(self):
        fw = FakeFirmware(time_scale=0.004)  # 250 ms nudge takes ~1 s real time
        arm, _ = make_arm(fw=fw)
        errors = []
        worker = threading.Thread(target=lambda: self._catch(arm.nudge, "up", errors))
        worker.start()
        deadline = time.monotonic() + 2
        while not fw.axes[1]["moving"] and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(fw.axes[1]["moving"] and fw.axes[2]["moving"])
        arm.stop("button released")
        worker.join(3)
        self.assertFalse(fw.axes[1]["moving"] or fw.axes[2]["moving"])
        self.assertEqual(fw.axes[1]["last"], "stop_command")
        self.assertIn("stopped by request", errors[0])
        self.assertFalse(fw.armed)

    def test_stop_latch_refuses_late_requests(self):
        arm, fw = make_arm()
        arm.stop("released")
        with self.assertRaisesRegex(ArmError, "stop was just requested"):
            arm.nudge("up")
        arm._stopped_at -= arm_module.STOP_LATCH_SECONDS + 0.01
        self.assertTrue(arm.nudge("up")["ok"])

    def test_communication_loss_aborts(self):
        fw = FakeFirmware(time_scale=0.004)
        arm, _ = make_arm(fw=fw)
        errors = []
        worker = threading.Thread(target=lambda: self._catch(arm.nudge, "down", errors))
        worker.start()
        deadline = time.monotonic() + 2
        while not fw.axes[1]["moving"] and time.monotonic() < deadline:
            time.sleep(0.005)
        arm.on_event({"ev": "link_lost", "reason": "unplugged"})
        worker.join(3)
        self.assertTrue(errors)
        self.assertIsNone(arm.operation)

    def test_nudge_can_never_exceed_unhomed_jog_cap(self):
        # Config clamps a nudge to <= 40 % / 400 ms, the firmware's unhomed jog cap.
        arm, fw = make_arm({"ARM_NUDGE_SPEED": "90", "ARM_NUDGE_MS": "2000", "ARM_JOG_MAX_SPEED": "40", "ARM_JOG_MAX_MS": "400"})
        result = arm.nudge("up")
        self.assertEqual((abs(result["elbowSpeed"]), result["ms"]), (40, 400))
        self.assertTrue(result["ok"])

    def test_interlocks(self):
        arm, fw = make_arm(driving=True)
        with self.assertRaisesRegex(ArmError, "driving"):
            arm.nudge("up")
        self.assertEqual(fw.move2_log, [])
        arm2, _ = make_arm()
        arm2._op_lock.acquire()
        arm2.operation = "nudge"
        self.assertIn("arm busy", arm2.drive_block_reason())  # wheels locked while the arm moves
        with self.assertRaisesRegex(ArmError, "busy"):
            arm2.nudge("up")

    def test_nudge_leaves_known_pose(self):
        arm, _ = make_arm()
        arm.home()
        self.assertEqual(arm.probe, "retracted")
        arm.nudge("down")
        self.assertEqual(arm.probe, "partial")  # timed motion is never treated as a position
        self.assertIn("pH probe", arm.drive_block_reason())

    def test_nudge_from_unknown_pose_locks_wheels(self):
        arm, _ = make_arm()
        self.assertEqual(arm.probe, "unknown")
        self.assertIsNone(arm.drive_block_reason())
        arm.nudge("down")
        self.assertEqual(arm.probe, "partial")
        self.assertIn("pH probe", arm.drive_block_reason())
        arm.home()  # operator confirms the arm is back at rest
        self.assertIsNone(arm.drive_block_reason())

    @staticmethod
    def _catch(fn, arg, errors):
        try:
            fn(arg)
        except ArmError as error:
            errors.append(str(error))


class NudgeHttpTests(unittest.TestCase):
    def serve(self, env):
        from test_http_api import build_bot

        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)
        bot = build_bot(env)
        bot.arm.on_connect()
        server = ThreadingHTTPServer(("127.0.0.1", 0), agribot_service.make_handler(bot))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return bot, f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, url, body):
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_nudge_endpoint_and_state(self):
        bot, base = self.serve({"ARM_MODE": "manual"})
        status, body = self.post(base + "/arm/nudge", {"direction": "up"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["data"]["elbowSpeed"], body["data"]["wristSpeed"]), (20, -20))
        with urllib.request.urlopen(base + "/arm", timeout=5) as response:
            controls = json.loads(response.read())["data"]["controls"]
        self.assertTrue(controls["nudgeAllowed"])
        self.assertFalse(controls["appControl"])
        self.assertEqual(self.post(base + "/arm/stop", {})[0], 200)
        status, body = self.post(base + "/arm/nudge", {"direction": "up"})
        self.assertEqual(status, 409)  # stop latch

    def test_nudge_refused_when_disabled(self):
        bot, base = self.serve({"ARM_MODE": "disabled"})
        status, body = self.post(base + "/arm/nudge", {"direction": "down"})
        self.assertEqual(status, 409)
        self.assertIn("ARM_MODE=disabled", body["message"])
        self.assertEqual(bot.esp32.fw.move2_log, [])

    def test_remote_app_needs_flag_and_token(self):
        from test_http_api import build_bot

        for env, token, expected in (
            ({"ARM_MODE": "manual", "AGRIBOT_LOCAL_TOKEN": "t0k"}, "t0k", "ARM_APP_CONTROL=false"),
            ({"ARM_MODE": "manual", "ARM_APP_CONTROL": "true"}, "", "X-AgriBot-Token"),
            ({"ARM_MODE": "manual", "ARM_APP_CONTROL": "true", "AGRIBOT_LOCAL_TOKEN": "t0k"}, "bad", "X-AgriBot-Token"),
        ):
            bot = build_bot(env)
            handler_cls = agribot_service.make_handler(bot)
            handler = handler_cls.__new__(handler_cls)
            handler.client_address = ("192.168.1.50", 5555)
            handler.headers = {"X-AgriBot-Token": token} if token else {}
            handler.command, handler.path = "POST", "/arm/nudge"
            sent = []
            handler._send = lambda status, payload, content_type=None: sent.append((status, payload))
            handler._handle()
            self.assertEqual(sent[0][0], 403, env)
            self.assertIn(expected, sent[0][1]["message"])
            self.assertEqual(bot.esp32.fw.move2_log, [])


if __name__ == "__main__":
    unittest.main()

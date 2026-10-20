"""Retract offered to the app only for a commissioned, firmware-homed arm."""

import io
import json
import time
import unittest
from unittest import mock

import agribot_service
from agribot import arm as arm_module

from test_http_api import build_bot

BASE = {"ESP32_ENABLED": "true", "ARM_MODE": "manual", "ARM_APP_CONTROL": "true", "AGRIBOT_LOCAL_TOKEN": "s3cret"}
COMMISSIONED = {**BASE, "ARM_COMMISSIONED": "true", "ARM_AXIS1_LIMITS": "-600:0", "ARM_AXIS2_LIMITS": "-600:600"}


class RetractFromAppTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def remote(self, bot, path, body=None, token="s3cret"):
        handler_cls = agribot_service.make_handler(bot)
        handler = handler_cls.__new__(handler_cls)
        handler.client_address = ("192.168.1.50", 5555)
        raw = json.dumps(body or {}).encode()
        handler.headers = {"Content-Length": str(len(raw)), "X-AgriBot-Token": token}
        handler.rfile = io.BytesIO(raw)
        handler.command, handler.path = "POST", path
        sent = []
        handler._send = lambda status, payload, content_type=None: sent.append((status, payload))
        handler._handle()
        return sent[0]

    def make(self, env):
        bot = build_bot(env)
        bot.arm.on_connect()
        return bot

    def test_not_offered_or_accepted_when_uncommissioned(self):
        bot = self.make(BASE)
        bot.arm.home()
        bot.arm.nudge("down")
        self.assertFalse(bot.arm.state()["controls"]["retractAllowed"])
        status, payload = self.remote(bot, "/arm/retract")
        self.assertEqual(status, 409)
        self.assertIn("commissioned", payload["message"])
        self.assertEqual(bot.arm.probe, "partial")

    def test_not_offered_without_firmware_home(self):
        bot = self.make(COMMISSIONED)
        bot.arm.nudge("down")  # never homed: no defined target pose
        self.assertFalse(bot.arm.state()["controls"]["retractAllowed"])
        self.assertFalse(bot.arm.state(include_firmware=False)["controls"]["retractAllowed"])

    def test_app_control_off_is_forbidden(self):
        bot = self.make({**COMMISSIONED, "ARM_APP_CONTROL": "false"})
        status, payload = self.remote(bot, "/arm/retract")
        self.assertEqual(status, 403)

    def test_commissioned_homed_arm_retracts_from_app(self):
        bot = self.make(COMMISSIONED)
        bot.arm.home()
        bot.arm._stopped_at = 0
        bot.arm.nudge("down")
        self.assertTrue(bot.arm.state()["controls"]["retractAllowed"])
        status, payload = self.remote(bot, "/arm/retract")
        self.assertEqual(status, 202, payload)
        deadline = time.monotonic() + 5
        while bot.arm.operation and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(bot.arm.last_result["ok"], bot.arm.last_result)
        self.assertEqual(bot.arm.probe, "retracted")
        self.assertIsNone(bot.arm.drive_block_reason())
        self.assertFalse(bot.arm.state()["controls"]["retractAllowed"])  # nothing left to retract


if __name__ == "__main__":
    unittest.main()

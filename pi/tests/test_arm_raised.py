"""Arm Raised confirmation: releases the wheel lock only on a verified safe state."""

import unittest
from unittest import mock

import agribot_service
from agribot import arm as arm_module
from agribot.arm import CONFIRM_RAISED_PHRASE, Arm, ArmError
from agribot.drive import DriveBlocked
from agribot.ph import PhProbe

from fakes import FakeFirmware, FakeLink, make_config

MANUAL = {"ESP32_ENABLED": "true", "ARM_MODE": "manual"}
REMOTE_OK = {**MANUAL, "ARM_APP_CONTROL": "true", "AGRIBOT_LOCAL_TOKEN": "s3cret"}


def make_arm(env=None, fw=None, connected=True, driving=False):
    cfg = make_config({**MANUAL, **(env or {})})
    link = FakeLink(fw or FakeFirmware(), connected=connected)
    arm = Arm(cfg, link, PhProbe(cfg, link), is_driving=lambda: driving)
    if connected:
        arm.on_connect()
    return arm, link.fw


def locked_arm(**kwargs):
    arm, fw = make_arm(**kwargs)
    arm.nudge("down")  # probe may be down -> wheels locked
    assert arm.drive_block_reason()
    return arm, fw


class ConfirmRaisedTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_releases_lock_without_moving_or_faking_home(self):
        # Commissioned config, so the only thing stopping insertion below is the probe state.
        arm, fw = locked_arm(env={"ARM_COMMISSIONED": "true", "ARM_INSERT_STEPS": "1:-20:300", "ARM_AXIS1_LIMITS": "-100:0"})
        log_before = len(fw.log)
        result = arm.confirm_raised(CONFIRM_RAISED_PHRASE, source="test")
        self.assertEqual(result["probe"], "clear")
        self.assertFalse(result["driveLocked"])
        self.assertIsNone(arm.drive_block_reason())
        sent = fw.log[log_before:]
        self.assertEqual([line.split()[1] for line in sent], ["STATUS"])  # read-only: no ARM/MOVE/HOME
        self.assertFalse(fw.axes[1]["homed"] or fw.axes[2]["homed"])  # position not invented
        self.assertEqual(arm.last_confirmation["source"], "test")
        with self.assertRaisesRegex(ArmError, "probe is clear"):  # not a home for automatic insertion
            arm._insert()

    def test_new_motion_relocks(self):
        arm, _ = locked_arm()
        arm.confirm_raised(CONFIRM_RAISED_PHRASE)
        arm.nudge("down")
        self.assertIn("pH probe is partial", arm.drive_block_reason())

    def test_requires_exact_phrase(self):
        arm, _ = locked_arm()
        for claim in (None, "", "yes", True, "raised"):
            with self.assertRaisesRegex(ArmError, "Explicit confirmation"):
                arm.confirm_raised(claim)
        self.assertIsNotNone(arm.drive_block_reason())

    def test_disconnected_esp32_keeps_lock(self):
        arm, _ = make_arm(connected=False)
        arm.probe = "partial"
        with self.assertRaisesRegex(ArmError, "not connected.*wheels stay locked"):
            arm.confirm_raised(CONFIRM_RAISED_PHRASE)
        self.assertEqual(arm.probe, "partial")

    def test_esp32_disabled_and_arm_mode_disabled(self):
        cfg = make_config({"ARM_MODE": "manual"})
        arm = Arm(cfg, FakeLink(enabled=False), None)
        with self.assertRaisesRegex(ArmError, "ESP32_ENABLED=false"):
            arm.confirm_raised(CONFIRM_RAISED_PHRASE)
        arm2, _ = make_arm({"ARM_MODE": "disabled"})
        with self.assertRaisesRegex(ArmError, "ARM_MODE=disabled"):
            arm2.confirm_raised(CONFIRM_RAISED_PHRASE)

    def test_unsafe_firmware_states_are_rejected(self):
        cases = {
            "armed": lambda fw: setattr(fw, "armed", True),
            "moving": lambda fw: fw.axes[2].update(moving=True, start=1e12, ms=3000),
        }
        for name, make_unsafe in cases.items():
            arm, fw = locked_arm()
            make_unsafe(fw)
            with self.assertRaisesRegex(ArmError, "Not safe to confirm.*wheels stay locked", msg=name):
                arm.confirm_raised(CONFIRM_RAISED_PHRASE)
            self.assertEqual(arm.probe, "partial", name)
            self.assertIn("rejected", arm.faults[-1]["message"])

    def test_unreadable_or_incomplete_status_is_rejected(self):
        arm, fw = locked_arm()
        original = arm.link.request

        def broken(command, *args, **kwargs):
            if command == "STATUS":
                return {"ok": True, "armed": False, "axes": [{"axis": 1, "moving": False}]}
            return original(command, *args, **kwargs)

        arm.link.request = broken
        with self.assertRaisesRegex(ArmError, "both servos"):
            arm.confirm_raised(CONFIRM_RAISED_PHRASE)

        from agribot.esp32 import Esp32Error

        def timeout(command, *args, **kwargs):
            raise Esp32Error("ESP32 did not answer STATUS within 1.0 s")

        arm.link.request = timeout
        with self.assertRaisesRegex(ArmError, "could not read the firmware state"):
            arm.confirm_raised(CONFIRM_RAISED_PHRASE)
        self.assertEqual(arm.probe, "partial")

    def test_busy_driving_and_inserted_are_rejected(self):
        arm, _ = locked_arm()
        arm._op_lock.acquire()
        arm.operation = "nudge"
        with self.assertRaisesRegex(ArmError, "busy"):
            arm.confirm_raised(CONFIRM_RAISED_PHRASE)
        arm2, _ = make_arm(driving=True)
        arm2.probe = "partial"
        with self.assertRaisesRegex(ArmError, "driving"):
            arm2.confirm_raised(CONFIRM_RAISED_PHRASE)
        arm3, _ = make_arm()
        arm3.probe = "inserted"
        with self.assertRaisesRegex(ArmError, "Retract"):
            arm3.confirm_raised(CONFIRM_RAISED_PHRASE)

    def test_state_reports_controls(self):
        arm, _ = locked_arm()
        controls = arm.state(include_firmware=False)["controls"]
        self.assertTrue(controls["driveLocked"])
        self.assertTrue(controls["confirmRaisedAllowed"])
        offline, _ = make_arm(connected=False)
        self.assertFalse(offline.state(include_firmware=False)["controls"]["confirmRaisedAllowed"])


class ConfirmRaisedHttpTests(unittest.TestCase):
    """Token handling through the real request handler, as a LAN client (the app)."""

    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def remote(self, bot, method, path, body=None, token=None):
        import io
        import json

        handler_cls = agribot_service.make_handler(bot)
        handler = handler_cls.__new__(handler_cls)
        handler.client_address = ("192.168.1.50", 5555)
        raw = json.dumps(body).encode() if body is not None else b""
        handler.headers = {"Content-Length": str(len(raw)), **({"X-AgriBot-Token": token} if token is not None else {})}
        handler.rfile = io.BytesIO(raw)
        handler.command, handler.path = method, path
        sent = []
        handler._send = lambda status, payload, content_type=None: sent.append((status, payload))
        handler._handle()
        return sent[0]

    def bot(self, env):
        from test_http_api import build_bot

        bot = build_bot(env)
        bot.arm.on_connect()
        bot.arm.nudge("down")  # locks the wheels
        return bot

    def test_valid_token_confirms_and_driving_resumes(self):
        bot = self.bot(REMOTE_OK)
        with self.assertRaises(DriveBlocked):
            bot.drive.set(0.5, 0.5)
        status, payload = self.remote(bot, "POST", "/arm/confirm-raised", {"confirm": CONFIRM_RAISED_PHRASE}, token="s3cret")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["probe"], "clear")
        self.assertIn("app 192.168.1.50", payload["data"]["confirmation"]["source"])
        bot.drive._stopped_at = 0
        self.assertTrue(bot.drive.set(0.5, 0.5)["ready"])
        bot.drive.stop()

    def test_invalid_or_missing_token_rejected(self):
        bot = self.bot(REMOTE_OK)
        for token in (None, "", "wrong", "s3cret "):
            status, payload = self.remote(bot, "POST", "/arm/confirm-raised", {"confirm": CONFIRM_RAISED_PHRASE}, token=token)
            self.assertEqual(status, 403, token)
        self.assertEqual(bot.arm.probe, "partial")

    def test_app_control_off_rejected_even_with_token(self):
        bot = self.bot({**MANUAL, "AGRIBOT_LOCAL_TOKEN": "s3cret"})
        status, payload = self.remote(bot, "POST", "/arm/confirm-raised", {"confirm": CONFIRM_RAISED_PHRASE}, token="s3cret")
        self.assertEqual(status, 403)
        self.assertIn("ARM_APP_CONTROL=false", payload["message"])
        self.assertEqual(bot.arm.probe, "partial")

    def test_unsafe_state_returns_conflict(self):
        bot = self.bot(REMOTE_OK)
        bot.esp32.fw.armed = True
        status, payload = self.remote(bot, "POST", "/arm/confirm-raised", {"confirm": CONFIRM_RAISED_PHRASE}, token="s3cret")
        self.assertEqual(status, 409)
        self.assertIn("wheels stay locked", payload["message"])
        status, payload = self.remote(bot, "POST", "/arm/confirm-raised", {"confirm": "yes"}, token="s3cret")
        self.assertEqual(status, 409)

    def test_stop_needs_no_token(self):
        bot = self.bot(REMOTE_OK)
        for path in ("/stop", "/arm/stop", "/drive/stop"):
            status, _ = self.remote(bot, "POST", path, {})
            self.assertEqual(status, 200, path)
        self.assertFalse(bot.esp32.fw.armed)
        self.assertFalse(bot.drive.moving)


if __name__ == "__main__":
    unittest.main()

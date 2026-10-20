"""ESP32 protocol, pH conversion and arm workflow (all hardware mocked)."""

import threading
import time
import unittest
from unittest import mock

from agribot import arm as arm_module
from agribot.arm import Arm, ArmError, Workflow, parse_limits, parse_steps
from agribot.esp32 import Esp32Error, Esp32Link, check_identity, format_command, parse_line
from agribot.ph import PhProbe, parse_calibration

from fakes import FakeFirmware, FakeLink, FakeSerial, make_config

CALIBRATION = "7.00:1500,4.01:1990"  # ~164 mV/pH, typical amplified board


class ProtocolParsingTests(unittest.TestCase):
    def test_parse_valid_json_line(self):
        self.assertEqual(parse_line(b'{"id":3,"ok":true}\r'), {"id": 3, "ok": True})

    def test_noise_and_malformed_lines_are_ignored(self):
        for raw in (b"", b"ets Jun  8 2016 00:22:57", b'{"id":3,', b"[1,2]", b'{"a":"' + b"x" * 2000 + b'"}', b"\xff\xfe{"):
            self.assertIsNone(parse_line(raw), raw)

    def test_format_command_validates(self):
        self.assertEqual(format_command(5, "move", (1, -30, 200)), b"5 MOVE 1 -30 200\n")
        with self.assertRaises(Esp32Error):
            format_command(1, "REBOOT", ())
        with self.assertRaises(Esp32Error):
            format_command(1, "MOVE", ("1 2",))  # embedded space would shift arguments
        with self.assertRaises(Esp32Error):
            format_command(1, "MOVE", ("1\n0 STOP",))
        with self.assertRaises(Esp32Error):
            format_command(1, "MOVE", ("9" * 100,))

    def test_identity_requires_firmware_name_and_protocol(self):
        self.assertEqual(check_identity({"id": 0, "ok": True, "fw": "agribot-esp32/1.3", "proto": 3}), "agribot-esp32/1.3")
        self.assertIsNone(check_identity({"id": 0, "ok": True, "fw": "other-sketch"}))
        self.assertIsNone(check_identity({"id": 7, "ok": True, "fw": "agribot-esp32/1.3", "proto": 3}))
        with self.assertRaises(Esp32Error):
            check_identity({"id": 0, "ok": True, "fw": "agribot-esp32/1.0"})  # old firmware, no proto
        with self.assertRaises(Esp32Error):
            check_identity({"id": 0, "ok": True, "fw": "agribot-esp32/1.1", "proto": 1})  # no TRIM/DIR/RAW
        with self.assertRaises(Esp32Error):
            check_identity({"id": 0, "ok": True, "fw": "agribot-esp32/1.2", "proto": 2})  # no MOVE2


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config({"ESP32_ENABLED": "true", "ESP32_PORT": "/dev/fake0"})
        self.fw = FakeFirmware()
        self.port = None

    def factory(self, path):
        self.port = FakeSerial(self.fw, noise=b"rst:0x1 (POWERON_RESET)\r\ngarbage line\r\n")
        return self.port

    def start_link(self, **kwargs):
        stop = threading.Event()
        self.addCleanup(stop.set)
        link = Esp32Link(self.cfg, serial_factory=self.factory, **kwargs)
        link.start(stop)
        deadline = time.monotonic() + 3
        while not link.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(link.connected, link.error)
        return link, stop

    def test_handshake_request_and_malformed_lines(self):
        link, _ = self.start_link()
        self.assertEqual(link.firmware, "agribot-esp32/1.3")
        self.assertTrue(link.request("STATUS")["ok"])
        self.port.feed(b"not json\n{broken\n")
        time.sleep(0.15)
        self.assertGreaterEqual(link.state()["stats"]["malformed"], 2)
        self.assertTrue(link.request("KA")["ok"])  # still healthy afterwards

    def test_rejected_command_raises(self):
        link, _ = self.start_link()
        with self.assertRaisesRegex(Esp32Error, "disarmed"):
            link.request("MOVE", 1, 20, 100)

    @mock.patch("agribot.esp32.KEEPALIVE_SECONDS", 0.1)
    def test_timeout_and_drop_on_dead_link(self):
        link, _ = self.start_link()
        self.port.silent = True
        with self.assertRaisesRegex(Esp32Error, "did not answer"):
            link.request("STATUS", timeout=0.2)
        dead_port = self.port
        deadline = time.monotonic() + 8  # keepalive drops after 3 misses, then reconnects
        while link.state()["stats"]["reconnects"] < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(link.state()["stats"]["reconnects"], 1)
        self.assertTrue(dead_port.closed)
        deadline = time.monotonic() + 3
        while not link.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNot(self.port, dead_port)  # reopened on a fresh port
        self.assertTrue(link.request("KA")["ok"])

    def test_events_are_dispatched(self):
        events = []
        link, _ = self.start_link(on_event=events.append)
        self.port.feed(b'{"ev":"boot","fw":"agribot-esp32/1.3","proto":3,"reset":1}\n')
        deadline = time.monotonic() + 1
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(events[0]["ev"], "boot")

    def test_unknown_firmware_is_never_commanded(self):
        self.fw.fw = "someone-elses-sketch"
        link = Esp32Link(self.cfg, serial_factory=self.factory)
        original = Esp32Link._handshake
        link._handshake = lambda ser: original(link, ser, 0.5)
        with self.assertRaisesRegex(Esp32Error, "no AgriBot firmware"):
            link._connect()
        self.assertTrue(all(w == b"0 PING\n" for w in self.port.written))

    def test_disabled_link_reports_state(self):
        link = Esp32Link(make_config())
        self.assertFalse(link.enabled)
        with self.assertRaises(Esp32Error):
            link.request("PING")


class PhTests(unittest.TestCase):
    def test_calibration_two_points(self):
        slope, intercept = parse_calibration(CALIBRATION)
        self.assertAlmostEqual(slope * 1500 + intercept, 7.0, places=2)
        self.assertAlmostEqual(slope * 1990 + intercept, 4.01, places=2)

    def test_bad_calibrations(self):
        for text in ("", "7:1500", "7:1500,4:1500", "7-1500,4-2000", "15:1500,4:2000", "7:1500,4:1501"):
            with self.assertRaises(ValueError, msg=text):
                parse_calibration(text)

    def probe(self, fw=None, **env):
        cfg = make_config({"ESP32_ENABLED": "true", "PH_CALIBRATION": CALIBRATION, **env})
        return PhProbe(cfg, FakeLink(fw or FakeFirmware()))

    def test_uncalibrated_reports_voltage_but_no_ph(self):
        probe = self.probe(PH_CALIBRATION="")
        reading = probe.read_live()
        self.assertEqual(reading["mv"], 1500.0)
        self.assertIsNone(reading["ph"])
        probe.measure(2, interval=0.01)
        self.assertIsNone(probe.latest()["value"])
        self.assertEqual(probe.latest()["status"], "uncalibrated")
        self.assertEqual(probe.latest()["mv"], 1500.0)
        self.assertIsNone(probe.latest()["estimate"])  # no formula configured: none invented

    def test_measure_and_latest(self):
        probe = self.probe()
        self.assertIsNone(probe.latest()["value"])  # nothing measured: no value invented
        result = probe.measure(3, interval=0.01)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["ph"], 7.0, places=1)
        self.assertEqual(probe.latest()["status"], "ok")
        self.assertEqual(probe.latest()["source"], "measured (calibrated)")

    def test_disconnected_sensor_is_rejected(self):
        probe = self.probe(FakeFirmware(ph_mv=5))  # floating/unpowered pin near 0 V
        result = probe.measure(2, interval=0.01)
        self.assertFalse(result["valid"])
        self.assertIsNone(probe.latest()["value"])

    def test_saturated_adc_is_rejected(self):
        probe = self.probe(FakeFirmware(ph_mv=3100))
        self.assertFalse(probe.read_live()["valid"])

    def test_stale_measurement_expires(self):
        probe = self.probe()
        probe.measure(1)
        with mock.patch("agribot.ph.time.monotonic", return_value=time.monotonic() + 7200):
            self.assertIsNone(probe.latest()["value"])
            self.assertIn("too old", probe.latest()["source"])


class ArmConfigTests(unittest.TestCase):
    def test_parse_steps_and_limits(self):
        self.assertEqual(parse_steps("1:-40:800, 2:30:500"), [(1, -40, 800), (2, 30, 500)])
        self.assertEqual(parse_limits("-1200:0"), (-1200.0, 0.0))
        self.assertIsNone(parse_limits(""))
        for bad in ("3:10:100", "1:0:100", "1:101:100", "1:10:5000", "1:10", "a:b:c"):
            with self.assertRaises(ValueError, msg=bad):
                parse_steps(bad)
        for bad in ("10:20", "-5:-1", "0:0", "x:y", "-5"):
            with self.assertRaises(ValueError, msg=bad):
                parse_limits(bad)

    def test_steps_must_fit_limits(self):
        cfg = make_config({"ESP32_ENABLED": "true", "ARM_MODE": "auto", "ARM_INSERT_STEPS": "1:-50:3000",
                           "ARM_AXIS1_LIMITS": "-1000:0"})
        arm = Arm(cfg, FakeLink(), None)
        self.assertIn("outside its limits", arm.config_error)
        self.assertEqual(arm.summary(), "config error")


def calibrated_env(mode="auto"):
    return {"ESP32_ENABLED": "true", "ARM_MODE": mode, "ARM_COMMISSIONED": "true", "PH_CALIBRATION": CALIBRATION,
            "ARM_INSERT_STEPS": "1:-40:1000,2:30:500", "ARM_AXIS1_LIMITS": "-600:0", "ARM_AXIS2_LIMITS": "0:200",
            "PH_SETTLE_SECONDS": "0", "PH_SAMPLES": "2", "PH_SAMPLE_INTERVAL_SECONDS": "0.1", "ARM_RETRACT_SPEED": "40"}


class ArmWorkflowTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, mode="auto", fw=None, driving=False, env=None):
        cfg = make_config({**calibrated_env(mode), **(env or {})})
        link = FakeLink(fw or FakeFirmware())
        ph = PhProbe(cfg, link)
        arm = Arm(cfg, link, ph, is_driving=lambda: driving)
        arm.on_connect()
        return arm, link.fw

    def test_disabled_mode_refuses_all_motion_but_allows_stop(self):
        arm, fw = self.make(mode="disabled")
        for call in (lambda: arm.jog(1, 20, 100), lambda: arm.home(), lambda: arm.retract(False), lambda: arm.measure_ph(False)):
            with self.assertRaisesRegex(ArmError, "ARM_MODE=disabled"):
                call()
        self.assertTrue(arm.stop()["stopped"])
        self.assertFalse(any(" MOVE " in line for line in fw.log))
        self.assertFalse(any(" ARM 1" in line for line in fw.log))

    def test_probe_position_is_never_assumed(self):
        arm, _ = self.make()
        self.assertEqual(arm.probe, "unknown")
        with self.assertRaisesRegex(ArmError, "probe is unknown"):
            arm.insert(background=False)

    def test_manual_mode_jog_home_but_no_insert(self):
        arm, fw = self.make(mode="manual")
        result = arm.jog(1, -30, 200)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["pos"], -60, delta=1)
        self.assertFalse(fw.armed)  # disarmed again after the operation
        with self.assertRaisesRegex(ArmError, "unhomed|jog needs"):
            arm.jog(1, -30, 900)  # longer than a jog is allowed to be
        arm.home()
        self.assertEqual(arm.probe, "retracted")
        with self.assertRaisesRegex(ArmError, "needs auto"):
            arm.insert(background=False)

    def test_full_measurement_workflow(self):
        arm, fw = self.make()
        arm.home()
        result = arm.measure_ph(background=False)
        self.assertTrue(result["ok"], result)
        steps = {s["step"]: s["status"] for s in result["workflow"]["steps"]}
        self.assertEqual(steps, {"checks": "done", "insert": "done", "settle": "done", "sample": "done", "retract": "done", "report": "done"})
        self.assertEqual(result["workflow"]["incomplete"], [])
        self.assertAlmostEqual(result["ph"]["ph"], 7.0, places=1)
        self.assertEqual(arm.probe, "retracted")
        self.assertFalse(fw.armed)
        for axis in (1, 2):
            self.assertLessEqual(abs(fw.axes[axis]["pos"]), arm_module.HOME_TOLERANCE)

    def test_workflow_refuses_without_calibration(self):
        arm, _ = self.make(env={"PH_CALIBRATION": ""})
        arm.home()
        with self.assertRaisesRegex(ArmError, "not calibrated"):
            arm.measure_ph(background=False)

    def test_bad_probe_board_aborts_before_moving(self):
        arm, fw = self.make(fw=FakeFirmware(ph_mv=3200))
        arm.home()
        with self.assertRaisesRegex(ArmError, "before moving"):
            arm.measure_ph(background=False)
        self.assertFalse(any(" MOVE " in line for line in fw.log))
        steps = {s["step"]: s["status"] for s in arm.last_result["workflow"]["steps"]}
        self.assertEqual(steps["checks"], "failed")
        self.assertEqual(steps["insert"], "skipped")
        self.assertEqual(arm.probe, "retracted")

    def test_move_stopped_early_marks_partial_and_reports(self):
        fw = FakeFirmware()
        arm, _ = self.make(fw=fw)
        arm.home()
        fw.fail_axis_with = "limit"
        with self.assertRaisesRegex(ArmError, "stopped early: limit"):
            arm.insert(background=False)
        self.assertEqual(arm.probe, "partial")
        self.assertIsNotNone(arm.drive_block_reason())  # wheels locked until resolved
        self.assertFalse(fw.armed)

    def test_stop_aborts_running_workflow_and_stays_still(self):
        arm, fw = self.make(env={"PH_SETTLE_SECONDS": "5"})
        arm.home()
        arm.measure_ph(background=True)
        deadline = time.monotonic() + 3
        while arm.probe != "inserted" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(arm.probe, "inserted")
        arm.stop()
        deadline = time.monotonic() + 3
        while arm.operation and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(arm.last_result["ok"])
        steps = {s["step"]: s["status"] for s in arm.last_result["workflow"]["steps"]}
        self.assertEqual(steps["settle"], "failed")
        self.assertEqual(steps["retract"], "skipped")  # explicit stop: no automatic motion
        self.assertIn(arm.probe, ("inserted", "partial"))
        self.assertIn("pH probe", arm.drive_block_reason())

    def test_esp32_reboot_forgets_home(self):
        arm, fw = self.make()
        arm.home()
        fw.boot()
        arm.on_event({"ev": "boot", "reset": 1})
        self.assertEqual(arm.probe, "unknown")
        with self.assertRaises(ArmError):
            arm.insert(background=False)

    def test_busy_and_driving_interlocks(self):
        arm, _ = self.make(driving=True)
        with self.assertRaisesRegex(ArmError, "driving"):
            arm.home()
        arm2, _ = self.make()
        arm2._op_lock.acquire()
        arm2.operation = "measure"
        with self.assertRaisesRegex(ArmError, "busy"):
            arm2.jog(1, 10, 100)

    def test_offline_esp32(self):
        cfg = make_config(calibrated_env())
        link = FakeLink(connected=False)
        arm = Arm(cfg, link, PhProbe(cfg, link))
        self.assertEqual(arm.summary(), "offline")
        with self.assertRaisesRegex(ArmError, "not connected"):
            arm.home()


class WorkflowRecordTests(unittest.TestCase):
    def test_states(self):
        wf = Workflow("measure", ("a", "b", "c"))
        wf.begin("a")
        wf.done("a")
        wf.begin("b")
        wf.close("boom")
        record = wf.as_dict()
        self.assertEqual([s["status"] for s in record["steps"]], ["done", "failed", "skipped"])
        self.assertEqual(record["incomplete"], ["b", "c"])
        self.assertEqual(record["steps"][1]["detail"], "boom")


if __name__ == "__main__":
    unittest.main()

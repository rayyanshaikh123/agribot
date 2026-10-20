"""Uncalibrated pH use, arm commissioning gates and missing soil sensor (all mocked)."""

import json
import unittest
from unittest import mock

from agribot import arm as arm_module
from agribot.arm import Arm, ArmError
from agribot.ph import PhProbe, provisional_estimate
from agribot.sensors import SensorHub

from fakes import FakeFirmware, FakeLink, make_config

UNCAL = {"ESP32_ENABLED": "true"}  # no PH_CALIBRATION: buffers not available yet
PROVISIONAL = {**UNCAL, "PH_PROVISIONAL_NEUTRAL_MV": "2500", "PH_PROVISIONAL_MV_PER_PH": "-170"}


def hub(env, link, probe, dht=(22.0, 55.0)):
    cfg = make_config({"DHT_BACKEND": "auto", "RAIN_ENABLED": "true", **env})
    with mock.patch.object(SensorHub, "_open_hardware", lambda self: None):
        h = SensorHub(cfg, ph=probe, link=link)

    class Dht:
        def read(self):
            return dht

        def close(self):
            pass

    class Pin:
        value = 1

        def close(self):
            pass

    h._dht, h._rain = Dht(), Pin()
    h.poll()
    return h


class UncalibratedPhTests(unittest.TestCase):
    def test_raw_and_voltage_without_formula(self):
        link = FakeLink(FakeFirmware(ph_mv=2310))
        probe = PhProbe(make_config(UNCAL), link)
        live = probe.read_live()
        self.assertEqual(live["mv"], 2310.0)
        self.assertIsNotNone(live["raw"])
        self.assertIsNone(live["ph"])
        self.assertIsNone(live["estimate"])  # no documented formula configured -> none invented
        self.assertEqual(live["label"], "uncalibrated")

    def test_provisional_estimate_is_labelled_and_not_published(self):
        link = FakeLink(FakeFirmware(ph_mv=2330))
        cfg = make_config(PROVISIONAL)
        probe = PhProbe(cfg, link)
        self.assertAlmostEqual(provisional_estimate(cfg, 2330), 8.0, places=2)
        result = probe.measure(2, interval=0.1, method="manual")
        self.assertIsNone(result["ph"])
        self.assertAlmostEqual(result["estimate"], 8.0, places=1)
        self.assertEqual((result["label"], result["method"]), ("uncalibrated", "manual"))
        h = hub(PROVISIONAL, link, probe)
        data = h.read_all()
        self.assertIsNone(data["sensors"]["ph"])  # app/backend never see the estimate
        self.assertEqual(data["status"]["ph"], "uncalibrated")
        self.assertIn("uncalibrated", data["sources"]["ph"])
        self.assertIsNone(h.publish_payload()[0]["ph"])

    def test_disconnected_or_invalid_sensor_reports_null(self):
        for fw in (FakeFirmware(ph_mv=4), FakeFirmware(ph_mv=3150)):
            link = FakeLink(fw)
            probe = PhProbe(make_config({**PROVISIONAL, "PH_CALIBRATION": "7:1500,4:1990"}), link)
            probe.measure(2, interval=0.1)
            latest = probe.latest()
            self.assertIsNone(latest["value"])
            self.assertEqual(latest["status"], "rejected")

    def test_esp32_offline_reports_unavailable(self):
        link = FakeLink(connected=False)
        probe = PhProbe(make_config(UNCAL), link)
        self.assertEqual(probe.latest()["status"], "unavailable")
        h = hub(UNCAL, link, probe)
        self.assertIsNone(h.read_all()["sensors"]["ph"])

    def test_no_fixed_ph_anywhere(self):
        cfg = make_config({"USE_FAKE_PH": "true", "FAKE_PH": "6.8", "PH_SOURCE": "fixed"})
        self.assertFalse(hasattr(cfg, "fake_ph"))
        h = hub({"USE_FAKE_PH": "true", "PH_SOURCE": "fixed"}, None, None)
        self.assertIsNone(h.read_all()["sensors"]["ph"])
        self.assertIsNone(h.publish_payload()[0]["ph"])

    def test_bad_provisional_settings_ignored(self):
        cfg = make_config({**UNCAL, "PH_PROVISIONAL_NEUTRAL_MV": "abc", "PH_PROVISIONAL_MV_PER_PH": "0"})
        self.assertIsNone(cfg.ph_provisional_neutral_mv)
        self.assertIsNone(cfg.ph_provisional_mv_per_ph)
        self.assertIsNone(provisional_estimate(cfg, 2000))

    def test_lack_of_calibration_never_moves_arm(self):
        env = {**UNCAL, "ARM_MODE": "auto", "ARM_COMMISSIONED": "true", "ARM_INSERT_STEPS": "1:-20:300",
               "ARM_AXIS1_LIMITS": "-100:0"}
        cfg = make_config(env)
        link = FakeLink()
        probe = PhProbe(cfg, link)
        arm = Arm(cfg, link, probe)
        with self.assertRaisesRegex(ArmError, "not calibrated"):
            arm.measure_ph(background=False)
        self.assertFalse(any(" MOVE " in line or " ARM 1" in line for line in link.fw.log))


class CommissioningTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(arm_module, "POLL_SECONDS", 0.005)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, env):
        cfg = make_config({"ESP32_ENABLED": "true", **env})
        link = FakeLink()
        arm = Arm(cfg, link, PhProbe(cfg, link))
        arm.on_connect()
        return arm, link.fw

    def test_defaults_are_disabled_and_uncommissioned(self):
        cfg = make_config()
        self.assertEqual((cfg.arm_mode, cfg.arm_commissioned), ("disabled", False))
        self.assertEqual((cfg.arm_servo1_stop, cfg.arm_servo2_stop), (96, 92))
        self.assertEqual((cfg.arm_servo1_dir, cfg.arm_servo2_dir), (1, 1))

    def test_stop_values_and_directions_pushed_on_connect(self):
        arm, fw = self.make({"ARM_SERVO1_STOP": "95", "ARM_SERVO2_STOP": "93", "ARM_SERVO2_DIR": "-1"})
        self.assertEqual((fw.axes[1]["stop"], fw.axes[2]["stop"]), (95, 93))
        self.assertEqual((fw.axes[1]["dir"], fw.axes[2]["dir"]), (1, -1))
        self.assertFalse(fw.armed)
        self.assertFalse(any(" MOVE " in line or " RAW " in line for line in fw.log))  # connect never moves

    def test_invalid_servo_settings_fall_back(self):
        cfg = make_config({"ARM_SERVO1_STOP": "150", "ARM_SERVO2_DIR": "2", "ARM_JOG_MAX_MS": "5000"})
        self.assertEqual((cfg.arm_servo1_stop, cfg.arm_servo2_dir, cfg.arm_jog_max_ms), (96, 1, 400))

    def test_neutral_test_manual_only_bounded_and_clears_home(self):
        arm, fw = self.make({"ARM_MODE": "manual"})
        arm.home()
        self.assertEqual(arm.probe, "retracted")
        result = arm.neutral_test(1, 95, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(arm.probe, "unknown")
        self.assertFalse(fw.axes[1]["homed"])  # creep possible: home must be re-established
        self.assertFalse(fw.armed)
        for args in ((1, 120, 200), (1, 95, 5000), (3, 95, 100)):
            with self.assertRaises(ArmError):
                arm.neutral_test(*args)
        disabled, fw2 = self.make({"ARM_MODE": "disabled"})
        with self.assertRaisesRegex(ArmError, "ARM_MODE=disabled"):
            disabled.neutral_test(1, 96, 100)
        self.assertFalse(any(" RAW " in line for line in fw2.log))

    def test_jog_caps_from_config(self):
        arm, _ = self.make({"ARM_MODE": "manual", "ARM_JOG_MAX_MS": "150", "ARM_JOG_MAX_SPEED": "20"})
        with self.assertRaisesRegex(ArmError, "ARM_JOG_MAX"):
            arm.jog(1, 25, 100)
        with self.assertRaisesRegex(ArmError, "ARM_JOG_MAX"):
            arm.jog(1, 20, 200)
        self.assertTrue(arm.jog(1, 20, 150)["ok"])

    def test_auto_requires_commissioned_flag(self):
        env = {"ARM_MODE": "auto", "ARM_INSERT_STEPS": "1:-20:300", "ARM_AXIS1_LIMITS": "-100:0", "PH_CALIBRATION": "7:1500,4:1990"}
        arm, fw = self.make(env)
        arm.home()
        self.assertEqual(arm.summary(), "uncommissioned")
        for call in (lambda: arm.insert(background=False), lambda: arm.measure_ph(background=False)):
            with self.assertRaisesRegex(ArmError, "not commissioned"):
                call()
        self.assertFalse(any(" MOVE " in line for line in fw.log))

    def test_stop_always_allowed_and_disarms(self):
        arm, fw = self.make({"ARM_MODE": "disabled"})
        fw.armed = True
        arm.stop()
        self.assertFalse(fw.armed)


class SoilPreparationTests(unittest.TestCase):
    def test_missing_sensor_variants_are_distinguishable(self):
        none_ph = hub({}, None, None)
        reading = none_ph.read_all()["readings"]["soil"]
        self.assertEqual((reading["status"], reading["percent"]), ("placeholder", 48))
        self.assertIn("no soil sensor installed", reading["source"])

        no_placeholder = hub({"SOIL_PLACEHOLDER_PERCENT": "none"}, None, None)
        self.assertEqual(no_placeholder.read_all()["status"]["soilMoisture"], "not_installed")
        self.assertIsNone(no_placeholder.read_all()["sensors"]["soilMoisture"])

        offline = hub({"SOIL_SENSOR": "esp32", "SOIL_PLACEHOLDER_PERCENT": "none"}, FakeLink(connected=False), None)
        self.assertEqual(offline.read_all()["status"]["soilMoisture"], "unavailable")
        self.assertTrue(any("soil" in b for b in offline.publish_payload()[2]))

        offline_ph = hub({"SOIL_SENSOR": "esp32"}, FakeLink(connected=False), None)
        reading = offline_ph.read_all()["readings"]["soil"]
        self.assertEqual(reading["status"], "placeholder")  # sensor configured but down: still labelled
        self.assertIn("error", reading["source"])

        simulated = SensorHub(make_config({"SIMULATE_SENSORS": "true"}))
        self.assertEqual(simulated.read_all()["status"]["soilMoisture"], "simulated")

    def test_digital_without_pin_is_reported(self):
        cfg = make_config({"SOIL_SENSOR": "digital"})
        h = SensorHub(cfg)
        self.assertIn("SOIL_DIGITAL_PIN", h.read_all()["readings"]["soil"]["source"])

    def test_soil_calibration_math(self):
        h = hub({"SOIL_SENSOR": "esp32", "SOIL_DRY_VALUE": "2600", "SOIL_WET_VALUE": "1200"}, FakeLink(FakeFirmware(ph_mv=1900)), None)
        self.assertAlmostEqual(h.read_all()["readings"]["soil"]["percent"], 50.0, places=1)
        same = hub({"SOIL_SENSOR": "esp32", "SOIL_DRY_VALUE": "1000", "SOIL_WET_VALUE": "1000", "SOIL_PLACEHOLDER_PERCENT": "none"},
                   FakeLink(FakeFirmware(ph_mv=1900)), None)
        self.assertIn("must differ", same.read_all()["readings"]["soil"]["source"])

    def test_bad_soil_option_falls_back(self):
        cfg = make_config({"SOIL_SENSOR": "magic"})
        self.assertEqual(cfg.soil_sensor, "none")


class ManualPhEndpointTest(unittest.TestCase):
    def test_ph_sample_endpoint_no_motion(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer

        import agribot_service
        from test_http_api import build_bot

        bot = build_bot({"PH_CALIBRATION": "", "ARM_MODE": "disabled", "PH_SAMPLES": "2", "PH_SAMPLE_INTERVAL_SECONDS": "0.1"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), agribot_service.make_handler(bot))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/ph/sample"
        with urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=5) as response:
            data = json.loads(response.read())["data"]
        self.assertEqual((data["label"], data["method"]), ("uncalibrated", "manual"))
        self.assertIsNone(data["ph"])
        self.assertEqual(bot.sensors.read_all()["status"]["ph"], "uncalibrated")
        self.assertFalse(any(" MOVE " in line or " ARM 1" in line for line in bot.esp32.fw.log))


if __name__ == "__main__":
    unittest.main()

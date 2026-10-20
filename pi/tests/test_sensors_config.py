"""Configuration loading and sensor validity labelling (hardware mocked)."""

import time
import unittest
from unittest import mock

from agribot import config as config_module
from agribot import sensors as sensors_module
from agribot.sensors import SensorHub, parse_gpioinfo

from fakes import FakeFirmware, FakeLink, make_config

GPIOINFO_V2 = '''gpiochip0 - 54 lines:
\tline  23:\t"GPIO23"        \tinput
\tline  24:\t"GPIO24"        \toutput consumer="dc"
\tline  27:\t"GPIO27"        \tinput bias=pull-down edges=both consumer="lg"
'''
GPIOINFO_V1 = '''gpiochip0 - 54 lines:
\tline  23:      "GPIO23"       unused   input  active-high
\tline  24:      "GPIO24"         "dc"  output  active-high [used]
'''


class ConfigTests(unittest.TestCase):
    def test_safe_defaults(self):
        cfg = make_config()
        self.assertFalse(cfg.esp32_enabled)
        self.assertEqual(cfg.arm_mode, "disabled")
        self.assertEqual(cfg.ph_source, "none")
        self.assertEqual(cfg.soil_sensor, "none")
        self.assertEqual(cfg.dht_pin, 24)
        self.assertEqual(cfg.rain_do_pin, 27)
        self.assertEqual((cfg.motor_left_forward, cfg.motor_left_backward, cfg.motor_left_enable), (5, 6, 12))
        self.assertEqual((cfg.motor_right_forward, cfg.motor_right_backward, cfg.motor_right_enable), (16, 26, 13))
        self.assertTrue(cfg.drive_interlock)

    def test_bad_numbers_fall_back_with_warning(self):
        cfg = make_config({"AGRIBOT_PORT": "eighty", "DRIVE_MAX_SPEED": "5", "ARM_MODE": "yolo"})
        self.assertEqual(cfg.port, 8000)
        self.assertEqual(cfg.drive_max_speed, 1.0)
        self.assertEqual(cfg.arm_mode, "disabled")
        joined = " ".join(config_module.CONFIG_WARNINGS)
        self.assertIn("AGRIBOT_PORT", joined)
        self.assertIn("ARM_MODE", joined)

    def test_legacy_fake_ph_is_not_published(self):
        cfg = make_config({"USE_FAKE_PH": "true", "FAKE_PH": "6.8"})
        self.assertEqual(cfg.ph_source, "none")
        self.assertTrue(any("USE_FAKE_PH" in w for w in config_module.CONFIG_WARNINGS))

    def test_ph_source_follows_esp32(self):
        self.assertEqual(make_config({"ESP32_ENABLED": "true"}).ph_source, "esp32")
        self.assertEqual(make_config({"PH_SOURCE": "fixed"}).ph_source, "none")  # no fixed pH any more
        self.assertTrue(any("fixed" in w for w in config_module.CONFIG_WARNINGS))

    def test_soil_placeholder_sources(self):
        self.assertEqual(make_config().soil_placeholder, 48)  # legacy default kept
        self.assertEqual(make_config({"USE_FAKE_SOIL_MOISTURE": "true", "FAKE_SOIL_MOISTURE": "30"}).soil_placeholder, 30)
        self.assertIsNone(make_config({"USE_FAKE_SOIL_MOISTURE": "false"}).soil_placeholder)
        self.assertIsNone(make_config({"SOIL_PLACEHOLDER_PERCENT": "none"}).soil_placeholder)
        self.assertEqual(make_config({"SOIL_PLACEHOLDER_PERCENT": "150"}).soil_placeholder, 100)

    def test_pin_conflict_warning(self):
        make_config({"DRIVE_ENABLED": "true", "RAIN_ENABLED": "true", "RAIN_DO_PIN": "26"})
        self.assertTrue(any("GPIO26" in w for w in config_module.CONFIG_WARNINGS))

    def test_env_file_is_literal(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("# comment\nX_AGRIBOT_TEST_KEY='a$b#c=d'\n\nBROKEN LINE\n")
        try:
            os.environ.pop("X_AGRIBOT_TEST_KEY", None)
            from pathlib import Path

            self.assertTrue(config_module.load_env_file(Path(handle.name)))
            self.assertEqual(os.environ.pop("X_AGRIBOT_TEST_KEY"), "a$b#c=d")
        finally:
            os.unlink(handle.name)


class GpioInfoTests(unittest.TestCase):
    def test_parse_v2_and_v1(self):
        self.assertEqual(parse_gpioinfo(GPIOINFO_V2, 24), "dc")
        self.assertEqual(parse_gpioinfo(GPIOINFO_V2, 27), "lg")
        self.assertIsNone(parse_gpioinfo(GPIOINFO_V2, 23))
        self.assertIs(parse_gpioinfo(GPIOINFO_V2, 4), False)
        self.assertEqual(parse_gpioinfo(GPIOINFO_V1, 24), "dc")
        self.assertIsNone(parse_gpioinfo(GPIOINFO_V1, 23))

    def test_conflict_message_names_the_owner(self):
        with mock.patch.object(sensors_module, "gpio_consumer", return_value="dc"):
            self.assertIn("'dc'", sensors_module.gpio_conflict(24))
        with mock.patch.object(sensors_module, "gpio_consumer", return_value="lg"):
            self.assertIsNone(sensors_module.gpio_conflict(27))


class IioDiscoveryTests(unittest.TestCase):
    def test_overlay_device_names_are_found(self):
        import tempfile
        from pathlib import Path as P

        for name in ("dht11", "dht11@18", "dht11@4", "dht22"):
            with tempfile.TemporaryDirectory() as root:
                dev = P(root, "iio:device0")
                dev.mkdir()
                (dev / "name").write_text(name + "\n")
                with mock.patch.object(sensors_module.glob, "glob", return_value=[str(dev / "name")]):
                    found = sensors_module.IioDht.find()
                self.assertIsNotNone(found, name)
                self.assertEqual(found.dir, str(dev))
        with tempfile.TemporaryDirectory() as root:
            dev = P(root, "iio:device0")
            dev.mkdir()
            (dev / "name").write_text("ads1015\n")
            with mock.patch.object(sensors_module.glob, "glob", return_value=[str(dev / "name")]):
                self.assertIsNone(sensors_module.IioDht.find())


class FlakyDht:
    def __init__(self, values):
        self.values = list(values)

    def read(self):
        value = self.values.pop(0) if self.values else RuntimeError("timeout")
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        pass


class FakePin:
    def __init__(self, value):
        self.value = value

    def close(self):
        pass


def hub_with(dht_values=(), rain=1, env=None, **kwargs):
    cfg = make_config({"DHT_BACKEND": "auto", "RAIN_ENABLED": "true", **(env or {})})
    with mock.patch.object(SensorHub, "_open_hardware", lambda self: None):
        hub = SensorHub(cfg, **kwargs)
    hub._dht = FlakyDht(dht_values)
    hub._rain = FakePin(rain) if rain is not None else None
    hub.poll()
    return hub


class SensorTests(unittest.TestCase):
    def test_real_readings_are_published(self):
        hub = hub_with([(24.5, 61.0)], rain=1)
        data = hub.read_all()
        self.assertEqual(data["sensors"]["temperature"], 24.5)
        self.assertEqual(data["status"]["dht22"], "ok")
        self.assertEqual(data["sources"]["dht22"], "hardware")
        self.assertFalse(data["readings"]["rainfall"]["isWet"])
        sensors, labels, blockers = hub.publish_payload()
        self.assertEqual(blockers, [])
        self.assertEqual(sensors["soilMoisture"], 48)
        self.assertEqual(labels["soilMoisture"], "placeholder")  # labelled, never "ok"
        self.assertIsNone(sensors["ph"])

    def test_failed_dht_is_never_fabricated(self):
        hub = hub_with([RuntimeError("GPIO busy")])
        data = hub.read_all()
        self.assertIsNone(data["sensors"]["temperature"])
        self.assertEqual(data["status"]["dht22"], "unavailable")
        _, _, blockers = hub.publish_payload()
        self.assertTrue(any("temperature" in b for b in blockers))

    def test_busy_gpio_error_is_explained(self):
        with mock.patch.object(sensors_module, "gpio_consumer", return_value="dc"):
            hub = hub_with([RuntimeError("GPIO busy")])
        self.assertIn("'dc'", hub.read_all()["sources"]["dht22"])

    def test_stale_then_unavailable(self):
        hub = hub_with([(20.0, 50.0), RuntimeError("checksum")])
        hub.poll()
        data = hub.read_all()
        self.assertEqual(data["status"]["dht22"], "stale")
        self.assertEqual(data["sensors"]["temperature"], 20.0)
        self.assertEqual(hub.publish_payload()[2], [])  # recent stale value is acceptable
        last = hub._last_good_dht
        hub._last_good_dht = (*last[:3], last[3] - 10_000)
        hub._dht.values = [RuntimeError("checksum")]
        hub.poll()
        self.assertEqual(hub.read_all()["status"]["dht22"], "unavailable")

    def test_out_of_range_dht_rejected(self):
        hub = hub_with([(-300.0, 50.0)])
        self.assertEqual(hub.read_all()["status"]["dht22"], "unavailable")

    def test_simulated_mode_is_labelled(self):
        cfg = make_config({"SIMULATE_SENSORS": "true"})
        hub = SensorHub(cfg)
        data = hub.read_all()
        self.assertEqual(hub.mode, "simulated")
        self.assertEqual(data["status"]["dht22"], "simulated")
        self.assertEqual(data["sources"]["rainfall"], "simulated")
        self.assertEqual(hub.publish_payload()[2], [])  # explicit demo mode may publish

    def test_missing_rain_sensor_blocks_publish(self):
        hub = hub_with([(20.0, 50.0)], rain=None)
        self.assertTrue(any("rain" in b for b in hub.publish_payload()[2]))

    def test_no_placeholder_blocks_publish(self):
        hub = hub_with([(20.0, 50.0)], env={"USE_FAKE_SOIL_MOISTURE": "false"})
        self.assertEqual(hub.read_all()["status"]["soilMoisture"], "not_installed")
        self.assertTrue(any("soil" in b for b in hub.publish_payload()[2]))

    def test_esp32_soil_sensor(self):
        link = FakeLink(FakeFirmware(ph_mv=1500))
        hub = hub_with([(20.0, 50.0)], env={"SOIL_SENSOR": "esp32", "SOIL_DRY_VALUE": "2500", "SOIL_WET_VALUE": "1000"}, link=link)
        reading = hub.read_all()["readings"]["soil"]
        self.assertEqual(reading["status"], "ok")
        self.assertAlmostEqual(reading["percent"], 66.7, places=1)

    def test_ph_from_probe(self):
        from agribot.ph import PhProbe

        cfg_env = {"ESP32_ENABLED": "true", "PH_CALIBRATION": "7:1500,4:1990"}
        link = FakeLink()
        probe = PhProbe(make_config(cfg_env), link)
        hub = hub_with([(20.0, 50.0)], env=cfg_env, ph=probe, link=link)
        self.assertIsNone(hub.read_all()["sensors"]["ph"])
        probe.measure(1)
        self.assertAlmostEqual(hub.read_all()["sensors"]["ph"], 7.0, places=1)
        self.assertAlmostEqual(hub.publish_payload()[0]["ph"], 7.0, places=1)

    def test_read_one_legacy_shape(self):
        hub = hub_with([(21.0, 40.0)])
        one = hub.read_one("dht22")
        self.assertEqual(one["sensor"], "dht22")
        self.assertEqual(one["reading"]["temperatureC"], 21.0)
        with self.assertRaises(KeyError):
            hub.read_one("nope")

    def test_cache_is_fast_even_if_dht_hangs(self):
        hub = hub_with([(21.0, 40.0)])
        started = time.monotonic()
        for _ in range(50):
            hub.read_all()
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()

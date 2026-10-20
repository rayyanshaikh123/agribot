"""SENSOR_DEMO_MODE: opt-in, clearly labelled, never actionable, contract-compatible."""

import time
import unittest
from unittest import mock

from agribot import config as config_module
from agribot.demo import DEMO_ROBOT_STATUS, DemoClimate
from agribot.sensors import SensorHub

from fakes import make_config

DEMO = {"SENSOR_DEMO_MODE": "true", "DHT_BACKEND": "none"}


class Pin:
    value = 1

    def close(self):
        pass


def hub(env, dht=None):
    cfg = make_config({"RAIN_ENABLED": "true", **env})
    with mock.patch.object(SensorHub, "_open_hardware", lambda self: None):
        h = SensorHub(cfg)
    h._dht, h._rain = dht, Pin()
    h.poll()
    return h


class FailingDht:
    def read(self):
        raise RuntimeError("DHT sensor not found, check wiring")

    def close(self):
        pass


class GoodDht:
    def read(self):
        return 23.4, 58.0

    def close(self):
        pass


class DemoModeTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(make_config().sensor_demo_mode)
        h = hub({"DHT_BACKEND": "none"})
        data = h.read_all()
        self.assertIsNone(data["sensors"]["temperature"])
        self.assertEqual(data["status"]["dht22"], "disabled")
        self.assertFalse(data["demoMode"])
        self.assertEqual(h.mode, "hardware")
        self.assertTrue(any("temperature" in b for b in h.publish_payload()[2]))

    def test_demo_values_are_labelled_simulated(self):
        h = hub(DEMO)
        data = h.read_all()
        reading = data["readings"]["dht22"]
        self.assertEqual(reading["status"], "simulated")
        self.assertTrue(reading["demo"])
        self.assertFalse(reading["actionable"])
        self.assertIn("not a measurement", reading["source"])
        self.assertEqual(data["status"]["dht22"], "simulated")
        self.assertIn("simulated", data["sources"]["dht22"])
        self.assertTrue(data["demoMode"])
        self.assertEqual(h.mode, "demo (simulated climate)")  # shown in the app as "Sensors: demo ..."
        self.assertIsInstance(data["sensors"]["temperature"], float)  # contract: plain numbers
        self.assertIsInstance(data["sensors"]["humidity"], float)
        sensors, labels, blockers = h.publish_payload()
        self.assertEqual(blockers, [])
        self.assertEqual(labels["dht22"], "simulated")

    def test_demo_requires_dht_backend_none(self):
        env = {"SENSOR_DEMO_MODE": "true", "DHT_BACKEND": "auto"}
        cfg = make_config(env)
        self.assertTrue(any("SENSOR_DEMO_MODE is ignored" in w for w in config_module.CONFIG_WARNINGS))
        real = hub(env, dht=GoodDht())
        self.assertIsNone(real.demo)
        self.assertEqual(real.read_all()["status"]["dht22"], "ok")
        self.assertEqual(real.read_all()["sensors"]["temperature"], 23.4)
        failing = hub(env, dht=FailingDht())  # a broken real sensor is never papered over
        self.assertEqual(failing.read_all()["status"]["dht22"], "unavailable")
        self.assertIsNone(failing.read_all()["sensors"]["temperature"])
        self.assertTrue(cfg.dht_backend == "auto")

    def test_real_driver_path_untouched_when_demo_off(self):
        h = hub({"DHT_BACKEND": "auto"}, dht=GoodDht())
        reading = h.read_all()["readings"]["dht22"]
        self.assertEqual((reading["status"], reading["source"], reading["actionable"]), ("ok", "hardware", True))
        self.assertNotIn("demo", reading)

    def test_actionable_excludes_everything_not_measured(self):
        h = hub(DEMO)
        values = h.actionable()
        self.assertIsNone(values["temperature"])
        self.assertIsNone(values["humidity"])
        self.assertIsNone(values["soilMoisture"])  # placeholder 48
        self.assertIsNone(values["ph"])
        self.assertEqual(values["rainfall"], 0)  # the real rain pin is still actionable
        real = hub({"DHT_BACKEND": "auto"}, dht=GoodDht())
        self.assertEqual(real.actionable()["temperature"], 23.4)

    def test_simulate_sensors_also_not_actionable(self):
        h = SensorHub(make_config({"SIMULATE_SENSORS": "true"}))
        self.assertTrue(all(v is None for v in h.actionable().values()))

    def test_demo_climate_is_plausible_over_a_day(self):
        climate = DemoClimate(seed=1)
        start = time.mktime((2026, 10, 21, 0, 0, 0, 0, 0, -1))
        temps, hums = [], []
        for minute in range(0, 24 * 60, 10):
            t, h = climate.read(start + minute * 60)
            temps.append(t)
            hums.append(h)
        self.assertTrue(all(18 <= t <= 36 for t in temps))
        self.assertTrue(all(20 <= h <= 98 for h in hums))
        self.assertGreater(max(temps) - min(temps), 4)  # follows a daily curve
        afternoon = climate.read(start + 15 * 3600)[0]
        dawn = climate.read(start + 3 * 3600)[0]
        self.assertGreater(afternoon, dawn)


class DemoServiceTests(unittest.TestCase):
    def _published(self, env):
        from test_http_api import build_bot

        bot = build_bot({"RAIN_ENABLED": "true", **env})
        bot.sensors._rain = Pin()
        bot.sensors.poll()
        sent = []
        bot.backend._key = "k"
        bot.backend._url_provider = lambda: "http://backend.invalid"
        with mock.patch.object(bot.backend, "request", side_effect=lambda m, path, body=None, timeout=10: sent.append((path, body)) or {}):
            bot.publish_reading()
        return bot, sent[0]

    def test_published_payload_keeps_contract_and_marks_demo(self):
        bot, (path, body) = self._published(DEMO)
        self.assertEqual(path, "/api/robot/data")
        self.assertEqual(set(body), {"sensors", "robot", "location"})
        self.assertEqual(set(body["sensors"]), {"soilMoisture", "temperature", "humidity", "rainfall", "ph"})
        for key in ("soilMoisture", "temperature", "humidity", "rainfall"):
            self.assertIsInstance(body["sensors"][key], (int, float))  # backend validation
        self.assertIsNone(body["sensors"]["ph"])
        self.assertEqual(body["robot"]["status"], DEMO_ROBOT_STATUS)  # stored in MongoDB with the reading
        status = bot.status()
        self.assertTrue(status["demoMode"])
        self.assertEqual(status["capabilities"]["sensors"], "demo (simulated climate)")
        self.assertIsInstance(status["capabilities"]["sensors"], str)  # app reads it as a String

    def test_demo_does_not_touch_actuators(self):
        bot, _ = self._published(DEMO)
        self.assertFalse(bot.drive.moving)
        self.assertEqual(bot.arm.operation, None)
        self.assertFalse(any(" MOVE " in line or " ARM 1" in line or " RAW " in line for line in bot.esp32.fw.log))


if __name__ == "__main__":
    unittest.main()

"""Demo data for temperature/humidity while no working DHT22 is available.

Opt-in only: SENSOR_DEMO_MODE=true AND DHT_BACKEND=none. This is a separate
data source; the real DHT22 readers in sensors.py are never altered by it.

Every value produced here is labelled status="simulated", demo=True and
actionable=False. Simulated values may be shown and published (so the app and
dashboard keep working for a demonstration), but they must never drive an
actuator or automation decision: use SensorHub.actionable() for that, which
excludes them.
"""

import math
import random
import time

DEMO_SOURCE = "simulated (SENSOR_DEMO_MODE=true demo data, not a measurement)"
# Robot status string stored with each published reading while demo data is in
# it, so the records in MongoDB stay identifiable as simulated.
DEMO_ROBOT_STATUS = "online (demo: simulated temperature/humidity)"


class DemoClimate:
    """Plausible field climate: a daily temperature curve (cool at dawn, warm
    mid-afternoon), humidity moving the opposite way, plus slow noise."""

    def __init__(self, base_temp=27.0, temp_swing=4.0, base_humidity=65.0, humidity_swing=12.0, seed=None):
        self.base_temp = base_temp
        self.temp_swing = temp_swing
        self.base_humidity = base_humidity
        self.humidity_swing = humidity_swing
        self._rng = random.Random(seed)
        self._noise_t = 0.0
        self._noise_h = 0.0

    def read(self, now=None):
        local = time.localtime(now if now is not None else time.time())
        hour = local.tm_hour + local.tm_min / 60
        # Peak around 15:00, minimum around 03:00.
        phase = math.cos((hour - 15.0) / 24.0 * 2 * math.pi)
        self._noise_t = max(-0.8, min(0.8, self._noise_t + self._rng.uniform(-0.15, 0.15)))
        self._noise_h = max(-3.0, min(3.0, self._noise_h + self._rng.uniform(-0.6, 0.6)))
        temperature = self.base_temp + self.temp_swing * phase + self._noise_t
        humidity = self.base_humidity - self.humidity_swing * phase + self._noise_h
        return round(temperature, 1), round(max(20.0, min(98.0, humidity)), 1)

"""Field sensors: DHT22 and FC-37 rain module on the Pi, pH and (optionally)
soil moisture through the ESP32.

A background loop reads the hardware every SENSOR_READ_SECONDS and caches the
results, so HTTP requests never wait on a slow DHT22 read. Every reading has a
status:

    ok             fresh hardware measurement
    stale          last good hardware value, older than one read cycle but
                   younger than DHT_STALE_SECONDS (the current read failed)
    unavailable    no trustworthy value (value is None, error says why)
    not_installed  no sensor configured (soil moisture today)
    placeholder    configured stand-in value for a sensor that does not exist
    simulated      SIMULATE_SENSORS=true demo values (no hardware involved), or
                   SENSOR_DEMO_MODE=true demo climate data (agribot/demo.py)
    uncalibrated   (pH only) voltage and optional provisional estimate; value
                   stays None and is never published

Simulated and placeholder values are always labelled as such. Values that are
neither real nor explicitly configured stand-ins are never sent to the backend.
Only real hardware readings are "actionable" (SensorHub.actionable()); anything
that could drive an actuator or automation must use that, never read_all().
"""

import glob
import os
import random
import shutil
import subprocess
import threading
import time

# Plausible DHT22 output ranges (datasheet: -40..80 C, 0..100 %RH).
DHT_TEMP_RANGE = (-40.0, 80.0)
DHT_HUMIDITY_RANGE = (0.0, 100.0)
# Reading statuses that come from real hardware and may inform decisions.
ACTIONABLE_STATUSES = {"ok", "stale"}
# Consumers that mean "this process owns the line" in gpioinfo output.
OWN_GPIO_CONSUMERS = {"lg", "lgpio", "gpiozero", "agribot", "dht11"}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(value, high))


def gpio_consumer(pin, chip="gpiochip0"):
    """Name of whatever holds a header GPIO (kernel driver or process), or None.

    Used only to explain failures such as lgpio's 'GPIO busy'."""
    tool = shutil.which("gpioinfo")
    if not tool:
        return None
    for args in ([tool, "-c", chip], [tool, chip]):
        try:
            output = subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        owner = parse_gpioinfo(output, pin)
        if owner is not False:
            return owner
    return None


def parse_gpioinfo(output, pin):
    """Consumer of `pin` in gpioinfo output (libgpiod v1 or v2), None if free,
    False if the pin is not listed."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.split(":")[0].split() != ["line", str(pin)]:
            continue
        if 'consumer="' in stripped:  # v2: line 24: "GPIO24" output consumer="dc"
            return stripped.split('consumer="', 1)[1].split('"', 1)[0]
        quoted = stripped.split('"')[1::2]  # v1: line 24: "GPIO24" "dc" output [used]
        if len(quoted) >= 2 and quoted[1] != "unused":
            return quoted[1]
        return None
    return False


def gpio_conflict(pin):
    """Human readable reason a pin is taken by something else, or None."""
    owner = gpio_consumer(pin)
    if owner and owner.lower() not in OWN_GPIO_CONSUMERS:
        return (f"GPIO{pin} is already claimed by '{owner}' (another driver or program). "
                f"On this Pi the SPI LCD overlay in /boot/firmware/config.txt uses GPIO24 as its 'dc' line: "
                f"move the sensor to a free GPIO or disable the overlay")
    return None


# --- DHT22 readers ---------------------------------------------------------------
class IioDht:
    """Kernel driver (dtoverlay=dht11,gpiopin=N works for the DHT22 too).

    Timing is done in the kernel, which is far more reliable on the Pi 5 than
    bit-banging from Python."""

    name = "iio"

    def __init__(self, device_dir):
        self.dir = device_dir

    @staticmethod
    def find():
        for name_file in sorted(glob.glob("/sys/bus/iio/devices/iio:device*/name")):
            try:
                with open(name_file) as handle:
                    # Overlay-created devices are named after the DT node, e.g. "dht11@18".
                    if handle.read().strip().split("@")[0] in {"dht11", "dht22"}:
                        return IioDht(os.path.dirname(name_file))
            except OSError:
                continue
        return None

    def read(self):
        with open(os.path.join(self.dir, "in_temp_input")) as handle:
            temperature = int(handle.read().strip()) / 1000
        with open(os.path.join(self.dir, "in_humidityrelative_input")) as handle:
            humidity = int(handle.read().strip()) / 1000
        return temperature, humidity

    def close(self):
        pass


class AdafruitDht:
    """adafruit-circuitpython-dht bit-banging through Blinka/lgpio."""

    name = "adafruit"

    def __init__(self, pin):
        import adafruit_dht
        import board

        self._device = adafruit_dht.DHT22(getattr(board, f"D{pin}"), use_pulseio=False)

    def read(self):
        temperature, humidity = self._device.temperature, self._device.humidity
        if temperature is None or humidity is None:
            raise RuntimeError("DHT22 returned no data")
        return float(temperature), float(humidity)

    def close(self):
        exit_ = getattr(self._device, "exit", None)
        if exit_:
            exit_()


def _validate_dht(temperature, humidity):
    if not DHT_TEMP_RANGE[0] <= temperature <= DHT_TEMP_RANGE[1]:
        raise ValueError(f"temperature {temperature} C out of range")
    if not DHT_HUMIDITY_RANGE[0] <= humidity <= DHT_HUMIDITY_RANGE[1]:
        raise ValueError(f"humidity {humidity} % out of range")
    return round(temperature, 1), round(humidity, 1)


class SensorHub:
    def __init__(self, config, ph=None, link=None):
        self.cfg = config
        self.ph = ph  # agribot.ph.PhProbe (optional)
        self.link = link  # agribot.esp32.Esp32Link (optional, for SOIL_SENSOR=esp32)
        self.simulated = config.simulate_sensors
        # Demo climate only with an explicit opt-in AND the real DHT22 switched off.
        self.demo = None
        if config.sensor_demo_mode and config.dht_backend == "none" and not self.simulated:
            from agribot.demo import DemoClimate

            self.demo = DemoClimate()
            print("[sensors] DEMO MODE: temperature/humidity are SIMULATED (SENSOR_DEMO_MODE=true, DHT_BACKEND=none). "
                  "They are labelled 'simulated', not measurements, and never used for automation.", flush=True)
        self._lock = threading.Lock()
        self._dht = self._rain = self._soil = None
        self._init_errors = {}
        self._last_good_dht = None  # (temperature, humidity, at_iso, monotonic)
        self._walk = {"soil": 48.0, "temp": 27.0, "hum": 62.0}
        self._cache = {}
        self._logged = {}
        if not self.simulated:
            self._open_hardware()
        if config.soil_sensor == "none":
            if config.soil_placeholder is not None:
                print(f"[sensors] Soil moisture: no sensor installed. The backend receives a PLACEHOLDER of "
                      f"{config.soil_placeholder:g} % (labelled 'placeholder'; not a measurement)", flush=True)
            else:
                print("[sensors] Soil moisture: no sensor installed and no placeholder; readings will not be published", flush=True)
        self.poll()  # first values before the loop starts

    # --- setup ----------------------------------------------------------------
    def _open_hardware(self):
        cfg = self.cfg
        if cfg.dht_backend != "none":
            try:
                self._dht = self._open_dht()
            except Exception as error:
                self._init_errors["dht22"] = str(error)
                self._log("dht22", f"DHT22 unavailable: {error}")
        if cfg.rain_enabled:
            try:
                from gpiozero import DigitalInputDevice

                self._rain = DigitalInputDevice(cfg.rain_do_pin, pull_up=False)
            except Exception as error:
                self._init_errors["rain"] = f"rain sensor GPIO{cfg.rain_do_pin} unavailable: {error}"
                self._log("rain", self._init_errors["rain"])
        try:
            if cfg.soil_sensor == "digital":
                if cfg.soil_digital_pin is None:
                    raise ValueError("SOIL_SENSOR=digital needs SOIL_DIGITAL_PIN")
                from gpiozero import DigitalInputDevice

                self._soil = DigitalInputDevice(cfg.soil_digital_pin, pull_up=False)
            elif cfg.soil_sensor == "mcp3008":
                from gpiozero import MCP3008

                self._soil = MCP3008(channel=cfg.soil_adc_channel)
        except Exception as error:
            self._init_errors["soil"] = f"soil sensor ({cfg.soil_sensor}) unavailable: {error}"
            self._log("soil", self._init_errors["soil"])

    def _open_dht(self):
        backend = self.cfg.dht_backend
        if backend in {"auto", "iio"}:
            device = IioDht.find()
            if device:
                print("[sensors] DHT22 via kernel IIO driver")
                return device
            if backend == "iio":
                raise RuntimeError("no dht11/dht22 IIO device: add dtoverlay=dht11,gpiopin=<pin> to /boot/firmware/config.txt and reboot")
        conflict = gpio_conflict(self.cfg.dht_pin)
        if conflict:
            raise RuntimeError(conflict)
        return AdafruitDht(self.cfg.dht_pin)

    @property
    def mode(self):
        if self.simulated:
            return "simulated"
        return "demo (simulated climate)" if self.demo else "hardware"

    @property
    def error(self):
        problems = [f"{name}: {r['error']}" for name, r in self.snapshot().items() if r.get("status") == "unavailable" and r.get("error")]
        return "; ".join(problems) or None

    def _log(self, key, message):
        """Print a message only when it changes, so a dead sensor does not flood the journal."""
        if self._logged.get(key) != message:
            self._logged[key] = message
            print(f"[sensors] {message}", flush=True)

    # --- individual sensors ---------------------------------------------------
    def _drift(self, key, step, low, high):
        self._walk[key] = _clamp(self._walk[key] + random.uniform(-step, step), low, high)
        return round(self._walk[key], 1)

    def _read_dht(self):
        at = now_iso()
        if self.simulated:
            return {"temperatureC": self._drift("temp", 0.4, 18, 38), "humidity": self._drift("hum", 1.5, 35, 95),
                    "status": "simulated", "source": "simulated", "at": at, "error": None}
        if self._dht is None and self.demo is not None:
            from agribot.demo import DEMO_SOURCE

            temperature, humidity = self.demo.read()
            return {"temperatureC": temperature, "humidity": humidity, "status": "simulated", "demo": True,
                    "source": DEMO_SOURCE, "at": at, "error": None}
        if self._dht is None:
            error = self._init_errors.get("dht22", "disabled by DHT_BACKEND=none")
            return {"temperatureC": None, "humidity": None, "status": "unavailable" if self.cfg.dht_backend != "none" else "disabled",
                    "source": f"unavailable: {error}", "at": at, "error": error}
        try:
            temperature, humidity = _validate_dht(*self._dht.read())
        except Exception as error:  # timeouts, checksum errors, "GPIO busy", out-of-range values
            message = str(error) or type(error).__name__
            if "busy" in message.lower():
                message = gpio_conflict(self.cfg.dht_pin) or f"GPIO{self.cfg.dht_pin} busy (used by another program?)"
            last = self._last_good_dht
            if last and time.monotonic() - last[3] <= self.cfg.dht_stale_seconds:
                age = int(time.monotonic() - last[3])
                return {"temperatureC": last[0], "humidity": last[1], "status": "stale", "at": last[2], "error": message,
                        "source": f"stale ({age} s old; DHT22 error: {message})"}
            self._log("dht22", f"DHT22 read failed: {message}")
            return {"temperatureC": None, "humidity": None, "status": "unavailable", "at": at, "error": message,
                    "source": f"unavailable: {message}"}
        self._last_good_dht = (temperature, humidity, at, time.monotonic())
        if self._logged.get("dht22"):
            self._log("dht22", "DHT22 reading again")
        return {"temperatureC": temperature, "humidity": humidity, "status": "ok", "source": "hardware", "at": at, "error": None}

    def _read_rain(self):
        at = now_iso()
        if self.simulated:
            return {"isWet": False, "digital": None, "percent": 0, "status": "simulated", "source": "simulated", "at": at, "error": None}
        if self._rain is None:
            error = self._init_errors.get("rain", "disabled by RAIN_ENABLED=false")
            return {"isWet": None, "digital": None, "percent": None, "status": "unavailable" if self.cfg.rain_enabled else "disabled",
                    "source": f"unavailable: {error}", "at": at, "error": error}
        try:
            digital = int(self._rain.value)
        except Exception as error:
            return {"isWet": None, "digital": None, "percent": None, "status": "unavailable", "source": f"unavailable: {error}", "at": at, "error": str(error)}
        wet = digital == self.cfg.rain_wet_value
        return {"isWet": wet, "digital": digital, "percent": 100 if wet else 0, "status": "ok", "source": "hardware", "at": at, "error": None}

    def _soil_percent(self, raw):
        dry, wet = self.cfg.soil_dry_value, self.cfg.soil_wet_value
        if dry == wet:
            raise ValueError("SOIL_DRY_VALUE and SOIL_WET_VALUE must differ")
        return round(_clamp((raw - dry) * 100 / (wet - dry)), 1)

    def _read_soil(self):
        at = now_iso()
        cfg = self.cfg
        if self.simulated:
            return {"percent": self._drift("soil", 1.2, 20, 85), "raw": None, "status": "simulated", "source": "simulated", "at": at, "error": None}
        try:
            if cfg.soil_sensor == "esp32":
                if not (self.link and self.link.connected):
                    raise RuntimeError("ESP32 not connected")
                reply = self.link.request("SOIL", 16, timeout=2.0)
                raw = float(reply["mv"])
                return {"percent": self._soil_percent(raw), "raw": round(raw, 1), "status": "ok", "source": "hardware (ESP32 ADC, mV)", "at": at, "error": None}
            if cfg.soil_sensor in {"digital", "mcp3008"}:
                if self._soil is None:
                    raise RuntimeError(self._init_errors.get("soil", "soil sensor not opened"))
                if cfg.soil_sensor == "digital":
                    digital = int(self._soil.value)
                    percent = 100.0 if digital == cfg.soil_wet_digital_value else 0.0
                    return {"percent": percent, "raw": digital, "status": "ok", "source": "hardware (digital wet/dry)", "at": at, "error": None}
                raw = float(self._soil.value)
                return {"percent": self._soil_percent(raw), "raw": round(raw, 4), "status": "ok", "source": "hardware", "at": at, "error": None}
        except Exception as error:
            self._log("soil", f"Soil sensor read failed: {error}")
            if cfg.soil_placeholder is not None:
                return {"percent": cfg.soil_placeholder, "raw": None, "status": "placeholder", "at": at, "error": str(error),
                        "source": f"placeholder (soil sensor error: {error})"}
            return {"percent": None, "raw": None, "status": "unavailable", "source": f"unavailable: {error}", "at": at, "error": str(error)}
        # SOIL_SENSOR=none: the probe is not installed yet.
        if cfg.soil_placeholder is not None:
            return {"percent": cfg.soil_placeholder, "raw": None, "status": "placeholder", "at": at, "error": None,
                    "source": "placeholder (no soil sensor installed)"}
        return {"percent": None, "raw": None, "status": "not_installed", "source": "not installed", "at": at, "error": None}

    def _read_ph(self):
        at = now_iso()
        if self.cfg.ph_source == "none" or self.ph is None:
            return {"value": None, "status": "disabled", "source": "not connected (PH_SOURCE=none)", "at": at, "error": None}
        latest = self.ph.latest()
        status = latest.get("status", "unavailable")
        return {**latest, "status": status, "at": latest.get("at", at), "error": None if status == "ok" else latest.get("source")}

    # --- acquisition loop -------------------------------------------------------
    def poll(self):
        """Read every Pi-side sensor once and refresh the cache."""
        readings = {"dht22": self._read_dht(), "rainfall": self._read_rain(), "soil": self._read_soil()}
        stamp = time.monotonic()
        with self._lock:
            for name, reading in readings.items():
                self._cache[name] = {**reading, "_mono": stamp}

    def start(self, stop):
        def loop():
            while not stop.wait(self.cfg.sensor_interval):
                try:
                    self.poll()
                except Exception as error:  # a sensor bug must not end acquisition
                    self._log("loop", f"Sensor loop error: {error!r}")

        threading.Thread(target=loop, name="sensors", daemon=True).start()

    def snapshot(self):
        """Cached readings plus the current pH, with ages, keyed by sensor."""
        now = time.monotonic()
        with self._lock:
            cached = {name: dict(r) for name, r in self._cache.items()}
        for reading in cached.values():
            reading["ageSeconds"] = round(now - reading.pop("_mono"), 1)
            # A cache entry that stopped updating (loop stuck) is not "ok" any more.
            if reading["status"] == "ok" and reading["ageSeconds"] > max(30, 4 * self.cfg.sensor_interval):
                reading["status"] = "stale"
        cached["ph"] = self._read_ph()
        for reading in cached.values():
            reading["actionable"] = reading.get("status") in ACTIONABLE_STATUSES and not reading.get("demo")
        return cached

    def actionable(self):
        """Values safe to base real-world actions on (watering, arm, alerts...).

        Only real hardware readings (ok/stale). Simulated, demo, placeholder,
        uncalibrated and unavailable values come back as None, so automation
        can never act on invented data."""
        snap = self.snapshot()
        def pick(name, key):
            reading = snap[name]
            return reading.get(key) if reading["actionable"] else None
        return {
            "temperature": pick("dht22", "temperatureC"),
            "humidity": pick("dht22", "humidity"),
            "rainfall": pick("rainfall", "percent"),
            "soilMoisture": pick("soil", "percent"),
            "ph": pick("ph", "value"),
        }

    # --- public API (shapes kept compatible with the app and earlier versions) ----
    def read_one(self, name):
        readers = {"soil-moisture": "soil", "dht22": "dht22", "rainfall": "rainfall", "ph": "ph"}
        if name not in readers:
            raise KeyError("Unknown sensor. Use soil-moisture, dht22, rainfall or ph.")
        return {"sensor": name, "reading": self.snapshot()[readers[name]], "timestamp": now_iso()}

    def read_all(self):
        snap = self.snapshot()
        climate, rain, soil, ph = snap["dht22"], snap["rainfall"], snap["soil"], snap["ph"]
        return {
            "sensors": {
                "soilMoisture": soil["percent"],
                "temperature": climate["temperatureC"],
                "humidity": climate["humidity"],
                "rainfall": rain["percent"],
                "ph": ph.get("value"),
            },
            "sources": {"soilMoisture": soil["source"], "dht22": climate["source"], "rainfall": rain["source"], "ph": ph["source"]},
            "status": {"soilMoisture": soil["status"], "dht22": climate["status"], "rainfall": rain["status"], "ph": ph["status"]},
            "readings": snap,
            "demoMode": self.demo is not None,
            "timestamp": now_iso(),
        }

    def publish_payload(self):
        """Sensor block for POST /api/robot/data, plus reasons it cannot be sent.

        The backend requires numbers for soilMoisture, temperature, humidity and
        rainfall (pH may be null). A field is only filled from a real reading
        (ok/stale), an explicit SIMULATE_SENSORS=true run, or the configured soil
        placeholder; otherwise the publish is blocked rather than invented."""
        allowed = {"ok", "stale", "simulated"}
        snap = self.snapshot()
        climate, rain, soil, ph = snap["dht22"], snap["rainfall"], snap["soil"], snap["ph"]
        blockers = []
        if climate["status"] not in allowed:
            blockers.append(f"temperature/humidity {climate['status']}: {climate.get('error') or 'no reading'}")
        if rain["status"] not in allowed:
            blockers.append(f"rain {rain['status']}: {rain.get('error') or 'no reading'}")
        if soil["status"] not in allowed | {"placeholder"}:
            blockers.append(f"soil moisture {soil['status']} and no SOIL_PLACEHOLDER_PERCENT set")
        sensors = {
            "soilMoisture": soil["percent"],
            "temperature": climate["temperatureC"],
            "humidity": climate["humidity"],
            "rainfall": rain["percent"],
            "ph": ph.get("value") if ph["status"] == "ok" else None,  # calibrated only
        }
        labels = {"soilMoisture": soil["status"], "dht22": climate["status"], "rainfall": rain["status"], "ph": ph["status"]}
        return sensors, labels, blockers

    def close(self):
        for device in (self._dht, self._soil, self._rain):
            if device is None:
                continue
            try:
                device.close()
            except Exception:
                pass

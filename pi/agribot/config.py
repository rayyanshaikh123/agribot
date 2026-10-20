"""Configuration loaded once from environment variables (see agribot.env.example).

A malformed value never stops the service: it falls back to the default and is
listed in CONFIG_WARNINGS (shown in GET /status), so a typo in /etc/agribot.env
cannot leave the robot without its safety loops.
"""

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PI_ROOT.parent

# Problems found while parsing the environment (never contains values).
CONFIG_WARNINGS = []


def _env(name, default=""):
    return os.getenv(name, default).strip()


def _flag(name, default):
    return _env(name, default).lower() in {"1", "true", "yes", "on"}


def _number(name, default, cast):
    raw = _env(name, str(default))
    try:
        return cast(raw)
    except ValueError:
        CONFIG_WARNINGS.append(f"{name} is not a valid number; using default {default}")
        return cast(default)


def _float(name, default):
    return _number(name, default, float)


def _int(name, default):
    return _number(name, default, int)


def _pin(name, default):
    value = _env(name, str(default))
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        CONFIG_WARNINGS.append(f"{name} is not a valid GPIO number; using default {default or 'none'}")
        return int(default) if str(default).strip() else None


def _choice(name, default, choices):
    value = _env(name, default).lower()
    if value not in choices:
        CONFIG_WARNINGS.append(f"{name} must be one of {', '.join(choices)}; using {default}")
        return default
    return value


def _clamped(value, low, high):
    return max(low, min(high, value))


def _path(name, default):
    path = Path(_env(name, default)).expanduser()
    return path if path.is_absolute() else (PI_ROOT / path).resolve()


def _unquote(value):
    """Strip one pair of matching quotes; everything else is kept literally."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_env_file(path):
    """Read KEY=VALUE lines literally (no $ expansion or escapes), so secrets
    may contain any characters. Real environment variables take precedence."""
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _unquote(value.strip()))
    return True


def env_file_candidates():
    explicit = os.getenv("AGRIBOT_ENV_FILE")
    return [Path(explicit)] if explicit else [Path("/etc/agribot.env"), PI_ROOT / "agribot.env"]


@dataclass(frozen=True)
class Config:
    # Local HTTP API (diagnostics, app driving, arm commissioning).
    host: str
    port: int
    local_api_token: str  # required for arm motion from non-loopback clients
    # Backend connection.
    backend_url: str
    robot_key: str
    latitude: float
    longitude: float
    publish_interval: int
    heartbeat_interval: int
    state_file: Path
    # Pi sensors.
    simulate_sensors: bool  # SIMULATE_SENSORS=true: labelled demo values, no hardware
    sensor_interval: float
    dht_pin: int
    dht_backend: str  # auto | iio | adafruit | none
    dht_stale_seconds: int
    sensor_demo_mode: bool  # SENSOR_DEMO_MODE=true + DHT_BACKEND=none: labelled demo climate data
    rain_enabled: bool
    rain_do_pin: int
    rain_wet_value: int
    # Soil moisture (not installed yet): none | digital | mcp3008 | esp32
    soil_sensor: str
    soil_adc_channel: int
    soil_dry_value: float  # mcp3008: 0..1 fraction; esp32: millivolts
    soil_wet_value: float
    soil_digital_pin: int | None
    soil_wet_digital_value: int
    soil_placeholder: float | None  # sent to the backend (which needs a number) while no sensor exists
    # pH probe (read by the ESP32): esp32 | none
    ph_source: str
    ph_calibration: str  # "pH:mV,pH:mV[,...]" measured at the ESP32 pin
    # Optional provisional conversion copied from the board's documentation
    # (used only for a labelled, never-published "uncalibrated" estimate).
    ph_provisional_neutral_mv: float | None  # mV at the ESP32 pin for pH 7
    ph_provisional_mv_per_ph: float | None  # signed mV change per +1 pH
    ph_max_age_seconds: int
    ph_settle_seconds: float
    ph_samples: int
    ph_sample_interval: float
    # ESP32 co-processor (USB serial): pH ADC + probe arm.
    esp32_enabled: bool
    esp32_port: str
    esp32_baud: int
    # Probe arm (two continuous-rotation servos on the ESP32).
    arm_mode: str  # disabled | manual | auto
    arm_commissioned: bool  # operator sign-off after physical verification
    arm_servo1_stop: int  # provisional neutral values (servo "angle" units)
    arm_servo2_stop: int
    arm_servo1_dir: int  # +1 / -1: which servo direction is "positive" travel
    arm_servo2_dir: int
    arm_jog_max_ms: int  # manual jog duration cap (firmware caps unhomed jogs at 400)
    arm_jog_max_speed: int
    # App "Arm Up / Arm Down" (servo 1 = elbow, servo 2 = wrist, moved together).
    arm_app_control: bool  # allow nudges from the app over the LAN (also needs AGRIBOT_LOCAL_TOKEN)
    arm_up_elbow_dir: int  # +1/-1: elbow (servo 1) speed sign for "up"
    arm_up_wrist_dir: int  # +1/-1: wrist (servo 2) speed sign for "up"; opposite of elbow by default
    arm_nudge_speed: int  # % speed of one nudge (capped by ARM_JOG_MAX_SPEED)
    arm_nudge_ms: int  # duration of one nudge (capped by ARM_JOG_MAX_MS)
    arm_insert_steps: str  # "axis:speed:ms,..." from the home (retracted) pose
    arm_retract_speed: int
    arm_axis1_limits: str  # "min:max" in estimate units, measured after homing
    arm_axis2_limits: str
    drive_interlock: bool
    # Vision.
    model_path: Path
    confidence: float
    iou: float
    camera: str  # auto | picamera | usb | none
    camera_index: int
    camera_width: int
    camera_height: int
    auto_scan_interval: int
    # Driving (L298N on the Pi + PS5 controller).
    drive_enabled: bool
    motor_left_forward: int
    motor_left_backward: int
    motor_right_forward: int
    motor_right_backward: int
    motor_left_enable: int | None
    motor_right_enable: int | None
    drive_max_speed: float
    drive_timeout_ms: int
    gamepad_enabled: bool
    gamepad_deadzone: float
    # Bluetooth provisioning.
    ble_enabled: bool
    ble_name: str
    ble_pin: str
    wifi_interface: str


def _soil_placeholder():
    """SOIL_PLACEHOLDER_PERCENT, else the legacy FAKE_SOIL_MOISTURE switch."""
    if _env("SOIL_PLACEHOLDER_PERCENT"):
        if _env("SOIL_PLACEHOLDER_PERCENT").lower() in {"none", "off"}:
            return None
        return _clamped(_float("SOIL_PLACEHOLDER_PERCENT", 48), 0, 100)
    if _flag("USE_FAKE_SOIL_MOISTURE", "true"):
        return _clamped(_float("FAKE_SOIL_MOISTURE", 48), 0, 100)
    return None


def _ph_source(esp32_enabled):
    # No fixed/demo pH exists any more: a pH is either measured or null.
    if _flag("USE_FAKE_PH", "false") or _env("PH_SOURCE").lower() == "fixed":
        CONFIG_WARNINGS.append("fixed/fake pH (USE_FAKE_PH, PH_SOURCE=fixed) is ignored: pH is measured or null")
    if _env("PH_SOURCE") and _env("PH_SOURCE").lower() != "fixed":
        return _choice("PH_SOURCE", "none", ("esp32", "none"))
    return "esp32" if esp32_enabled else "none"


def _optional_float(name, low, high):
    raw = _env(name)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        CONFIG_WARNINGS.append(f"{name} is not a number; ignored")
        return None
    if not low <= value <= high or value == 0:
        CONFIG_WARNINGS.append(f"{name} must be within {low}..{high} and not 0; ignored")
        return None
    return value


def _servo_stop(name, default):
    value = _int(name, default)
    if not 70 <= value <= 110:
        CONFIG_WARNINGS.append(f"{name} must be 70..110 (near the 90 neutral); using {default}")
        return default
    return value


def _direction(name):
    return _direction_default(name, 1)


def _direction_default(name, default):
    value = _int(name, default)
    if value not in (1, -1):
        CONFIG_WARNINGS.append(f"{name} must be 1 or -1; using {default}")
        return default
    return value


def load_config():
    CONFIG_WARNINGS.clear()
    for candidate in env_file_candidates():
        if load_env_file(candidate):
            print(f"[config] Loaded {candidate}")
            break
    esp32_enabled = _flag("ESP32_ENABLED", "false")
    config = Config(
        host=_env("AGRIBOT_HOST", "0.0.0.0"),
        port=_int("AGRIBOT_PORT", 8000),
        local_api_token=_env("AGRIBOT_LOCAL_TOKEN"),
        backend_url=_env("AGRIBOT_BACKEND_URL").rstrip("/"),
        robot_key=_env("ROBOT_INGEST_KEY"),
        latitude=_float("ROBOT_LATITUDE", 19.047838),
        longitude=_float("ROBOT_LONGITUDE", 72.872712),
        publish_interval=max(5, _int("PUBLISH_INTERVAL_SECONDS", 30)),
        heartbeat_interval=max(1, _int("HEARTBEAT_SECONDS", 3)),
        state_file=_path("STATE_FILE", "state.json"),
        simulate_sensors=_env("SIMULATE_SENSORS", "auto").lower() in {"1", "true", "yes", "on"},
        sensor_interval=_clamped(_float("SENSOR_READ_SECONDS", 5), 2.5, 300),
        dht_pin=_int("DHT_PIN", 24),
        dht_backend=_choice("DHT_BACKEND", "auto", ("auto", "iio", "adafruit", "none")),
        dht_stale_seconds=max(10, _int("DHT_STALE_SECONDS", 300)),
        sensor_demo_mode=_flag("SENSOR_DEMO_MODE", "false"),
        rain_enabled=_flag("RAIN_ENABLED", "true"),
        rain_do_pin=_int("RAIN_DO_PIN", 27),
        rain_wet_value=_int("RAIN_WET_DIGITAL_VALUE", 0),
        soil_sensor=_choice("SOIL_SENSOR", "none", ("none", "digital", "mcp3008", "esp32")),
        soil_adc_channel=_int("SOIL_ADC_CHANNEL", 0),
        soil_dry_value=_float("SOIL_DRY_VALUE", 0.75),
        soil_wet_value=_float("SOIL_WET_VALUE", 0.35),
        soil_digital_pin=_pin("SOIL_DIGITAL_PIN", ""),
        soil_wet_digital_value=_int("SOIL_WET_DIGITAL_VALUE", 0),
        soil_placeholder=_soil_placeholder(),
        ph_source=_ph_source(esp32_enabled),
        ph_calibration=_env("PH_CALIBRATION"),
        ph_provisional_neutral_mv=_optional_float("PH_PROVISIONAL_NEUTRAL_MV", 1, 3300),
        ph_provisional_mv_per_ph=_optional_float("PH_PROVISIONAL_MV_PER_PH", -1000, 1000),
        ph_max_age_seconds=max(60, _int("PH_MAX_AGE_SECONDS", 3600)),
        ph_settle_seconds=_clamped(_float("PH_SETTLE_SECONDS", 20), 0, 300),
        ph_samples=_clamped(_int("PH_SAMPLES", 5), 1, 20),
        ph_sample_interval=_clamped(_float("PH_SAMPLE_INTERVAL_SECONDS", 1.0), 0.1, 10),
        esp32_enabled=esp32_enabled,
        esp32_port=_env("ESP32_PORT", "auto"),
        esp32_baud=_int("ESP32_BAUD", 115200),
        arm_mode=_choice("ARM_MODE", "disabled", ("disabled", "manual", "auto")),
        arm_commissioned=_flag("ARM_COMMISSIONED", "false"),
        arm_servo1_stop=_servo_stop("ARM_SERVO1_STOP", 96),
        arm_servo2_stop=_servo_stop("ARM_SERVO2_STOP", 92),
        arm_servo1_dir=_direction("ARM_SERVO1_DIR"),
        arm_servo2_dir=_direction("ARM_SERVO2_DIR"),
        arm_jog_max_ms=int(_clamped(_int("ARM_JOG_MAX_MS", 300), 20, 400)),
        arm_jog_max_speed=int(_clamped(_int("ARM_JOG_MAX_SPEED", 30), 5, 40)),
        arm_app_control=_flag("ARM_APP_CONTROL", "false"),
        arm_up_elbow_dir=_direction("ARM_UP_ELBOW_DIR"),
        arm_up_wrist_dir=_direction_default("ARM_UP_WRIST_DIR", -1),
        arm_nudge_speed=int(_clamped(_int("ARM_NUDGE_SPEED", 20), 5, 40)),
        arm_nudge_ms=int(_clamped(_int("ARM_NUDGE_MS", 250), 50, 400)),
        arm_insert_steps=_env("ARM_INSERT_STEPS"),
        arm_retract_speed=_clamped(_int("ARM_RETRACT_SPEED", 30), 5, 100),
        arm_axis1_limits=_env("ARM_AXIS1_LIMITS"),
        arm_axis2_limits=_env("ARM_AXIS2_LIMITS"),
        drive_interlock=_flag("DRIVE_INTERLOCK", "true"),
        model_path=_path("MODEL_PATH", str(REPO_ROOT / "ai" / "models" / "weights" / "best.onnx")),
        confidence=_clamped(_float("CONFIDENCE_THRESHOLD", 0.35), 0.01, 0.99),
        iou=_clamped(_float("IOU_THRESHOLD", 0.45), 0.01, 0.99),
        camera=_choice("CAMERA", "auto", ("auto", "picamera", "usb", "none")),
        camera_index=_int("CAMERA_INDEX", 0),
        camera_width=_int("CAMERA_WIDTH", 1280),
        camera_height=_int("CAMERA_HEIGHT", 960),
        auto_scan_interval=max(0, _int("AUTO_SCAN_INTERVAL_SECONDS", 0)),
        drive_enabled=_flag("DRIVE_ENABLED", "true"),
        motor_left_forward=_int("MOTOR_LEFT_FORWARD_PIN", 5),
        motor_left_backward=_int("MOTOR_LEFT_BACKWARD_PIN", 6),
        motor_right_forward=_int("MOTOR_RIGHT_FORWARD_PIN", 16),
        motor_right_backward=_int("MOTOR_RIGHT_BACKWARD_PIN", 26),
        motor_left_enable=_pin("MOTOR_LEFT_ENABLE_PIN", 12),
        motor_right_enable=_pin("MOTOR_RIGHT_ENABLE_PIN", 13),
        drive_max_speed=_clamped(_float("DRIVE_MAX_SPEED", 1.0), 0.1, 1.0),
        drive_timeout_ms=int(_clamped(_int("DRIVE_TIMEOUT_MS", 600), 200, 5000)),
        gamepad_enabled=_flag("GAMEPAD_ENABLED", "true"),
        gamepad_deadzone=_clamped(_float("GAMEPAD_DEADZONE", 0.12), 0.0, 0.5),
        ble_enabled=_flag("BLE_ENABLED", "true"),
        ble_name=_env("BLE_NAME", "AgriBot"),
        ble_pin=_env("BLE_PAIRING_PIN"),
        wifi_interface=_env("WIFI_INTERFACE", "wlan0"),
    )
    _check_pins(config)
    if config.arm_up_elbow_dir == config.arm_up_wrist_dir:
        CONFIG_WARNINGS.append("ARM_UP_ELBOW_DIR and ARM_UP_WRIST_DIR are equal: Arm Up turns both servos the same way (intended: opposite)")
    if config.arm_nudge_speed > config.arm_jog_max_speed or config.arm_nudge_ms > config.arm_jog_max_ms:
        config = dataclasses.replace(config, arm_nudge_speed=min(config.arm_nudge_speed, config.arm_jog_max_speed),
                                     arm_nudge_ms=min(config.arm_nudge_ms, config.arm_jog_max_ms))
        CONFIG_WARNINGS.append("ARM_NUDGE_SPEED/ARM_NUDGE_MS capped to ARM_JOG_MAX_SPEED/ARM_JOG_MAX_MS")
    if config.sensor_demo_mode and config.dht_backend != "none":
        CONFIG_WARNINGS.append("SENSOR_DEMO_MODE is ignored because DHT_BACKEND is not 'none': the real DHT22 is used")
    for warning in CONFIG_WARNINGS:
        print(f"[config] Warning: {warning}")
    return config


def _check_pins(config):
    """Flag Pi GPIOs that two features would both claim."""
    users = {}
    pins = [
        ("drive", config.motor_left_forward), ("drive", config.motor_left_backward),
        ("drive", config.motor_right_forward), ("drive", config.motor_right_backward),
        ("drive", config.motor_left_enable), ("drive", config.motor_right_enable),
    ] if config.drive_enabled else []
    if config.dht_backend != "none":
        pins.append(("DHT22", config.dht_pin))
    if config.rain_enabled:
        pins.append(("rain", config.rain_do_pin))
    if config.soil_sensor == "digital":
        pins.append(("soil", config.soil_digital_pin))
    for owner, pin in pins:
        if pin is None:
            continue
        if pin in users and users[pin] != owner:
            CONFIG_WARNINGS.append(f"GPIO{pin} is assigned to both {users[pin]} and {owner}")
        users.setdefault(pin, owner)

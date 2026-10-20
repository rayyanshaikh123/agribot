# AgriBot commissioning and operations (Pi 5 + ESP32)

Checklist for bringing the hardware into service safely, plus commands for testing the
software and checking the running service. Status as of 2026-10-21.

* Code: `/opt/agribot/pi` → `~/Desktop/rc_car/AgriBot-Smart_Farming/pi`
* Service: `agribot.service` (root), config `/etc/agribot.env` (root-only, holds the robot key)
* ESP32 firmware: `esp32/agribot_esp32/` (protocol, flashing, pH voltage check: `esp32/README.md`)
* Operator CLI (talks to the running service; never opens GPIO/serial itself):
  `python3 /opt/agribot/pi/tools/agribotctl.py <command>`

**Emergency stop at any time:** `agribotctl.py stop` (or `curl -X POST localhost:8000/stop`)
stops wheels and arm. Physically: switch off the motor battery / servo supply.

## 1. Pin map

### Raspberry Pi 5 (BCM numbers)

| Function | BCM | Header pin | State (2026-10-21) |
| --- | --- | ---: | --- |
| L298N IN1 (left A) | 5 | 29 | Claimed by the service (`gpioinfo`: `lg`); matches wiring notes |
| L298N IN2 (left B) | 6 | 31 | Claimed by the service |
| L298N ENA (left enable/PWM) | 12 | 32 | Claimed by the service; held high, speed is PWM on IN pins |
| L298N IN3 (right A) | 16 | 36 | Claimed by the service |
| L298N IN4 (right B) | 26 | 37 | Claimed by the service |
| L298N ENB (right enable/PWM) | 13 | 33 | Claimed by the service |
| L298N GND | GND | 39 | Common ground required |
| DHT22 data | 24 | 18 | **Held by the leftover LCD overlay (`dc`) until the fix + reboot (§2)** |
| Rain sensor DO | 27 | 13 | Claimed by the service (input, pull-down) |
| Soil moisture | — | — | Not installed (§6) |
| Logitech C270 (046d:0825) | USB | — | Detected; captured through Picamera2/libcamera (UVC) |
| ESP32 (CP2102 10c4:ea60) | USB | — | `/dev/ttyUSB0`, `/dev/serial/by-id/usb-Silicon_Labs_CP2102_..._0001-if00-port0` |

"Claimed by the service" proves the software owns the pin, not where the wires go.

### ESP32 (ESP32 GPIO numbers)

| Function | GPIO | Provisional setting |
| --- | --- | --- |
| pH board analog out | 34 | ADC1 input-only; must stay ≤ 3.1 V (§4.1) |
| Soil analog out (future option) | 35 | `SOIL_SENSOR=esp32` |
| Servo 1 (continuous) | 26 | stop **96** (`ARM_SERVO1_STOP`), dir `ARM_SERVO1_DIR=1` — unverified |
| Servo 2 (continuous) | 25 | stop **92** (`ARM_SERVO2_STOP`), dir `ARM_SERVO2_DIR=1` — unverified |

### Standalone scripts and services that conflict with the drive

* `pi/ps5.py` (GPIO 16/20/21/26) and `pi/controls.py` (GPIO 17/18/22/23, broken): do not run
  while the service runs. The service already provides app + PS5 driving.
* `rc-car.service` is **enabled** and restarts every 2 s trying to run the missing
  `~/Desktop/rc_car/ps5.py`. It currently fails harmlessly, but would grab motor pins if that
  file reappeared. Recommended (needs approval + sudo): `sudo systemctl disable --now rc-car.service`.

## 2. DHT22 / GPIO24 fix (prepared, needs sudo + one reboot)

No LCD is fitted, but `/boot/firmware/config.txt` still loads two 3.5" ILI9486 LCD overlays
(line 9 `dtoverlay=waveshare35a`, line 21 `dtoverlay=mpi3501-custom`). Decoded with `dtc`,
both set `dc-gpios` = GPIO24 (also reset = GPIO25, touch IRQ = GPIO17), which is why the DHT22
read fails with "GPIO busy". Nothing else on the system references GPIO24.

`pi/tools/free_gpio24.sh` (run with sudo, does not reboot):
* backs up `config.txt` to `/boot/firmware/config.txt.bak-lcd-<timestamp>` and `~/config.txt.bak-lcd-<timestamp>`,
  and `/etc/rc.local` to `/etc/rc.local.bak-lcd-<timestamp>`;
* comments out exactly those two `dtoverlay=` lines;
* comments out `fbcp &` and `con2fbmap 1 0` in `/etc/rc.local` (LCD framebuffer copy);
* disables `tft-touch-bridge.service` (reads only the LCD's ADS7846 touch device);
* prints the diffs and the DHT settings in `/etc/agribot.env` (`DHT_PIN=24`, unchanged).

Left untouched on purpose: `dtparam=spi=on`, `hdmi_*` lines 10–17 (likely LCD-install
leftovers, but unrelated to GPIO24), `vc4-kms-v3d`, auto-detect lines, `[pi5] dtoverlay=nospi10`,
the X11 config snippets. Note: line 25 of `config.txt` has `camera_auto_detect=1` accidentally
merged into a comment; the USB camera does not need it.

**A reboot is required** (overlays are applied at boot; the LCD driver holds GPIO24 until then).
The reboot also restarts `agribot`, which starts the new service code (2.2.0).

After the reboot (read-only, no sudo):
```bash
/opt/agribot/pi/tools/check_dht22.sh      # GPIO24 owner, LCD drivers gone, 5 DHT22 readings via the service
```
Success = at least 3 of 5 readings with status `ok` and plausible values. If it still fails:
check the 4.7–10 kΩ pull-up and wiring, then consider the kernel driver
(`dtoverlay=dht11,gpiopin=24` in `config.txt` + reboot; `DHT_BACKEND=auto` picks it up).

Until the DHT22 reads, **no readings are published** to the backend (temperature is never invented);
`agribotctl.py status` → `publish.blockedBy` says why.

### 2.1 Status after the fix (2026-10-21) and temporary demo mode
The reboot freed GPIO24, but the DHT22 still does not answer. The Python reader says "DHT sensor
not found" and the live kernel-driver test (`pi/tools/dht22_live_test.sh`, log
`~/dht22_live_test-20261021-015316.log`) got `Only 0 signal edges detected` on all 8 reads,
while GPIO24 reads HIGH against the internal pull-down (something powered pulls it up).
Open physical checks: Pi off, measure pin 18 to pin 1 resistance (≈0 Ω = data wired to 3.3 V,
fix before powering on; 4.7–10 kΩ = normal pull-up); trace VCC/DATA/GND; check 3.3 V at the
sensor; try a known-good sensor.

Until then, for demonstrations only, set in `/etc/agribot.env`:
```
DHT_BACKEND=none        # stop driving GPIO24 for reads
SENSOR_DEMO_MODE=true   # labelled simulated temperature/humidity (agribot/demo.py)
```
Demo values are status `simulated` with `demo: true` and `actionable: false` in `/sensors`; the
app's Robot screen shows `Sensors: demo (simulated climate)`; each published reading is stored
with `robot.status = "online (demo: simulated temperature/humidity)"`. `SensorHub.actionable()`
returns `None` for them, so nothing may base an action on them. To go back to the real sensor:
remove both lines (or set `DHT_BACKEND=auto`, `SENSOR_DEMO_MODE=false`) and restart.

## 3. Mobile drive test (wheels off the ground)

Software-verified (tests): pin mapping matches §1, left/right direction logic, PWM capped at
`DRIVE_MAX_SPEED`, non-finite/invalid input rejected and stops the wheels, explicit stop
latches for 0.4 s over in-flight commands, app watchdog stops after `DRIVE_TIMEOUT_MS` (600 ms),
PS5 controller has priority while in use, arm interlock blocks motion (never stop).

Physical procedure — **only with the operator present**:
1. Put the robot on a stand so **all four wheels are clear of the ground**. Keep hands, cables
   and clothing away from the wheels. Have the motor battery switch within reach.
2. In `/etc/agribot.env` set `DRIVE_MAX_SPEED=0.3` (restart required).
3. One short command at a time from the Pi (the watchdog stops it after ~0.6 s):
   ```bash
   curl -s -X POST localhost:8000/drive -d '{"left":0.5,"right":0}'   # left side only, forward
   curl -s -X POST localhost:8000/drive -d '{"left":0,"right":0.5}'   # right side only, forward
   curl -s -X POST localhost:8000/drive -d '{"left":-0.5,"right":-0.5}'  # both reverse
   curl -s -X POST localhost:8000/drive/stop
   ```
   Record for each: which wheels turned, direction, did both wheels of a side turn.
4. Wrong direction on one side → swap that side's motor wires at the L298N (not the code).
5. Stop tests: drive from the app, press its stop → immediate stop; close the app mid-drive →
   stop within ~0.6 s; PS5 Circle → stop; switch the controller off → stop.
6. Restore `DRIVE_MAX_SPEED` afterwards.

## 4. pH probe

### 4.1 Voltage safety check — before connecting GPIO34 (physical, required)
The ESP32 ADC is **not 5 V tolerant** and the board model/output range is not verified. Many
"analog pH" boards run from 5 V and can output above 3.3 V. Measure with a multimeter, board
powered as in use, **not** connected to the ESP32, in the most acidic and most basic liquids
available (and with the offset trimmer at both ends). If anything exceeds **3.1 V**, add a divider
(e.g. 10 kΩ series + 20 kΩ to GND = ×0.667) and re-measure at the divider output. Details:
`esp32/README.md`.

### 4.2 Provisional use now (no buffer solutions)
Without calibration the system reports the **raw ADC value and the voltage at GPIO34**; the
app/backend get pH `null`. No conversion formula is built in, because the board model is
unknown.
1. Flash the firmware (approval), set `ESP32_ENABLED=true`, keep `ARM_MODE=disabled`, restart.
2. `agribotctl.py ph-read` — live voltage (probe in air/water/soil). `noiseMv` should be small.
3. Hold the probe in the sample **by hand** and run `agribotctl.py ph-sample` — samples
   `PH_SAMPLES` readings and stores them with status `uncalibrated`, method `manual`. No motion.
4. Optional: if the board's datasheet documents its output (e.g. "2.50 V at pH 7, −0.17 V per pH"),
   set `PH_PROVISIONAL_NEUTRAL_MV=2500` and `PH_PROVISIONAL_MV_PER_PH=-170` (values **at the ESP32
   pin**, so scale them by any divider). The Pi then shows an `estimate` labelled `uncalibrated`
   in `/ph`, `/sensors` and the logs. It is never published and is not an accurate pH.
   Never guess these numbers.

Statuses: `ok` (calibrated, published) · `uncalibrated` · `rejected` (ADC at 0 V / ceiling,
noisy, drifting) · `stale` (older than `PH_MAX_AGE_SECONDS`) · `unavailable` (ESP32 not connected)
· `disabled`. A disconnected or invalid sensor always gives `null`, never a fixed value.

### 4.3 Calibration later (buffer solutions)
1. Get pH 7.00 and pH 4.01 buffers (pH 10.01 optional). Rinse the probe with distilled water
   between buffers; let it settle 1–2 min in each.
2. For each buffer: `agribotctl.py ph-read` repeatedly until `mv` is stable; note it.
3. Set `PH_CALIBRATION=7.00:<mv7>,4.01:<mv4>[,10.01:<mv10>]` in `/etc/agribot.env`, restart.
   The service rejects implausible slopes. Then `ph-sample` in a buffer should read its pH ±0.1.
4. Recalibrate every few weeks or after storage; probes drift.

## 5. Probe arm (two continuous-rotation servos) — keep DISABLED until verified

Facts that shape the procedure: continuous servos have **no position feedback**; the stop
values 96/92 are provisional; timed motion is **not** a precise position; overrunning the
mechanical travel grinds gears. The software therefore: starts disabled (`ARM_MODE=disabled`),
never moves at startup, never assumes the arm position, requires an operator `home`, refuses
automatic insertion unless `ARM_MODE=auto` **and** `ARM_COMMISSIONED=true` **and** limits +
insert steps are configured **and** pH is calibrated. The firmware is disarmed at boot, stops and
disarms on STOP, bad input or 2.5 s without Pi traffic, and caps every move at 3 s
(unhomed: 40 % / 400 ms).

### 5.1 Preparation (approval required for each motion step)
1. Robot powered off, secured on the bench so the arm can swing freely and cannot hit anything
   (probe tip clear of the table). **Keep hands and cables clear of the arm whenever it is powered.**
2. Servo supply with an accessible switch. First tests: servo horns/linkage **disconnected** if possible.
3. Flash the firmware (`esp32/README.md`), `ESP32_ENABLED=true`, restart; `agribotctl.py esp32`
   → `connected: true`, `firmware: agribot-esp32/1.3`. Still `ARM_MODE=disabled`: confirm
   `agribotctl.py jog 1 10 100` is refused.
4. Set `ARM_MODE=manual` (keep `ARM_COMMISSIONED=false`), restart. Test **one servo at a time**.

### 5.2 True neutral (stop) value, per servo
1. `agribotctl.py neutral 1 96 2000` holds servo 1 at value 96 for 2 s. Watch the horn.
2. If it creeps, try neighbouring values (`neutral 1 95 2000`, `neutral 1 97 2000`, …; allowed
   range = configured stop ±10). The true stop is the value (or middle of the range of values)
   with **no** movement. Repeat for servo 2 around 92.
3. Write them to `ARM_SERVO1_STOP` / `ARM_SERVO2_STOP`, restart, and confirm with a 3 s
   neutral test at exactly that value: no creep. (A neutral test clears the home position.)

### 5.3 Direction
`agribotctl.py jog 1 20 150` → note whether the arm moves **toward the soil**. Choose the
convention "negative speed = toward the soil" for both axes; if a servo moves the other way,
set `ARM_SERVO<n>_DIR=-1`, restart, verify again.

### 5.4 Travel limits (with the linkage connected)
1. Jog by hand-sized steps to the fully **retracted** rest pose; inspect by eye; `agribotctl.py home`
   (asks for confirmation).
2. Move toward the soil in steps of ≤ 30 % / ≤ 300 ms (`jog 1 -20 200`). After each step
   `agribotctl.py arm` shows `firmware.axes[].pos` (estimate, ms at 100 %).
3. Note the estimate where the probe reaches the wanted depth and where the mechanism would
   hit its end stop. **Stop before any strain or grinding** (stop command or servo power switch).
4. `agribotctl.py retract`, then check by eye that it is really back at rest. Repeat 3–4 times;
   the spread of the "home" error shows the drift — add it to your margin.
5. Configure with ≥ 20 % margin from the end stop, e.g.
   `ARM_AXIS1_LIMITS=-900:0`, `ARM_INSERT_STEPS=1:-30:2500,2:25:1200` (checked against the limits at startup).

### 5.5 Emergency stop verification
While a jog runs: `agribotctl.py stop` → stops immediately; stop the service
(`sudo systemctl stop agribot`) → ESP32 stops within 2.5 s; unplug USB → stops within 2.5 s;
reset the ESP32 → servos start stopped and the Pi reports `unknown` (re-home needed).

### 5.7 App Arm Up / Arm Down (manual, after 5.2–5.5)
Mapping (configurable, not yet verified): servo 1 / ESP32 GPIO26 = **elbow**, servo 2 / GPIO25 =
**wrist**, shoulder never commanded. **Up** = elbow `ARM_UP_ELBOW_DIR` and wrist `ARM_UP_WRIST_DIR`
(opposite by default), started together (firmware `MOVE2`); **Down** reverses both. Each nudge is
`ARM_NUDGE_SPEED` % for `ARM_NUDGE_MS` ms (≤ 40 % / 400 ms); the app repeats nudges while the
button is held and sends STOP on release; the red STOP stops wheels and arm.
1. From the Pi first: `agribotctl.py up`, then `agribotctl.py down` (one nudge each). If "up"
   moves the probe down, swap both signs (`ARM_UP_ELBOW_DIR=-1`, `ARM_UP_WRIST_DIR=1`); if the
   elbow and wrist fight each other instead of lifting together, flip only the wrongly-turning
   one. Restart after each change.
2. Enable the app: set a strong `AGRIBOT_LOCAL_TOKEN`, `ARM_APP_CONTROL=true`, restart; in the
   app's Drive screen tap the **key** icon on the Arm bar and enter the token (stored in the iOS
   Keychain). The buttons stay disabled, with the reason shown, unless the robot reports app
   control on and an arm mode that allows motion. **Arm raised** appears enabled only while the
   wheels are locked by the arm and the ESP32 is connected; **Retract** appears only for a
   commissioned (`ARM_COMMISSIONED=true`), homed arm.
3. Test hold/release, release mid-move (must stop at once), STOP button, and Wi-Fi loss while
   holding (the current nudge ends by itself within `ARM_NUDGE_MS`; the ESP32 disarms after 2.5 s).
Nudges are timed open-loop moves: they never establish a position, and after any nudge the probe
state is `partial` (wheels locked) until the arm is retracted or re-homed.

### 5.6 Sign-off
Only when 5.2–5.5 passed: set `ARM_COMMISSIONED=true`, `ARM_MODE=auto`, calibrate pH (§4.3),
restart, `home`, then `agribotctl.py measure` with supervision (`agribotctl.py watch`). A STOP
during the sequence leaves the arm where it is and locks the wheels until retract/re-home.

## 6. Soil-moisture sensor — prepared, not installed

The software supports `SOIL_SENSOR=none|esp32|mcp3008|digital`. Today `none`: the backend
(which requires a number) receives `SOIL_PLACEHOLDER_PERCENT` (48), labelled `placeholder` in
`/sensors`, `/status` and the startup log. Set `SOIL_PLACEHOLDER_PERCENT=none` to stop publishing
instead. Statuses: `ok`, `placeholder`, `not_installed`, `unavailable`, `simulated`.

The Raspberry Pi has **no analog inputs**: an analog soil sensor cannot go directly to a Pi GPIO.

| Option | Sensor | Wiring | Notes |
| --- | --- | --- | --- |
| **A (recommended)** `esp32` | Capacitive soil moisture sensor (v1.2/v2.0 type, analog out) | VCC 3.3 V, GND, AOUT → **ESP32 GPIO35** | No extra parts; ADC1 works with Wi-Fi. Verify AOUT ≤ 3.1 V (powered from 3.3 V it normally is). |
| B `mcp3008` | same | AOUT → MCP3008 CH0, MCP3008 on SPI0 CE0 (pins 19/21/23/24), 3.3 V VREF | SPI0 CE0 becomes free only after the LCD overlay fix (§2). |
| C `digital` | Comparator module (LM393 DO) | DO → a free Pi GPIO, module powered from **3.3 V** | Wet/dry only (0 or 100 %). Set `SOIL_DIGITAL_PIN`. |

Avoid bare resistive (fork) probes: they corrode within weeks.

Calibration (options A/B): `agribotctl.py sensors` shows `readings.soil.raw` (mV for A, 0–1 for B).
Record it in air/very dry soil → `SOIL_DRY_VALUE`, and in water-saturated soil → `SOIL_WET_VALUE`
(capacitive sensors read lower when wet), restart. Then set `SOIL_SENSOR`, and remove the placeholder
(`SOIL_PLACEHOLDER_PERCENT=none`) so a failed sensor blocks publishing instead of sending 48.

## 7. Camera and disease detection

Verified 2026-10-21 (non-invasive): C270 present on USB; the running service captured a
640×360 frame via `/camera/frame.jpg`; the ONNX model loaded and ran inference in ~450 ms on it.

**Finding:** that frame showed a curtain and a laptop (no plants), and the model reported
"Early Blight" 58 % and "Late Blight" 57 %. The model gives confident false positives on non-leaf
scenes, so scan results are **not reliable** yet. Needed: validate on real labelled leaf images,
and likely retrain with background/no-leaf images or raise `CONFIDENCE_THRESHOLD` (default 0.35)
based on that validation. Accuracy has not been measured.

## 8. Software tests (no hardware)

```bash
cd /opt/agribot/pi
/opt/agribot/venv/bin/python -B -m unittest discover -s tests -v
arduino-cli compile --fqbn esp32:esp32:esp32 ~/Desktop/rc_car/AgriBot-Smart_Farming/esp32/agribot_esp32
```
Tests mock GPIO, serial, camera and backend; they check logic and API shapes, **not** that the
physical sensors, servos, motors or camera work.

## 9. Checking the running service (read-only)

```bash
systemctl status agribot --no-pager
journalctl -u agribot -n 100 --no-pager | grep -v '\[http\]'
curl -s localhost:8000/health
python3 /opt/agribot/pi/tools/agribotctl.py status    # capabilities, errors, publish.blockedBy, esp32, arm
python3 /opt/agribot/pi/tools/agribotctl.py sensors   # every reading with its status
gpioinfo -c gpiochip0 | grep -E 'line +(5|6|12|13|16|24|26|27):'
```

## 10. Local API safety
* `POST /drive` is unauthenticated on the LAN (the app uses it). Use trusted networks/hotspots only.
* Arm motion endpoints (`/arm/jog|neutral-test|home|retract|insert`, `/ph/measure`) accept only the
  Pi itself or `X-AgriBot-Token: $AGRIBOT_LOCAL_TOKEN`. `/ph/read` and `/ph/sample` never move anything.
* Stopping is always allowed: `POST /stop`, `/drive/stop`, `/arm/stop`.

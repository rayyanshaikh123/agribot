# AgriBot ESP32 firmware (pH probe ADC + probe arm)

`agribot_esp32/agribot_esp32.ino` runs on the ESP32 that is plugged into the
Raspberry Pi 5 over USB (CP2102 bridge, `/dev/serial/by-id/usb-Silicon_Labs_CP2102_...`).
It reads the pH interface board and drives the two continuous-rotation servos of
the probe arm. The Pi service (`pi/agribot/esp32.py`, `ph.py`, `arm.py`) is the
only program that talks to it.

## Wiring (ESP32 GPIO numbers, not Pi numbers)

| Signal | ESP32 GPIO | Notes |
| --- | --- | --- |
| pH board analog out | 34 | ADC1, input only. **Max 3.3 V**: see "pH voltage check" below |
| Soil moisture analog out (optional) | 35 | ADC1, input only. Only used with `SOIL_SENSOR=esp32` |
| Servo 1 signal | 26 | Continuous rotation, **provisional stop value 96** |
| Servo 2 signal | 25 | Continuous rotation, **provisional stop value 92** |
| Servo power | external 5–6 V supply | Not the ESP32 3V3 pin. Join supply GND to ESP32 GND |
| USB | Pi USB port | Power + serial link |

The two stop values are different and must never be swapped. They are compiled in as
`SERVO1_STOP` / `SERVO2_STOP` (used from power-on), and the Pi overrides them on every
connect with `ARM_SERVO1_STOP` / `ARM_SERVO2_STOP` (`TRIM`), together with the direction
`ARM_SERVO1_DIR` / `ARM_SERVO2_DIR` (`DIR`). Neither value has been physically verified yet:
find the true neutral with the neutral test (`docs/COMMISSIONING.md` §5.2).

The stop values are servo "angles" in the ESP32Servo library's default pulse range
(`attach(pin)` = 544–2400 µs, so 96 ≈ 1534 µs and 92 ≈ 1493 µs). If the sketch you
used to find 96 and 92 attached the servos with a different range, change
`SERVO_MIN_US`/`SERVO_MAX_US` to that range, or the "stop" pulse will creep.

## Build and flash

**Use `esp32/flash.sh`** (operator present). It builds for **ESP32 Dev Module** (`esp32:esp32:esp32`,
4 MB flash), refuses unless the port is free, you type `FLASH` (servo power off or arm clear) and
`esptool` reports a plain `ESP32`, then uploads and checks the boot line
(`{"ev":"boot","fw":"agribot-esp32/1.4","proto":3,...}`):

```bash
sudo systemctl stop agribot            # only needed if ESP32_ENABLED=true (service holds the port)
cd ~/Desktop/rc_car/AgriBot-Smart_Farming && bash esp32/flash.sh
sudo systemctl start agribot
```

Stop values and directions sent by the Pi (`TRIM`/`DIR`, from `ARM_SERVO*_STOP` / `ARM_SERVO*_DIR`)
are saved in the ESP32's flash and used from the next power-up, so a commissioned neutral is in
effect before the Pi connects. Manual equivalent of the script:

The toolchain is installed on the Pi (`arduino-cli`, core `esp32:esp32` 3.3.x,
library `ESP32Servo` 3.2.x). Stop the service first so nothing else holds the port,
and keep the arm clear: the board resets during flashing.

```bash
sudo systemctl stop agribot          # releases /dev/ttyUSB0 and stops the wheels
cd ~/Desktop/rc_car/AgriBot-Smart_Farming/esp32/agribot_esp32
arduino-cli compile --fqbn esp32:esp32:esp32 .
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0 .
sudo systemctl start agribot
```

Flashing and live tests require the operator's approval (the board resets; servos may twitch).
Then set `ESP32_ENABLED=true` in `/etc/agribot.env` and restart the service. Leave
`ARM_MODE=disabled` until the arm is commissioned (`docs/COMMISSIONING.md` §5).

Checked 2026-10-21 without opening the port: the CP2102 is enumerated (10c4:ea60,
`/dev/ttyUSB0`) and nothing holds it open. What is currently flashed on the board is unknown;
the Pi never sends anything but `PING` to a board that does not identify as AgriBot firmware.

## Protocol v3 (firmware agribot-esp32/1.4)

Serial 115200 8N1, newline-terminated lines.

* Pi → ESP32: `<id> <COMMAND> [args...]` in ASCII, max 95 characters. `id` is an integer
  echoed in the reply.
* ESP32 → Pi: one JSON object per line. Replies carry `id` and `ok`, and on failure `err`.
* Unsolicited events carry `ev`: `boot` (with `fw`, `proto`, `reset` reason), `stop`
  (axis stopped: `done`, `limit`, `comm_timeout`, `stop_command`, `disarm`,
  `bad_command`), and `fault` (`disarmed: <reason>`).
* Lines that do not start with `{` (ROM boot text) are ignored by the Pi.

| Command | Reply fields | Notes |
| --- | --- | --- |
| `PING` | `fw`, `proto`, `up` | Handshake. The Pi only accepts `fw` starting with `agribot-esp32/` and `proto` 3 |
| `KA` | – | Keepalive; the Pi sends one every second |
| `STATUS` | `fw`, `proto`, `up`, `armed`, `axes[]` | Per axis: `pin`, `stop`, `dir`, `raw`, `moving`, `speed`, `pos`, `homed`, `limits`, `min`, `max`, `last` |
| `STOP` | – | Stops both servos and disarms. Always accepted |
| `ARM 0\|1` | `armed` | Motion permission. The Pi arms only for the duration of an operation |
| `MOVE axis speed ms` | `axis`, `value`, `ms` | speed −100..100 (not 0), ms 1..3000. Refused when disarmed, busy, unhomed beyond jog limits, or past a limit |
| `MOVE2 speed1 speed2 ms` | `values`, `ms` | Starts servo 1 (elbow) and servo 2 (wrist) in the same instant (app Arm Up/Down). Both are checked first (armed, not busy, jog cap if unhomed, limits); if either is refused, neither moves. 0 = that servo stays stopped |
| `HOME axis` | `pos` | Operator declares the current pose as home (fully retracted), estimate = 0 |
| `TRIM axis value` | `stop` | Set the neutral stop value (70..110). Not while moving |
| `DIR axis 1\|-1` | `dir` | Which rotation is positive speed. Changing it clears home |
| `RAW axis value ms` | `value`, `ms` | Neutral test: hold a raw value within stop ±10 for ≤ 3 s. Armed only; clears home |
| `LIMITS axis min max` | `min`, `max` | Allowed estimate range, need min ≤ 0 ≤ max |
| `PH [n]` | `pin`, `seq`, `up`, `mv`, `raw`, `min`, `max`, `n` | Average of n (1–64) calibrated ADC reads at GPIO34. `seq` counts pH reads since boot |
| `SOIL [n]` | same as PH | GPIO35 |

### Safety behaviour in the firmware

* Both servos get their stop values first thing in `setup()`.
* Motion is **disarmed at boot**. `MOVE` is refused until `ARM 1`. `STOP`, malformed
  input, an over-long line and link silence all disarm.
* Every move has a duration (≤ 3 s) and ends by itself. One move per axis at a time,
  so repeated commands never queue up extra travel.
* If the Pi sends nothing for 2.5 s while armed or moving, everything stops and is disarmed.
* Until an axis is homed and has limits, only jogs of ≤ 40 % speed and ≤ 400 ms are accepted.
* With home and limits, moves whose end estimate would leave `[min, max]` are rejected, and a
  running move stops when its estimate reaches a limit.
* The loop task is watched by the task watchdog. A hang resets the board, which restarts with
  both servos stopped and disarmed.

**Limits of this design:** position is an open-loop estimate (speed × time). Load,
gravity and battery voltage make it drift, and a stall is not detected. Limits must
be set with margin, and the arm should be re-homed whenever it is moved by hand or
the ESP32 restarts. Real end-stop protection needs limit switches or a position sensor.

## pH voltage check (do this before connecting GPIO34)

The ESP32 ADC is **not 5 V tolerant**. Many pH interface boards (for example PH-4502C
types) are powered from 5 V and can output above 3.3 V, especially in acidic solutions.

1. Power the pH board as it will be used, with the BNC probe attached, and **not** connected to the ESP32.
2. With a multimeter, measure the board's analog output (Po/AO) against GND in pH 4.0
   buffer, pH 7.0 buffer, and (if available) pH 10.0 buffer. Also check with the offset trimmer at both ends of its range.
3. If any reading is above **3.1 V**, add a divider before GPIO34 (for example 10 kΩ from
   the board to GPIO34 and 20 kΩ from GPIO34 to GND, which gives ×0.667), and measure again at GPIO34.
4. Only then connect it to GPIO34.

The calibration (`PH_CALIBRATION`) is done with the voltage at the ESP32 pin, so the divider
is accounted for automatically.

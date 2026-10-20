// AgriBot ESP32 co-processor: pH probe ADC + 2-axis pH-probe arm.
//
// Connected to the Raspberry Pi 5 over USB serial (115200 baud, 8N1).
// Protocol v1 and safety rules: see esp32/README.md. In short:
//   Pi  -> ESP32  "<id> <COMMAND> [args...]\n"        (ASCII, one line)
//   ESP32 -> Pi   {"id":<id>,"ok":true|false,...}\n   (one JSON object per line)
//   ESP32 -> Pi   {"ev":"boot"|"stop"|"fault",...}\n  (unsolicited events)
//
// The arm uses two CONTINUOUS-ROTATION servos. They have no position feedback:
// a servo value only sets speed and direction, and each one stops at its own
// calibrated value. Position here is an open-loop ESTIMATE (speed% x time), so
// the travel limits only work if the arm is homed and the limits are measured.
//
// Safety behaviour:
//   * Both servos are commanded to their calibrated stop values first thing at boot.
//   * Motion is DISARMED at boot. MOVE is refused until the Pi sends "ARM 1";
//     STOP, bad input and link loss disarm again.
//   * Every move has a duration, capped at MAX_MOVE_MS, and stops by itself.
//   * One move per axis at a time; a MOVE while that axis is moving is rejected
//     (repeated commands never queue up or add travel).
//   * Moves stop, and motion is disarmed, if the Pi sends nothing for
//     COMM_TIMEOUT_MS (the Pi sends a keepalive every second).
//   * The loop task is watched by the task watchdog: a hang resets the board,
//     and a reset always starts with both servos stopped.
//   * Until an axis is homed AND has limits, only short, slow jogs are allowed.
//   * Homed moves that would leave [min, max] are rejected; a running move is
//     stopped the moment its estimate reaches a limit.

#include <Arduino.h>
#include <ESP32Servo.h>

static const char *FIRMWARE = "agribot-esp32/1.3";
static const int PROTOCOL = 3;  // bump on incompatible protocol changes

// ---- Wiring (ESP32 GPIO numbers, unrelated to the Pi's GPIO numbers) --------
static const int PH_PIN = 34;    // pH interface board analog out (ADC1, input-only pin)
static const int SOIL_PIN = 35;  // optional soil-moisture analog out (ADC1, input-only pin)
static const int SERVO1_PIN = 26;
static const int SERVO1_STOP = 96;  // provisional neutral for servo 1 - never swap (Pi may TRIM it)
static const int SERVO2_PIN = 25;
static const int SERVO2_STOP = 92;  // provisional neutral for servo 2 - never swap (Pi may TRIM it)
static const int TRIM_MIN = 70;     // allowed range for runtime stop-value changes
static const int TRIM_MAX = 110;
static const int RAW_MAX_OFFSET = 10;  // RAW test pulses stay within stop +/- this

// Servo pulse range used to calibrate the stop values below. The values 96
// and 92 were measured with the ESP32Servo library's default range
// (attach(pin) = 544..2400 us). If your test sketch used another range,
// change these two numbers so the stop values keep meaning the same pulse.
static const int SERVO_MIN_US = 544;
static const int SERVO_MAX_US = 2400;

// ---- Motion safety limits ---------------------------------------------------
static const int MAX_DELTA = 30;              // servo value offset at 100% speed
static const uint32_t MAX_MOVE_MS = 3000;     // hard cap for a single move
static const int JOG_MAX_SPEED = 40;          // % allowed before homing/limits
static const uint32_t JOG_MAX_MS = 400;       // ms allowed before homing/limits
static const uint32_t COMM_TIMEOUT_MS = 2500;  // stop + disarm if the Pi goes quiet
static const float POS_ABS_MAX = 60000.0f;    // sanity bound for limits

struct Axis {
  int pin;
  int stopValue;  // neutral value (never swap between axes)
  int dir;        // +1 / -1: maps positive speed to servo direction
  Servo servo;
  bool moving;
  int speed;  // -100..100 while moving
  uint32_t moveStart;
  uint32_t moveMs;
  float pos;  // estimate at moveStart (units: ms at 100% speed)
  bool raw;   // RAW neutral test running (position estimate invalid afterwards)
  bool homed;
  bool limitsSet;
  float minPos;
  float maxPos;
  const char *lastStop;
};

// Axis 1: continuous servo on GPIO 26, stops at 96.
// Axis 2: continuous servo on GPIO 25, stops at 92.
Axis axes[2];

static char lineBuf[96];
static size_t lineLen = 0;
static bool lineOverflow = false;
static uint32_t lastRxMs = 0;
static bool armed = false;  // motion permission from the Pi
static uint32_t phSeq = 0;

// ---------------------------------------------------------------------------
static void writeStop(Axis &a) {
  a.servo.write(a.stopValue);
}

static float livePos(const Axis &a, uint32_t now) {
  if (!a.moving) return a.pos;
  uint32_t elapsed = now - a.moveStart;
  if (elapsed > a.moveMs) elapsed = a.moveMs;
  return a.pos + (float)a.speed * (float)elapsed / 100.0f;
}

static void emitStop(int index, const char *reason) {
  Axis &a = axes[index];
  Serial.printf("{\"ev\":\"stop\",\"axis\":%d,\"reason\":\"%s\",\"pos\":%.1f}\n", index + 1, reason, a.pos);
}

static void stopAxis(int index, const char *reason, uint32_t now) {
  Axis &a = axes[index];
  writeStop(a);  // always re-send the stop value, even if already idle
  if (a.moving) {
    a.pos = livePos(a, now);
    if (a.raw) {
      a.raw = false;
      a.homed = false;  // unknown motion during a neutral test: home is lost
    }
    a.moving = false;
    a.speed = 0;
    a.lastStop = reason;
    emitStop(index, reason);
  }
}

static void stopAll(const char *reason) {
  uint32_t now = millis();
  for (int i = 0; i < 2; i++) stopAxis(i, reason, now);
}

static void disarm(const char *reason) {
  stopAll(reason);
  if (armed) {
    armed = false;
    Serial.printf("{\"ev\":\"fault\",\"msg\":\"disarmed: %s\"}\n", reason);
  }
}

// ---------------------------------------------------------------------------
static void replyError(long id, const char *error) {
  Serial.printf("{\"id\":%ld,\"ok\":false,\"err\":\"%s\"}\n", id, error);
}

static void printAxis(int index, uint32_t now) {
  const Axis &a = axes[index];
  Serial.printf(
      "{\"axis\":%d,\"pin\":%d,\"stop\":%d,\"dir\":%d,\"raw\":%s,\"moving\":%s,\"speed\":%d,\"pos\":%.1f,\"homed\":%s,"
      "\"limits\":%s,\"min\":%.1f,\"max\":%.1f,\"last\":\"%s\"}",
      index + 1, a.pin, a.stopValue, a.dir, a.raw ? "true" : "false", a.moving ? "true" : "false", a.speed, livePos(a, now),
      a.homed ? "true" : "false", a.limitsSet ? "true" : "false", a.minPos, a.maxPos, a.lastStop);
}

static void replyStatus(long id) {
  uint32_t now = millis();
  Serial.printf("{\"id\":%ld,\"ok\":true,\"fw\":\"%s\",\"proto\":%d,\"up\":%lu,\"armed\":%s,\"axes\":[", id, FIRMWARE,
                PROTOCOL, (unsigned long)now, armed ? "true" : "false");
  printAxis(0, now);
  Serial.print(",");
  printAxis(1, now);
  Serial.print("]}\n");
}

static int parseAxis(const char *token) {
  if (!token) return -1;
  int n = atoi(token);
  return (n == 1 || n == 2) ? n - 1 : -1;
}

static bool parseLong(const char *token, long &out) {
  if (!token || !*token) return false;
  char *end = nullptr;
  out = strtol(token, &end, 10);
  return end && *end == '\0';
}

static bool parseFloat(const char *token, float &out) {
  if (!token || !*token) return false;
  char *end = nullptr;
  out = strtof(token, &end);
  return end && *end == '\0' && isfinite(out);
}

// Checks one axis move without starting it. Returns an error text or nullptr.
static const char *checkMove(int index, long speed, long ms) {
  Axis &a = axes[index];
  if (!armed) return "disarmed";
  if (a.moving) return "axis busy";
  bool bounded = a.homed && a.limitsSet;
  if (!bounded && (labs(speed) > JOG_MAX_SPEED || ms > (long)JOG_MAX_MS)) return "not homed/limited: jog only";
  if (bounded) {
    float target = a.pos + (float)speed * (float)ms / 100.0f;
    if (target < a.minPos || target > a.maxPos) return "would exceed limit";
  }
  return nullptr;
}

// Starts a move that was accepted by checkMove. Returns the servo value written.
static int startMove(int index, long speed, long ms, uint32_t now) {
  Axis &a = axes[index];
  int value = a.stopValue + a.dir * (int)lroundf((float)speed * MAX_DELTA / 100.0f);
  value = constrain(value, 0, 180);
  a.speed = (int)speed;
  a.moveMs = (uint32_t)ms;
  a.moveStart = now;
  a.moving = true;
  a.lastStop = "";
  a.servo.write(value);
  return value;
}

static void cmdMove(long id, char *args) {
  int index = parseAxis(strtok(args, " "));
  long speed, ms;
  if (index < 0) return replyError(id, "axis must be 1 or 2");
  if (!parseLong(strtok(nullptr, " "), speed) || speed == 0 || speed < -100 || speed > 100)
    return replyError(id, "speed must be -100..100 and not 0");
  if (!parseLong(strtok(nullptr, " "), ms) || ms < 1 || ms > (long)MAX_MOVE_MS)
    return replyError(id, "ms out of range");
  const char *error = checkMove(index, speed, ms);
  if (error) return replyError(id, error);
  int value = startMove(index, speed, ms, millis());
  Serial.printf("{\"id\":%ld,\"ok\":true,\"axis\":%d,\"value\":%d,\"ms\":%ld}\n", id, index + 1, value, ms);
}

// MOVE2 speed1 speed2 ms: start both servos in the same instant (e.g. elbow +
// wrist for "arm up/down"). Both moves are validated first; if either would be
// refused, neither starts. A speed of 0 leaves that servo stopped.
static void cmdMove2(long id, char *args) {
  long speed1, speed2, ms;
  if (!parseLong(strtok(args, " "), speed1) || speed1 < -100 || speed1 > 100 ||
      !parseLong(strtok(nullptr, " "), speed2) || speed2 < -100 || speed2 > 100 || (speed1 == 0 && speed2 == 0))
    return replyError(id, "speeds must be -100..100, not both 0");
  if (!parseLong(strtok(nullptr, " "), ms) || ms < 1 || ms > (long)MAX_MOVE_MS) return replyError(id, "ms out of range");
  const long speeds[2] = {speed1, speed2};
  for (int i = 0; i < 2; i++) {
    if (speeds[i] == 0) continue;
    const char *error = checkMove(i, speeds[i], ms);
    if (error) {
      static char message[48];
      snprintf(message, sizeof(message), "axis %d: %s", i + 1, error);
      return replyError(id, message);
    }
  }
  uint32_t now = millis();
  int values[2] = {axes[0].stopValue, axes[1].stopValue};
  for (int i = 0; i < 2; i++) {
    if (speeds[i] != 0) values[i] = startMove(i, speeds[i], ms, now);
  }
  Serial.printf("{\"id\":%ld,\"ok\":true,\"values\":[%d,%d],\"ms\":%ld}\n", id, values[0], values[1], ms);
}

// TRIM axis value: set the neutral (stop) value, e.g. after finding the true stop point.
static void cmdTrim(long id, char *args) {
  int index = parseAxis(strtok(args, " "));
  long value;
  if (index < 0) return replyError(id, "axis must be 1 or 2");
  if (!parseLong(strtok(nullptr, " "), value) || value < TRIM_MIN || value > TRIM_MAX) return replyError(id, "stop value 70..110");
  Axis &a = axes[index];
  if (a.moving) return replyError(id, "axis busy");
  a.stopValue = (int)value;
  writeStop(a);
  Serial.printf("{\"id\":%ld,\"ok\":true,\"axis\":%d,\"stop\":%d}\n", id, index + 1, a.stopValue);
}

// DIR axis 1|-1: which servo rotation counts as positive speed.
static void cmdDir(long id, char *args) {
  int index = parseAxis(strtok(args, " "));
  long dir;
  if (index < 0) return replyError(id, "axis must be 1 or 2");
  if (!parseLong(strtok(nullptr, " "), dir) || (dir != 1 && dir != -1)) return replyError(id, "dir must be 1 or -1");
  Axis &a = axes[index];
  if (a.moving) return replyError(id, "axis busy");
  if (a.dir != (int)dir) a.homed = false;  // estimate sign changes meaning
  a.dir = (int)dir;
  Serial.printf("{\"id\":%ld,\"ok\":true,\"axis\":%d,\"dir\":%d}\n", id, index + 1, a.dir);
}

// RAW axis value ms: hold one servo value near neutral for a short time to find
// the true stop point (no creep). Armed only; clears the axis home.
static void cmdRaw(long id, char *args) {
  int index = parseAxis(strtok(args, " "));
  long value, ms;
  if (index < 0) return replyError(id, "axis must be 1 or 2");
  Axis &a = axes[index];
  if (!parseLong(strtok(nullptr, " "), value) || labs(value - a.stopValue) > RAW_MAX_OFFSET || value < 0 || value > 180)
    return replyError(id, "value must be within stop +/- 10");
  if (!parseLong(strtok(nullptr, " "), ms) || ms < 1 || ms > (long)MAX_MOVE_MS) return replyError(id, "ms out of range");
  if (!armed) return replyError(id, "disarmed");
  if (a.moving) return replyError(id, "axis busy");
  a.speed = 0;  // speed unknown: the estimate does not change
  a.moveMs = (uint32_t)ms;
  a.moveStart = millis();
  a.moving = true;
  a.raw = true;
  a.lastStop = "";
  a.servo.write((int)value);
  Serial.printf("{\"id\":%ld,\"ok\":true,\"axis\":%d,\"value\":%ld,\"ms\":%ld}\n", id, index + 1, value, ms);
}

static void cmdHome(long id, char *args) {
  int index = parseAxis(strtok(args, " "));
  if (index < 0) return replyError(id, "axis must be 1 or 2");
  Axis &a = axes[index];
  if (a.moving) return replyError(id, "axis busy");
  a.pos = 0;
  a.homed = true;
  Serial.printf("{\"id\":%ld,\"ok\":true,\"axis\":%d,\"pos\":0}\n", id, index + 1);
}

static void cmdLimits(long id, char *args) {
  int index = parseAxis(strtok(args, " "));
  float lo, hi;
  if (index < 0) return replyError(id, "axis must be 1 or 2");
  if (!parseFloat(strtok(nullptr, " "), lo) || !parseFloat(strtok(nullptr, " "), hi))
    return replyError(id, "usage: LIMITS axis min max");
  if (!(lo <= 0 && hi >= 0 && lo < hi && fabsf(lo) <= POS_ABS_MAX && fabsf(hi) <= POS_ABS_MAX))
    return replyError(id, "need min <= 0 <= max");
  Axis &a = axes[index];
  if (a.moving) return replyError(id, "axis busy");
  a.minPos = lo;
  a.maxPos = hi;
  a.limitsSet = true;
  Serial.printf("{\"id\":%ld,\"ok\":true,\"axis\":%d,\"min\":%.1f,\"max\":%.1f}\n", id, index + 1, lo, hi);
}

static void cmdArm(long id, char *args) {
  long on;
  if (!parseLong(strtok(args, " "), on) || (on != 0 && on != 1)) return replyError(id, "usage: ARM 0|1");
  if (on) {
    armed = true;
  } else {
    stopAll("disarm");
    armed = false;
  }
  Serial.printf("{\"id\":%ld,\"ok\":true,\"armed\":%s}\n", id, armed ? "true" : "false");
}

// Averages `samples` ADC reads (calibrated millivolts) of one input-only pin.
static void replyAdc(long id, char *args, int pin, uint32_t *seq) {
  long samples = 16;
  char *token = strtok(args, " ");
  if (token && (!parseLong(token, samples) || samples < 1 || samples > 64)) return replyError(id, "samples 1..64");
  uint32_t mvSum = 0, rawSum = 0;
  uint32_t mvMin = UINT32_MAX, mvMax = 0;
  for (long i = 0; i < samples; i++) {
    uint32_t mv = analogReadMilliVolts(pin);
    rawSum += analogRead(pin);
    mvSum += mv;
    mvMin = min(mvMin, mv);
    mvMax = max(mvMax, mv);
    delay(2);
  }
  uint32_t n = seq ? ++(*seq) : 0;
  Serial.printf("{\"id\":%ld,\"ok\":true,\"pin\":%d,\"seq\":%lu,\"up\":%lu,\"mv\":%.1f,\"raw\":%.1f,\"min\":%lu,\"max\":%lu,\"n\":%ld}\n",
                id, pin, (unsigned long)n, (unsigned long)millis(), (float)mvSum / samples, (float)rawSum / samples,
                (unsigned long)mvMin, (unsigned long)mvMax, samples);
}

static void handleLine(char *line) {
  char *idToken = strtok(line, " ");
  char *command = strtok(nullptr, " ");
  char *args = strtok(nullptr, "");  // rest of the line
  long id;
  if (!parseLong(idToken, id) || !command) {
    disarm("bad_command");  // garbage on the link: be safe
    replyError(-1, "expected: <id> <COMMAND> [args]");
    return;
  }
  for (char *p = command; *p; p++) *p = toupper(*p);
  static char empty[] = "";
  if (!args) args = empty;

  if (!strcmp(command, "PING")) {
    Serial.printf("{\"id\":%ld,\"ok\":true,\"fw\":\"%s\",\"proto\":%d,\"up\":%lu}\n", id, FIRMWARE, PROTOCOL,
                  (unsigned long)millis());
  } else if (!strcmp(command, "KA")) {
    Serial.printf("{\"id\":%ld,\"ok\":true}\n", id);
  } else if (!strcmp(command, "STATUS")) {
    replyStatus(id);
  } else if (!strcmp(command, "STOP")) {
    stopAll("stop_command");
    armed = false;
    Serial.printf("{\"id\":%ld,\"ok\":true}\n", id);
  } else if (!strcmp(command, "ARM")) {
    cmdArm(id, args);
  } else if (!strcmp(command, "MOVE2")) {
    cmdMove2(id, args);
  } else if (!strcmp(command, "MOVE")) {
    cmdMove(id, args);
  } else if (!strcmp(command, "TRIM")) {
    cmdTrim(id, args);
  } else if (!strcmp(command, "DIR")) {
    cmdDir(id, args);
  } else if (!strcmp(command, "RAW")) {
    cmdRaw(id, args);
  } else if (!strcmp(command, "HOME")) {
    cmdHome(id, args);
  } else if (!strcmp(command, "LIMITS")) {
    cmdLimits(id, args);
  } else if (!strcmp(command, "PH")) {
    replyAdc(id, args, PH_PIN, &phSeq);
  } else if (!strcmp(command, "SOIL")) {
    replyAdc(id, args, SOIL_PIN, nullptr);
  } else {
    replyError(id, "unknown command");
  }
}

static void readSerial() {
  while (Serial.available()) {
    int c = Serial.read();
    lastRxMs = millis();
    if (c == '\r') continue;
    if (c == '\n') {
      if (!lineOverflow && lineLen > 0) {
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf);
      } else if (lineOverflow) {
        disarm("bad_command");
        replyError(-1, "line too long");
      }
      lineLen = 0;
      lineOverflow = false;
    } else if (c < 32 || c > 126) {
      continue;  // ignore non-printable bytes
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = (char)c;
    } else {
      lineOverflow = true;
    }
  }
}

static void updateMotion() {
  uint32_t now = millis();
  bool anyMoving = false;
  for (int i = 0; i < 2; i++) {
    Axis &a = axes[i];
    if (!a.moving) continue;
    anyMoving = true;
    if (now - a.moveStart >= a.moveMs) {
      stopAxis(i, "done", now);
      continue;
    }
    if (a.homed && a.limitsSet) {
      float p = livePos(a, now);
      if (p < a.minPos || p > a.maxPos) stopAxis(i, "limit", now);
    }
  }
  if ((anyMoving || armed) && now - lastRxMs > COMM_TIMEOUT_MS) disarm("comm_timeout");
}

static void initAxis(Axis &a, int pin, int stopValue) {
  a.pin = pin;
  a.stopValue = stopValue;
  a.dir = 1;
  a.raw = false;
  a.moving = false;
  a.speed = 0;
  a.moveStart = 0;
  a.moveMs = 0;
  a.pos = 0;
  a.homed = false;
  a.limitsSet = false;
  a.minPos = 0;
  a.maxPos = 0;
  a.lastStop = "boot";
  a.servo.setPeriodHertz(50);
  a.servo.attach(pin, SERVO_MIN_US, SERVO_MAX_US);
  writeStop(a);
}

void setup() {
  // Servos first: command the calibrated stop values before anything else.
  initAxis(axes[0], SERVO1_PIN, SERVO1_STOP);
  initAxis(axes[1], SERVO2_PIN, SERVO2_STOP);

  analogReadResolution(12);
  analogSetPinAttenuation(PH_PIN, ADC_11db);    // ~0.15-3.1 V usable; never exceed 3.3 V
  analogSetPinAttenuation(SOIL_PIN, ADC_11db);

  Serial.begin(115200);
  lastRxMs = millis();
  enableLoopWDT();  // a hung loop resets the board (servos restart stopped)
  Serial.printf("\n{\"ev\":\"boot\",\"fw\":\"%s\",\"proto\":%d,\"reset\":%d}\n", FIRMWARE, PROTOCOL,
                (int)esp_reset_reason());
}

void loop() {
  readSerial();
  updateMotion();
  delay(1);
}

"""Two-axis pH-probe arm driven by continuous-rotation servos on the ESP32.

The servos have no position feedback, so everything here is open loop. The
ESP32 integrates speed x time into a position ESTIMATE per axis (unit: ms at
100 % speed) and enforces the travel limits against it. The estimate drifts
(load, gravity, battery voltage), so:

  * The arm position is never assumed. After every start or ESP32 reset the
    probe state is "unknown" until an operator jogs the arm to its fully
    retracted rest pose and declares it with HOME.
  * Limits (ARM_AXIS*_LIMITS) and the insert sequence (ARM_INSERT_STEPS) must
    be measured on the real arm with margin before the mechanical end stops
    (docs/COMMISSIONING.md).
  * Without homing and limits the ESP32 only accepts short, slow jogs.

ARM_MODE gates what the Pi will ask for:
  disabled  no motion at all (default). STOP and status still work.
  manual    commissioning: neutral test, jog, home, retract.
  auto      also insert and the full pH measurement sequence; requires a
            valid insert sequence and limits AND ARM_COMMISSIONED=true, which
            the operator sets only after physically verifying stop values,
            directions, limits and emergency stop (docs/COMMISSIONING.md).

The servo stop values (ARM_SERVO1_STOP=96, ARM_SERVO2_STOP=92) and directions
(ARM_SERVO*_DIR) are provisional and pushed to the ESP32 on every connect.

App "Arm Up / Arm Down" (nudge): servo 1 = elbow, servo 2 = wrist, started in
the same instant (firmware MOVE2) in opposite directions (ARM_UP_ELBOW_DIR /
ARM_UP_WRIST_DIR); "down" reverses both. The shoulder is never commanded. Each
nudge is one short capped move (ARM_NUDGE_SPEED / ARM_NUDGE_MS); the app repeats
it while the button is held and sends STOP on release. If requests stop arriving
the move ends by itself, and the firmware stops/disarms after 2.5 s of silence.

Before each operation the Pi arms the firmware ("ARM 1") and disarms it
afterwards; the firmware also disarms on boot, STOP, bad input and link loss.
Real end-stop protection needs limit switches or a position sensor; this
module only keeps the open-loop motion bounded and stoppable.
"""

import math
import threading
import time

from agribot.esp32 import Esp32Error

AXES = (1, 2)
MAX_STEP_MS = 3000  # must match MAX_MOVE_MS in the firmware
RETRACT_CHUNK_MS = 2500
HOME_TOLERANCE = 15.0  # estimate units treated as "at home"
POLL_SECONDS = 0.15  # STATUS polling while moving
JOG_MAX_MS = 400  # firmware cap for unhomed axes; ARM_JOG_MAX_MS may be lower
JOG_MAX_SPEED_UNHOMED = 40
RAW_MAX_OFFSET = 10  # neutral test stays within stop +/- 10 (firmware enforces too)
NEUTRAL_TEST_MAX_MS = 3000
STOP_LATCH_SECONDS = 0.5  # after a stop, nudges are refused briefly (late in-flight requests)
NUDGE_DIRECTIONS = ("up", "down")
MODES = ("disabled", "manual", "auto")
PROBE_STATES = ("unknown", "retracted", "moving", "inserted", "partial", "clear")
# "clear" = an operator visually confirmed the arm is raised and the probe is off
# the ground, while the firmware reported both servos stopped and disarmed. It is
# NOT a measured position: it only releases the wheel lock, and does not count
# as home/retracted for automatic insertion.
CONFIRM_RAISED_PHRASE = "raised-and-clear"


class ArmError(RuntimeError):
    pass


def parse_steps(text):
    """'1:-40:800, 2:30:500' -> [(1, -40, 800), (2, 30, 500)]."""
    steps = []
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        fields = part.split(":")
        if len(fields) != 3:
            raise ValueError(f"bad step '{part}' (use axis:speed:ms)")
        try:
            axis, speed, ms = (int(f) for f in fields)
        except ValueError:
            raise ValueError(f"bad step '{part}' (whole numbers only)") from None
        if axis not in AXES or speed == 0 or not -100 <= speed <= 100 or not 1 <= ms <= MAX_STEP_MS:
            raise ValueError(f"bad step '{part}' (axis 1|2, speed -100..100 not 0, ms 1..{MAX_STEP_MS})")
        steps.append((axis, speed, ms))
    return steps


def parse_limits(text):
    """'-1200:0' -> (-1200.0, 0.0); '' -> None."""
    if not text:
        return None
    low, sep, high = text.partition(":")
    try:
        low, high = float(low), float(high)
    except ValueError:
        raise ValueError(f"bad limits '{text}' (use min:max)") from None
    if not sep or not low <= 0 <= high or low == high:
        raise ValueError(f"bad limits '{text}' (need min <= 0 <= max, home is 0)")
    return low, high


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Workflow:
    """Step-by-step record of one arm operation, for the API and the logs."""

    def __init__(self, name, steps):
        self.name = name
        self.started = now_iso()
        self.finished = None
        self.steps = [{"step": s, "status": "pending", "startedAt": None, "finishedAt": None, "detail": None} for s in steps]

    def _get(self, name):
        for step in self.steps:
            if step["step"] == name:
                return step
        raise KeyError(name)

    def begin(self, name):
        step = self._get(name)
        step.update(status="running", startedAt=now_iso())

    def done(self, name, detail=None):
        self._get(name).update(status="done", finishedAt=now_iso(), detail=detail)

    def fail(self, name, detail):
        self._get(name).update(status="failed", finishedAt=now_iso(), detail=detail)

    def close(self, failure=None):
        """Mark whatever did not run; a running step at failure time is the failed one."""
        self.finished = now_iso()
        for step in self.steps:
            if step["status"] == "running":
                step.update(status="failed", finishedAt=self.finished, detail=step["detail"] or failure)
            elif step["status"] == "pending":
                step["status"] = "skipped"

    def as_dict(self):
        incomplete = [s["step"] for s in self.steps if s["status"] != "done"]
        return {"name": self.name, "startedAt": self.started, "finishedAt": self.finished, "steps": self.steps, "incomplete": incomplete}


class Arm:
    def __init__(self, config, link, ph, is_driving=lambda: False):
        self.cfg = config
        self.link = link
        self.ph = ph
        self.is_driving = is_driving
        self.mode = config.arm_mode if config.arm_mode in MODES else "disabled"
        self.config_error = None
        self.insert_steps = []
        self.limits = {}
        self.probe = "unknown"  # never assumed: an operator must HOME the arm
        self.operation = None
        self.workflow = None
        self.last_result = None
        self.last_stop_event = None
        self.faults = []
        self._abort = threading.Event()
        self._op_lock = threading.Lock()
        self._stopped_at = 0.0
        self.last_confirmation = None
        try:
            self.insert_steps = parse_steps(config.arm_insert_steps)
            for axis, text in ((1, config.arm_axis1_limits), (2, config.arm_axis2_limits)):
                limits = parse_limits(text)
                if limits:
                    self.limits[axis] = limits
            self._check_steps_fit()
        except ValueError as error:
            self.config_error = str(error)
            print(f"[arm] Config error, automatic sequences disabled: {error}")

    def _check_steps_fit(self):
        position = {1: 0.0, 2: 0.0}
        for axis, speed, ms in self.insert_steps:
            if axis not in self.limits:
                raise ValueError(f"ARM_INSERT_STEPS uses axis {axis} but ARM_AXIS{axis}_LIMITS is not set")
            position[axis] += speed * ms / 100
            low, high = self.limits[axis]
            if not low <= position[axis] <= high:
                raise ValueError(f"ARM_INSERT_STEPS takes axis {axis} to {position[axis]:.0f}, outside its limits {low:.0f}:{high:.0f}")

    # --- status -------------------------------------------------------------------
    @property
    def available(self):
        return self.link.enabled and self.link.connected

    @property
    def auto_ready(self):
        return self.mode == "auto" and self.cfg.arm_commissioned and not self.config_error and bool(self.insert_steps)

    def stop_value(self, axis):
        return self.cfg.arm_servo1_stop if axis == 1 else self.cfg.arm_servo2_stop

    def _auto_blocker(self):
        if self.config_error:
            return f"arm config error: {self.config_error}"
        if not self.cfg.arm_commissioned:
            return "arm not commissioned (ARM_COMMISSIONED=false): verify stop values, directions, limits and emergency stop first"
        if not self.insert_steps:
            return "ARM_INSERT_STEPS is not set: commission the arm first"
        return None

    def _firmware_axes(self):
        axes = self.link.request("STATUS")["axes"]
        return {a["axis"]: a for a in axes}

    def summary(self):
        if not self.link.enabled:
            return "esp32 disabled"
        if not self.link.connected:
            return "offline"
        if self.operation:
            return "busy"
        if self.mode == "disabled":
            return "motion disabled"
        if self.config_error:
            return "config error"
        if self.mode == "manual":
            return "manual (uncommissioned)" if not self.cfg.arm_commissioned else "manual"
        return "ready" if self.auto_ready else "uncommissioned"

    def retract_allowed(self, firmware_axes=None):
        """Whether a remote (app) retract may be offered: only on a commissioned arm
        whose axes the firmware reports as homed, so the target pose is defined."""
        if not (self.cfg.arm_commissioned and self.mode in ("manual", "auto") and self.available) or self.operation:
            return False
        if self.probe in ("retracted", "moving") or not isinstance(firmware_axes, list):
            return False
        homed = {a.get("axis"): a.get("homed") for a in firmware_axes if isinstance(a, dict)}
        return homed.get(1) is True and homed.get(2) is True

    def state(self, include_firmware=True):
        firmware = None
        if include_firmware and self.link.connected:
            try:
                firmware = self.link.request("STATUS", timeout=1.0)
                firmware = {"armed": firmware.get("armed"), "axes": firmware.get("axes")}
            except Esp32Error as error:
                firmware = {"error": str(error)}
        return {
            "status": self.summary(),
            "mode": self.mode,
            "commissioned": self.cfg.arm_commissioned,
            "servos": {"1": {"stop": self.cfg.arm_servo1_stop, "dir": self.cfg.arm_servo1_dir},
                       "2": {"stop": self.cfg.arm_servo2_stop, "dir": self.cfg.arm_servo2_dir}},
            "jogLimits": {"maxMs": self.cfg.arm_jog_max_ms, "maxSpeed": self.cfg.arm_jog_max_speed},
            "controls": {
                "appControl": self.cfg.arm_app_control,
                "nudgeAllowed": self.mode in ("manual", "auto") and self.available,
                "nudge": {"speed": self.cfg.arm_nudge_speed, "ms": self.cfg.arm_nudge_ms},
                "up": {"elbow(servo1)": self.cfg.arm_up_elbow_dir, "wrist(servo2)": self.cfg.arm_up_wrist_dir},
                "driveLocked": self.drive_block_reason() is not None,
                "confirmRaisedAllowed": (self.mode in ("manual", "auto") and self.available and not self.operation
                                         and self.probe not in ("inserted", "moving")),
                "retractAllowed": self.retract_allowed((firmware or {}).get("axes")),
            },
            "lastConfirmation": self.last_confirmation,
            "probe": self.probe,
            "operation": self.operation,
            "workflow": self.workflow.as_dict() if self.workflow else None,
            "configError": self.config_error,
            "insertSteps": [{"axis": a, "speed": s, "ms": m} for a, s, m in self.insert_steps],
            "limits": {str(a): {"min": lo, "max": hi} for a, (lo, hi) in self.limits.items()},
            "firmware": firmware,
            "lastResult": self.last_result,
            "lastStopEvent": self.last_stop_event,
            "faults": self.faults[-10:],
        }

    def drive_block_reason(self):
        """Reason the wheels must not move (probe could be in the soil), or None."""
        if not self.cfg.drive_interlock or not self.link.enabled:
            return None
        if self.operation:
            return f"arm busy ({self.operation})"
        if self.probe in ("inserted", "moving", "partial"):
            return (f"pH probe is {self.probe}: retract or re-home the arm, or confirm it is raised "
                    "(app: Arm Raised) before driving")
        return None

    def _fault(self, message):
        self.faults.append({"at": now_iso(), "message": message})
        del self.faults[:-20]
        print(f"[arm] Fault: {message}")

    # --- ESP32 callbacks ----------------------------------------------------------
    def on_connect(self):
        """Push limits to the ESP32 and work out what is known about the arm."""
        try:
            self.link.request("ARM", 0)
            for axis in AXES:
                self.link.request("TRIM", axis, self.stop_value(axis))
                self.link.request("DIR", axis, self.cfg.arm_servo1_dir if axis == 1 else self.cfg.arm_servo2_dir)
            for axis, (low, high) in self.limits.items():
                self.link.request("LIMITS", axis, f"{low:.1f}", f"{high:.1f}")
            axes = self._firmware_axes()
            if all(axes[a]["homed"] for a in AXES) and self.probe in ("retracted", "unknown"):
                # Firmware kept its home (Pi service restarted, ESP32 did not).
                at_home = all(abs(axes[a]["pos"]) <= HOME_TOLERANCE for a in AXES)
                self.probe = "retracted" if at_home else "partial"
        except (Esp32Error, KeyError, TypeError) as error:
            self._fault(f"could not configure the ESP32: {error}")

    def on_event(self, event):
        kind = event.get("ev")
        if kind == "boot":
            # The ESP32 restarted (power dip, reset): servos stopped, home and limits lost.
            self._fault(f"ESP32 restarted (reset reason {event.get('reset')}): arm must be re-homed")
            self._abort.set()
            self.probe = "partial" if self.probe in ("inserted", "moving", "partial") else "unknown"
            if self.link.connected:
                self.on_connect()
        elif kind == "link_lost":
            self._abort.set()
            if self.probe in ("moving", "inserted"):
                self.probe = "partial"
        elif kind == "stop":
            self.last_stop_event = {**event, "at": now_iso()}
            if event.get("reason") not in ("done", "stop_command"):
                self._fault(f"axis {event.get('axis')} stopped: {event.get('reason')}")
        elif kind == "fault":
            self._fault(str(event.get("msg") or event))

    # --- motion primitives ------------------------------------------------------------
    def _check_abort(self):
        """A stop requested since this operation began wins over the next command."""
        if self._abort.is_set():
            self._safe_stop()
            raise ArmError("stopped by request")

    def _move(self, axis, speed, ms):
        """Run one bounded move and wait until the ESP32 reports it finished."""
        self._check_abort()
        self.link.request("MOVE", axis, int(speed), int(ms))
        deadline = time.monotonic() + ms / 1000 + 2.0
        while True:
            if self._abort.wait(POLL_SECONDS):
                self._safe_stop()
                raise ArmError("stopped by request")
            status = self._firmware_axes()[axis]
            if not status["moving"]:
                if status["last"] != "done":
                    raise ArmError(f"axis {axis} stopped early: {status['last']}")
                return status["pos"]
            if time.monotonic() > deadline:
                self._safe_stop()
                raise ArmError(f"axis {axis} move did not finish in time")

    def _move2(self, speed1, speed2, ms):
        """Start both servos in the same instant and wait until both have stopped."""
        self._check_abort()
        self.link.request("MOVE2", int(speed1), int(speed2), int(ms))
        moved = [axis for axis, speed in ((1, speed1), (2, speed2)) if speed]
        deadline = time.monotonic() + ms / 1000 + 2.0
        while True:
            if self._abort.wait(POLL_SECONDS):
                self._safe_stop()
                raise ArmError("stopped by request")
            axes = self._firmware_axes()
            if not any(axes[a]["moving"] for a in moved):
                for axis in moved:
                    if axes[axis]["last"] != "done":
                        raise ArmError(f"axis {axis} stopped early: {axes[axis]['last']}")
                return {str(a): axes[a]["pos"] for a in (1, 2)}
            if time.monotonic() > deadline:
                self._safe_stop()
                raise ArmError("move did not finish in time")

    def _safe_stop(self):
        try:
            self.link.request("STOP", timeout=1.0)
        except Esp32Error:
            pass  # the ESP32 also stops by itself when the link goes quiet

    def _require_mode(self, needed):
        order = {"disabled": 0, "manual": 1, "auto": 2}
        if order[self.mode] < order[needed]:
            raise ArmError(f"ARM_MODE={self.mode} does not allow this (needs {needed}); see docs/COMMISSIONING.md")

    def _begin(self, name, needed_mode):
        self._require_mode(needed_mode)
        if not self.link.enabled:
            raise ArmError("Arm unavailable: ESP32_ENABLED=false")
        if not self.link.connected:
            raise ArmError(f"ESP32 not connected: {self.link.error}")
        if not self._op_lock.acquire(blocking=False):
            raise ArmError(f"Arm busy ({self.operation})")
        self.operation = name  # from here on the drive interlock is active
        if self.is_driving():
            self.operation = None
            self._op_lock.release()
            raise ArmError("Robot is driving: stop the wheels before moving the arm")
        self._abort.clear()

    def _finish(self, name, outcome, error=None):
        try:
            self.link.request("ARM", 0, timeout=1.0)
        except Esp32Error:
            pass  # firmware disarms by itself on link loss
        if self.workflow:
            self.workflow.close(error)
        result = {"operation": name, "ok": error is None, "at": now_iso(), **(outcome or {})}
        if error is not None:
            result["error"] = error
        if self.workflow:
            result["workflow"] = self.workflow.as_dict()
        self.last_result = result
        self.operation = None
        self._op_lock.release()
        level = "done" if error is None else f"failed: {error}"
        print(f"[arm] {name} {level} (probe {self.probe})")
        return result

    def _execute(self, name, needed_mode, body, steps=(), background=False):
        """Run an operation with the lock held; optionally in a background thread."""
        self._begin(name, needed_mode)
        self.workflow = Workflow(name, steps) if steps else None

        def run():
            try:
                self._check_abort()  # a stop between _begin and here must not be re-armed away
                self.link.request("ARM", 1)
                outcome = body()
            except (ArmError, Esp32Error, RuntimeError, KeyError, TypeError) as error:
                self._safe_stop()
                return self._finish(name, None, str(error) or type(error).__name__)
            except BaseException as error:  # never leave the servos armed
                self._safe_stop()
                self._finish(name, None, f"unexpected error: {error!r}")
                raise
            return self._finish(name, outcome)

        if background:
            threading.Thread(target=run, name=f"arm-{name}", daemon=True).start()
            return {"operation": name, "accepted": True, "probe": self.probe}
        result = run()
        if not result["ok"]:
            raise ArmError(result["error"])
        return result

    # --- public operations ------------------------------------------------------------
    def stop(self, reason="stop requested"):
        """Stop arm motion now. Always allowed, in every mode, even mid-operation."""
        self._abort.set()
        self._stopped_at = time.monotonic()
        if self.link.connected:
            self._safe_stop()
        if self.operation:
            print(f"[arm] Stopped: {reason}")
        if self.probe == "moving":
            self.probe = "partial"
        return {"stopped": True, "probe": self.probe}

    def jog(self, axis, speed, ms):
        try:
            axis, speed, ms = int(axis), int(speed), int(ms)
        except (TypeError, ValueError):
            raise ArmError("jog needs whole numbers: axis, speed, ms") from None
        max_ms, max_speed = self.cfg.arm_jog_max_ms, self.cfg.arm_jog_max_speed
        if axis not in AXES or speed == 0 or abs(speed) > max_speed or not 1 <= ms <= max_ms:
            raise ArmError(f"jog needs axis 1|2, speed -{max_speed}..{max_speed} (not 0), ms 1..{max_ms} (ARM_JOG_MAX_SPEED/ARM_JOG_MAX_MS)")

        def body():
            self.probe = "partial"  # any jog leaves the known pose; wheels locked until retract/home
            pos = self._move(axis, speed, ms)
            return {"axis": axis, "pos": pos}

        return self._execute("jog", "manual", body)

    def nudge(self, direction):
        """One short simultaneous elbow + wrist move for the app's Arm Up / Arm Down."""
        if direction not in NUDGE_DIRECTIONS:
            raise ArmError("direction must be 'up' or 'down'")
        if time.monotonic() - self._stopped_at < STOP_LATCH_SECONDS:
            raise ArmError("stop was just requested; release and press again")
        sign = 1 if direction == "up" else -1
        elbow = sign * self.cfg.arm_up_elbow_dir * self.cfg.arm_nudge_speed
        wrist = sign * self.cfg.arm_up_wrist_dir * self.cfg.arm_nudge_speed
        ms = self.cfg.arm_nudge_ms

        def body():
            # Timed motion: the pose is not known any more and the probe may be
            # down, so the wheels stay locked until a retract or re-home.
            self.probe = "partial"
            pos = self._move2(elbow, wrist, ms)
            return {"direction": direction, "elbowSpeed": elbow, "wristSpeed": wrist, "ms": ms, "pos": pos}

        return self._execute("nudge", "manual", body)

    def confirm_raised(self, confirmation, source="unknown"):
        """Operator confirms the arm is raised and the probe is clear of the ground.

        Accepted only if: the exact confirmation phrase is sent, ARM_MODE allows
        manual control, the ESP32 is connected, no arm operation is running, the
        wheels are stopped, the probe is not known to be inserted, and a FRESH
        firmware STATUS shows motion disarmed and both servos stopped. Nothing is
        sent that could move a servo. The firmware cannot measure position, so
        this records an operator statement; it never fakes a home position."""
        if confirmation != CONFIRM_RAISED_PHRASE:
            raise ArmError(f'Explicit confirmation required: send "confirm": "{CONFIRM_RAISED_PHRASE}"')
        self._require_mode("manual")
        if not self.link.enabled:
            raise ArmError("ESP32_ENABLED=false: arm state cannot be verified; wheels stay locked")
        if not self.link.connected:
            raise ArmError(f"ESP32 not connected ({self.link.error}): cannot verify the servos are stopped; wheels stay locked")
        if self.probe in ("inserted", "moving"):
            raise ArmError(f"probe is {self.probe} according to the last sequence: use Retract instead")
        if self.is_driving():
            raise ArmError("Robot is driving: stop the wheels first")
        if not self._op_lock.acquire(blocking=False):
            raise ArmError(f"Arm busy ({self.operation}); wheels stay locked")
        self.operation = "confirm_raised"
        try:
            try:
                status = self.link.request("STATUS", timeout=1.0)  # fresh, never cached
            except Esp32Error as error:
                raise ArmError(f"could not read the firmware state ({error}); wheels stay locked") from None
            axes = status.get("axes")
            problems = []
            if status.get("armed") is not False:
                problems.append("firmware motion is armed")
            if not isinstance(axes, list) or sorted(a.get("axis") for a in axes if isinstance(a, dict)) != [1, 2]:
                problems.append("firmware did not report both servos")
            else:
                problems += [f"servo {a['axis']} is moving" for a in axes if a.get("moving") is not False]
            if problems:
                self._fault("Arm Raised confirmation rejected: " + "; ".join(problems))
                raise ArmError("Not safe to confirm: " + "; ".join(problems) + "; wheels stay locked")
            previous = self.probe
            self.probe = "clear"
            self.last_confirmation = {"at": now_iso(), "source": source, "previousProbe": previous,
                                      "firmwareUptimeMs": status.get("up"), "note": "operator visual confirmation; position not measured"}
            print(f"[arm] Operator confirmed arm raised and probe clear (via {source}; was {previous}); wheel lock released")
        finally:
            self.operation = None
            self._op_lock.release()
        # Evaluated after the operation ended, so it reflects the real lock state.
        return {"probe": self.probe, "driveLocked": self.drive_block_reason() is not None, "confirmation": self.last_confirmation}

    def neutral_test(self, axis, value, ms):
        """Hold one raw servo value near neutral for a short time to find the true
        stop point (the value where the horn does not creep). Manual mode only.
        The position estimate is lost afterwards, so the arm must be re-homed."""
        try:
            axis, value, ms = int(axis), int(value), int(ms)
        except (TypeError, ValueError):
            raise ArmError("neutral test needs whole numbers: axis, value, ms") from None
        if axis not in AXES:
            raise ArmError("axis must be 1 or 2")
        stop = self.stop_value(axis)
        if abs(value - stop) > RAW_MAX_OFFSET or not 1 <= ms <= NEUTRAL_TEST_MAX_MS:
            raise ArmError(f"neutral test needs value {stop - RAW_MAX_OFFSET}..{stop + RAW_MAX_OFFSET} and ms 1..{NEUTRAL_TEST_MAX_MS}")

        def body():
            self.probe = "unknown"  # the arm may creep: position no longer known
            self.link.request("RAW", axis, value, ms)
            deadline = time.monotonic() + ms / 1000 + 2.0
            while True:
                if self._abort.wait(POLL_SECONDS):
                    self._safe_stop()
                    raise ArmError("stopped by request")
                status = self._firmware_axes()[axis]
                if not status["moving"]:
                    break
                if time.monotonic() > deadline:
                    self._safe_stop()
                    raise ArmError("neutral test did not finish in time")
            return {"axis": axis, "value": value, "ms": ms, "configuredStop": stop,
                    "note": "watch the servo horn: no movement at this value means it is the true stop; set ARM_SERVO%d_STOP accordingly, then re-home" % axis}

        return self._execute("neutral_test", "manual", body)

    def home(self, axes=AXES):
        """Operator declares: the arm is at its fully retracted rest pose right now."""
        try:
            axes = tuple(sorted({int(a) for a in axes}))
        except (TypeError, ValueError):
            raise ArmError("axes must be a list of 1 and/or 2") from None
        if not axes or any(a not in AXES for a in axes):
            raise ArmError("axes must be a list of 1 and/or 2")

        def body():
            for axis in axes:
                self.link.request("HOME", axis)
            for axis, (low, high) in self.limits.items():
                if axis in axes:
                    self.link.request("LIMITS", axis, f"{low:.1f}", f"{high:.1f}")
            if set(axes) == set(AXES):
                self.probe = "retracted"
            return {"homed": list(axes), "probe": self.probe}

        return self._execute("home", "manual", body)

    def _insert(self, workflow=None):
        if not self.cfg.arm_commissioned:  # defence in depth: never insert uncommissioned
            raise ArmError("arm not commissioned (ARM_COMMISSIONED=false)")
        if self.config_error:
            raise ArmError(f"arm config error: {self.config_error}")
        if not self.insert_steps:
            raise ArmError("ARM_INSERT_STEPS is not set: commission the arm first (docs/COMMISSIONING.md)")
        if self.probe != "retracted":
            raise ArmError(f"probe is {self.probe}: retract or home the arm before inserting")
        axes = self._firmware_axes()
        for axis, _, _ in self.insert_steps:
            if not (axes[axis]["homed"] and axes[axis]["limits"]):
                raise ArmError(f"axis {axis} is not homed/limited on the ESP32")
        self.probe = "moving"
        try:
            for axis, speed, ms in self.insert_steps:
                self._move(axis, speed, ms)
        except Exception:
            self.probe = "partial"
            raise
        self.probe = "inserted"

    def _retract(self):
        """Drive each axis back to its home estimate, last inserted axis first."""
        order = []
        for axis, _, _ in reversed(self.insert_steps):
            if axis not in order:
                order.append(axis)
        order += [a for a in AXES if a not in order]
        axes = self._firmware_axes()
        if not all(axes[a]["homed"] for a in order):
            raise ArmError("arm is not homed: jog it back to the rest pose, check it by eye, then HOME it")
        self.probe = "moving"
        try:
            for axis in order:
                pos = axes[axis]["pos"]
                guard = 0
                while abs(pos) > HOME_TOLERANCE:
                    guard += 1
                    if guard > 20:
                        raise ArmError(f"axis {axis} did not reach home after 20 moves")
                    speed = self.cfg.arm_retract_speed if pos < 0 else -self.cfg.arm_retract_speed
                    # Round DOWN: never aim past home (a limit often ends exactly at 0,
                    # and the firmware rejects moves that would cross it).
                    ms = min(RETRACT_CHUNK_MS, max(1, math.floor(abs(pos) * 100 / abs(speed))))
                    pos = self._move(axis, speed, ms)
        except Exception:
            self.probe = "partial"
            raise
        self.probe = "retracted"

    def retract(self, background=True):
        return self._execute("retract", "manual", lambda: self._retract() or {"probe": self.probe}, background=background)

    def insert(self, background=True):
        self._require_mode("auto")
        if self._auto_blocker():
            raise ArmError(self._auto_blocker())
        return self._execute("insert", "auto", lambda: self._insert() or {"probe": self.probe}, background=background)

    def measure_ph(self, background=True):
        """Full workflow: checks -> insert -> settle -> sample -> retract -> report."""
        self._require_mode("auto")
        if self._auto_blocker():
            raise ArmError(self._auto_blocker())
        if not self.ph.calibrated:
            raise ArmError(self.ph.error or "pH probe is not calibrated (set PH_CALIBRATION)")

        def body():
            wf = self.workflow
            wf.begin("checks")
            status = self.link.request("STATUS")
            if not all(a["homed"] for a in status["axes"]):
                raise ArmError("arm is not homed")
            pre = self.ph.read_live()
            if any("ADC" in w for w in pre["warnings"]):
                raise ArmError("pH board check failed before moving: " + "; ".join(pre["warnings"]))
            wf.done("checks", {"firmware": self.link.firmware, "preCheckMv": pre["mv"]})

            wf.begin("insert")
            self._insert()
            wf.done("insert")
            result = None
            try:
                wf.begin("settle")
                if self._abort.wait(self.cfg.ph_settle_seconds):
                    raise ArmError("stopped by request")
                wf.done("settle", {"seconds": self.cfg.ph_settle_seconds})
                wf.begin("sample")
                result = self.ph.measure(self.cfg.ph_samples, interval=self.cfg.ph_sample_interval, abort=self._abort)
                wf.done("sample", result)
            finally:
                if not self._abort.is_set():  # an explicit stop means "stay still"
                    wf.begin("retract")
                    self._retract()
                    wf.done("retract", {"probe": self.probe})
            wf.begin("report")
            wf.done("report")
            if not result["valid"]:
                raise ArmError("measurement rejected: " + "; ".join(result["warnings"] or ["no pH value"]))
            return {"ph": result, "probe": self.probe}

        return self._execute("measure", "auto", body, steps=("checks", "insert", "settle", "sample", "retract", "report"), background=background)

    def shutdown(self):
        self.stop("service stopping")

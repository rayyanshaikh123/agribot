"""Motor control for the L298N, driven by a PS5 controller or the phone app.

Pins follow docs/PINOUT.md. Speed is PWM on the IN pins, so it works with the
ENA/ENB jumper caps left on (the enable GPIOs are then simply unconnected).

Safety: the motors stop when no command arrives for DRIVE_TIMEOUT_MS, when the
controller disconnects, and when the service exits.
"""

import threading
import time

GAMEPAD_NAMES = ("dualsense", "wireless controller", "ps5", "playstation")


def _clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, float(value)))


def arcade(throttle, turn):
    """Stick values (-1..1) to left/right wheel speeds (-1..1)."""
    left, right = throttle + turn, throttle - turn
    peak = max(1.0, abs(left), abs(right))
    return left / peak, right / peak


class Drive:
    def __init__(self, config):
        self.cfg = config
        self.error = None
        self.left = self.right = 0.0
        self.source = None
        self.gamepad = None  # connected controller name
        self._last_command = 0.0
        self._gamepad_active_at = 0.0
        self._lock = threading.Lock()
        self._motors = None
        if not config.drive_enabled:
            self.error = "disabled by DRIVE_ENABLED=false"
            return
        try:
            from gpiozero import Motor

            self._motors = (
                Motor(config.motor_left_forward, config.motor_left_backward, enable=config.motor_left_enable, pwm=True),
                Motor(config.motor_right_forward, config.motor_right_backward, enable=config.motor_right_enable, pwm=True),
            )
            print(f"[drive] Motors ready (max speed {config.drive_max_speed:.0%})")
        except Exception as error:
            self.error = f"motors unavailable: {error}"
            print(f"[drive] Disabled: {self.error}")

    @property
    def ready(self):
        return self._motors is not None

    def _apply(self, left, right):
        self.left, self.right = left, right
        if not self._motors:
            return
        limit = self.cfg.drive_max_speed
        for motor, speed in zip(self._motors, (left, right)):
            if speed > 0.02:
                motor.forward(min(1.0, speed * limit))
            elif speed < -0.02:
                motor.backward(min(1.0, -speed * limit))
            else:
                motor.stop()

    def set(self, left, right, source="app"):
        """Set wheel speeds. The controller wins over the app while it is in use."""
        left, right = _clamp(left), _clamp(right)
        with self._lock:
            now = time.monotonic()
            if source == "gamepad":
                if left or right:
                    self._gamepad_active_at = now
                elif self.source != "gamepad":
                    return self.state()  # an idle controller never cancels the app
            elif now - self._gamepad_active_at < 0.5:
                return self.state()
            self._last_command = now
            self.source = source if (left or right) else None
            self._apply(left, right)
            return self.state()

    def stop(self):
        with self._lock:
            self.source = None
            self._apply(0.0, 0.0)

    def state(self):
        return {"ready": self.ready, "left": round(self.left, 2), "right": round(self.right, 2), "source": self.source, "gamepad": self.gamepad, "error": self.error}

    # --- background loops ---------------------------------------------------
    def _watchdog(self, stop):
        timeout = self.cfg.drive_timeout_ms / 1000
        while not stop.wait(0.1):
            if (self.left or self.right) and self.source == "app" and time.monotonic() - self._last_command > timeout:
                self.stop()

    def _find_gamepad(self, evdev):
        for path in evdev.list_devices():
            device = evdev.InputDevice(path)
            name = device.name.lower()
            # The DualSense also exposes motion-sensor and touchpad devices; skip those.
            if any(n in name for n in GAMEPAD_NAMES) and "motion" not in name and "touchpad" not in name:
                if evdev.ecodes.ABS_Y in dict(device.capabilities().get(evdev.ecodes.EV_ABS, [])):
                    return device
            device.close()
        return None

    def _gamepad_loop(self, stop):
        try:
            import evdev
        except ImportError:
            print("[drive] PS5 controller support off: install python3-evdev")
            return
        codes = evdev.ecodes
        while not stop.is_set():
            device = self._find_gamepad(evdev)
            if device is None:
                stop.wait(2)
                continue
            self.gamepad = device.name
            print(f"[drive] Controller connected: {device.name}")
            ranges = {code: (info.min, info.max) for code, info in device.capabilities()[codes.EV_ABS]}
            axes = {codes.ABS_Y: 0.0, codes.ABS_RX: 0.0, codes.ABS_X: 0.0, codes.ABS_Z: 0.0, codes.ABS_RZ: 0.0}

            def norm(code, value):
                low, high = ranges.get(code, (0, 255))
                return (2 * (value - low) / (high - low)) - 1 if high > low else 0.0

            try:
                for event in device.read_loop():
                    if stop.is_set():
                        break
                    if event.type == codes.EV_KEY and event.code in (codes.BTN_EAST, codes.BTN_B) and event.value:
                        axes.update({k: 0.0 for k in axes})  # Circle = emergency stop
                    elif event.type == codes.EV_ABS and event.code in axes:
                        axes[event.code] = norm(event.code, event.value)
                    if event.type != codes.EV_SYN:
                        continue  # apply once per input report
                    dead = self.cfg.gamepad_deadzone
                    stick = -axes[codes.ABS_Y]
                    # R2 forward / L2 reverse also work (triggers rest at -1).
                    triggers = (axes[codes.ABS_RZ] + 1) / 2 - (axes[codes.ABS_Z] + 1) / 2
                    throttle = stick if abs(stick) > abs(triggers) else triggers
                    turn = axes[codes.ABS_RX] if abs(axes[codes.ABS_RX]) > dead else axes[codes.ABS_X]
                    throttle = 0.0 if abs(throttle) < dead else throttle
                    turn = 0.0 if abs(turn) < dead else turn
                    self.set(*arcade(throttle, turn), source="gamepad")
            except OSError:
                pass  # controller switched off or out of range
            finally:
                self.gamepad = None
                self._gamepad_active_at = 0.0
                self.stop()
                try:
                    device.close()
                except Exception:
                    pass
                print("[drive] Controller disconnected, motors stopped")

    def start(self, stop):
        if not self.ready:
            return
        threading.Thread(target=self._watchdog, args=(stop,), name="drive-watchdog", daemon=True).start()
        if self.cfg.gamepad_enabled:
            threading.Thread(target=self._gamepad_loop, args=(stop,), name="gamepad", daemon=True).start()

    def close(self):
        self.stop()
        for motor in self._motors or ():
            try:
                motor.close()
            except Exception:
                pass

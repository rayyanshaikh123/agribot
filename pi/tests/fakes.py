"""Test doubles: configuration builder, firmware emulator, fake serial port/link.

These mimic the documented behaviour of esp32/agribot_esp32 so the Pi-side
logic can be tested without hardware. Passing tests do NOT show that the real
servos, ADC, motors or sensors work.
"""

import dataclasses
import json
import os
import threading
import time

from agribot import config as config_module


def make_config(env=None, **overrides):
    """Config from defaults (+ env strings), with no env file and no hardware."""
    saved = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.startswith(("AGRIBOT", "ROBOT_", "ESP32", "ARM_", "PH_", "SOIL", "DHT", "RAIN", "USE_FAKE", "FAKE_", "SIMULATE", "DRIVE", "MOTOR", "CAMERA", "GAMEPAD", "BLE", "SENSOR")):
                del os.environ[key]
        os.environ["AGRIBOT_ENV_FILE"] = "/nonexistent/agribot.env"
        os.environ.update({"CAMERA": "none", "DRIVE_ENABLED": "false", "BLE_ENABLED": "false", "DHT_BACKEND": "none", "RAIN_ENABLED": "false"})
        os.environ.update(env or {})
        cfg = config_module.load_config()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


class FakeFirmware:
    """Python model of the ESP32 firmware command handling (protocol v1).

    time_scale = real seconds per firmware millisecond, so moves finish fast."""

    def __init__(self, time_scale=0.00002, ph_mv=1500.0, ph_noise=4, fw="agribot-esp32/1.3", proto=3):
        self.time_scale = time_scale
        self.ph_mv = ph_mv
        self.ph_noise = ph_noise
        self.fw = fw
        self.proto = proto
        self.armed = False
        self.ph_seq = 0
        self.log = []
        self.events = []
        self.fail_axis_with = None
        self.move2_log = []  # (speeds, ms, start time) of every accepted MOVE2  # e.g. "limit": next move of any axis ends early
        self.axes = {a: {"moving": False, "speed": 0, "start": 0.0, "ms": 0, "pos": 0.0, "homed": False, "raw": False,
                         "limits": False, "min": 0.0, "max": 0.0, "last": "boot", "stop": 96 if a == 1 else 92, "dir": 1} for a in (1, 2)}
        self._lock = threading.Lock()

    def _live(self, a):
        ax = self.axes[a]
        if not ax["moving"]:
            return ax["pos"]
        elapsed_ms = min(ax["ms"], (time.monotonic() - ax["start"]) / self.time_scale)
        return ax["pos"] + ax["speed"] * elapsed_ms / 100

    def _update(self):
        for a, ax in self.axes.items():
            if ax["moving"] and (time.monotonic() - ax["start"]) / self.time_scale >= ax["ms"]:
                self._end(ax, a, self.fail_axis_with or "done")
                self.events.append({"ev": "stop", "axis": a, "reason": ax["last"], "pos": ax["pos"]})

    def _end(self, ax, a, reason):
        ax["pos"] = self._live(a)
        ax["moving"] = False
        ax["last"] = reason
        if ax["raw"]:
            ax["raw"] = False
            ax["homed"] = False

    def stop_all(self, reason):
        for a, ax in self.axes.items():
            if ax["moving"]:
                self._end(ax, a, reason)

    def handle(self, line):
        """One command line (str) -> reply dict."""
        with self._lock:
            self._update()
            self.log.append(line)
            parts = line.split()
            try:
                rid = int(parts[0])
                cmd = parts[1].upper()
            except (IndexError, ValueError):
                self.stop_all("bad_command")
                self.armed = False
                return {"id": -1, "ok": False, "err": "expected: <id> <COMMAND> [args]"}
            args = parts[2:]
            err = lambda e: {"id": rid, "ok": False, "err": e}  # noqa: E731
            if cmd == "PING":
                return {"id": rid, "ok": True, "fw": self.fw, "proto": self.proto, "up": 1}
            if cmd == "KA":
                return {"id": rid, "ok": True}
            if cmd == "STATUS":
                axes = [{"axis": a, "stop": ax["stop"], "dir": ax["dir"], "raw": ax["raw"], "moving": ax["moving"], "speed": ax["speed"], "pos": round(self._live(a), 1), "homed": ax["homed"],
                         "limits": ax["limits"], "min": ax["min"], "max": ax["max"], "last": ax["last"]} for a, ax in self.axes.items()]
                return {"id": rid, "ok": True, "fw": self.fw, "proto": self.proto, "armed": self.armed, "axes": axes}
            if cmd == "STOP":
                self.stop_all("stop_command")
                self.armed = False
                return {"id": rid, "ok": True}
            if cmd == "ARM":
                if args not in (["0"], ["1"]):
                    return err("usage: ARM 0|1")
                if args == ["0"]:
                    self.stop_all("disarm")
                self.armed = args == ["1"]
                return {"id": rid, "ok": True, "armed": self.armed}
            if cmd == "MOVE2":
                speeds, ms = (int(args[0]), int(args[1])), int(args[2])
                if speeds == (0, 0):
                    return err("speeds must be -100..100, not both 0")
                for a, speed in zip((1, 2), speeds):
                    if not speed:
                        continue
                    ax = self.axes[a]
                    if not self.armed:
                        return err("disarmed")
                    if ax["moving"]:
                        return err(f"axis {a}: axis busy")
                    if not (ax["homed"] and ax["limits"]) and (abs(speed) > 40 or ms > 400):
                        return err(f"axis {a}: not homed/limited: jog only")
                    if ax["homed"] and ax["limits"] and not ax["min"] <= ax["pos"] + speed * ms / 100 <= ax["max"]:
                        return err(f"axis {a}: would exceed limit")
                now = time.monotonic()
                for a, speed in zip((1, 2), speeds):
                    if speed:
                        self.axes[a].update(moving=True, speed=speed, ms=ms, start=now, last="")
                self.move2_log.append((speeds, ms, now))
                return {"id": rid, "ok": True}
            if cmd == "MOVE":
                a, speed, ms = int(args[0]), int(args[1]), int(args[2])
                ax = self.axes[a]
                if not self.armed:
                    return err("disarmed")
                if ax["moving"]:
                    return err("axis busy")
                bounded = ax["homed"] and ax["limits"]
                if not bounded and (abs(speed) > 40 or ms > 400):
                    return err("not homed/limited: jog only")
                if bounded:
                    target = ax["pos"] + speed * ms / 100
                    if not ax["min"] <= target <= ax["max"]:
                        return err("would exceed limit")
                ax.update(moving=True, speed=speed, ms=ms, start=time.monotonic(), last="")
                return {"id": rid, "ok": True, "axis": a}
            if cmd == "TRIM":
                ax = self.axes[int(args[0])]
                if not 70 <= int(args[1]) <= 110:
                    return err("stop value 70..110")
                ax["stop"] = int(args[1])
                return {"id": rid, "ok": True}
            if cmd == "DIR":
                ax = self.axes[int(args[0])]
                if int(args[1]) != ax["dir"]:
                    ax["homed"] = False
                ax["dir"] = int(args[1])
                return {"id": rid, "ok": True}
            if cmd == "RAW":
                a, value, ms = int(args[0]), int(args[1]), int(args[2])
                ax = self.axes[a]
                if abs(value - ax["stop"]) > 10:
                    return err("value must be within stop +/- 10")
                if not self.armed:
                    return err("disarmed")
                ax.update(moving=True, speed=0, ms=ms, start=time.monotonic(), last="", raw=True)
                return {"id": rid, "ok": True}
            if cmd == "HOME":
                ax = self.axes[int(args[0])]
                if ax["moving"]:
                    return err("axis busy")
                ax.update(pos=0.0, homed=True)
                return {"id": rid, "ok": True}
            if cmd == "LIMITS":
                ax = self.axes[int(args[0])]
                ax.update(min=float(args[1]), max=float(args[2]), limits=True)
                return {"id": rid, "ok": True}
            if cmd in ("PH", "SOIL"):
                n = int(args[0]) if args else 16
                self.ph_seq += 1
                mv = self.ph_mv
                return {"id": rid, "ok": True, "seq": self.ph_seq, "up": 1000, "mv": mv, "raw": mv * 1.2,
                        "min": mv - self.ph_noise / 2, "max": mv + self.ph_noise / 2, "n": n}
            return err("unknown command")

    def boot(self):
        """Simulate a reset: everything forgotten, servos stopped, disarmed."""
        with self._lock:
            self.__init__(self.time_scale, self.ph_mv, self.ph_noise, self.fw, self.proto)


class FakeLink:
    """Stands in for Esp32Link, routing requests straight to a FakeFirmware."""

    def __init__(self, firmware=None, enabled=True, connected=True):
        self.fw = firmware or FakeFirmware()
        self.enabled = enabled
        self._connected = connected
        self.firmware = self.fw.fw if connected else None
        self.error = None if connected else "not connected"
        self._next = 1

    @property
    def connected(self):
        return self.enabled and self._connected

    def request(self, command, *args, timeout=2.0):
        from agribot.esp32 import Esp32Error

        if not self.connected:
            raise Esp32Error("ESP32 not connected")
        self._next += 1
        reply = self.fw.handle(" ".join([str(self._next), command, *map(str, args)]))
        if not reply.get("ok"):
            raise Esp32Error(reply.get("err"))
        return reply

    def state(self):
        return {"enabled": self.enabled, "connected": self.connected}


class FakeSerial:
    """Byte-level fake of a pyserial port wired to a FakeFirmware."""

    def __init__(self, firmware, noise=b""):
        self.fw = firmware
        self._out = bytearray(noise)
        self._cv = threading.Condition()
        self.closed = False
        self.written = []
        self.silent = False  # True: swallow commands (dead link)

    def reset_input_buffer(self):
        pass

    def write(self, data):
        if self.closed:
            raise OSError("port closed")
        self.written.append(data)
        if self.silent:
            return len(data)
        for line in data.decode().splitlines():
            reply = self.fw.handle(line)
            self.feed(json.dumps(reply).encode() + b"\n")
        return len(data)

    def feed(self, data):
        with self._cv:
            self._out.extend(data)
            self._cv.notify_all()

    def flush(self):
        pass

    def read(self, size):
        with self._cv:
            if not self._out and not self.closed:
                self._cv.wait(0.05)
            if self.closed:
                raise OSError("port closed")
            chunk = bytes(self._out[:size])
            del self._out[:size]
            return chunk

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()

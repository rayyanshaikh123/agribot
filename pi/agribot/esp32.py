"""USB serial link to the ESP32 co-processor (pH ADC + probe arm).

Protocol v3 (full description: esp32/README.md)
  Pi -> ESP32    "<id> <COMMAND> [args...]\\n"   ASCII, one command per line
  ESP32 -> Pi    {"id":<id>,"ok":true|false,...}\\n   one JSON reply per command
  ESP32 -> Pi    {"ev":"boot"|"stop"|"fault",...}\\n  unsolicited events

The link is opt-in (ESP32_ENABLED=true) and only talks to a port whose
firmware answers the PING handshake with "fw":"agribot-esp32/..." and the
expected protocol version. An unknown sketch might treat serial text as servo
commands, so nothing but PING is sent until the firmware has identified itself.

While connected, a keepalive runs every KEEPALIVE_SECONDS. If the ESP32 stops
answering, the link is dropped and reopened; the firmware independently stops
and disarms the servos when the Pi goes quiet.
"""

import json
import os
import threading
import time
from pathlib import Path

FIRMWARE_PREFIX = "agribot-esp32/"
PROTOCOL_VERSION = 3
KEEPALIVE_SECONDS = 1.0
KEEPALIVE_FAILURES_TO_DROP = 3
MAX_LINE_BYTES = 1024
# USB-UART bridges used by common ESP32 dev boards.
AUTO_PORT_HINTS = ("cp210", "silicon_labs", "ch340", "ch341", "ch910", "wch", "esp32", "espressif")
# Commands the Pi may send; anything else is refused before reaching the wire.
COMMANDS = {"PING", "KA", "STATUS", "STOP", "ARM", "MOVE", "HOME", "LIMITS", "PH", "SOIL", "TRIM", "DIR", "RAW", "MOVE2"}


class Esp32Error(RuntimeError):
    pass


def parse_line(raw):
    """One received line -> dict, or None for noise (boot ROM text, partial or malformed JSON)."""
    raw = raw.strip()
    if not raw.startswith(b"{") or len(raw) > MAX_LINE_BYTES:
        return None
    try:
        message = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def format_command(request_id, command, args):
    """Build one wire line, refusing anything the firmware parser could misread."""
    command = str(command).upper()
    if command not in COMMANDS:
        raise Esp32Error(f"unknown ESP32 command {command!r}")
    parts = [str(int(request_id)), command]
    for arg in args:
        text = str(arg)
        if not text or any(c.isspace() for c in text) or not all(32 < ord(c) < 127 for c in text):
            raise Esp32Error(f"invalid argument {text!r} for {command}")
        parts.append(text)
    line = " ".join(parts) + "\n"
    if len(line) > 90:  # firmware line buffer is 96 bytes
        raise Esp32Error("command line too long")
    return line.encode("ascii")


def check_identity(message):
    """Firmware name if a PING reply identifies compatible AgriBot firmware, else None."""
    if not message or message.get("id") != 0 or not message.get("ok"):
        return None
    firmware = str(message.get("fw", ""))
    if not firmware.startswith(FIRMWARE_PREFIX):
        return None
    if message.get("proto") != PROTOCOL_VERSION:
        raise Esp32Error(f"{firmware} speaks protocol {message.get('proto')!r}, this service needs {PROTOCOL_VERSION}: reflash esp32/agribot_esp32")
    return firmware


class Esp32Link:
    def __init__(self, config, on_event=None, on_connect=None, serial_factory=None):
        self.cfg = config
        self.on_event = on_event
        self.on_connect = on_connect
        self.port = None
        self.firmware = None
        self.error = None if config.esp32_enabled else "disabled by ESP32_ENABLED=false"
        self.connected_at = None
        self.last_rx = None  # monotonic time of the last valid message
        self.stats = {"malformed": 0, "timeouts": 0, "reconnects": 0, "events": 0}
        self._serial_factory = serial_factory  # tests inject a fake port
        self._serial = None
        self._write_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._closing = False

    @property
    def enabled(self):
        return self.cfg.esp32_enabled

    @property
    def connected(self):
        return self._serial is not None and self.firmware is not None

    # --- public API ---------------------------------------------------------------
    def request(self, command, *args, timeout=2.0):
        """Send one command and return the ESP32's JSON reply (raises Esp32Error)."""
        ser = self._serial
        if ser is None or self.firmware is None:
            raise Esp32Error(f"ESP32 not connected: {self.error or 'connecting'}")
        with self._pending_lock:
            request_id = self._next_id
            self._next_id = self._next_id % 999_999 + 1
            line = format_command(request_id, command, args)
            waiter = {"event": threading.Event(), "reply": None}
            self._pending[request_id] = waiter
        try:
            with self._write_lock:
                ser.write(line)
                ser.flush()
        except Exception as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._drop(f"write failed: {error}")
            raise Esp32Error(f"ESP32 write failed: {error}") from error
        if not waiter["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self.stats["timeouts"] += 1
            raise Esp32Error(f"ESP32 did not answer {command} within {timeout:.1f} s")
        reply = waiter["reply"]
        if reply is None:
            raise Esp32Error("ESP32 disconnected")
        if not reply.get("ok"):
            raise Esp32Error(reply.get("err") or f"ESP32 rejected {command}")
        return reply

    def start(self, stop):
        if not self.enabled:
            return
        threading.Thread(target=self._run, args=(stop,), name="esp32", daemon=True).start()
        threading.Thread(target=self._keepalive, args=(stop,), name="esp32-keepalive", daemon=True).start()

    def close(self):
        if self.connected:
            try:
                self.request("STOP", timeout=0.5)
            except Esp32Error:
                pass
        self._closing = True
        self._drop("service stopping")

    def state(self):
        age = None if self.last_rx is None else round(time.monotonic() - self.last_rx, 1)
        return {"enabled": self.enabled, "connected": self.connected, "port": self.port, "firmware": self.firmware,
                "protocol": PROTOCOL_VERSION, "lastMessageAgeSeconds": age, "error": self.error, "stats": dict(self.stats)}

    # --- connection management --------------------------------------------------------
    def _candidates(self):
        if self.cfg.esp32_port and self.cfg.esp32_port != "auto":
            return [self.cfg.esp32_port]
        by_id = Path("/dev/serial/by-id")
        if not by_id.is_dir():
            return []
        return [str(p) for p in sorted(by_id.iterdir()) if any(h in p.name.lower() for h in AUTO_PORT_HINTS)]

    def _open(self, path):
        if self._serial_factory:
            return self._serial_factory(path)
        import serial

        ser = serial.Serial()
        ser.port = path
        ser.baudrate = self.cfg.esp32_baud
        ser.timeout = 0.2
        ser.write_timeout = 1.0
        # Keep DTR/RTS released: on most ESP32 boards they drive EN/BOOT, and
        # toggling them on open would reset the board.
        ser.dtr = False
        ser.rts = False
        ser.open()
        try:
            import fcntl
            import termios

            fcntl.ioctl(ser.fileno(), termios.TIOCEXCL)  # refuse other (non-root) openers
        except Exception:
            pass
        return ser

    def _handshake(self, ser, seconds=4.0):
        """Return the firmware name, or None if the port is not AgriBot firmware."""
        deadline = time.monotonic() + seconds
        next_ping = 0.0
        buffer = b""
        ser.reset_input_buffer()
        while time.monotonic() < deadline:
            if time.monotonic() >= next_ping:
                ser.write(b"0 PING\n")
                ser.flush()
                next_ping = time.monotonic() + 0.7
            buffer += ser.read(256)
            if len(buffer) > 8192:
                buffer = buffer[-1024:]
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                firmware = check_identity(parse_line(raw))
                if firmware:
                    return firmware
        return None

    def _connect(self):
        candidates = self._candidates()
        if not candidates:
            raise Esp32Error(f"no serial port found (ESP32_PORT={self.cfg.esp32_port})")
        problems = []
        for path in candidates:
            if not self._serial_factory and not os.path.exists(path):
                problems.append(f"{path} missing")
                continue
            ser = None
            try:
                ser = self._open(path)
                firmware = self._handshake(ser)
            except Exception as error:
                problems.append(f"{path}: {error}")
                if ser:
                    ser.close()
                continue
            if firmware:
                self._serial, self.port, self.firmware, self.error = ser, path, firmware, None
                self.connected_at = self.last_rx = time.monotonic()
                return
            ser.close()
            problems.append(f"{path}: no AgriBot firmware answered (flash esp32/agribot_esp32)")
        raise Esp32Error("; ".join(problems))

    def _drop(self, reason):
        ser, self._serial = self._serial, None
        was_connected = self.firmware is not None
        self.firmware = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            waiter["event"].set()  # reply stays None -> "disconnected"
        if was_connected and not self._closing:
            self.error = f"link lost: {reason}"
            self.stats["reconnects"] += 1
            print(f"[esp32] Link lost: {reason}")
            self._notify(self.on_event, {"ev": "link_lost", "reason": reason})

    def _run(self, stop):
        logged = None
        while not stop.is_set() and not self._closing:
            if self._serial is None:
                try:
                    self._connect()
                except Exception as error:
                    self.error = str(error)
                    if logged != self.error:
                        print(f"[esp32] Not connected: {error}")
                        logged = self.error
                    stop.wait(3)
                    continue
                logged = None
                print(f"[esp32] Connected on {self.port} ({self.firmware}, protocol {PROTOCOL_VERSION})")
                self._notify(self.on_connect)
            self._read_loop(stop)

    def _keepalive(self, stop):
        failures = 0
        while not stop.wait(KEEPALIVE_SECONDS) and not self._closing:
            if not self.connected:
                failures = 0
                continue
            try:
                self.request("KA", timeout=1.0)
                failures = 0
            except Esp32Error as error:
                failures += 1
                if failures >= KEEPALIVE_FAILURES_TO_DROP and self.connected:
                    self._drop(f"no answer to keepalive ({error})")
                    failures = 0

    def _read_loop(self, stop):
        buffer = b""
        while not stop.is_set() and self._serial is not None:
            try:
                chunk = self._serial.read(256)
            except Exception as error:
                self._drop(f"read failed: {error}")
                return
            if not chunk:
                continue
            buffer += chunk
            if len(buffer) > 8192:
                buffer = b""  # no newline in 8 KB: garbage, resynchronise
                self.stats["malformed"] += 1
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self.handle_line(raw)

    def handle_line(self, raw):
        message = parse_line(raw)
        if message is None:
            if raw.strip():
                self.stats["malformed"] += 1
            return
        self.last_rx = time.monotonic()
        if "ev" in message:
            self.stats["events"] += 1
            self._notify(self.on_event, message)
            return
        with self._pending_lock:
            waiter = self._pending.pop(message.get("id"), None)
        if waiter:
            waiter["reply"] = message
            waiter["event"].set()

    @staticmethod
    def _notify(callback, *args):
        # Callbacks may send requests, which need this reader thread running.
        if callback:
            threading.Thread(target=callback, args=args, name="esp32-callback", daemon=True).start()

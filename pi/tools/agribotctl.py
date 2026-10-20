#!/usr/bin/env python3
"""Operator CLI for the running AgriBot service (local HTTP API on the Pi).

It talks to agribot.service instead of opening GPIO or the serial port itself,
so it never fights the service for hardware. Run it on the Pi:

  python3 tools/agribotctl.py status            # service, sensors, ESP32, arm summary
  python3 tools/agribotctl.py sensors           # readings with validity labels
  python3 tools/agribotctl.py esp32             # serial link state
  python3 tools/agribotctl.py ph-read           # pH board voltage now (no motion)
  python3 tools/agribotctl.py ph-sample         # probe held in soil/solution BY HAND: sample + store (no motion)
  python3 tools/agribotctl.py arm               # arm state incl. firmware position estimates
  python3 tools/agribotctl.py stop              # stop wheels AND arm (always allowed)
  python3 tools/agribotctl.py arm-stop          # stop the arm only

Motion (needs ARM_MODE=manual or auto; read docs/COMMISSIONING.md first):
  python3 tools/agribotctl.py neutral AXIS VALUE MS  # hold a raw value near stop (e.g. neutral 1 95 2000) to find the true stop
  python3 tools/agribotctl.py up | down          # one Arm Up / Arm Down nudge (elbow + wrist together)
  python3 tools/agribotctl.py raised            # confirm arm raised + probe clear (releases the wheel lock)
  python3 tools/agribotctl.py jog AXIS SPEED MS  # e.g. jog 1 -20 200  (caps: ARM_JOG_MAX_SPEED/ARM_JOG_MAX_MS)
  python3 tools/agribotctl.py home [AXIS ...]    # declare: arm is fully retracted NOW
  python3 tools/agribotctl.py retract
  python3 tools/agribotctl.py insert             # ARM_MODE=auto only
  python3 tools/agribotctl.py measure            # ARM_MODE=auto: insert, settle, sample, retract
  python3 tools/agribotctl.py watch              # follow the arm workflow until it ends

Options: --url http://127.0.0.1:8000   --token TOKEN (or env AGRIBOT_LOCAL_TOKEN for remote use)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(args, method, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-AgriBot-Token"] = args.token
    request = urllib.request.Request(args.url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {"success": False, "message": error.reason}
    except urllib.error.URLError as error:
        sys.exit(f"Cannot reach the AgriBot service at {args.url}: {error.reason}\nCheck: systemctl status agribot")


def show(status, payload):
    print(json.dumps(payload.get("data", payload), indent=2))
    if status >= 400:
        sys.exit(f"HTTP {status}: {payload.get('message')}")


def watch(args):
    last = None
    while True:
        _, payload = call(args, "GET", "/arm")
        data = payload.get("data", {})
        workflow = data.get("workflow") or {}
        line = f"probe={data.get('probe')} op={data.get('operation')} " + " ".join(f"{s['step']}:{s['status']}" for s in workflow.get("steps", []))
        if line != last:
            print(time.strftime("%H:%M:%S"), line)
            last = line
        if not data.get("operation"):
            print(json.dumps(data.get("lastResult"), indent=2))
            return
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("AGRIBOT_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("AGRIBOT_LOCAL_TOKEN", ""))
    parser.add_argument("command")
    parser.add_argument("params", nargs="*")
    args = parser.parse_args()
    cmd, params = args.command, args.params

    simple_get = {"status": "/status", "sensors": "/sensors", "esp32": "/esp32", "arm": "/arm", "ph": "/ph", "drive": "/drive"}
    simple_post = {"stop": "/stop", "arm-stop": "/arm/stop", "ph-read": "/ph/read", "ph-sample": "/ph/sample", "retract": "/arm/retract",
                   "insert": "/arm/insert", "measure": "/ph/measure"}
    if cmd in simple_get:
        return show(*call(args, "GET", simple_get[cmd]))
    if cmd in simple_post:
        status, payload = call(args, "POST", simple_post[cmd], {})
        show(status, payload)
        if status == 202:
            watch(args)
        return None
    if cmd == "jog":
        if len(params) != 3:
            sys.exit("usage: jog AXIS SPEED MS   (e.g. jog 1 -20 200)")
        axis, speed, ms = (int(p) for p in params)
        return show(*call(args, "POST", "/arm/jog", {"axis": axis, "speed": speed, "ms": ms}))
    if cmd == "raised":
        answer = input("Have you LOOKED at the arm: raised, probe off the ground, nothing moving? Type 'yes': ")
        if answer.strip().lower() != "yes":
            sys.exit("Not confirmed; wheels stay locked.")
        return show(*call(args, "POST", "/arm/confirm-raised", {"confirm": "raised-and-clear"}))
    if cmd in ("up", "down"):
        return show(*call(args, "POST", "/arm/nudge", {"direction": cmd}))
    if cmd == "neutral":
        if len(params) != 3:
            sys.exit("usage: neutral AXIS VALUE MS   (e.g. neutral 1 95 2000)")
        axis, value, ms = (int(p) for p in params)
        return show(*call(args, "POST", "/arm/neutral-test", {"axis": axis, "value": value, "ms": ms}))
    if cmd == "home":
        axes = [int(p) for p in params] or [1, 2]
        answer = input(f"Is the arm physically at its fully RETRACTED rest pose for axes {axes}? Type 'yes': ")
        if answer.strip().lower() != "yes":
            sys.exit("Not homed.")
        return show(*call(args, "POST", "/arm/home", {"axes": axes}))
    if cmd == "watch":
        return watch(args)
    parser.error(f"unknown command {cmd!r}")
    return None


if __name__ == "__main__":
    main()

"""pH probe read through the ESP32's ADC (ESP32 GPIO 34).

The ESP32 reports the voltage at its ADC pin in millivolts. That voltage is
NOT a pH value: converting it needs a calibration with buffer solutions
(PH_CALIBRATION, at least two "pH:mV" points measured at the ESP32 pin, so
any voltage divider is included automatically). Without a calibration the
voltage is still reported, but no pH value is produced or published.

Without buffer solutions there is no calibration. Then:
  * the raw ADC value and the pin voltage are always reported;
  * if the board's documented conversion is configured
    (PH_PROVISIONAL_NEUTRAL_MV + PH_PROVISIONAL_MV_PER_PH, copied from its
    datasheet), an "estimate" is computed and labelled uncalibrated;
  * nothing uncalibrated is ever sent to the backend/app (pH stays null).
No conversion formula is built in, because the board model is not verified.

A pH value only means something while the probe is in the soil, so the value
sent to the app/backend is the result of the last probe measurement (by hand
with POST /ph/sample, or by the commissioned arm), never a live reading.
"""

import statistics
import threading
import time

# ESP32 ADC with 11 dB attenuation is only usable up to roughly 3.1 V.
ADC_CEILING_MV = 3050
ADC_FLOOR_MV = 30
# Readings spread wider than this within one measurement are not trusted.
MAX_SPREAD_MV = 60


def parse_calibration(text):
    """'7.00:1500, 4.01:2030' -> (slope pH/mV, intercept) by least squares. Raises ValueError."""
    points = []
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        ph, sep, mv = part.partition(":")
        if not sep:
            raise ValueError(f"bad calibration point '{part}' (use pH:mV)")
        ph, mv = float(ph), float(mv)
        if not 0 <= ph <= 14 or not 0 <= mv <= 3300:
            raise ValueError(f"calibration point '{part}' out of range (pH 0-14, mV 0-3300)")
        points.append((ph, mv))
    if len(points) < 2:
        raise ValueError("PH_CALIBRATION needs at least two 'pH:mV' points, e.g. 7.00:1500,4.01:2030")
    mean_mv = statistics.fmean(mv for _, mv in points)
    mean_ph = statistics.fmean(ph for ph, _ in points)
    spread = sum((mv - mean_mv) ** 2 for _, mv in points)
    if spread == 0:
        raise ValueError("PH_CALIBRATION points need different millivolt values")
    slope = sum((mv - mean_mv) * (ph - mean_ph) for ph, mv in points) / spread
    # A real probe moves ~59 mV per pH unit at the electrode; boards amplify
    # that, so accept a wide band but reject obviously broken calibrations.
    if not 1 / 1000 <= abs(slope) <= 1 / 10:
        raise ValueError(f"PH_CALIBRATION slope {1 / slope:.0f} mV/pH is implausible (expected 10-1000 mV per pH unit)")
    return slope, mean_ph - slope * mean_mv


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def provisional_estimate(config, mv):
    """Uncalibrated pH from the board's documented neutral voltage and slope, or None."""
    neutral, per_ph = config.ph_provisional_neutral_mv, config.ph_provisional_mv_per_ph
    if neutral is None or per_ph is None:
        return None
    return round(7.0 + (mv - neutral) / per_ph, 2)


class PhProbe:
    def __init__(self, config, link):
        self.cfg = config
        self.link = link
        self.error = None
        self.calibration = None
        self._last = None
        self._seq = 0
        self._lock = threading.Lock()
        if config.ph_source == "esp32" and config.ph_calibration:
            try:
                self.calibration = parse_calibration(config.ph_calibration)
            except ValueError as error:
                self.error = str(error)
                print(f"[ph] {error}")

    @property
    def calibrated(self):
        return self.calibration is not None

    def to_ph(self, mv):
        if not self.calibration:
            return None
        slope, intercept = self.calibration
        return round(slope * mv + intercept, 2)

    def read_live(self, samples=16):
        """Read the probe voltage now (probe may be in air, buffer or soil). No motion."""
        reply = self.link.request("PH", int(samples), timeout=3.0)
        try:
            mv = float(reply["mv"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"malformed PH reply from ESP32: {reply}") from error
        warnings = []
        if reply.get("max", 0) >= ADC_CEILING_MV:
            warnings.append("ADC at its ceiling: the board output may exceed ~3.1 V (add a divider, check wiring)")
        if reply.get("min", ADC_CEILING_MV) <= ADC_FLOOR_MV:
            warnings.append("ADC near 0 V: probe board unpowered or not wired to ESP32 GPIO 34?")
        noise = reply.get("max", 0) - reply.get("min", 0)
        if noise > MAX_SPREAD_MV:
            warnings.append(f"noisy signal ({noise} mV peak-to-peak)")
        with self._lock:
            self._seq += 1
            seq = self._seq
        return {
            "seq": seq,
            "espSeq": reply.get("seq"),
            "espUptimeMs": reply.get("up"),
            "mv": round(mv, 1),
            "raw": reply.get("raw"),
            "noiseMv": noise,
            "ph": self.to_ph(mv),
            "calibrated": self.calibrated,
            "estimate": None if self.calibrated else provisional_estimate(self.cfg, mv),
            "label": "calibrated" if self.calibrated else "uncalibrated",
            "valid": not warnings,
            "warnings": warnings,
            "at": now_iso(),
        }

    def measure(self, count, interval=1.0, abort=None, method="arm"):
        """Take several readings and keep the median as the probe measurement.

        method: "arm" (commissioned arm workflow) or "manual" (probe placed by hand)."""
        readings = []
        for index in range(count):
            if abort is not None and abort.is_set():
                raise RuntimeError("pH measurement aborted")
            readings.append(self.read_live())
            if index < count - 1:
                if abort is not None:
                    if abort.wait(interval):
                        raise RuntimeError("pH measurement aborted")
                else:
                    time.sleep(interval)
        mvs = [r["mv"] for r in readings]
        mv = statistics.median(mvs)
        warnings = sorted({w for r in readings for w in r["warnings"]})
        spread = round(max(mvs) - min(mvs), 1)
        if spread > MAX_SPREAD_MV:
            warnings.append(f"readings drifted {spread} mV during sampling (probe not settled?)")
        ph = self.to_ph(mv)
        if ph is not None and not 0 <= ph <= 14:
            warnings.append(f"computed pH {ph} is outside 0-14 (recalibrate or check wiring)")
        raws = [r["raw"] for r in readings if isinstance(r.get("raw"), (int, float))]
        result = {
            "ph": ph,
            "mv": round(mv, 1),
            "raw": round(statistics.median(raws), 1) if raws else None,
            "spreadMv": spread,
            "samples": len(readings),
            "calibrated": self.calibrated,
            "estimate": None if self.calibrated else provisional_estimate(self.cfg, mv),
            "label": "calibrated" if self.calibrated else "uncalibrated",
            "method": method,
            "valid": ph is not None and not warnings,
            "signalOk": not warnings,
            "warnings": warnings,
            "at": now_iso(),
            "_monotonic": time.monotonic(),
        }
        with self._lock:
            self._last = result
        if self.calibrated:
            print(f"[ph] Measured pH {ph} ({mv:.0f} mV, {method}){' REJECTED: ' + '; '.join(warnings) if warnings else ''}")
        else:
            estimate = result["estimate"]
            print(f"[ph] UNCALIBRATED probe reading {mv:.0f} mV (raw {result['raw']}, {method})"
                  + (f", provisional estimate pH {estimate} (uncalibrated, not published)" if estimate is not None else ", no pH conversion configured")
                  + (f"; signal warnings: {'; '.join(warnings)}" if warnings else ""))
        return {k: v for k, v in result.items() if not k.startswith("_")}

    def latest(self):
        """Last probe measurement for the sensor pipeline.

        status: ok (calibrated, fresh, clean signal -> value is a pH)
                uncalibrated (voltage/estimate only, value None)
                rejected | stale | unavailable | disabled (value None)
        Only status "ok" carries a pH value; nothing else is published."""
        with self._lock:
            last = self._last
        if not self.link.enabled:
            return {"value": None, "status": "disabled", "source": "not connected (ESP32_ENABLED=false)"}
        if self.error:
            return {"value": None, "status": "unavailable", "source": f"calibration error: {self.error}"}
        if last is None:
            status = "unavailable" if not self.link.connected else "no_measurement"
            source = f"ESP32 not connected: {self.link.error}" if not self.link.connected else "no probe measurement yet (POST /ph/sample)"
            return {"value": None, "status": status, "source": source}
        age = time.monotonic() - last["_monotonic"]
        base = {"mv": last["mv"], "raw": last.get("raw"), "estimate": last.get("estimate"), "label": last.get("label"),
                "method": last.get("method"), "at": last["at"], "ageSeconds": int(age), "warnings": last["warnings"]}
        if age > self.cfg.ph_max_age_seconds:
            return {"value": None, "status": "stale", "source": f"last measurement too old ({int(age)} s)", **base}
        if last["warnings"]:
            return {"value": None, "status": "rejected", "source": "rejected: " + "; ".join(last["warnings"]), **base}
        if last["ph"] is None:
            note = f"; provisional estimate pH {last['estimate']} (uncalibrated)" if last.get("estimate") is not None else ""
            return {"value": None, "status": "uncalibrated", "source": f"uncalibrated: {last['mv']} mV{note}", **base}
        return {"value": last["ph"], "status": "ok", "source": "measured (calibrated)", **base}

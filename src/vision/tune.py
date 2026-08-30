"""Measuring one pipeline configuration, and sweeping several.

**Pose noise is the accuracy proxy.** Detection range is the usual tuning metric
and a small room cannot produce it, but corner noise is what becomes pose noise
downstream, and standard deviation on a static tag measures it directly.
Run-to-run repeatability is around 0.001 deg, so treat smaller differences as
noise rather than signal.

Every row records the exposure and the negotiated resolution read **from the
device**, not from the label. A label is typed by a human and a forgotten UI
step makes it a lie; the readback cannot be.

What this cannot show: range, and anything about motion. A static tag never
exhibits motion blur, which is the entire reason short exposure matters.
"""

import csv
import math
import os
import statistics
import time

from . import camera as cam
from . import nt

SETTLE_S = 2.0          # pipeline restarts and auto-anything need to settle
KEYS = ("fps", "latencyMillis", "hasTarget", "targetYaw", "targetPitch",
        "targetArea", nt.PIPELINE_REQUEST, nt.PIPELINE_STATE)


def measure(entries, seconds, dev, label):
    """Sample one configuration and return a row of statistics."""
    fps, lat, yaw, pitch, area = [], [], [], [], []
    seen = total = flips = 0
    prev = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        has = nt.read(entries["hasTarget"])
        if has is not None:
            total += 1
            seen += 1 if has else 0
            if prev is not None and has != prev:
                flips += 1
            prev = has
            if has:
                for lst, key in ((yaw, "targetYaw"), (pitch, "targetPitch"),
                                 (area, "targetArea")):
                    v = nt.read(entries[key])
                    if isinstance(v, (int, float)):
                        lst.append(float(v))
        for lst, key in ((fps, "fps"), (lat, "latencyMillis")):
            v = nt.read(entries[key])
            if isinstance(v, (int, float)):
                lst.append(float(v))
        time.sleep(0.02)
    dt = time.time() - t0

    def stat(vals, fn):
        try:
            return fn(vals)
        except Exception:
            return float("nan")

    exp = cam.read_exposure(dev)
    fmt = cam.current_format(dev)
    return {
        "label": label,
        # State, not request: PhotonVision reporting which pipeline is actually
        # selected rather than an echo of what we asked for.
        "pipeline": nt.read(entries[nt.PIPELINE_STATE]),
        "exposure_ms": round(exp / cam.EXPOSURE_TICKS_PER_MS, 1) if exp else float("nan"),
        "width": fmt[0] if fmt else "",
        "height": fmt[1] if fmt else "",
        "fourcc": fmt[2] if fmt else "",
        "seconds": round(dt, 1),
        "fps": round(stat(fps, statistics.mean), 1),
        "latency_ms": round(stat(lat, statistics.mean), 1),
        "latency_max_ms": round(max(lat), 1) if lat else float("nan"),
        "detect_pct": round(100.0 * seen / total, 1) if total else 0.0,
        "dropouts_per_s": round(flips / 2.0 / dt, 2),
        "yaw_mean": round(stat(yaw, statistics.mean), 3),
        "yaw_sd": round(stat(yaw, statistics.pstdev), 4),
        "pitch_sd": round(stat(pitch, statistics.pstdev), 4),
        "area_mean": round(stat(area, statistics.mean), 3),
        "area_sd": round(stat(area, statistics.pstdev), 4),
        "samples": len(yaw),
    }


def verdict(row):
    """What the row means - including when it means the experiment is wrong."""
    notes = []
    if row["detect_pct"] < 99:
        notes.append("detection NOT solid (%.1f%%, %.2f dropouts/s) - noise figures "
                     "from an intermittent target are not comparable"
                     % (row["detect_pct"], row["dropouts_per_s"]))
    exp, fps = row["exposure_ms"], row["fps"]
    if not (math.isnan(exp) or math.isnan(fps)):
        # Decimation trades detector work for accuracy, so it only pays when the
        # detector is the bottleneck. Pinned by exposure, a sweep measures
        # nothing and every row looks the same - which reads as "decimation does
        # not matter" rather than "wrong experiment".
        ceiling = 1000.0 / max(exp, 0.1)
        if fps >= 0.85 * ceiling:
            notes.append("EXPOSURE-BOUND: %.1fms caps the camera at %.1f fps and you "
                         "measured %.1f. Decimation cannot help here."
                         % (exp, ceiling, fps))
    return notes


def identical(rows):
    """True when every row measured the same capture configuration."""
    if len(rows) < 2:
        return False
    return len({(r["exposure_ms"], r["width"], r["height"], r["pipeline"])
                for r in rows}) == 1


def append_csv(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)

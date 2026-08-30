#!/usr/bin/env python3
"""Measure pipeline configurations, one row each, and sweep them automatically.

Stage 5 asks you to trade resolution and decimation against framerate and
accuracy. Doing that by hand means changing a setting, running a measurement,
and remembering which was which - and a mislabelled row is worse than no row.
This drives the sweep itself.

    # one measurement of whatever is configured right now
    .venv/bin/python src/tune_measure.py --label "baseline" --csv tuning.csv

    # sweep exposure, writing it directly to the camera
    .venv/bin/python src/tune_measure.py --sweep-exposure 120,200,320,500,800 --csv tuning.csv

    # sweep pipelines - make one pipeline per setting in the UI first, then:
    .venv/bin/python src/tune_measure.py --sweep-pipeline 0,1,2,3 --csv tuning.csv

Pipeline switching uses **two** topics, which is easy to get wrong: PhotonLib
publishes the requested index to `pipelineIndexRequest` and reads the actual one
from `pipelineIndexState`. A plain `pipelineIndex` topic appears in older NT API
documentation and nothing consumes it - writing it is accepted and silently
ignored, and reading it back returns your own value, which looks like
confirmation. This tool uses the request/state pair, so the readback is
PhotonVision's state rather than an echo.

Every row also records the width, height and exposure read **from the device**,
so a switch that did not take cannot produce a fake comparison.

Both sweeps restore the original setting when they finish, including on Ctrl-C.

**Every row records the exposure read back from the device**, not the label. A
label is typed by a human and a forgotten UI change makes it a lie; the readback
cannot be. If two rows share an `exposure_ms`, nothing changed between them.

**Pose noise is the accuracy proxy.** Detection *range* is the usual stage 5
metric and a small room cannot produce it, but corner noise is what becomes pose
noise downstream, and standard deviation on a static tag measures it directly.
Run-to-run repeatability is around 0.001 deg, so treat smaller differences as
noise rather than signal.

What this cannot tell you, and needs a real space: range, and anything about
motion. A static tag never shows you motion blur, which is the entire reason
short exposure matters.
"""

import argparse
import csv
import math
import os
import statistics
import sys
import time

from common import load_config, device_from, sh
from pipeline_doctor import connect
import camera_doctor

SETTLE_S = 2.0          # pipeline restarts and auto-anything needs to settle
KEYS = ("fps", "latencyMillis", "hasTarget", "targetYaw", "targetPitch",
        "targetArea", "pipelineIndexRequest", "pipelineIndexState")


def value(entry):
    try:
        v = entry.getValue()
        return v.value() if hasattr(v, "value") else None
    except Exception:
        return None


def read_exposure(dev):
    out = sh(["v4l2-ctl", "-d", dev, "--get-ctrl=exposure_time_absolute"])
    if out and ":" in out:
        try:
            return float(out.split(":")[-1].strip())
        except ValueError:
            pass
    return None


def set_exposure(dev, ticks):
    """Write exposure and confirm it took. v4l2 reports success while clamping."""
    sh(["v4l2-ctl", "-d", dev, "-c", "exposure_time_absolute=%d" % ticks])
    time.sleep(0.3)
    got = read_exposure(dev)
    return got, (got is not None and abs(got - ticks) <= max(2.0, ticks * 0.02))


def measure(entries, seconds, dev, label):
    """Sample one configuration and return a row of statistics."""
    fps, lat, yaw, pitch, area = [], [], [], [], []
    seen = total = flips = 0
    prev = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        has = value(entries["hasTarget"])
        if has is not None:
            total += 1
            seen += 1 if has else 0
            if prev is not None and has != prev:
                flips += 1
            prev = has
            if has:
                for lst, key in ((yaw, "targetYaw"), (pitch, "targetPitch"),
                                 (area, "targetArea")):
                    v = value(entries[key])
                    if isinstance(v, (int, float)):
                        lst.append(float(v))
        for lst, key in ((fps, "fps"), (lat, "latencyMillis")):
            v = value(entries[key])
            if isinstance(v, (int, float)):
                lst.append(float(v))
        time.sleep(0.02)
    dt = time.time() - t0

    def stat(vals, fn):
        try:
            return fn(vals)
        except Exception:
            return float("nan")

    exp = read_exposure(dev)
    # State, not request: PhotonVision reporting which pipeline is actually
    # selected, rather than an echo of what we asked for.
    pipe = value(entries["pipelineIndexState"])
    # The negotiated format, read from the device. This is the only way to tell
    # whether a pipeline switch actually changed anything: the pipeline index we
    # write may only be echoing our own publication.
    fmt = camera_doctor.current_format(dev)
    return {
        "label": label,
        "exposure_ms": round(exp / 10.0, 1) if exp else float("nan"),
        "width": fmt[0] if fmt else "",
        "height": fmt[1] if fmt else "",
        "fourcc": fmt[2] if fmt else "",
        "pipeline": pipe if pipe is not None else "",
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
    """Say what the row means, including when it means the experiment is wrong."""
    notes = []
    if row["detect_pct"] < 99:
        notes.append("detection NOT solid (%.1f%%, %.2f dropouts/s) - noise "
                     "figures from an intermittent target are not comparable"
                     % (row["detect_pct"], row["dropouts_per_s"]))
    exp = row["exposure_ms"]
    if not math.isnan(exp) and not math.isnan(row["fps"]):
        ceiling = 1000.0 / max(exp, 0.1)
        if row["fps"] >= 0.85 * ceiling:
            notes.append("EXPOSURE-BOUND: %.1fms caps the camera at %.1f fps and "
                         "you measured %.1f. Decimation reduces detector work, so "
                         "it cannot help here." % (exp, ceiling, row["fps"]))
    return notes


def append_csv(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def show(row, notes):
    print("  " + "  ".join("%s=%s" % (k, row[k]) for k in
                           ("pipeline", "width", "height", "exposure_ms", "fps",
                            "latency_ms", "detect_pct", "yaw_sd")))
    for n in notes:
        print("    ! " + n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config")
    ap.add_argument("--label", default="run")
    ap.add_argument("-s", "--seconds", type=float, default=12.0)
    ap.add_argument("--csv")
    ap.add_argument("--camera")
    ap.add_argument("--sweep-exposure", metavar="A,B,C",
                    help="exposure_time_absolute values, in units of 100us")
    ap.add_argument("--sweep-pipeline", metavar="A,B,C",
                    help="pipeline indices; create them in the UI first")
    args = ap.parse_args()

    cfg, err = load_config(args.config)
    if err:
        print("config: %s" % err)
        return 2
    pv = cfg.get("photonvision", {})
    cam = args.camera or pv.get("camera_name")
    dev = device_from(cfg)

    inst, cerr = connect(cfg.get("networktables"),
                         cfg.get("preflight", {}).get("nt_timeout_s", 5.0))
    if inst is None or cerr:
        print("networktables: %s" % (cerr or "no instance"))
        return 2

    base = "/%s/%s" % (pv.get("nt_table", "photonvision").strip("/"), cam)
    entries = {k: inst.getEntry("%s/%s" % (base, k)) for k in KEYS}
    time.sleep(0.5)
    if value(entries["fps"]) is None and value(entries["hasTarget"]) is None:
        print("nothing at %s - is the camera nickname right?" % base)
        print("run pipeline_doctor.py to see what is actually published")
        return 2

    rows = []
    original_exposure = read_exposure(dev)

    # pipelineIndexState is published by PhotonVision, so it is a real
    # confirmation rather than an echo of our own request. If it is absent there
    # is nothing trustworthy to verify a switch against, or to restore to.
    original_pipeline = value(entries["pipelineIndexState"])
    pipeline_published = original_pipeline is not None
    if args.sweep_pipeline and not pipeline_published:
        print("NOTE: pipelineIndexState is not published, so a switch cannot be")
        print("      confirmed and nothing will be restored afterwards. Check the")
        print("      camera name and that PhotonVision is publishing.\n")

    try:
        if args.sweep_exposure:
            steps = [int(x) for x in args.sweep_exposure.split(",")]
            print("sweeping exposure %s - keep the tag and camera still\n" % steps)
            for ticks in steps:
                got, ok = set_exposure(dev, ticks)
                if not ok:
                    # Either PhotonVision reimposed its own value, or the driver
                    # clamped. Either way the row would be mislabelled, so say so.
                    print("  requested %d, device reports %s - skipping"
                          % (ticks, got))
                    continue
                time.sleep(SETTLE_S)
                row = measure(entries, args.seconds, dev,
                              "%s exp=%d" % (args.label, ticks))
                rows.append(row)
                show(row, verdict(row))

        elif args.sweep_pipeline:
            steps = [int(x) for x in args.sweep_pipeline.split(",")]
            print("sweeping pipelines %s - keep the tag and camera still\n" % steps)
            for idx in steps:
                try:
                    entries["pipelineIndexRequest"].setInteger(idx)
                except Exception:
                    entries["pipelineIndexRequest"].setDouble(float(idx))
                time.sleep(SETTLE_S)
                got = value(entries["pipelineIndexState"])
                if got is None or int(got) != idx:
                    print("  pipeline %d did not take (reports %s) - skipping"
                          % (idx, got))
                    continue
                row = measure(entries, args.seconds, dev,
                              "%s pipeline=%d" % (args.label, idx))
                rows.append(row)
                show(row, verdict(row))

        else:
            print("sampling %s for %.0fs - keep the tag and camera still"
                  % (args.label, args.seconds))
            row = measure(entries, args.seconds, dev, args.label)
            rows.append(row)
            print()
            for k, v in row.items():
                print("  %-16s %s" % (k, v))
            print()
            for n in verdict(row):
                print("  ! " + n)

    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        # Leave the camera as we found it, whatever happened.
        if args.sweep_exposure and original_exposure:
            set_exposure(dev, int(original_exposure))
            print("\nrestored exposure to %.1f ms" % (original_exposure / 10.0))
        if args.sweep_pipeline:
            back = int(original_pipeline) if pipeline_published else 0
            try:
                entries["pipelineIndexRequest"].setInteger(back)
                print("restored pipeline %d%s"
                      % (back, "" if pipeline_published else " (assumed - the "
                         "original index was never published)"))
            except Exception:
                print("could not restore the pipeline - set it in the UI")

    if args.csv:
        for row in rows:
            append_csv(args.csv, row)
        print("\nwrote %d row(s) to %s" % (len(rows), args.csv))

    # Identical exposure across rows means nothing actually changed - the most
    # common way a sweep produces four rows of the same measurement.
    if len(rows) > 1:
        signature = {(r["exposure_ms"], r["width"], r["height"]) for r in rows}
        if len(signature) == 1:
            exp, w, h = signature.pop()
            print("\n  WARNING: every row measured the same configuration -")
            print("  exposure %s ms at %sx%s. Nothing about the capture changed"
                  % (exp, w, h))
            print("  between them, so these are repeat measurements, not a sweep.")
            if args.sweep_pipeline:
                print("  Either the pipelines are configured identically, or")
                print("  the switch did not take - check pipelineIndexState.")
                print("  Check in the UI which pipeline is selected right now.")
            print("  Useful as a repeatability check, useless as a comparison.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

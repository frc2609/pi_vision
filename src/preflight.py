#!/usr/bin/env python3
"""One command, run before a match: is the whole vision chain actually ready?

Each layer of this system has its own tool. This composes them into a single
pass/fail so nobody has to remember which of five things to check while the
field is being reset.

    power      -> throttle bits clean
    camera     -> present, at the expected resolution and format
    controls   -> v4l2 controls match config (they do not survive a reboot)
    calibration-> present, and the focal length still implies the right FOV
    service    -> PhotonVision running
    network    -> something is actually publishing to NetworkTables
    detections -> a tag is being seen (optional; only if one is in view)

Exit code is the number of failed checks, so `preflight.py && echo ready` works.
A SKIP is never a failure - it means the check could not run, and it says why.
"""

import argparse
import math
import os
import sys

from common import (load_config, device_from, service_state, sh, throttled,
                    repo_root)
import camera_doctor as cd

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=""):
        self.rows.append((status, name, detail))
        return status

    def failures(self):
        return sum(1 for s, _, _ in self.rows if s == FAIL)

    def render(self):
        print()
        for status, name, detail in self.rows:
            print("  %-4s  %-22s %s" % (status, name, detail))
        n = self.failures()
        skipped = sum(1 for s, _, _ in self.rows if s == SKIP)
        print()
        if n:
            print("  NOT READY - %d check(s) failed%s"
                  % (n, ", %d skipped" % skipped if skipped else ""))
        else:
            print("  READY%s" % (" (%d skipped)" % skipped if skipped else ""))
        print()


def check_power(r, cfg):
    if not cfg.get("preflight", {}).get("require_clean_throttle", True):
        return r.add(SKIP, "power", "not required by config")
    raw, text = throttled()
    if raw is None:
        return r.add(SKIP, "power", "vcgencmd unavailable - not a Pi?")
    if raw == 0:
        return r.add(PASS, "power", "0x0 clean")
    return r.add(FAIL, "power", "0x%x - %s" % (raw, text))


def check_camera(r, cfg, dev):
    if not os.path.exists(dev):
        return r.add(FAIL, "camera", "no device at %s" % dev)
    exp = cfg.get("camera", {}).get("expect", {})
    cur = cd.current_format(dev)
    if not cur:
        return r.add(FAIL, "camera", "present but format unreadable")
    w, h, fourcc = cur
    want = (exp.get("width"), exp.get("height"), exp.get("fourcc"))
    if None in want:
        return r.add(SKIP, "camera", "no expected format in config")
    if (w, h, fourcc) != tuple(want):
        return r.add(FAIL, "camera",
                     "running %dx%d %s, expected %dx%d %s" % (w, h, fourcc, *want))
    return r.add(PASS, "camera", "%dx%d %s" % (w, h, fourcc))


def check_controls(r, cfg, dev):
    wanted = cfg.get("camera", {}).get("controls") or []
    if not wanted:
        return r.add(SKIP, "controls", "none configured")
    got = cd.controls(dev) or {}
    bad = []
    for item in wanted:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name, value = item
        elif isinstance(item, dict):
            name, value = next(iter(item.items()))
        else:
            continue
        actual = (got.get(name) or (None,))[0]
        if actual is None or str(actual) != str(value):
            bad.append("%s=%s (want %s)" % (name, actual, value))
    if bad:
        return r.add(FAIL, "controls", "; ".join(bad) +
                     "  - rerun apply_camera_settings.py")
    return r.add(PASS, "controls", "%d match config" % len(wanted))


def check_calibration(r, cfg):
    cal = cfg.get("calibration", {})
    name = cal.get("file")
    if not name:
        return r.add(SKIP, "calibration", "no file configured")
    for cand in (name, os.path.join(repo_root(), name),
                 os.path.expanduser("~/vision/calib/" + os.path.basename(name))):
        if os.path.exists(cand):
            break
    else:
        return r.add(FAIL, "calibration", "%s not found" % name)

    exp = cfg.get("camera", {}).get("expect", {})
    try:
        import json
        d = json.load(open(cand))
        K = d.get("camera_matrix") or d.get("cameraIntrinsics", {}).get("data")
        if isinstance(K, list) and K and isinstance(K[0], list):
            K = [x for row in K for x in row]
        fx = float(K[0])
        if fx <= 0:
            return r.add(FAIL, "calibration",
                         "fx=%.1f - a negative or zero focal length is a diverged solve" % fx)
        width = exp.get("width")
        if not width:
            return r.add(PASS, "calibration", "fx=%.1f" % fx)
        hfov = 2 * math.degrees(math.atan(width / (2 * fx)))
        want, tol = cal.get("expect_hfov_deg"), cal.get("tolerance_deg", 2.0)
        if want is None:
            return r.add(PASS, "calibration", "fx=%.1f, HFOV %.1f deg" % (fx, hfov))
        if abs(hfov - want) > tol:
            return r.add(FAIL, "calibration",
                         "HFOV %.1f deg, expected %.1f +/- %.1f" % (hfov, want, tol))
        return r.add(PASS, "calibration", "HFOV %.1f deg" % hfov)
    except Exception as e:
        return r.add(FAIL, "calibration", "unreadable: %s" % e)


def check_service(r, cfg):
    if not cfg.get("preflight", {}).get("require_photonvision", True):
        return r.add(SKIP, "photonvision", "not required by config")
    svc = cfg.get("photonvision", {}).get("service", "photonvision")
    state = service_state(svc)
    if state == "active":
        return r.add(PASS, "photonvision", "active")
    return r.add(FAIL, "photonvision", "%s is %s" % (svc, state or "unknown"))


def check_network(r, cfg):
    pf = cfg.get("preflight", {})
    if not pf.get("require_nt", True):
        return r.add(SKIP, "networktables", "not required by config")
    try:
        import pipeline_doctor as pd
    except Exception as e:
        return r.add(SKIP, "networktables", "pipeline_doctor unavailable: %s" % e)
    inst, err = pd.connect(cfg.get("networktables"), pf.get("nt_timeout_s", 5.0))
    if inst is None or err:
        return r.add(FAIL, "networktables", err or "no instance")
    table = cfg.get("photonvision", {}).get("nt_table", "photonvision")
    cams, topics = pd.watch(inst, table, seconds=1.5)
    live = {p: d for p, d in topics.items() if d["rate"] > 0}
    if not live:
        return r.add(FAIL, "networktables",
                     "connected but nothing is updating under /%s" % table)
    fastest = max(live.values(), key=lambda d: d["rate"])["rate"]
    r.add(PASS, "networktables", "%d topic(s) live, fastest %.0f Hz" % (len(live), fastest))

    if pf.get("require_detections", False):
        ht = [p for p in topics if p.endswith("/hasTarget")]
        if not ht:
            return r.add(SKIP, "detections", "no hasTarget topic published")
        val = str(topics[ht[0]]["value"]).lower()
        if val in ("true", "1"):
            return r.add(PASS, "detections", "a tag is in view")
        return r.add(FAIL, "detections", "hasTarget is %s" % val)
    return r.add(SKIP, "detections", "not required by config")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config")
    ap.add_argument("-d", "--device")
    args = ap.parse_args()

    cfg, err = load_config(args.config)
    if err:
        print("config: %s" % err)
        return 1
    dev = device_from(cfg, args.device)

    r = Report()
    check_power(r, cfg)
    check_camera(r, cfg, dev)
    check_controls(r, cfg, dev)
    check_calibration(r, cfg)
    check_service(r, cfg)
    check_network(r, cfg)
    r.render()
    return r.failures()


if __name__ == "__main__":
    sys.exit(main())

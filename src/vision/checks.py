"""Preflight checks: each one reads the facts and returns a verdict.

A check returns (status, name, detail). Statuses are PASS, FAIL and SKIP, and
**SKIP is never a failure** - it means the check could not run, and it says why.

Checks take the facts dict rather than probing the system themselves, so the
whole preflight costs one pass over the hardware and each check stays trivially
readable.
"""

import math

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def power(cfg, f):
    if not cfg.get("preflight", {}).get("require_clean_throttle", True):
        return SKIP, "power", "not required by config"
    t = f.get("throttled")
    if not t:
        return SKIP, "power", "vcgencmd unavailable - not a Pi?"
    if t["raw"] == "0x0":
        return PASS, "power", "0x0 clean"
    # Sticky bits clear only on a full power cycle, so a fault here may be
    # history rather than now - the monitor's *_since_start fields separate them.
    return FAIL, "power", "%s - %s" % (t["raw"], t["text"])


def camera(cfg, f):
    if not f.get("asked"):
        return FAIL, "camera", "no device, or format unreadable, at %s" % f["device"]
    exp = cfg.get("camera", {}).get("expect", {})
    want = (exp.get("width"), exp.get("height"), exp.get("fourcc"))
    got = (f["asked"]["width"], f["asked"]["height"], f["asked"]["fourcc"])
    if None in want:
        return SKIP, "camera", "no expected format in config"
    if got != tuple(want):
        return FAIL, "camera", "running %dx%d %s, expected %dx%d %s" % (got + tuple(want))
    return PASS, "camera", "%dx%d %s" % got


def controls(cfg, f):
    wanted = cfg.get("camera", {}).get("controls") or []
    if not wanted:
        return SKIP, "controls", "none configured"
    got = f.get("controls", {})
    bad = []
    for item in wanted:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name, value = item
        elif isinstance(item, dict):
            name, value = next(iter(item.items()))
        else:
            continue
        actual = got.get(name, {}).get("value")
        if actual is None or str(actual) != str(value):
            bad.append("%s=%s (want %s)" % (name, actual, value))
    if bad:
        # Expected while PhotonVision owns the camera: it sets its own exposure
        # when it opens the device, and it wins.
        return FAIL, "controls", "; ".join(bad) + "  - PhotonVision overrides these while it owns the camera"
    return PASS, "controls", "%d match config" % len(wanted)


def calibration(cfg, f):
    c = f.get("calibration") or {}
    if not c.get("path"):
        return (SKIP if c.get("error") == "not configured" else FAIL), \
               "calibration", c.get("error", "not found")
    fx = c.get("fx")
    if fx is None:
        return FAIL, "calibration", "no intrinsics in %s" % c["path"]
    if fx <= 0:
        # A negative focal length is a mirrored solve, not a bad calibration.
        return FAIL, "calibration", "fx=%.1f - negative focal length means a diverged solve" % fx
    width = cfg.get("camera", {}).get("expect", {}).get("width")
    if not width:
        return PASS, "calibration", "fx=%.1f" % fx
    hfov = 2 * math.degrees(math.atan(width / (2 * fx)))
    cal = cfg.get("calibration", {})
    want, tol = cal.get("expect_hfov_deg"), cal.get("tolerance_deg", 2.0)
    if want is None:
        return PASS, "calibration", "fx=%.1f, HFOV %.1f deg" % (fx, hfov)
    if abs(hfov - want) > tol:
        return FAIL, "calibration", "HFOV %.1f deg, expected %.1f +/- %.1f" % (hfov, want, tol)
    return PASS, "calibration", "HFOV %.1f deg" % hfov


def service(cfg, f):
    if not cfg.get("preflight", {}).get("require_photonvision", True):
        return SKIP, "photonvision", "not required by config"
    name = cfg.get("photonvision", {}).get("service", "photonvision")
    state = f.get("services", {}).get(name)
    if state == "active":
        return PASS, "photonvision", "active"
    # Says nothing about whether detection works - that is the network check.
    return FAIL, "photonvision", "%s is %s" % (name, state or "unknown")


def network(cfg, f=None):
    """Connects to NetworkTables. The one check that touches the network."""
    pf = cfg.get("preflight", {})
    if not pf.get("require_nt", True):
        return [(SKIP, "networktables", "not required by config")]
    from . import nt
    inst, err = nt.connect(cfg.get("networktables"), pf.get("nt_timeout_s", 5.0))
    if inst is None or err:
        return [(FAIL, "networktables", err or "no instance")]
    table = cfg.get("photonvision", {}).get("nt_table", "photonvision")
    cams, topics = nt.watch(inst, table, seconds=1.5)
    live = {p: d for p, d in topics.items() if d["rate"] > 0}
    if not live:
        return [(FAIL, "networktables",
                 "connected but nothing is updating under /%s" % table)]
    fastest = max(live.values(), key=lambda d: d["rate"])["rate"]
    rows = [(PASS, "networktables",
             "%d topic(s) live, fastest %.0f Hz" % (len(live), fastest))]

    if pf.get("require_detections", False):
        ht = [(p, d) for p, d in topics.items() if p.endswith("/hasTarget")]
        if not ht:
            rows.append((SKIP, "detections", "no hasTarget topic published"))
        elif str(ht[0][1]["value"]).lower() in ("true", "1"):
            rows.append((PASS, "detections", "a tag is in view"))
        else:
            rows.append((FAIL, "detections", "hasTarget is %s" % ht[0][1]["value"]))
    else:
        rows.append((SKIP, "detections", "not required by config"))
    return rows


def run_all(cfg, f):
    rows = [power(cfg, f), camera(cfg, f), controls(cfg, f),
            calibration(cfg, f), service(cfg, f)]
    rows.extend(network(cfg, f))
    return rows

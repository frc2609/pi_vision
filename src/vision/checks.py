"""Preflight checks: each one reads the facts and returns a verdict.

A check returns (status, name, detail). Statuses are PASS, FAIL and SKIP, and
**SKIP is never a failure** - it means the check could not run, and it says why.

Checks take the facts dict rather than probing the system themselves, so the
whole preflight costs one pass over the hardware and each check stays trivially
readable.

**Check outcomes, not settings.** The one check that was removed verified
configuration rather than effect, and configuration here has a second owner:
PhotonVision sets exposure, brightness and white balance when it opens the
camera. It reported a failure on a control with no demonstrated effect while
saying nothing about the setting that actually mattered. Every check below asks
what the system is *doing*.
"""

import math
import os

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


def identity(cfg, f):
    """Is this the camera we calibrated?

    Intrinsics belong to a specific physical camera. With two identical models
    on the bench, swapping them silently invalidates the calibration and nothing
    else would notice - the format, the framerate and the detections all look
    perfect while every distance is wrong.
    """
    serial = cfg.get("camera", {}).get("serial")
    if not serial:
        return SKIP, "identity", "no camera.serial in config"
    path = f.get("stable_path")
    if not path:
        return FAIL, "identity", ("no by-id path - cannot confirm which camera this "
                                  "is, and device numbering moves on replug")
    if serial not in path:
        return FAIL, "identity", ("expected serial %s, connected camera is %s"
                                  % (serial, os.path.basename(path)))
    return PASS, "identity", serial


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


def calibration_resolution(cfg, f):
    """Does the calibration cover the resolution actually running?

    Intrinsics are in pixels and do not transfer between resolutions.
    PhotonVision keeps one calibration per resolution and falls back to
    estimating from the FOV setting when one is missing - which measured as a
    0.65 deg systematic yaw error at 640x400, with nothing looking wrong.
    """
    c = f.get("calibration") or {}
    size = c.get("image_size")
    asked = f.get("asked")
    if not c.get("path"):
        return SKIP, "calibration_res", "no calibration to check"
    if not size:
        return SKIP, "calibration_res", "calibration file records no image size"
    if not asked:
        return SKIP, "calibration_res", "current format unknown"
    if [asked["width"], asked["height"]] != list(size):
        return FAIL, "calibration_res", (
            "calibrated at %dx%d but running %dx%d - intrinsics do not transfer "
            "between resolutions" % (size[0], size[1], asked["width"], asked["height"]))
    return PASS, "calibration_res", "%dx%d" % (size[0], size[1])


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
    """Connects to NetworkTables. The one check that touches the network.

    Returns several rows: the connection, the pipeline rate, and detections -
    all outcome checks, and the last of them is the one that matters most.
    """
    pf = cfg.get("preflight", {})
    if not pf.get("require_nt", True):
        return [(SKIP, "networktables", "not required by config")]
    from . import nt
    inst, err = nt.connect(cfg.get("networktables"), pf.get("nt_timeout_s", 5.0))
    if inst is None or err:
        return [(FAIL, "networktables", err or "no instance")]
    table = cfg.get("photonvision", {}).get("nt_table", "photonvision")
    cam_name = cfg.get("photonvision", {}).get("camera_name")
    cams, topics = nt.watch(inst, table, seconds=1.5)
    live = {p: d for p, d in topics.items() if d["rate"] > 0}
    if not live:
        return [(FAIL, "networktables",
                 "connected but nothing is updating under /%s" % table)]
    fastest = max(live.values(), key=lambda d: d["rate"])["rate"]
    rows = [(PASS, "networktables",
             "%d topic(s) live, fastest %.0f Hz" % (len(live), fastest))]

    def value(suffix):
        for p, d in topics.items():
            if p.endswith("/" + suffix) and (not cam_name or p.startswith(cam_name + "/")):
                return d["value"]
        return None

    # Pipeline rate. Nothing is broken at a low rate, but it is worth saying:
    # framerate here is set by exposure, and exposure is set by the light.
    fps_min = pf.get("min_fps")
    fps = value("fps")
    if fps is None:
        rows.append((SKIP, "pipeline_fps", "no fps topic published"))
    elif fps_min is None:
        rows.append((PASS, "pipeline_fps", "%s fps" % fps))
    elif float(fps) < float(fps_min):
        rows.append((FAIL, "pipeline_fps",
                     "%s fps, below the %s minimum - usually exposure, which is "
                     "set by the available light" % (fps, fps_min)))
    else:
        rows.append((PASS, "pipeline_fps", "%s fps" % fps))

    # The headline. Everything else can pass while detection is silently dead:
    # exposure drifting below the light's detection floor produces no error
    # anywhere, just hasTarget going false.
    ht = value("hasTarget")
    if ht is None:
        rows.append((SKIP, "detections", "no hasTarget topic published"))
    elif not pf.get("require_detections", False):
        rows.append((SKIP, "detections",
                     "not required by config (hasTarget is %s)" % ht))
    elif str(ht).lower() in ("true", "1"):
        rows.append((PASS, "detections", "a tag is in view"))
    else:
        rows.append((FAIL, "detections",
                     "hasTarget is false - no tag detected. If one is in view, "
                     "exposure is probably below the detection floor for the "
                     "current light"))
    return rows


def run_all(cfg, f):
    rows = [power(cfg, f), camera(cfg, f), identity(cfg, f),
            calibration(cfg, f), calibration_resolution(cfg, f), service(cfg, f)]
    rows.extend(network(cfg, f))
    return rows

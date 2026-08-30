"""Preflight checks. Each reads the facts and returns a finding.

A finding is `{status, camera, name, detail}`. `camera` is None for checks that
are not about a particular camera.

**SKIP means the check could not run**, and it says why. It is not a soft
failure, and the distinction matters because SKIP publishes green to the
dashboard: something the system is *missing* has to FAIL, or an uncalibrated
camera shows a reassuring light. "Could not check" and "checked, and it is
absent" are different answers.

Checks take facts rather than probing the system themselves, so a preflight
costs one pass over the hardware per camera and each check stays readable.

**Check outcomes, not settings.** The one check that was removed verified
configuration rather than effect, and configuration here has a second owner:
PhotonVision sets exposure, brightness and white balance when it opens a camera.
It reported a failure on a control with no demonstrated effect while saying
nothing about the setting that actually mattered. Every check below asks what
the system is *doing*.
"""

import math
import os

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def finding(status, name, detail, camera=None):
    return {"status": status, "camera": camera, "name": name, "detail": detail}


# --- global ---------------------------------------------------------------

def power(cfg, facts_by_camera):
    """One Pi, one power state - so this is asked once regardless of cameras."""
    if not cfg.get("preflight", {}).get("require_clean_throttle", True):
        return finding(SKIP, "power", "not required by config")
    t = next((f.get("throttled") for f in facts_by_camera if f.get("throttled")), None)
    if not t:
        return finding(SKIP, "power", "vcgencmd unavailable - not a Pi?")
    if t["raw"] == "0x0":
        return finding(PASS, "power", "0x0 clean")
    # Sticky bits clear only on a full power cycle, so a fault here may be
    # history rather than now - the monitor's *_since_start fields separate them.
    return finding(FAIL, "power", "%s - %s" % (t["raw"], t["text"]))


def service(cfg, facts_by_camera):
    if not cfg.get("preflight", {}).get("require_photonvision", True):
        return finding(SKIP, "photonvision", "not required by config")
    name = cfg.get("photonvision", {}).get("service", "photonvision")
    state = next((f.get("services", {}).get(name) for f in facts_by_camera), None)
    if state == "active":
        return finding(PASS, "photonvision", "active")
    # Says nothing about whether detection works - that is the network check.
    return finding(FAIL, "photonvision", "%s is %s" % (name, state or "unknown"))


def configured(cfg, cameras):
    """Are there any cameras configured at all?"""
    if cameras:
        return finding(PASS, "configured", "%d camera(s): %s"
                       % (len(cameras), ", ".join(c["name"] for c in cameras)))
    return finding(FAIL, "configured",
                   "no cameras in config.yaml - nothing else can be checked")


def unique_devices(cfg, cameras):
    """Two cameras resolving to one device is silent and ruinous.

    Identical models can ship with identical USB descriptors, in which case
    by-id collides and both entries resolve to whichever enumerated first. Every
    other check then passes while half the system watches the wrong camera.
    """
    if len(cameras) < 2:
        return finding(SKIP, "distinct_devices", "only one camera configured")
    seen = {}
    for c in cameras:
        seen.setdefault(os.path.realpath(c["device"]), []).append(c["name"])
    clashes = {d: n for d, n in seen.items() if len(n) > 1}
    if clashes:
        return finding(FAIL, "distinct_devices",
                       "; ".join("%s all resolve to %s" % (", ".join(n), d)
                                 for d, n in clashes.items())
                       + "  - identical descriptors; identify by by_path instead")
    return finding(PASS, "distinct_devices", "%d distinct devices" % len(seen))


# --- per camera -----------------------------------------------------------

def camera_format(cfg, cam, f):
    name = cam["name"]
    if not f.get("asked"):
        return finding(FAIL, "camera", "no device, or format unreadable, at %s"
                       % f["device"], name)
    exp = cam.get("expect", {})
    want = (exp.get("width"), exp.get("height"), exp.get("fourcc"))
    got = (f["asked"]["width"], f["asked"]["height"], f["asked"]["fourcc"])
    if None in want:
        return finding(SKIP, "camera", "no expected format in config", name)
    if got != tuple(want):
        return finding(FAIL, "camera", "running %dx%d %s, expected %dx%d %s"
                       % (got + tuple(want)), name)
    return finding(PASS, "camera", "%dx%d %s" % got, name)


def identity(cfg, cam, f):
    """Can we say *which* physical camera this is?

    Intrinsics belong to a specific sensor and lens, so a swap silently
    invalidates a calibration while format, framerate and detections all still
    look perfect.

    Two ways to establish identity, in order of preference:

    - **by_path** - the physical USB port. Unambiguous, and the only option for
      cameras whose USB descriptors are not unique. Measured on two Thriftiest
      Cams: both report `DCX-260513-K / Thrifty / 01.00.00`, so /dev/v4l/by-id/
      holds one link for both and the second to enumerate overwrites the first.
      A config using by_id then points at whichever plugged in last, with every
      other check still passing.
    - **serial** - only meaningful when the descriptor really is per-unit.
    """
    name = cam["name"]
    dev = f.get("device")

    path = cam.get("by_path")
    if path:
        if not os.path.exists(path):
            return finding(FAIL, "identity",
                           "no camera in the configured port (%s)"
                           % os.path.basename(path), name)
        if os.path.realpath(path) != os.path.realpath(dev or ""):
            return finding(FAIL, "identity",
                           "configured port resolves to %s but using %s"
                           % (os.path.realpath(path), dev), name)
        return finding(PASS, "identity", "port %s%s"
                       % (os.path.basename(path),
                          "  (%s)" % cam["label"] if cam.get("label") else ""), name)

    serial = cam.get("serial")
    if not serial:
        return finding(SKIP, "identity",
                       "no by_path or serial - cannot tell which camera this is", name)
    byid = f.get("stable_path")
    if not byid:
        return finding(FAIL, "identity", "no by-id path - cannot confirm which "
                       "camera this is, and device numbering moves on replug", name)
    if serial not in byid:
        return finding(FAIL, "identity", "expected serial %s, connected camera is %s"
                       % (serial, os.path.basename(byid)), name)
    return finding(PASS, "identity", serial, name)


def calibration(cfg, cam, f):
    """A configured camera with no calibration is not ready - it FAILS.

    An earlier version made "not configured" a SKIP, which publishes green: an
    uncalibrated camera showed two reassuring lights on the dashboard. A check
    that cannot run is a SKIP; a camera that is missing something it needs is a
    failure. Set `calibration.required: false` while bringing one up.
    """
    name = cam["name"]
    c = f.get("calibration") or {}
    required = cam.get("calibration", {}).get("required", True)
    if not c.get("path"):
        if not required:
            return finding(SKIP, "calibration",
                           "%s (calibration.required is false)"
                           % c.get("error", "not found"), name)
        if c.get("error") == "not configured":
            return finding(FAIL, "calibration",
                           "no calibration - calibrate it in PhotonVision, then "
                           "set calibration.file", name)
        return finding(FAIL, "calibration", c.get("error", "not found"), name)
    fx = c.get("fx")
    if fx is None:
        return finding(FAIL, "calibration", "no intrinsics in %s" % c["path"], name)
    if fx <= 0:
        # A negative focal length is a mirrored solve, not a bad calibration.
        return finding(FAIL, "calibration",
                       "fx=%.1f - negative focal length means a diverged solve" % fx, name)
    width = cam.get("expect", {}).get("width")
    if not width:
        return finding(PASS, "calibration", "fx=%.1f" % fx, name)
    hfov = 2 * math.degrees(math.atan(width / (2 * fx)))
    cal = cam.get("calibration", {})
    want, tol = cal.get("expect_hfov_deg"), cal.get("tolerance_deg", 2.0)
    if want is None:
        return finding(PASS, "calibration", "fx=%.1f, HFOV %.1f deg" % (fx, hfov), name)
    if abs(hfov - want) > tol:
        return finding(FAIL, "calibration", "HFOV %.1f deg, expected %.1f +/- %.1f"
                       % (hfov, want, tol), name)
    return finding(PASS, "calibration", "HFOV %.1f deg" % hfov, name)


def calibration_resolution(cfg, cam, f):
    """Does the calibration cover the resolution actually running?

    Intrinsics are in pixels and do not transfer between resolutions.
    PhotonVision keeps one per resolution and falls back to estimating from the
    FOV setting when one is missing - which measured as 0.65 deg of systematic
    yaw error at 640x400, with nothing looking wrong.
    """
    name = cam["name"]
    c = f.get("calibration") or {}
    size, asked = c.get("image_size"), f.get("asked")
    if not c.get("path"):
        # The calibration check already reports this as a failure; repeating it
        # here would just double-count one problem.
        return finding(SKIP, "calibration_res", "no calibration - see above", name)
    if not size:
        return finding(SKIP, "calibration_res", "calibration records no image size", name)
    if not asked:
        return finding(SKIP, "calibration_res", "current format unknown", name)
    if [asked["width"], asked["height"]] != list(size):
        return finding(FAIL, "calibration_res",
                       "calibrated at %dx%d but running %dx%d - intrinsics do not "
                       "transfer between resolutions"
                       % (size[0], size[1], asked["width"], asked["height"]), name)
    return finding(PASS, "calibration_res", "%dx%d" % (size[0], size[1]), name)


# --- network, per camera --------------------------------------------------

def network(cfg, cameras):
    """Connects to NetworkTables, then asks per camera whether it is producing.

    The connection is global; the rate and the detections are not.
    """
    pf = cfg.get("preflight", {})
    if not pf.get("require_nt", True):
        return [finding(SKIP, "networktables", "not required by config")]
    from . import nt
    inst, err = nt.connect(cfg.get("networktables"), pf.get("nt_timeout_s", 5.0))
    if inst is None or err:
        return [finding(FAIL, "networktables", err or "no instance")]
    table = cfg.get("photonvision", {}).get("nt_table", "photonvision")
    published, topics = nt.watch(inst, table, seconds=1.5)
    live = {p: d for p, d in topics.items() if d["rate"] > 0}
    if not live:
        return [finding(FAIL, "networktables",
                        "connected but nothing is updating under /%s" % table)]
    fastest = max(live.values(), key=lambda d: d["rate"])["rate"]
    rows = [finding(PASS, "networktables", "%d topic(s) live, fastest %.0f Hz"
                    % (len(live), fastest))]

    for cam in cameras:
        name = cam["name"]
        if name not in published:
            rows.append(finding(FAIL, "publishing",
                                "not publishing under /%s - is the PhotonVision "
                                "nickname '%s'? saw: %s"
                                % (table, name, ", ".join(published) or "none"), name))
            continue

        def value(suffix):
            d = topics.get("%s/%s" % (name, suffix))
            return d["value"] if d else None

        fps, fps_min = value("fps"), pf.get("min_fps")
        if fps is None:
            rows.append(finding(SKIP, "pipeline_fps", "no fps topic", name))
        elif fps_min is not None and float(fps) < float(fps_min):
            # Nothing is broken at a low rate, but it is worth saying: framerate
            # here is set by exposure, and exposure is set by the light.
            rows.append(finding(FAIL, "pipeline_fps",
                                "%s fps, below the %s minimum - usually exposure, "
                                "which is set by the available light"
                                % (fps, fps_min), name))
        else:
            rows.append(finding(PASS, "pipeline_fps", "%s fps" % fps, name))

        # The headline. Everything else can pass while detection is silently
        # dead: exposure drifting below the light's detection floor produces no
        # error anywhere, just hasTarget going false.
        ht = value("hasTarget")
        if ht is None:
            rows.append(finding(SKIP, "detections", "no hasTarget topic", name))
        elif not pf.get("require_detections", False):
            rows.append(finding(SKIP, "detections",
                                "not required by config (hasTarget is %s)" % ht, name))
        elif str(ht).lower() in ("true", "1"):
            rows.append(finding(PASS, "detections", "a tag is in view", name))
        else:
            rows.append(finding(FAIL, "detections",
                                "hasTarget is false - no tag detected. If one is in "
                                "view, exposure is probably below the detection "
                                "floor for the current light", name))
    return rows


def run_all(cfg, cameras, facts_by_camera):
    """Every check, ordered so each camera's results end up together.

    The network pass runs last because it is the slow one, but its per-camera
    findings are folded back in beside that camera's other results - reading
    them at the bottom would split each camera across two places.
    """
    rows = [configured(cfg, cameras), power(cfg, facts_by_camera),
            service(cfg, facts_by_camera), unique_devices(cfg, cameras)]
    net = network(cfg, cameras)
    rows.extend(r for r in net if r["camera"] is None)
    for cam, f in zip(cameras, facts_by_camera):
        rows.append(camera_format(cfg, cam, f))
        rows.append(identity(cfg, cam, f))
        rows.append(calibration(cfg, cam, f))
        rows.append(calibration_resolution(cfg, cam, f))
        rows.extend(r for r in net if r["camera"] == cam["name"])
    return rows

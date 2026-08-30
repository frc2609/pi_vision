"""Everything that reads or writes the camera device.

Parsing is deliberately split from shelling out: every `parse_*` function takes
text and returns data, so they can be tested against captured `v4l2-ctl` output
with no camera and no Pi - the same rule the health monitor's tests follow.
"""

import glob
import os
import re
import socket
import time

from .util import sh

# UVC exposure is in units of 100us, so a control value of 120 is 12ms and the
# framerate it permits is 10000/value. That arithmetic explains most "why is it
# slow" questions in this project.
EXPOSURE_TICKS_PER_MS = 10.0


# --- parsers (pure functions - test these) --------------------------------

def parse_formats(text):
    """`v4l2-ctl --list-formats-ext` -> {fourcc: [(w, h, fps), ...]}"""
    if not text:
        return {}
    caps, fmt, size = {}, None, None
    for line in text.splitlines():
        m = re.search(r"\]:\s*'(\w+)'", line)
        if m:
            fmt = m.group(1)
            caps.setdefault(fmt, [])
            continue
        m = re.search(r"Size:\s*Discrete\s*(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and fmt and size:
            caps[fmt].append((size[0], size[1], float(m.group(1))))
    return caps


def parse_current_format(text):
    """`v4l2-ctl --all` -> (width, height, fourcc) or None"""
    if not text:
        return None
    w = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", text)
    f = re.search(r"Pixel Format\s*:\s*'(\w+)'", text)
    if not (w and f):
        return None
    return int(w.group(1)), int(w.group(2)), f.group(1)


def parse_controls(text):
    """`v4l2-ctl --list-ctrls` -> {name: (value, flags)}

    The flags matter: `inactive` means an automatic mode owns the control and
    writes to it are accepted and silently discarded.
    """
    ctrls = {}
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\(\w+\)\s*:\s*(.*)$", line)
        if not m:
            continue
        val = re.search(r"value=(\S+)", m.group(2))
        flags = re.search(r"flags=(.*)$", m.group(2))
        ctrls[m.group(1)] = (val.group(1) if val else None,
                             flags.group(1).strip() if flags else "")
    return ctrls


def best_mode(caps, fourcc):
    """Highest pixel count for one format, as a headline figure."""
    modes = (caps or {}).get(fourcc) or []
    return max(modes, key=lambda m: m[0] * m[1], default=None)


def max_fps_for_exposure(ticks):
    """The framerate an exposure permits. Above this, exposure is the limit."""
    return 10000.0 / max(float(ticks), 1.0)


# --- device access --------------------------------------------------------

def list_devices():
    nodes = sorted(glob.glob("/dev/video*"))
    byid = {}
    for link in sorted(glob.glob("/dev/v4l/by-id/*")):
        try:
            byid[os.path.realpath(link)] = link
        except OSError:
            pass
    return nodes, byid


def bus(dev):
    out = sh(["v4l2-ctl", "-d", dev, "--info"])
    m = re.search(r"Bus info\s*:\s*(\S+)", out) if out else None
    return m.group(1) if m else None


def capabilities(dev):
    return parse_formats(sh(["v4l2-ctl", "-d", dev, "--list-formats-ext"]))


def current_format(dev):
    return parse_current_format(sh(["v4l2-ctl", "-d", dev, "--all"]))


def controls(dev):
    return parse_controls(sh(["v4l2-ctl", "-d", dev, "--list-ctrls"]))


def read_exposure(dev):
    out = sh(["v4l2-ctl", "-d", dev, "--get-ctrl=exposure_time_absolute"])
    if out and ":" in out:
        try:
            return float(out.split(":")[-1].strip())
        except ValueError:
            pass
    return None


def set_exposure(dev, ticks):
    """Write exposure, then confirm. v4l2 reports success while clamping."""
    sh(["v4l2-ctl", "-d", dev, "-c", "exposure_time_absolute=%d" % ticks])
    time.sleep(0.3)
    got = read_exposure(dev)
    return got, (got is not None and abs(got - ticks) <= max(2.0, ticks * 0.02))


def set_control(dev, name, value):
    """Set one control and report what the device actually holds afterwards."""
    sh(["v4l2-ctl", "-d", dev, "-c", "%s=%s" % (name, value)])
    got = controls(dev).get(name, (None, ""))[0]
    return got, (got is not None and str(got) == str(value))


def holder(dev):
    """Which process has the device open, via /proc.

    Returns (pid, name), "denied" when other users' descriptors are unreadable,
    or None when nothing holds it. Only one process can hold a V4L2 capture
    device, which is why anything wanting to measure has to ask first.
    """
    try:
        target = os.path.realpath(dev)
    except OSError:
        return None
    denied = False
    for fddir in glob.glob("/proc/[0-9]*/fd"):
        pid = fddir.split("/")[2]
        try:
            for fd in os.listdir(fddir):
                try:
                    if os.path.realpath(os.path.join(fddir, fd)) == target:
                        try:
                            name = open("/proc/%s/comm" % pid).read().strip()
                        except OSError:
                            name = "?"
                        return int(pid), name
                except OSError:
                    continue
        except PermissionError:
            denied = True
        except OSError:
            continue
    return "denied" if denied else None


def probe(dev, seconds=2.0, family="DICT_APRILTAG_36h11"):
    """Open the device and measure. Only possible when nothing else holds it."""
    try:
        import cv2
    except ImportError:
        return {"error": "cv2 not available"}
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        return {"error": "could not open (in use?)"}
    for _ in range(10):
        cap.read()
    n, frame = 0, None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        ok, f = cap.read()
        if ok:
            n += 1
            frame = f
    dt = time.perf_counter() - t0
    got = (cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode(errors="replace")
    cap.release()
    out = {"fps": n / dt if dt else 0.0, "frames": n,
           "size": (int(got[0]), int(got[1])), "fourcc": fourcc,
           "mean": None, "tags": None}
    if frame is not None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out["mean"] = float(gray.mean())
        try:
            d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, family))
            _, ids, _ = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters()).detectMarkers(gray)
            out["tags"] = [] if ids is None else [int(i) for i in ids.flatten()]
        except Exception:
            pass
    return out


# --- host -----------------------------------------------------------------

def addresses():
    """(primary_ip, [all non-loopback IPv4s]).

    The IP is the address that actually works: Chrome blocks `.local`
    sub-resources from a plain-HTTP page, so PhotonVision's embedded stream is
    blank over mDNS and fine over the IP.
    """
    ips = []
    out = sh(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    if out:
        ips = re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    primary = None
    try:
        # Nothing is sent; this asks the routing table which source address
        # would be used, which is the one a laptop can reach.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        primary = s.getsockname()[0]
        s.close()
    except OSError:
        primary = ips[0] if ips else None
    if primary and primary not in ips:
        ips.insert(0, primary)
    return primary, ips


def throttled():
    """(raw, description) from vcgencmd, or (None, None) off a Pi."""
    out = sh(["vcgencmd", "get_throttled"])
    if not out or "=" not in out:
        return None, None
    try:
        raw = int(out.split("=")[1].strip(), 16)
    except ValueError:
        return None, None
    bits = {0: "under-voltage now", 1: "arm freq capped now", 2: "throttled now",
            3: "soft temp limit now", 16: "under-voltage has occurred",
            17: "arm freq capped has occurred", 18: "throttling has occurred",
            19: "soft temp limit has occurred"}
    hits = [t for b, t in bits.items() if raw & (1 << b)]
    return raw, "; ".join(hits) if hits else "clean"


def service_state(name):
    out = sh(["systemctl", "is-active", name])
    return out.strip() if out else None

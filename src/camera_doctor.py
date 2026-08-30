#!/usr/bin/env python3
"""Camera diagnostics: what the camera can do, what is being asked of it, and what
it is actually delivering.

The health monitor answers "is the Pi healthy". This answers "is the camera doing
what I think it is", which is a different question and fails in different ways.

The single most useful thing it prints is the gap between three columns:

    CAN         what the hardware advertises      (v4l2 format enumeration)
    ASKED       what the current holder requested (v4l2 current format)
    DELIVERED   what actually arrives             (measured, when the device is free)

Almost every camera problem in this project has been a disagreement between those.
PhotonVision defaulting to 320x240 YUYV on a camera capable of 1280x800 MJPEG at
120fps was invisible in the UI and obvious here.

Only one process can hold a V4L2 capture device. If PhotonVision has it, this tool
cannot open it - so it says who holds it and where that process publishes its own
stream, rather than fighting for the device.

Fails soft throughout: every probe returns None rather than raising. A diagnostic
that crashes when one reading is unavailable is worse than useless.

Exit codes, so preflight and shell scripts can use it:
    0  camera present and running at its best mode for the current format
    1  running below what the hardware advertises, or capabilities unreadable
    2  no device
"""

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

DEFAULT_DEV = "/dev/video0"
PV_UI_PORT = 5800
PV_STREAM_PORTS = (1181, 1182)  # raw, processed - one pair per camera


# --- shelling out, safely ------------------------------------------------

def sh(args, timeout=6):
    """Run a command, return stdout as text. None on any failure at all."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    # Some tools write useful output and still exit non-zero, so take whatever
    # came back rather than gating on the return code.
    return (p.stdout or "") + (p.stderr or "")


def have(cmd):
    return sh(["which", cmd]) not in (None, "")


# --- what exists ---------------------------------------------------------

def list_devices():
    nodes = sorted(glob.glob("/dev/video*"))
    byid = {}
    for link in sorted(glob.glob("/dev/v4l/by-id/*")):
        try:
            byid[os.path.realpath(link)] = link
        except OSError:
            pass
    return nodes, byid


def usb_line(dev):
    """The lsusb entry for the device, plus which USB bus it landed on.

    Bus matters: a USB 2.0 device in a blue port is still USB 2.0, and the bus
    is where a bandwidth ceiling comes from.
    """
    out = sh(["v4l2-ctl", "-d", dev, "--info"])
    bus = None
    if out:
        m = re.search(r"Bus info\s*:\s*(\S+)", out)
        if m:
            bus = m.group(1)
    return bus, sh(["lsusb"])


# --- what it can do ------------------------------------------------------

def capabilities(dev):
    """Parse `--list-formats-ext` into {fourcc: [(w, h, fps), ...]}."""
    out = sh(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    if not out:
        return None
    caps, fmt, size = {}, None, None
    for line in out.splitlines():
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


def best(caps, fourcc):
    """Highest pixel count for one format, as a quick headline figure."""
    modes = (caps or {}).get(fourcc) or []
    return max(modes, key=lambda m: m[0] * m[1], default=None)


# --- what is being asked of it -------------------------------------------

def current_format(dev):
    out = sh(["v4l2-ctl", "-d", dev, "--all"])
    if not out:
        return None
    w = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
    f = re.search(r"Pixel Format\s*:\s*'(\w+)'", out)
    if not (w and f):
        return None
    return int(w.group(1)), int(w.group(2)), f.group(1)


def controls(dev):
    """{name: (value, flags)} - flags matter: 'inactive' means an auto mode owns it."""
    out = sh(["v4l2-ctl", "-d", dev, "--list-ctrls"])
    if not out:
        return None
    ctrls = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\(\w+\)\s*:\s*(.*)$", line)
        if not m:
            continue
        name, rest = m.group(1), m.group(2)
        val = re.search(r"value=(\S+)", rest)
        flags = re.search(r"flags=(.*)$", rest)
        ctrls[name] = (val.group(1) if val else None,
                       flags.group(1).strip() if flags else "")
    return ctrls


def holder(dev):
    """Which process has the device open.

    Reads /proc directly so it works without fuser. Other users' file
    descriptors are unreadable without root, so a None here means "cannot
    tell", not "nobody" - the caller says so.
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


# --- the other consumers on this box -------------------------------------

def service(name):
    out = sh(["systemctl", "is-active", name])
    return out.strip() if out else None


def pv_requested():
    """What PhotonVision last asked the camera for, from its own log."""
    out = sh(["journalctl", "-u", "photonvision", "--no-pager", "-n", "500"])
    if not out:
        return None
    hits = re.findall(r"Setting video mode to FPS:\s*(\d+)\s*Width:\s*(\d+)\s*"
                      r"Height:\s*(\d+)\s*Pixel Format:\s*(\w+)", out)
    if not hits:
        return None
    fps, w, h, fmt = hits[-1]
    return int(w), int(h), fmt.replace("k", ""), int(fps)


def listening_ports():
    out = sh(["ss", "-tln"]) or sh(["netstat", "-tln"]) or ""
    return {int(p) for p in re.findall(r":(\d+)\s", out)}


def addresses():
    """(primary_ip, [all non-loopback IPv4s]).

    The IP is the address that actually works: Chrome blocks sub-resources on a
    `.local` name from a plain-HTTP page, so the PhotonVision UI shows a blank
    stream over mDNS and a working one over the IP. Print both, IP first.
    """
    ips = []
    out = sh(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    if out:
        ips = re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    primary = None
    try:
        # Nothing is sent; this just asks the routing table which source
        # address would be used, which is the one a laptop can reach.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        primary = s.getsockname()[0]
        s.close()
    except OSError:
        primary = ips[0] if ips else None
    if primary and primary not in ips:
        ips.insert(0, primary)
    return primary, ips


# --- what is actually delivered ------------------------------------------

def live_probe(dev, seconds=2.0, family="DICT_APRILTAG_36h11"):
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
    n, t0, mean, tags = 0, time.perf_counter(), None, None
    frame = None
    while time.perf_counter() - t0 < seconds:
        ok, frame = cap.read()
        if ok:
            n += 1
    dt = time.perf_counter() - t0
    got = (cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode(errors="replace")
    cap.release()
    if frame is not None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean = float(gray.mean())
        try:
            d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, family))
            _, ids, _ = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters()).detectMarkers(gray)
            tags = [] if ids is None else [int(i) for i in ids.flatten()]
        except Exception:
            tags = None
    return {"fps": n / dt if dt else 0.0, "frames": n,
            "size": (int(got[0]), int(got[1])), "fourcc": fourcc,
            "mean": mean, "tags": tags}


# --- the report ----------------------------------------------------------

def row(label, value):
    print("  %-22s %s" % (label, "--" if value in (None, "") else value))


def collect(dev, family, probe=True):
    """Everything the report is built from, as plain data.

    Kept separate from printing so `--json` and preflight can consume the same
    facts rather than parsing text.
    """
    nodes, byid = list_devices()
    bus, lsusb = usb_line(dev)
    caps = capabilities(dev)
    cur = current_format(dev)
    ctrls = controls(dev) or {}
    h = holder(dev)
    ip, ips = addresses()

    data = {
        "device": dev,
        "nodes": nodes,
        "stable_path": byid.get(os.path.realpath(dev)),
        "usb_bus": bus,
        "can": {f: {"best": best(caps, f), "modes": len(caps[f])} for f in caps} if caps else None,
        "asked": {"width": cur[0], "height": cur[1], "fourcc": cur[2]} if cur else None,
        "controls": {k: {"value": v, "flags": fl} for k, (v, fl) in ctrls.items()},
        "holder": ({"pid": h[0], "name": h[1]} if isinstance(h, tuple)
                   else ("denied" if h == "denied" else None)),
        "services": {"photonvision": service("photonvision"),
                     "health_monitor": service("health_monitor")},
        "photonvision_asked": (dict(zip(("width", "height", "fourcc", "fps"), pv_requested()))
                               if pv_requested() else None),
        "listening": sorted(p for p in listening_ports()
                            if p in (PV_UI_PORT,) + PV_STREAM_PORTS),
        "hostname": socket.gethostname(),
        "ip": ip,
        "ips": ips,
        "delivered": None,
    }

    # The measured column is only possible when nothing else holds the device -
    # a V4L2 capture device has exactly one owner.
    if probe and not (isinstance(h, tuple) or h == "denied"):
        data["delivered"] = live_probe(dev, family=family)

    # Status is deliberately conservative: this is a diagnostic, so "something
    # looks off" is worth a non-zero exit, but only a missing device is fatal.
    if not cur:
        data["status"] = 2
    elif caps:
        b = best(caps, cur[2])
        data["status"] = 1 if (b and (cur[0], cur[1]) != (b[0], b[1])) else 0
    else:
        data["status"] = 1
    return data


def report(dev, family):
    host = socket.gethostname()
    print("\n=== devices ===")
    nodes, byid = list_devices()
    row("video nodes", " ".join(nodes) or None)
    stable = byid.get(os.path.realpath(dev))
    row("stable path", stable)
    if stable is None:
        print("  NOTE: no /dev/v4l/by-id entry - device numbering can move on replug")

    bus, lsusb = usb_line(dev)
    row("usb bus", bus)
    if lsusb and bus:
        for line in lsusb.splitlines():
            if "root hub" not in line:
                print("  %-22s %s" % ("", line.strip()))

    print("\n=== CAN (hardware advertises) ===")
    caps = capabilities(dev)
    if not caps:
        print("  -- could not enumerate (is v4l-utils installed?)")
    else:
        for fmt in sorted(caps):
            b = best(caps, fmt)
            row(fmt, "%dx%d @ %.0f fps  (%d modes)" % (b[0], b[1], b[2], len(caps[fmt]))
                if b else "no modes")

    print("\n=== ASKED (current negotiated format) ===")
    cur = current_format(dev)
    row("format", "%dx%d %s" % cur if cur else None)
    if cur and caps:
        b = best(caps, cur[2])
        if b and (cur[0], cur[1]) != (b[0], b[1]):
            print("  WARNING: running %dx%d but %s supports %dx%d" %
                  (cur[0], cur[1], cur[2], b[0], b[1]))

    ctrls = controls(dev) or {}
    for name in ("auto_exposure", "exposure_time_absolute", "brightness", "gain",
                 "power_line_frequency", "focus_automatic_continuous"):
        if name in ctrls:
            v, flags = ctrls[name]
            row(name, "%s%s" % (v, "  [%s]" % flags if flags else ""))
    if "gain" not in ctrls:
        print("  NOTE: no gain control exposed - exposure is the only real lever")

    print("\n=== WHO HAS IT ===")
    h = holder(dev)
    if h == "denied":
        row("holder", "cannot tell - rerun with sudo")
    elif h:
        row("holder", "pid %d (%s)" % h)
    else:
        row("holder", "nobody - device is free")

    print("\n=== CONSUMERS ===")
    row("photonvision", service("photonvision"))
    req = pv_requested()
    row("photonvision asked for", "%dx%d %s @ %d fps" % req if req else None)
    row("health_monitor", service("health_monitor"))
    ports = listening_ports()
    row("ports listening", " ".join(str(p) for p in sorted(
        p for p in ports if p in (PV_UI_PORT,) + PV_STREAM_PORTS)) or None)

    print("\n=== DELIVERED (measured) ===")
    if h and h != "denied":
        print("  skipped - %s holds the device; only one process can capture" %
              (h[1] if isinstance(h, tuple) else h))
    else:
        p = live_probe(dev, family=family)
        if "error" in p:
            row("probe", p["error"])
        else:
            row("measured fps", "%.1f  (%d frames)" % (p["fps"], p["frames"]))
            row("delivered", "%dx%d %s" % (p["size"][0], p["size"][1], p["fourcc"]))
            row("mean brightness", "%.1f  (0=black, 255=white)" % p["mean"]
                if p["mean"] is not None else None)
            row("tags visible", p["tags"] if p["tags"] else
                ("none" if p["tags"] == [] else None))

    ip, ips = addresses()
    mdns = host if host.endswith(".local") else host + ".local"

    print("\n=== WHERE TO LOOK ===")
    row("this host", "%s   %s" % (mdns, " ".join(ips) if ips else "(no address)"))
    if len(ips) > 1:
        print("  NOTE: more than one address - a viewer must be on the same network")
    if service("photonvision") == "active":
        print("  PhotonVision UI      http://%s:%d" % (ip or mdns, PV_UI_PORT))
        print("  raw camera stream    http://%s:%d/stream.mjpg" % (ip or mdns, PV_STREAM_PORTS[0]))
        print("  processed stream     http://%s:%d/stream.mjpg" % (ip or mdns, PV_STREAM_PORTS[1]))
        if ip:
            print("    use the IP above, not %s - Chrome blocks .local" % mdns)
            print("    sub-resources from a plain-HTTP page, so the UI's embedded")
            print("    stream stays blank over mDNS while the same URL works alone")
        print("  its log              journalctl -u photonvision -f")
    print("  health monitor log   journalctl -u health_monitor -f")
    print("  health monitor files /var/log/health_monitor/")
    print("  live camera format   v4l2-ctl -d %s --all | grep -A4 'Format Video'" % dev)
    print("  this tool, streaming %s --stream   -> http://%s:8090"
          % (os.path.basename(sys.argv[0]), ip or mdns))
    print()

    # Recomputed rather than threaded through the printing above: the report is
    # for a human, the exit code is for a script, and keeping them independent
    # means neither can quietly break the other.
    if not cur:
        return 2
    if caps:
        b = best(caps, cur[2])
        if b and (cur[0], cur[1]) != (b[0], b[1]):
            return 1
    return 0


# --- streaming -----------------------------------------------------------

def serve(dev, port, family, fps_cap=15):
    """Serve an annotated MJPEG stream so you can see what the camera sees.

    Deliberately capped well below the camera's framerate: this exists to be
    looked at by a human over the network, not to be fast.
    """
    import cv2
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    det = None
    try:
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, family))
        det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
    except Exception:
        pass

    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        h = holder(dev)
        print("could not open %s%s" % (dev, " - held by pid %d (%s)" % h
                                       if isinstance(h, tuple) else ""))
        return 1
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 800)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            last = time.perf_counter()
            while True:
                ok, frame = cap.read()
                if not ok:
                    continue
                now = time.perf_counter()
                rate, last = 1.0 / max(now - last, 1e-6), now
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                note = "mean %.0f   %.0f fps" % (gray.mean(), rate)
                if det is not None:
                    corners, ids, _ = det.detectMarkers(gray)
                    if ids is not None:
                        cv2.aruco.drawDetectedMarkers(frame, corners, ids)
                        p = corners[0].reshape(4, 2)
                        note = "id %d  %.0f px wide   %s" % (
                            ids.flatten()[0], max(p[:, 0]) - min(p[:, 0]), note)
                    else:
                        note = "no tag   " + note
                cv2.putText(frame, note, (16, 40), cv2.FONT_HERSHEY_SIMPLEX,
                            0.9, (0, 255, 0), 2)
                ok, jpg = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ok:
                    continue
                try:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                     + jpg.tobytes() + b"\r\n")
                except Exception:
                    return  # client went away
                time.sleep(1.0 / fps_cap)

    host = socket.gethostname()
    ip, _ = addresses()
    print("streaming  (ctrl-C to stop)")
    if ip:
        print("  http://%s:%d" % (ip, port))
    print("  http://%s:%d" % (host if host.endswith(".local") else host + ".local", port))
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
    return 0


def resolve_device(explicit):
    """Explicit flag wins, then config.yaml's by-id path, then /dev/video0."""
    if explicit:
        return explicit
    try:
        from common import load_config, device_from
        cfg, err = load_config()
        if not err:
            return device_from(cfg)
    except Exception:
        pass
    return DEFAULT_DEV


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-d", "--device", help="default: camera.by_id from config.yaml")
    ap.add_argument("--stream", action="store_true",
                    help="serve an annotated MJPEG stream after reporting")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--family", default="DICT_APRILTAG_36h11")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output, for snapshots and scripts")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()

    dev = resolve_device(args.device)

    if not os.path.exists(dev):
        nodes, _ = list_devices()
        msg = {"device": dev, "error": "no such device", "nodes": nodes, "status": 2}
        if args.json:
            print(json.dumps(msg, indent=2))
        else:
            print("no such device: %s" % dev)
            print("available: %s" % (" ".join(nodes) or "none - is the camera plugged in?"))
        return 2

    if args.json:
        data = collect(dev, args.family)
        print(json.dumps(data, indent=2, default=str))
        return data.get("status", 0)

    code = 0 if args.no_report else report(dev, args.family)
    if args.stream:
        return serve(dev, args.port, args.family)
    return code


if __name__ == "__main__":
    sys.exit(main())

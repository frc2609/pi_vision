"""One model of the system's state, from which every view is built.

`facts()` gathers what is true right now. The human report, the JSON dump, the
saved snapshot and the preflight checks are all views onto that same dict rather
than four separate implementations of "look at the camera".

Snapshots are flat `path/like/this` keys on purpose: diffing two nested
structures is fiddly, and a flat map diffs in six lines.
"""

import glob
import hashlib
import json
import os
import re
import time

from . import camera as cam
from .util import sh, repo_root, find_file

MAX_HASH_BYTES = 8 * 1024 * 1024


def file_hash(path):
    try:
        # Hashing a 120MB jar on every snapshot is not worth the seconds, but
        # "too large" would make it invisible to a diff. Size and mtime change
        # whenever the file is replaced, which is what we need.
        if os.path.getsize(path) > MAX_HASH_BYTES:
            st = os.stat(path)
            return "size=%d mtime=%d" % (st.st_size, int(st.st_mtime))
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError as e:
        return "unreadable:%s" % e.__class__.__name__


def pkg_version(name):
    out = sh(["dpkg-query", "-W", "-f=${Version}", name])
    return out.strip() if out and "no packages" not in out.lower() else None


def calibration(cfg, cam=None):
    """Intrinsics from a camera's calibration file, if it can be found.

    Per camera, always: intrinsics belong to one physical sensor and lens.
    """
    name = ((cam or {}).get("calibration") or {}).get("file")
    path = find_file(cfg, name)
    if not path:
        return {"path": None, "error": "not found" if name else "not configured"}
    out = {"path": path, "sha256_16": file_hash(path)}
    try:
        d = json.load(open(path))
        K = d.get("camera_matrix") or d.get("cameraIntrinsics", {}).get("data")
        if isinstance(K, list) and K and isinstance(K[0], list):
            K = [x for row in K for x in row]
        if K:
            out["fx"], out["fy"] = round(float(K[0]), 2), round(float(K[4]), 2)
            out["cx"], out["cy"] = round(float(K[2]), 2), round(float(K[5]), 2)
        # Intrinsics are in pixels and do not transfer between resolutions, so
        # the size they were measured at is part of the calibration.
        size = d.get("image_size") or d.get("resolution")
        if isinstance(size, dict):
            size = [size.get("width"), size.get("height")]
        if isinstance(size, list) and len(size) == 2 and None not in size:
            out["image_size"] = [int(size[0]), int(size[1])]
    except Exception as e:
        out["error"] = str(e)
    return out


def facts(cfg, camera_cfg, probe=False, family="DICT_APRILTAG_36h11"):
    """Everything true about one camera and its consumers, right now."""
    dev = camera_cfg["device"]
    nodes, byid = cam.list_devices()
    caps = cam.capabilities(dev)
    cur = cam.current_format(dev)
    ctrls = cam.controls(dev)
    who = cam.holder(dev)
    ip, ips = cam.addresses()
    raw, thr = cam.throttled()
    pv = (cfg or {}).get("photonvision", {})

    f = {
        "name": camera_cfg.get("name"),
        "device": dev,
        "nodes": nodes,
        "stable_path": byid.get(os.path.realpath(dev)) if os.path.exists(dev) else None,
        "usb_bus": cam.bus(dev),
        "can": {k: {"best": cam.best_mode(caps, k), "modes": len(v)}
                for k, v in (caps or {}).items()} or None,
        "asked": {"width": cur[0], "height": cur[1], "fourcc": cur[2]} if cur else None,
        "controls": {k: {"value": v, "flags": fl} for k, (v, fl) in (ctrls or {}).items()},
        "holder": ({"pid": who[0], "name": who[1]} if isinstance(who, tuple)
                   else ("denied" if who == "denied" else None)),
        "services": {pv.get("service", "photonvision"):
                     cam.service_state(pv.get("service", "photonvision")),
                     "health_monitor": cam.service_state("health_monitor")},
        "throttled": None if raw is None else {"raw": "0x%x" % raw, "text": thr},
        "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        "ip": ip,
        "ips": ips,
        "calibration": calibration(cfg, camera_cfg),
        "delivered": None,
    }

    # The measured view is only possible when nothing else holds the device -
    # a V4L2 capture device has exactly one owner.
    if probe and not (isinstance(who, tuple) or who == "denied"):
        f["delivered"] = cam.probe(dev, family=family)

    # Conservative: only a missing device is fatal; running below the hardware's
    # best mode is worth a non-zero exit but not an error.
    if not cur:
        f["status"] = 2
    elif caps:
        b = cam.best_mode(caps, cur[2])
        f["status"] = 1 if (b and (cur[0], cur[1]) != (b[0], b[1])) else 0
    else:
        f["status"] = 1
    return f


# --- snapshots ------------------------------------------------------------

def snapshot(cfg, camera_list):
    """A flat, diffable capture of everything that can silently change.

    Per-camera keys are namespaced by name, so a two-camera setup diffs cleanly
    and a camera appearing or disappearing shows up as added or removed keys.
    """
    snap = {"when": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    snap["host/uname"] = (sh(["uname", "-a"]) or "").strip() or None

    raw, text = cam.throttled()
    snap["power/throttled"] = None if raw is None else "0x%x (%s)" % (raw, text)

    for p in ("python3-opencv", "python3-yaml", "v4l-utils", "openjdk-25-jre-headless"):
        snap["pkg/" + p] = pkg_version(p)
    jar = "/opt/photonvision/photonvision.jar"
    snap["pkg/photonvision.jar"] = file_hash(jar) if os.path.exists(jar) else None
    # Loose regex on purpose: the log line's wording changes between releases.
    m = re.search(r"v(20\d\d\.\d+\.\d+[A-Za-z0-9.\-]*)",
                  sh(["journalctl", "-u", "photonvision", "--no-pager", "-n", "2000"]) or "")
    snap["pkg/photonvision_version"] = m.group(1) if m else None

    pv = (cfg or {}).get("photonvision", {})
    for svc in (pv.get("service", "photonvision"), "health_monitor"):
        snap["service/%s/active" % svc] = cam.service_state(svc)
        out = sh(["systemctl", "is-enabled", svc])
        snap["service/%s/enabled" % svc] = out.strip() if out else None

    nodes, byid = cam.list_devices()
    snap["camera/nodes"] = " ".join(nodes) or None
    for entry in camera_list or []:
        name, dev = entry.get("name", "camera"), entry["device"]
        pre = "camera/%s/" % name
        snap[pre + "device"] = dev
        snap[pre + "by_id"] = byid.get(os.path.realpath(dev)) if os.path.exists(dev) else None
        snap[pre + "usb_bus"] = cam.bus(dev)
        caps = cam.capabilities(dev) or {}
        for fmt in caps:
            b = cam.best_mode(caps, fmt)
            snap[pre + "can/" + fmt] = ("%dx%d@%.0f (%d modes)"
                                        % (b[0], b[1], b[2], len(caps[fmt]))) if b else None
        cur = cam.current_format(dev)
        snap[pre + "current_format"] = "%dx%d %s" % cur if cur else None
        # v4l2 controls are the classic silent change: they do not survive a
        # reboot or a replug, and nothing announces the reversion.
        for cname, (val, flags) in (cam.controls(dev) or {}).items():
            snap[pre + "ctrl/" + cname] = "%s%s" % (val, " [%s]" % flags if flags else "")
        c = calibration(cfg, entry)
        for k in ("path", "sha256_16", "fx", "fy", "image_size"):
            if k in c:
                snap[pre + "calibration/" + k] = c[k]
    sdir = pv.get("settings_dir")
    if sdir and os.path.isdir(sdir):
        for path in sorted(glob.glob(os.path.join(sdir, "**", "*"), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, sdir)
            # Logs change every second and would drown the real changes.
            if rel.startswith("logs" + os.sep) or rel.endswith(".log"):
                continue
            snap["pv_settings/" + rel] = file_hash(path)
    elif sdir:
        snap["pv_settings"] = "not readable (try sudo)"
    return snap


def snapshot_dir():
    d = os.path.join(repo_root(), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def newest_snapshots(n=2):
    files = sorted(glob.glob(os.path.join(snapshot_dir(), "*.json")))
    return files[-n:] if files else []


def diff(a, b):
    """[(key, old, new)] for every field that differs, ignoring the timestamp."""
    out = []
    for k in sorted(set(a) | set(b)):
        if k == "when":
            continue
        av, bv = a.get(k, "<absent>"), b.get(k, "<absent>")
        if av != bv:
            out.append((k, av, bv))
    return out

#!/usr/bin/env python3
"""Capture the whole camera-side configuration, and diff two captures.

This exists to answer one question: **"it worked yesterday - what changed?"**

That is the most common question at a competition and the hardest to answer from
memory, because the things that change are the things nobody thinks of as state:
a v4l2 control reset by a replug, a resolution someone tried and left, a package
updated by an unattended upgrade, a PhotonVision setting adjusted between
matches.

Take a snapshot when things are known good, take another when they are not, and
diff them.

    python3 src/config_snapshot.py                 # capture
    python3 src/config_snapshot.py --diff          # newest two
    python3 src/config_snapshot.py --diff a.json b.json
    python3 src/config_snapshot.py --list

Snapshots are small JSON and are meant to be committed. A snapshot from the last
event is worth more than any amount of remembering.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time

from common import load_config, device_from, service_state, sh, throttled, repo_root
import camera_doctor as cd

MAX_HASH_BYTES = 8 * 1024 * 1024


def file_hash(path):
    try:
        # Hashing a 120MB jar on every snapshot is not worth the seconds it
        # costs, but "too-large" would make it invisible to a diff. Size and
        # mtime change whenever the file is replaced, which is what we need.
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


def capture(cfg, dev):
    snap = {}

    snap["when"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    snap["host/uname"] = (sh(["uname", "-a"]) or "").strip() or None
    raw, text = throttled()
    snap["power/throttled"] = None if raw is None else "0x%x (%s)" % (raw, text)

    # --- packages and versions ------------------------------------------
    for p in ("python3-opencv", "python3-yaml", "v4l-utils", "openjdk-25-jre-headless"):
        snap["pkg/" + p] = pkg_version(p)
    jar = "/opt/photonvision/photonvision.jar"
    snap["pkg/photonvision.jar"] = file_hash(jar) if os.path.exists(jar) else None
    # Loose on purpose: the log line's wording has changed between releases, so
    # match the version shape anywhere rather than a phrase that may not recur.
    pv_log = sh(["journalctl", "-u", "photonvision", "--no-pager", "-n", "2000"]) or ""
    m = re.search(r"v(20\d\d\.\d+\.\d+[A-Za-z0-9.\-]*)", pv_log)
    snap["pkg/photonvision_version"] = m.group(1) if m else None

    # --- services -------------------------------------------------------
    for svc in (cfg.get("photonvision", {}).get("service", "photonvision"),
                "health_monitor"):
        snap["service/%s/active" % svc] = service_state(svc)
        out = sh(["systemctl", "is-enabled", svc])
        snap["service/%s/enabled" % svc] = out.strip() if out else None

    # --- the camera itself ----------------------------------------------
    snap["camera/device"] = dev
    nodes, byid = cd.list_devices()
    snap["camera/nodes"] = " ".join(nodes) or None
    snap["camera/by_id"] = byid.get(os.path.realpath(dev))
    bus, lsusb = cd.usb_line(dev)
    snap["camera/usb_bus"] = bus
    if lsusb:
        for line in lsusb.splitlines():
            if "root hub" in line:
                continue
            m = re.search(r"ID\s+([0-9a-f]{4}:[0-9a-f]{4})\s+(.*)$", line)
            if m:
                snap["camera/usb_%s" % m.group(1)] = m.group(2).strip()

    caps = cd.capabilities(dev) or {}
    for fmt, modes in caps.items():
        b = cd.best(caps, fmt)
        snap["camera/can/%s" % fmt] = "%dx%d@%.0f (%d modes)" % (b[0], b[1], b[2], len(modes)) if b else None
    cur = cd.current_format(dev)
    snap["camera/current_format"] = "%dx%d %s" % cur if cur else None

    # v4l2 controls are the classic silent change: they do not survive a reboot
    # or a replug, and nothing announces that they have gone back to default.
    for name, (val, flags) in (cd.controls(dev) or {}).items():
        snap["camera/ctrl/%s" % name] = "%s%s" % (val, " [%s]" % flags if flags else "")

    # --- calibration ----------------------------------------------------
    calfile = cfg.get("calibration", {}).get("file")
    if calfile:
        for cand in (calfile, os.path.join(repo_root(), calfile),
                     os.path.expanduser("~/vision/calib/" + os.path.basename(calfile))):
            if os.path.exists(cand):
                snap["calibration/path"] = cand
                snap["calibration/sha256_16"] = file_hash(cand)
                try:
                    d = json.load(open(cand))
                    K = d.get("camera_matrix") or d.get("cameraIntrinsics", {}).get("data")
                    if isinstance(K, list) and K and isinstance(K[0], list):
                        K = [x for row in K for x in row]
                    if K:
                        snap["calibration/fx"] = round(float(K[0]), 2)
                        snap["calibration/fy"] = round(float(K[4]), 2)
                except Exception:
                    pass
                break

    # --- PhotonVision's own settings ------------------------------------
    sdir = cfg.get("photonvision", {}).get("settings_dir")
    if sdir and os.path.isdir(sdir):
        for path in sorted(glob.glob(os.path.join(sdir, "**", "*"), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, sdir)
            # Log files change every second and would drown the real changes in
            # every diff. A snapshot is for configuration, not for activity.
            if rel.startswith("logs" + os.sep) or rel.endswith(".log"):
                continue
            snap["pv_settings/" + rel] = file_hash(path)
    elif sdir:
        snap["pv_settings"] = "not readable (try sudo)"

    return snap


def snap_dir():
    d = os.path.join(repo_root(), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def newest(n=2):
    files = sorted(glob.glob(os.path.join(snap_dir(), "*.json")))
    return files[-n:] if files else []


def diff(a_path, b_path):
    a = json.load(open(a_path))
    b = json.load(open(b_path))
    keys = sorted(set(a) | set(b))
    changed = 0
    print("\n--- %s" % os.path.basename(a_path))
    print("+++ %s\n" % os.path.basename(b_path))
    for k in keys:
        if k == "when":
            continue
        av, bv = a.get(k, "<absent>"), b.get(k, "<absent>")
        if av != bv:
            changed += 1
            print("  %s" % k)
            print("    - %s" % av)
            print("    + %s" % bv)
    if not changed:
        print("  identical (ignoring the timestamp)")
    print("\n  %d field(s) changed\n" % changed)
    return 1 if changed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config")
    ap.add_argument("-d", "--device")
    ap.add_argument("--diff", nargs="*", metavar="FILE",
                    help="diff two snapshots; with no arguments, the newest two")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--print", action="store_true", help="print instead of saving")
    args = ap.parse_args()

    if args.list:
        for f in sorted(glob.glob(os.path.join(snap_dir(), "*.json"))):
            print("  %s" % f)
        return 0

    if args.diff is not None:
        files = args.diff if len(args.diff) == 2 else newest(2)
        if len(files) != 2:
            print("need two snapshots to diff; have %d" % len(files))
            return 2
        return diff(files[0], files[1])

    cfg, err = load_config(args.config)
    if err:
        print("config: %s" % err)
        return 2
    dev = device_from(cfg, args.device)
    snap = capture(cfg, dev)

    if args.print:
        print(json.dumps(snap, indent=2))
        return 0

    out = os.path.join(snap_dir(), time.strftime("%Y%m%d-%H%M%S") + ".json")
    with open(out, "w") as f:
        json.dump(snap, f, indent=2, sort_keys=True)
    print("wrote %s  (%d fields)" % (out, len(snap)))
    print("commit it - a snapshot from the last event is worth more than remembering")
    return 0


if __name__ == "__main__":
    sys.exit(main())

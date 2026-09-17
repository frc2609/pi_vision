#!/usr/bin/env python3
"""usb_load - can every connected camera stream at once? No PhotonVision.

The USB half of "can the Pi handle N cameras", on its own. Tag detection is the
CPU half and needs PhotonVision (scripts/camera_load.py). This is the half no
software fixes: a USB camera reserves bandwidth when it starts streaming, and
when its controller has none left the start fails outright. uvcvideo logs
"No space left on device" and the application simply never gets a frame.

    cd ~/vision/camera
    .venv/bin/python scripts/usb_load.py
    .venv/bin/python scripts/usb_load.py --seconds 20

Plug every camera in first. It stops PhotonVision (only one process can hold a
camera), streams 1, then 2, then all of them at once, and starts PhotonVision
again however it exits - ctrl-C included.

Exposure is set short so the light cannot cap the framerate. PhotonVision
writes its own exposure back when it reopens the cameras, so nothing here
outlives the run. Point the cameras at something bright and busy if you can:
MJPEG frames of a dark, empty scene are small and understate the sustained
bandwidth - though not the reservation, which is the part that fails.
"""

import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camera_load import devices, kernel_since, port                    # noqa: E402
from vision import camera, util                                          # noqa: E402

FPS = re.compile(r"([\d.]+) fps")
DROPPED = re.compile(r"dropped buffers: (\d+)")
FULL_RATE = 0.95            # within 5% of the advertised rate counts as full


def advertised_fps(dev, width, height, fourcc):
    modes = camera.parse_formats(util.sh(["v4l2-ctl", "-d", dev, "--list-formats-ext"]))
    rates = [f for w, h, f in modes.get(fourcc, []) if (w, h) == (width, height)]
    return max(rates) if rates else None


def start_stream(dev, width, height, fourcc):
    # v4l2-ctl prints one '<' per frame and a running "N fps" line, to stderr.
    # No decoding happens, so this loads the USB bus and almost nothing else.
    return subprocess.Popen(
        ["v4l2-ctl", "-d", dev,
         "--set-fmt-video=width=%d,height=%d,pixelformat=%s" % (width, height, fourcc),
         "--stream-mmap"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def stop_stream(proc, seconds):
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return parse_stream(out or "", seconds)


def parse_stream(out, seconds):
    """v4l2-ctl --stream-mmap output -> {fps, dropped, errors}."""
    rates = [float(x) for x in FPS.findall(out)]
    dropped = [int(x) for x in DROPPED.findall(out)]
    errors = [line.strip() for line in out.splitlines()
              if "VIDIOC" in line or "error" in line.lower() or "busy" in line.lower()]
    if len(rates) > 1:
        fps = sum(rates[1:]) / len(rates[1:])       # first reading includes start-up
    elif rates:
        fps = rates[0]
    else:
        fps = out.count("<") / seconds if "<" in out else 0.0
    return {"fps": fps, "dropped": max(dropped) if dropped else 0, "errors": errors}


def describe(result):
    if result is None:
        return "-"
    if result["errors"] and result["fps"] == 0:
        e = " ".join(result["errors"])
        if "No space left" in e:
            return "FAIL no bandwidth"
        if "busy" in e.lower():
            return "FAIL busy"
        return "FAIL"
    return "%.1f" % result["fps"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--seconds", type=float, default=10.0, help="per step")
    ap.add_argument("--exposure", type=int, default=50,
                    help="ticks of 100us; 50 is 5 ms, which allows 200 fps")
    args = ap.parse_args()

    cfg, err = util.load_config()
    if err:
        print(err)
        return 2
    exp = cfg.get("camera_defaults", {}).get("expect", {})
    width, height = exp.get("width", 1280), exp.get("height", 800)
    fourcc = exp.get("fourcc", "MJPG")
    service = cfg.get("photonvision", {}).get("service", "photonvision")

    names = {c["by_path"]: c["name"] for c in cfg.get("cameras", []) if c.get("by_path")}
    devs = devices()
    if not devs:
        print("no USB cameras connected")
        return 2
    # Configured cameras first, in config order, so the columns read the same
    # way every run; anything unconfigured after them, named by its port.
    order = [l for l in names if l in devs] + [l for l in devs if l not in names]
    cams = []
    for link in order:
        ctl, prt = port(link)
        cams.append({"link": link, "dev": devs[link], "controller": ctl, "port": prt,
                     "name": names.get(link) or "new@%s:%s" % (ctl, prt)})

    print("%d camera(s): %s" % (len(cams), ", ".join(
        "%s (controller %s port %s)" % (c["name"], c["controller"], c["port"])
        for c in cams)))
    print("stopping %s - it holds the cameras" % service)
    if subprocess.run(["sudo", "systemctl", "stop", service]).returncode != 0:
        print("could not stop %s" % service)
        return 2

    results, procs = [], []
    try:
        time.sleep(2)
        for c in cams:
            h = camera.holder(c["dev"])
            if isinstance(h, tuple):
                print("%s is still held by %s (pid %d) - stop that first"
                      % (c["name"], h[1], h[0]))
                return 2
            camera.set_control(c["dev"], "auto_exposure", 1)        # 1 = manual
            got, ok = camera.set_exposure(c["dev"], args.exposure)
            c["expected"] = advertised_fps(c["dev"], width, height, fourcc)
            if not ok:
                print("  %s: asked for exposure %d, holds %s" % (c["name"], args.exposure, got))

        for k in range(1, len(cams) + 1):
            live = cams[:k]
            print("\n--- %d stream%s at once ---" % (k, "" if k == 1 else "s"))
            started = time.time()
            procs = [start_stream(c["dev"], width, height, fourcc) for c in live]
            time.sleep(args.seconds)
            step = {}
            for c, p in zip(live, procs):
                step[c["name"]] = stop_stream(p, args.seconds)
            procs = []
            step["_kernel"] = kernel_since(started - 1)
            results.append(step)
            for c in live:
                r = step[c["name"]]
                print("  %-12s %-18s of %s   dropped %d%s"
                      % (c["name"], describe(r),
                         "%.0f" % c["expected"] if c["expected"] else "?", r["dropped"],
                         ("   " + r["errors"][0]) if r["errors"] else ""))
            for line in (step["_kernel"] or [])[-6:]:
                print("  kernel: " + line)
    except KeyboardInterrupt:
        print("\n(stopped)")
    finally:
        for p in procs:
            p.kill()
        subprocess.run(["sudo", "systemctl", "start", service])
        state = (util.sh(["systemctl", "is-active", service]) or "").strip()
        print("\n%s restarted: %s" % (service, state or "unknown"))
        if state != "active":
            print("  START IT BY HAND: sudo systemctl start %s" % service)

    if not results:
        return 1

    heads = ["%d stream%s" % (i + 1, "" if i == 0 else "s") for i in range(len(results))]
    w = max(len(c["name"]) for c in cams) + 2
    print("\n" + " " * w + "".join("%20s" % h for h in heads))
    for c in cams:
        print(c["name"].ljust(w) + "".join(
            "%20s" % describe(r.get(c["name"])) for r in results))

    last = results[-1]
    full = [c for c in cams if c["name"] in last and c["expected"]
            and last[c["name"]]["fps"] >= FULL_RATE * c["expected"]]
    print()
    if len(results) < len(cams):
        print("stopped after %d of %d steps - no verdict for all %d at once"
              % (len(results), len(cams), len(cams)))
    elif len(full) == len(cams):
        print("PASS - USB carries all %d at full rate at once. The limit, if there is "
              "one, is CPU: run camera_load.py next." % len(cams))
    else:
        short = [c["name"] for c in cams if c not in full]
        print("NOT FULL RATE with all %d streaming: %s" % (len(cams), ", ".join(short)))
        by_ctl = {}
        for c in cams:
            by_ctl.setdefault(c["controller"], []).append(c["name"])
        for ctl, ns in sorted(by_ctl.items()):
            if len(ns) > 1:
                print("  %s share controller %s. Try the failing one in a port on the "
                      "other controller, or compare with it alone."
                      % (" and ".join(ns), ctl))
    if any(r["_kernel"] is None for r in results):
        print("kernel log unreadable, so USB errors were not checked "
              "(sudo usermod -aG adm $USER, then log in again)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

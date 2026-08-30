#!/usr/bin/env python3
"""Apply the v4l2 controls from config.yaml, then verify they took.

**v4l2 controls do not survive a reboot or a replug.** They live on the device,
not in a file, and nothing announces that they have gone back to default. That is
a silent change of exactly the kind config_snapshot exists to catch, and this
script is the fix.

Two things make this less trivial than it looks:

- **Order matters.** `exposure_time_absolute` is flagged inactive while
  `auto_exposure` is in an automatic mode, and a write to an inactive control is
  accepted and discarded. Manual mode has to be set first.
- **A successful write proves nothing.** v4l2 reports success while clamping or
  ignoring the value, so every control is read back and compared.

Honest limitation: **PhotonVision imposes its own pipeline settings when it opens
the camera.** These values govern the window before PhotonVision starts and any
tool that uses the camera directly. Where PhotonVision owns the camera, set
exposure in its UI - this script cannot win that argument and does not try.

Exit codes:  0 all applied   1 something did not take   2 no device
"""

import argparse
import re
import sys
import time

from common import load_config, device_from, sh


def wait_for_device(dev, timeout):
    """Boot races the USB enumeration; wait rather than fail."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sh(["v4l2-ctl", "-d", dev, "--info"]):
            return True
        time.sleep(0.5)
    return False


def read_controls(dev):
    out = sh(["v4l2-ctl", "-d", dev, "--list-ctrls"]) or ""
    got = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\(\w+\)\s*:\s*(.*)$", line)
        if not m:
            continue
        v = re.search(r"value=(\S+)", m.group(2))
        got[m.group(1)] = v.group(1) if v else None
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config")
    ap.add_argument("-d", "--device")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="seconds to wait for the device to appear")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg, err = load_config(args.config)
    if err:
        print("config: %s" % err)
        return 2
    dev = device_from(cfg, args.device)

    if not wait_for_device(dev, args.wait):
        print("no camera at %s after %.0fs" % (dev, args.wait))
        return 2

    wanted = []
    for item in cfg.get("camera", {}).get("controls", []) or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            wanted.append((str(item[0]), item[1]))
        elif isinstance(item, dict):
            wanted.extend((str(k), v) for k, v in item.items())

    if not wanted:
        print("no camera.controls in config - nothing to apply")
        return 0

    # One call per control, deliberately: batched with commas, a single
    # rejected control aborts the rest and you cannot tell which.
    for name, value in wanted:
        sh(["v4l2-ctl", "-d", dev, "-c", "%s=%s" % (name, value)])

    got = read_controls(dev)
    bad = []
    for name, value in wanted:
        actual = got.get(name)
        ok = actual is not None and str(actual) == str(value)
        if not ok:
            bad.append((name, value, actual))
        if not args.quiet:
            print("  %-28s want %-6s got %-6s  %s"
                  % (name, value, actual, "ok" if ok else "MISMATCH"))

    if bad:
        print("%d control(s) did not take on %s" % (len(bad), dev))
        for name, want, actual in bad:
            if actual is None:
                print("  %s: control not present on this camera" % name)
            else:
                print("  %s: wanted %s, device reports %s - clamped, or an auto "
                      "mode still owns it" % (name, want, actual))
        return 1

    if not args.quiet:
        print("all %d control(s) applied to %s" % (len(wanted), dev))
    return 0


if __name__ == "__main__":
    sys.exit(main())

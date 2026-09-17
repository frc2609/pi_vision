#!/usr/bin/env python3
"""camera_load - what each added camera costs the Pi, measured one at a time.

A one-off. Start it, then plug cameras in one at a time. It notices each one,
waits for PhotonVision to start publishing for it, measures, and waits for the
next. After the last camera (or ctrl-C) it prints one column per camera count,
so the cost of each camera reads straight across.

    cd ~/vision/camera
    .venv/bin/python scripts/camera_load.py
    .venv/bin/python scripts/camera_load.py --seconds 180    # let heat settle

Start with every camera unplugged for an idle baseline, or with one in.
Configure each new camera in PhotonVision the same as the others (1280x800
MJPEG, AprilTag pipeline) or the columns compare different workloads. Each
camera's format is reported so a mismatch shows.

Three things it cannot measure directly, so it says so instead:

- **An exposure-bound camera barely loads the CPU.** Framerate here is set by
  exposure, and exposure by the light. Each camera's framerate is compared with
  the ceiling its exposure allows - three cameras at 12 fps is not three
  cameras at 40, and only the second is the question.
- **Power is the Pi's own rails.** The PMIC has no current channel on the 5 V
  input, so a camera's own USB draw is invisible. What moves is the processing
  it causes. Same method as the health monitor.
- **Temperature lags.** A minute is not steady state, so the slope is reported
  to show whether it was still climbing.
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
from vision import camera, nt, util                                     # noqa: E402

DEVICES = "/dev/v4l/by-path/*usb*-video-index0"    # index0 is capture, index1 metadata
PORT = re.compile(r"xhci-hcd\.(\d+)-usb-0:([\d.]+):")
PMIC = re.compile(r"\s*(\S+?)_(A|V)\s+(?:current|volt)\(\d+\)=([0-9.]+)[AV]")
# A camera that cannot get USB bandwidth fails in the kernel log and nowhere
# else - uvcvideo reports "No space left on device" (-28) and PhotonVision just
# never gets a frame.
KERNEL = re.compile(r"(?i)(uvcvideo|xhci|usb).*(error|fail|over-current|"
                    r"no space|-28|unable|disconnect|reset)")
THERMAL = "/sys/class/thermal/thermal_zone0/temp"
CLOCK = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
CLOCK_MAX = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"
CLK_TCK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 4
EXPOSURE_BOUND = 0.85       # framerate within 15% of what exposure allows


# --- sampling -------------------------------------------------------------

def devices():
    """Connected USB capture devices, {by-path link: /dev/videoN}."""
    return {link: os.path.realpath(link) for link in sorted(glob.glob(DEVICES))}


def port(link):
    m = PORT.search(link)
    return m.groups() if m else (None, None)


def read_num(path, scale=1.0):
    try:
        with open(path) as f:
            return float(f.read().strip()) / scale
    except (OSError, ValueError):
        return None


def cpu_times():
    """{cpu line: (total ticks, idle ticks)} from /proc/stat."""
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            name, *vals = line.split()
            vals = [int(v) for v in vals[:8]]
            out[name] = (sum(vals), vals[3] + vals[4])          # idle + iowait
    return out


def busy(a, b):
    out = {}
    for k in b:
        if k in a:
            dt, di = b[k][0] - a[k][0], b[k][1] - a[k][1]
            out[k] = 100.0 * (dt - di) / dt if dt > 0 else 0.0
    return out


def service_pid(name):
    out = util.sh(["systemctl", "show", "-p", "MainPID", "--value", name])
    try:
        pid = int((out or "").strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def proc_ticks(pid):
    """utime + stime of a whole process, every thread included."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            rest = f.read().rsplit(")", 1)[1].split()   # the name may hold spaces
        return int(rest[11]) + int(rest[12])
    except (OSError, IndexError, ValueError, TypeError):
        return None


def pmic_watts():
    rails = {}
    for line in (util.sh(["vcgencmd", "pmic_read_adc"]) or "").splitlines():
        m = PMIC.match(line)
        if m:
            rails.setdefault(m.group(1), {})[m.group(2)] = float(m.group(3))
    w = [r["A"] * r["V"] for r in rails.values() if "A" in r and "V" in r]
    return sum(w) if w else None


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        pass
    return None


def kernel_since(epoch):
    """USB/UVC trouble logged since `epoch`, or None if the log is unreadable.

    None rather than [] matters: an unreadable log must not report as clean.
    """
    try:
        p = subprocess.run(["journalctl", "-k", "-o", "cat", "--no-pager",
                            "--since", "@%d" % int(epoch)],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or "insufficient permissions" in p.stderr \
            or "not seeing messages" in p.stderr:
        return None
    return [line for line in p.stdout.splitlines() if KERNEL.search(line)]


def slope_per_min(points):
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 3:
        return None
    mt = sum(t for t, _ in pts) / len(pts)
    mv = sum(v for _, v in pts) / len(pts)
    den = sum((t - mt) ** 2 for t, _ in pts)
    return 60.0 * sum((t - mt) * (v - mv) for t, v in pts) / den if den else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# --- PhotonVision ---------------------------------------------------------

class PV:
    """Per-camera fps and heartbeat, read straight from NetworkTables.

    Alive means the heartbeat *changed*. PhotonVision's topics outlive an
    unplugged camera, so a value being present proves nothing.
    """

    def __init__(self, inst, table):
        self.inst, self.table, self.entries = inst, table, {}

    def names(self):
        return nt.cameras(nt.discover(self.inst, self.table), "/" + self.table)

    def read(self):
        out = {}
        for cam in self.names():
            if cam not in self.entries:
                base = "/%s/%s/" % (self.table, cam)
                self.entries[cam] = (self.inst.getEntry(base + "fps"),
                                     self.inst.getEntry(base + "heartbeat"))
            fps, beat = self.entries[cam]
            out[cam] = (nt.read(fps), nt.read(beat))
        return out

    def alive(self, seconds):
        first, seen = self.read(), {}
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(0.25)
            for cam, (fps, beat) in self.read().items():
                if beat is not None and beat != first.get(cam, (None, None))[1]:
                    seen[cam] = True
                elif beat is None and fps:          # no heartbeat topic: fall back
                    seen[cam] = True
        return set(seen)


def wait_for_pv(pv, want, settle):
    """Until PhotonVision is publishing for `want` cameras, or `settle` runs out."""
    end = time.time() + settle
    while time.time() < end:
        live = pv.alive(2.0)
        print("\r  waiting for PhotonVision: %d/%d publishing " % (len(live), want),
              end="", flush=True)
        if len(live) >= want:
            print()
            return
    print("\n  PhotonVision is publishing for %d of %d connected cameras."
          % (len(pv.alive(2.0)), want))
    print("  If the new one needs enabling, do it in the UI now - same settings as")
    input("  the others (1280x800 MJPEG, AprilTag). Enter to measure anyway: ")


# --- one stage ------------------------------------------------------------

def measure(pv, seconds, pid, names_by_path):
    devs = devices()
    started = time.time()
    raw0, _ = camera.throttled()
    s = {"total": [], "core": [], "pv": [], "temp": [], "clock": [],
         "watts": [], "mem": [], "now_bits": 0}
    fps, beats = {}, {}

    prev_c, prev_p, prev_t = cpu_times(), proc_ticks(pid) if pid else None, started
    end = started + seconds
    while time.time() < end:
        time.sleep(1.0)
        now, c = time.time(), cpu_times()
        p = proc_ticks(pid) if pid else None
        b = busy(prev_c, c)
        s["total"].append(b.get("cpu"))
        s["core"].append(max((v for k, v in b.items() if k != "cpu"), default=None))
        if p is not None and prev_p is not None:
            s["pv"].append((p - prev_p) / CLK_TCK / (now - prev_t))
        prev_c, prev_p, prev_t = c, p, now

        s["temp"].append((now, read_num(THERMAL, 1000.0)))
        s["clock"].append(read_num(CLOCK, 1000.0))
        s["watts"].append(pmic_watts())
        s["mem"].append(mem_available_mb())
        raw, _ = camera.throttled()
        if raw is not None:
            s["now_bits"] |= raw & 0xF              # bits 0-3 are "right now"
        for cam, (f, beat) in pv.read().items():
            if f is not None:
                fps.setdefault(cam, []).append(float(f))
            beats.setdefault(cam, set()).add(beat)
        left = int(end - time.time())
        print("\r  measuring... %3ds left " % max(left, 0), end="", flush=True)
    print()

    alive = {c for c, b in beats.items() if len(b - {None}) > 1}
    # No heartbeat topic at all: fall back to a non-zero framerate.
    alive |= {c for c in fps if beats.get(c, {None}) == {None} and mean(fps[c])}
    raw1, text1 = camera.throttled()

    # Which PhotonVision camera is which device. Configured cameras map by
    # port; one unconfigured device and one unconfigured PhotonVision camera
    # can only be each other.
    rows = []
    for link, dev in devs.items():
        ctl, prt = port(link)
        fmt = camera.current_format(dev)
        ticks = camera.read_exposure(dev)
        rows.append({"link": link, "dev": dev, "controller": ctl, "port": prt,
                     "format": "%dx%d %s" % fmt if fmt else "?",
                     "exposure": ticks,
                     "ceiling": camera.max_fps_for_exposure(ticks) if ticks else None,
                     "name": names_by_path.get(link)})
    spare_dev = [r for r in rows if not r["name"]]
    spare_pv = sorted(alive - {r["name"] for r in rows})
    if len(spare_dev) == 1 and len(spare_pv) == 1:
        spare_dev[0]["name"] = spare_pv[0]
        spare_dev[0]["paired"] = True

    temps = [v for _, v in s["temp"] if v is not None]
    return {
        "cameras": len(devs), "alive": sorted(alive),
        "fps": {c: mean(v) for c, v in fps.items() if c in alive},
        "fps_min": {c: min(v) for c, v in fps.items() if c in alive and v},
        "cpu": mean(s["total"]), "core": mean(s["core"]), "pv_cores": mean(s["pv"]),
        "temp_end": temps[-1] if temps else None,
        "temp_max": max(temps) if temps else None,
        "slope": slope_per_min(s["temp"]),
        "clock_min": min((c for c in s["clock"] if c), default=None),
        "watts": mean(s["watts"]), "mem": min((m for m in s["mem"] if m), default=None),
        "throttled": raw1, "throttled_text": text1, "throttled_start": raw0,
        "now_bits": s["now_bits"],
        "kernel": kernel_since(started - 30),       # catches the plug-in itself
        "devices": rows,
    }


# --- report ---------------------------------------------------------------

def fmt(v, spec="%.0f", none="-"):
    return none if v is None else spec % v


def report(stages, min_fps):
    if not stages:
        print("nothing measured")
        return
    clock_max = read_num(CLOCK_MAX, 1000.0)
    heads = ["%d cam%s" % (s["cameras"], "" if s["cameras"] == 1 else "s")
             for s in stages]
    cams = []
    for s in stages:
        cams += [c for c in s["fps"] if c not in cams]

    lines = [("cameras publishing", ["%d/%d" % (len(s["alive"]), s["cameras"])
                                     for s in stages])]
    for c in cams:
        lines.append(("fps  " + c, [fmt(s["fps"].get(c), "%.1f") for s in stages]))
    lines += [
        ("CPU total %", [fmt(s["cpu"]) for s in stages]),
        ("busiest core %", [fmt(s["core"]) for s in stages]),
        ("PhotonVision cores", [fmt(s["pv_cores"], "%.2f") + "/%d" % NCPU
                                if s["pv_cores"] is not None else "-" for s in stages]),
        ("temp C, end", [fmt(s["temp_end"], "%.1f") for s in stages]),
        ("temp C/min", [fmt(s["slope"], "%+.1f") for s in stages]),
        ("ARM clock min MHz", [fmt(s["clock_min"]) for s in stages]),
        ("power W, Pi rails", [fmt(s["watts"], "%.2f") for s in stages]),
        ("mem available MB", [fmt(s["mem"]) for s in stages]),
        ("throttled", ["0x%x" % s["throttled"] if s["throttled"] is not None else "-"
                       for s in stages]),
        ("kernel USB errors", ["?" if s["kernel"] is None else str(len(s["kernel"]))
                               for s in stages]),
    ]
    w = max(len(k) for k, _ in lines) + 2
    print("\n" + " " * w + "".join("%12s" % h for h in heads))
    for k, vals in lines:
        print(k.ljust(w) + "".join("%12s" % v for v in vals))

    last = stages[-1]
    print("\ncameras at %d:" % last["cameras"])
    for r in last["devices"]:
        print("  %-12s %-11s controller %s port %s  %-14s exposure %s -> max %s fps"
              % (r["name"] or "?", r["dev"], r["controller"], r["port"], r["format"],
                 fmt(r["exposure"]), fmt(r["ceiling"], "%.0f"))
              + ("   (paired by elimination)" if r.get("paired") else ""))

    print("\nread this before believing the table:")
    notes = []
    for s in stages:
        tag = "%d cam: " % s["cameras"]
        if len(s["alive"]) < s["cameras"]:
            notes.append(tag + "%d connected, only %d publishing - check the kernel "
                         "lines below and the PhotonVision UI"
                         % (s["cameras"], len(s["alive"])))
        for c, f in s["fps"].items():
            if min_fps and f is not None and f < min_fps:
                notes.append(tag + "%s at %.1f fps, under the %s minimum" % (c, f, min_fps))
        if s["now_bits"]:
            notes.append(tag + "THROTTLING happened during the measurement (0x%x)"
                         % s["now_bits"])
        if s["temp_max"] is not None and s["temp_max"] >= 80:
            notes.append(tag + "hit %.1f C - the soft limit is 80, hard throttle 85"
                         % s["temp_max"])
        if s["slope"] is not None and s["slope"] > 1.0:
            notes.append(tag + "temperature still rising at %+.1f C/min - steady "
                         "state is higher than shown. --seconds 300 to see it"
                         % s["slope"])
        if clock_max and s["clock_min"] and s["clock_min"] < 0.95 * clock_max:
            notes.append(tag + "ARM clock dropped to %.0f of %.0f MHz"
                         % (s["clock_min"], clock_max))
        if s["kernel"]:
            notes.append(tag + "kernel:\n      " + "\n      ".join(s["kernel"][-8:]))
    if any(s["kernel"] is None for s in stages):
        notes.append("kernel log unreadable - USB errors could not be checked. "
                     "sudo usermod -aG adm $USER, then log in again")

    bound = [r["name"] or r["dev"] for r in last["devices"]
             if r["ceiling"] and last["fps"].get(r["name"] or "")
             and last["fps"][r["name"]] >= EXPOSURE_BOUND * r["ceiling"]]
    if bound:
        notes.append("EXPOSURE-BOUND: %s at the framerate exposure allows, "
                     "so the CPU had headroom it never used. This is the light's "
                     "load, not the worst case - shorten exposure or add light and "
                     "rerun to find where the CPU actually runs out"
                     % ", ".join(bound))
    fmts = {r["format"] for r in last["devices"]}
    if len(fmts) > 1:
        notes.append("cameras are not all the same format (%s) - the columns compare "
                     "different workloads" % ", ".join(sorted(fmts)))
    shared = {}
    for r in last["devices"]:
        shared.setdefault(r["controller"], []).append(r["name"] or r["dev"])
    for ctl, names in shared.items():
        if len(names) > 1:
            notes.append("%s share USB controller %s - first suspect if one fails to "
                         "start" % (" and ".join(names), ctl))
    notes.append("power is the Pi's own rails; the cameras' USB draw is not in it")
    for n in notes:
        print("  - " + n)


def append_csv(path, s):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "cameras", "publishing", "fps", "cpu_pct",
                        "busiest_core_pct", "pv_cores", "temp_end_c", "temp_c_per_min",
                        "clock_min_mhz", "pi_rails_w", "mem_available_mb",
                        "throttled", "kernel_usb_errors"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), s["cameras"], len(s["alive"]),
                    ";".join("%s=%.1f" % (c, f) for c, f in s["fps"].items()
                             if f is not None),
                    fmt(s["cpu"], "%.1f", ""), fmt(s["core"], "%.1f", ""),
                    fmt(s["pv_cores"], "%.2f", ""), fmt(s["temp_end"], "%.1f", ""),
                    fmt(s["slope"], "%.2f", ""), fmt(s["clock_min"], "%.0f", ""),
                    fmt(s["watts"], "%.2f", ""), fmt(s["mem"], "%.0f", ""),
                    "" if s["throttled"] is None else "0x%x" % s["throttled"],
                    "" if s["kernel"] is None else len(s["kernel"])])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--seconds", type=float, default=60.0, help="measurement per stage")
    ap.add_argument("--warmup", type=float, default=10.0,
                    help="seconds after PhotonVision picks a camera up, before measuring")
    ap.add_argument("--settle", type=float, default=45.0,
                    help="how long to wait for PhotonVision to pick up a new camera")
    ap.add_argument("--max", type=int, default=3, help="stop after this many cameras")
    ap.add_argument("--csv", default="camera_load.csv")
    args = ap.parse_args()

    cfg, err = util.load_config()
    if err:
        print(err)
        return 2
    pvcfg = cfg.get("photonvision", {})
    inst, err = nt.connect(cfg.get("networktables"), 5.0)
    if inst is None or err:
        print("networktables: %s" % (err or "no instance"))
        return 2
    pv = PV(inst, pvcfg.get("nt_table", "photonvision"))
    pid = service_pid(pvcfg.get("service", "photonvision"))
    if not pid:
        print("note: no PhotonVision process found - its CPU column will be blank")
    names_by_path = {c["by_path"]: c["name"] for c in cfg.get("cameras", [])
                     if c.get("by_path")}
    min_fps = cfg.get("preflight", {}).get("min_fps")

    stages = []
    try:
        while True:
            n = len(devices())
            print("\n=== %d camera%s connected ===" % (n, "" if n == 1 else "s"))
            if n:
                wait_for_pv(pv, n, args.settle)
            if args.warmup:
                print("  warming up %.0fs" % args.warmup)
                time.sleep(args.warmup)
            st = measure(pv, args.seconds, pid, names_by_path)
            stages.append(st)
            append_csv(args.csv, st)
            print("  %s | CPU %s%% | PV %s cores | %s C | %s W"
                  % (", ".join("%s %.1f fps" % (c, f) for c, f in st["fps"].items()
                               if f is not None) or "no cameras",
                     fmt(st["cpu"]), fmt(st["pv_cores"], "%.2f"),
                     fmt(st["temp_end"], "%.1f"), fmt(st["watts"], "%.2f")))
            if n >= args.max:
                break
            print("\nPlug in the next camera (ctrl-C to finish)...")
            while len(devices()) <= n:
                if len(devices()) < n:
                    print("  a camera went away - plug it back in, or ctrl-C")
                    time.sleep(3)
                time.sleep(0.5)
            print("  detected - giving the kernel a moment")
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n(stopped)")

    report(stages, min_fps)
    print("\nrows appended to %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""vd - vision diagnostics. One command per question.

    vd doctor        is the camera delivering what we asked for?
    vd nt            is PhotonVision publishing anything useful?
    vd preflight     is the whole chain ready, yes or no?
    vd snapshot      capture the configuration; --diff says what changed
    vd tune          measure a configuration, or sweep several
    vd stream        annotated live view, when the camera is free
    vd field         generate a two-tag corner field layout

Exit codes are meaningful everywhere: 0 good, 1 degraded, 2 cannot tell.
`vd preflight && echo ready` works.

See docs/RUNNING.md for how to read the output - several of these are easy to
misread in ways that send you looking in the wrong place.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vision import camera as cam                                    # noqa: E402
from vision import checks, field, nt, state, tune                   # noqa: E402
from vision.util import load_config, device_from, repo_root         # noqa: E402

PV_UI_PORT = 5800
PV_STREAM_PORTS = (1181, 1182)


def row(label, value):
    print("  %-22s %s" % (label, "--" if value in (None, "") else value))


# --- doctor ---------------------------------------------------------------

def cmd_doctor(cfg, args):
    dev = device_from(cfg, args.device)
    if not os.path.exists(dev):
        nodes, _ = cam.list_devices()
        if args.json:
            print(json.dumps({"device": dev, "error": "no such device",
                              "nodes": nodes, "status": 2}, indent=2))
        else:
            print("no such device: %s" % dev)
            print("available: %s" % (" ".join(nodes) or "none - is it plugged in?"))
        return 2

    f = state.facts(cfg, dev, probe=not args.no_probe, family=args.family)
    if args.json:
        print(json.dumps(f, indent=2, default=str))
        return f.get("status", 0)

    print("\n=== devices ===")
    row("video nodes", " ".join(f["nodes"]))
    row("stable path", f["stable_path"])
    if not f["stable_path"]:
        print("  NOTE: no by-id entry - device numbering can move on replug")
    row("usb bus", f["usb_bus"])

    print("\n=== CAN (hardware advertises) ===")
    for fmt, d in sorted((f["can"] or {}).items()):
        b = d["best"]
        row(fmt, "%dx%d @ %.0f fps  (%d modes)" % (b[0], b[1], b[2], d["modes"])
            if b else "no modes")

    print("\n=== ASKED (current negotiated format) ===")
    a = f["asked"]
    row("format", "%dx%d %s" % (a["width"], a["height"], a["fourcc"]) if a else None)
    if a and f["can"]:
        b = (f["can"].get(a["fourcc"]) or {}).get("best")
        if b and (a["width"], a["height"]) != (b[0], b[1]):
            print("  WARNING: running %dx%d but %s supports %dx%d"
                  % (a["width"], a["height"], a["fourcc"], b[0], b[1]))
    for name in ("auto_exposure", "exposure_time_absolute", "brightness", "gain",
                 "power_line_frequency", "focus_automatic_continuous"):
        c = f["controls"].get(name)
        if c:
            row(name, "%s%s" % (c["value"], "  [%s]" % c["flags"] if c["flags"] else ""))
    exp = f["controls"].get("exposure_time_absolute", {}).get("value")
    if exp:
        try:
            row("-> max fps", "%.1f  (10000 / %s)"
                % (cam.max_fps_for_exposure(float(exp)), exp))
        except ValueError:
            pass
    if "gain" not in f["controls"]:
        print("  NOTE: no gain control exposed - exposure is the only real lever")

    print("\n=== WHO HAS IT ===")
    h = f["holder"]
    row("holder", "cannot tell - rerun with sudo" if h == "denied"
        else ("pid %d (%s)" % (h["pid"], h["name"]) if h else "nobody - device is free"))

    print("\n=== CONSUMERS ===")
    for name, st in sorted(f["services"].items()):
        row(name, st)
    row("power", "%s (%s)" % (f["throttled"]["raw"], f["throttled"]["text"])
        if f["throttled"] else None)

    print("\n=== DELIVERED (measured) ===")
    d = f["delivered"]
    if d is None:
        print("  skipped - %s" % ("another process holds the device; only one can capture"
                                  if h else "not probed"))
    elif "error" in d:
        row("probe", d["error"])
    else:
        row("measured fps", "%.1f  (%d frames)" % (d["fps"], d["frames"]))
        row("delivered", "%dx%d %s" % (d["size"][0], d["size"][1], d["fourcc"]))
        row("mean brightness", "%.1f  (0=black, 255=white)" % d["mean"]
            if d["mean"] is not None else None)
        row("tags visible", d["tags"] if d["tags"] else
            ("none" if d["tags"] == [] else None))

    print("\n=== WHERE TO LOOK ===")
    ip = f["ip"]
    mdns = (f["hostname"] or "") + ("" if (f["hostname"] or "").endswith(".local") else ".local")
    row("this host", "%s   %s" % (mdns, " ".join(f["ips"]) or "(no address)"))
    pv = cfg.get("photonvision", {})
    if f["services"].get(pv.get("service", "photonvision")) == "active":
        print("  PhotonVision UI      http://%s:%d" % (ip or mdns, pv.get("ui_port", PV_UI_PORT)))
        for label, p in zip(("raw camera stream", "processed stream"),
                            pv.get("stream_ports", PV_STREAM_PORTS)):
            print("  %-20s http://%s:%d/stream.mjpg" % (label, ip or mdns, p))
        if ip:
            print("    use the IP, not %s - Chrome blocks .local sub-resources" % mdns)
            print("    from a plain-HTTP page, so the embedded stream stays blank")
        print("  its log              journalctl -u photonvision -f")
    print("  health monitor log   journalctl -u health_monitor -f")
    print("  live camera format   v4l2-ctl -d %s --all | grep -A4 'Format Video'" % dev)
    print("  this tool, streaming vd stream   -> http://%s:8090\n" % (ip or mdns))
    return f.get("status", 0)


# --- nt -------------------------------------------------------------------

def cmd_nt(cfg, args):
    pv = cfg.get("photonvision", {})
    table = pv.get("nt_table", "photonvision")
    cam_name = pv.get("camera_name")
    inst, err = nt.connect(cfg.get("networktables"),
                           cfg.get("preflight", {}).get("nt_timeout_s", 5.0))
    if inst is None:
        print("networktables: %s" % err)
        return 2
    cams, topics = nt.watch(inst, table, args.seconds)
    live = {p: d for p, d in topics.items() if d["rate"] > 0}

    if args.json:
        print(json.dumps({"error": err, "cameras": cams, "topics": topics},
                         indent=2, default=str))
        return 0 if live else (2 if err else 1)

    print("\n=== networktables ===")
    row("mode", cfg.get("networktables", {}).get("mode"))
    row("connection", err or "connected")
    row("table", "/" + table)
    row("cameras publishing", ", ".join(cams) or "none")

    def owned(p):
        return cam_name and p.startswith(cam_name + "/")

    if cam_name and cams and cam_name not in cams:
        print("  WARNING: config expects '%s' but PhotonVision publishes %s"
              % (cam_name, cams))
        print("           the UI nickname is the NT table name")
    stale = sorted({c for c in cams if c != cam_name} &
                   {p.split("/", 1)[0] for p, d in topics.items()
                    if "/" in p and d["rate"] == 0})
    if cam_name in cams and stale:
        print("  NOTE: %s present but not updating - a previous nickname."
              % ", ".join(stale))
        print("        It clears on a PhotonVision restart. Ignored below.")

    print("\n=== topics (%.1fs sample) ===" % args.seconds)
    if not topics:
        print("  none announced under /%s" % table)
        print("  in order of likelihood:")
        print("   1. PhotonVision's 'Run NetworkTables Server' is off, and with no")
        print("      roboRIO nothing else is the server")
        print("   2. PhotonVision points at a different server")
        print("   3. the pipeline is in driver mode, or stopped")
        print("  what is listening:  ss -tln | grep -E '5810|1735'")
    for p in sorted(topics, key=lambda p: -topics[p]["rate"]):
        d = topics[p]
        if not args.all and p.rsplit("/", 1)[-1] not in nt.INTERESTING and d["rate"] == 0:
            continue
        v = d["value"]
        print("  %-34s %6.1f Hz   %s" % (p, d["rate"],
                                         v[:37] + "..." if v and len(v) > 40 else v))

    print("\n=== verdict ===")
    if not live:
        print("  NOTHING IS UPDATING.  journalctl -u photonvision -f")
        return 1
    fastest = max(live.items(), key=lambda kv: kv[1]["rate"])
    print("  publishing - fastest %s at %.1f Hz" % (fastest[0], fastest[1]["rate"]))
    if fastest[1]["rate"] >= nt.NT_SEND_HZ * 0.9:
        print("  NOTE: ~%.0f Hz is NetworkTables' default send period, not the" % nt.NT_SEND_HZ)
        print("        pipeline rate. Use the fps topic below.")
    for p, d in sorted(topics.items()):
        if p.rsplit("/", 1)[-1] == "fps" and d["value"] is not None:
            if not (cam_name in cams) or owned(p):
                print("  pipeline fps   %-28s %s  (PhotonVision's own count)" % (p, d["value"]))
    lat = [d["value"] for p, d in topics.items()
           if p.endswith("/latencyMillis") and (not (cam_name in cams) or owned(p))]
    if lat:
        print("  latency        %s ms" % lat[0])
    ht = [(p, d) for p, d in topics.items() if p.endswith("/hasTarget")]
    if ht:
        p, d = ht[0]
        print("  hasTarget = %s" % d["value"])
        if d["rate"] > 0.3:
            # A rate on a boolean means it is flipping, not updating.
            print("  WARNING: hasTarget toggled %.1f/s - detection is INTERMITTENT," % d["rate"])
            print("           not solid. Usually light, blur, or too small in frame.")
    print()
    return 0


# --- preflight ------------------------------------------------------------

def publish_preflight(cfg, dev, interval):
    """Publish the preflight verdict to NetworkTables for a dashboard.

    Booleans and numbers only, per the operating standards - a formatted string
    does not graph and cannot be consumed without parsing.

    Runs as a long-lived publisher on purpose. NT4 unpublishes a client's topics
    when it disconnects, so **if this process dies the lights disappear from the
    dashboard** rather than freezing on their last good values. A stale green
    light is the failure mode worth designing out.
    """
    import time as _t
    from vision import nt as _nt

    inst, err = nt.connect(cfg.get("networktables"),
                           cfg.get("preflight", {}).get("nt_timeout_s", 5.0))
    if inst is None or err:
        print("networktables: %s" % (err or "no instance"))
        return 2

    table = inst.getTable("Vision/preflight")
    seq = 0
    print("publishing to /Vision/preflight every %.0fs (ctrl-C to stop)" % interval)
    try:
        while True:
            f = state.facts(cfg, dev, probe=False)
            rows = checks.run_all(cfg, f)
            fails = sum(1 for s, _, _ in rows if s == checks.FAIL)
            skips = sum(1 for s, _, _ in rows if s == checks.SKIP)
            for status, name, _ in rows:
                # SKIP is never a failure, so it shows green. `skipped` says how
                # many checks could not run.
                table.getEntry("%s_ok" % name).setBoolean(status != checks.FAIL)
            table.getEntry("ready").setBoolean(fails == 0)
            table.getEntry("failed").setDouble(float(fails))
            table.getEntry("skipped").setDouble(float(skips))
            seq += 1
            # A flat `seq` on a graph means this publisher is wedged but still
            # connected - the one failure the vanishing-topics trick misses.
            table.getEntry("seq").setDouble(float(seq))
            table.getEntry("run_epoch_s").setDouble(_t.time())
            inst.flush()
            if interval <= 0:
                _t.sleep(1.0)          # let the values reach the server
                return 0 if fails == 0 else fails
            _t.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_preflight(cfg, args):
    dev = device_from(cfg, args.device)
    if args.publish:
        return publish_preflight(cfg, dev, args.interval)
    f = state.facts(cfg, dev, probe=False)
    rows = checks.run_all(cfg, f)
    print()
    for status, name, detail in rows:
        print("  %-4s  %-22s %s" % (status, name, detail))
    fails = sum(1 for s, _, _ in rows if s == checks.FAIL)
    skips = sum(1 for s, _, _ in rows if s == checks.SKIP)
    print()
    print("  NOT READY - %d check(s) failed%s" % (fails, ", %d skipped" % skips if skips else "")
          if fails else "  READY%s" % (" (%d skipped)" % skips if skips else ""))
    print()
    return fails


# --- snapshot -------------------------------------------------------------

def cmd_snapshot(cfg, args):
    if args.list:
        import glob
        for f in sorted(glob.glob(os.path.join(state.snapshot_dir(), "*.json"))):
            print("  %s" % f)
        return 0

    if args.diff is not None:
        files = args.diff if len(args.diff) == 2 else state.newest_snapshots(2)
        if len(files) != 2:
            print("need two snapshots to diff; have %d" % len(files))
            return 2
        a, b = (json.load(open(p)) for p in files)
        changes = state.diff(a, b)
        print("\n--- %s\n+++ %s\n" % (os.path.basename(files[0]),
                                      os.path.basename(files[1])))
        for k, av, bv in changes:
            print("  %s\n    - %s\n    + %s" % (k, av, bv))
        if not changes:
            print("  identical (ignoring the timestamp)")
        print("\n  %d field(s) changed\n" % len(changes))
        return 1 if changes else 0

    import time as _t
    snap = state.snapshot(cfg, device_from(cfg, args.device))
    if args.print:
        print(json.dumps(snap, indent=2, sort_keys=True))
        return 0
    out = os.path.join(state.snapshot_dir(), _t.strftime("%Y%m%d-%H%M%S") + ".json")
    with open(out, "w") as fh:
        json.dump(snap, fh, indent=2, sort_keys=True)
    print("wrote %s  (%d fields)" % (out, len(snap)))
    print("commit it - a snapshot from the last event beats remembering")
    return 0


# --- tune -----------------------------------------------------------------

def cmd_tune(cfg, args):
    pv = cfg.get("photonvision", {})
    name = args.camera or pv.get("camera_name")
    dev = device_from(cfg, args.device)
    inst, err = nt.connect(cfg.get("networktables"),
                           cfg.get("preflight", {}).get("nt_timeout_s", 5.0))
    if inst is None or err:
        print("networktables: %s" % (err or "no instance"))
        return 2
    entries, base = nt.camera_entries(inst, pv.get("nt_table", "photonvision"),
                                      name, tune.KEYS)
    import time as _t
    _t.sleep(0.5)
    if nt.read(entries["fps"]) is None and nt.read(entries["hasTarget"]) is None:
        print("nothing at %s - is the camera nickname right?  try: vd nt" % base)
        return 2

    rows = []
    original_exposure = cam.read_exposure(dev)
    original_pipeline = nt.read(entries[nt.PIPELINE_STATE])
    try:
        if args.sweep_exposure:
            steps = [int(x) for x in args.sweep_exposure.split(",")]
            print("sweeping exposure %s - keep the tag and camera still\n" % steps)
            for ticks in steps:
                got, ok = cam.set_exposure(dev, ticks)
                if not ok:
                    print("  requested %d, device reports %s - skipping" % (ticks, got))
                    continue
                _t.sleep(tune.SETTLE_S)
                r = tune.measure(entries, args.seconds, dev, "%s exp=%d" % (args.label, ticks))
                rows.append(r)
                _show(r)
        elif args.sweep_pipeline:
            steps = [int(x) for x in args.sweep_pipeline.split(",")]
            print("sweeping pipelines %s - keep the tag and camera still\n" % steps)
            for idx in steps:
                nt.request_pipeline(entries, idx)
                _t.sleep(tune.SETTLE_S)
                got = nt.read(entries[nt.PIPELINE_STATE])
                if got is None or int(got) != idx:
                    print("  pipeline %d did not take (state reports %s) - skipping"
                          % (idx, got))
                    continue
                r = tune.measure(entries, args.seconds, dev,
                                 "%s pipeline=%d" % (args.label, idx))
                rows.append(r)
                _show(r)
        else:
            print("sampling %s for %.0fs - keep the tag and camera still" % (args.label, args.seconds))
            r = tune.measure(entries, args.seconds, dev, args.label)
            rows.append(r)
            print()
            for k, v in r.items():
                print("  %-16s %s" % (k, v))
            print()
            for n in tune.verdict(r):
                print("  ! " + n)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if args.sweep_exposure and original_exposure:
            cam.set_exposure(dev, int(original_exposure))
            print("\nrestored exposure to %.1f ms"
                  % (original_exposure / cam.EXPOSURE_TICKS_PER_MS))
        if args.sweep_pipeline and original_pipeline is not None:
            nt.request_pipeline(entries, int(original_pipeline))
            print("restored pipeline %d" % int(original_pipeline))

    if args.csv:
        for r in rows:
            tune.append_csv(args.csv, r)
        print("\nwrote %d row(s) to %s" % (len(rows), args.csv))
    if tune.identical(rows):
        print("\n  WARNING: every row measured the same configuration. Nothing")
        print("  changed between them, so these are repeat measurements, not a")
        print("  sweep. Useful for repeatability, useless as a comparison.")
    return 0


def _show(r):
    print("  " + "  ".join("%s=%s" % (k, r[k]) for k in
                           ("pipeline", "width", "height", "exposure_ms", "fps",
                            "latency_ms", "detect_pct", "yaw_sd")))
    for n in tune.verdict(r):
        print("    ! " + n)


# --- field ----------------------------------------------------------------

def cmd_field(cfg, args):
    def parse(parts, what):
        got = {}
        for p in parts:
            if "=" not in p:
                raise SystemExit("%s: expected key=value, got %r" % (what, p))
            k, v = p.split("=", 1)
            got[k.strip().lower()] = v.strip()
        for need in ("id", "dist", "height"):
            if need not in got:
                raise SystemExit("%s: missing %s=" % (what, need))
        return {"id": int(got["id"]), "dist": float(got["dist"]),
                "height": float(got["height"])}

    left, right = parse(args.left, "--left"), parse(args.right, "--right")
    layout = field.corner_layout(left, right, tuple(args.field))
    text = json.dumps(layout, indent=2)
    if args.out:
        open(args.out, "w").write(text + "\n")
        print("wrote %s" % args.out)
    else:
        print(text)
    d = field.implied_separation(left, right)
    print("\ncentre-to-centre implied by this layout: %.3f m (%.1f cm)" % (d, d * 100))
    print("tape-measure that before trusting it - layout error becomes pose")
    print("error directly, and the solver has no way to know.")
    print("\nimport: Settings -> Device Control -> Import Settings -> AprilTag Layout")
    return 0


# --- entry point ----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="vd", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("-c", "--config")
    ap.add_argument("-d", "--device", help="default: camera.by_id from config")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="camera diagnostics")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-probe", action="store_true", help="skip the measured column")
    p.add_argument("--family", default="DICT_APRILTAG_36h11")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("nt", help="NetworkTables diagnostics")
    p.add_argument("-s", "--seconds", type=float, default=3.0)
    p.add_argument("--all", action="store_true", help="list every topic")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_nt)

    p = sub.add_parser("preflight", help="whole-chain pass/fail")
    p.add_argument("--publish", action="store_true",
                   help="publish the verdict to /Vision/preflight for a dashboard")
    p.add_argument("--interval", type=float, default=30.0,
                   help="seconds between publishes; 0 publishes once and exits")
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("snapshot", help="capture or diff the configuration")
    p.add_argument("--diff", nargs="*", metavar="FILE")
    p.add_argument("--list", action="store_true")
    p.add_argument("--print", action="store_true")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("tune", help="measure or sweep a configuration")
    p.add_argument("--label", default="run")
    p.add_argument("-s", "--seconds", type=float, default=12.0)
    p.add_argument("--csv")
    p.add_argument("--camera")
    p.add_argument("--sweep-exposure", metavar="A,B,C")
    p.add_argument("--sweep-pipeline", metavar="A,B,C")
    p.set_defaults(fn=cmd_tune)

    p = sub.add_parser("stream", help="annotated live view")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--family", default="DICT_APRILTAG_36h11")
    p.set_defaults(fn=lambda cfg, a: __import__("vision.stream", fromlist=["serve"])
                   .serve(device_from(cfg, a.device), a.port, a.family))

    p = sub.add_parser("field", help="generate a corner field layout")
    p.add_argument("--left", nargs="+", metavar="K=V", required=True)
    p.add_argument("--right", nargs="+", metavar="K=V", required=True)
    p.add_argument("--field", type=float, nargs=2, default=[5.0, 5.0])
    p.add_argument("-o", "--out")
    # Pure geometry - it never touches the camera, so it must not need a config.
    p.set_defaults(fn=cmd_field, needs_config=False)

    args = ap.parse_args()
    cfg, err = load_config(args.config)
    if err:
        if getattr(args, "needs_config", True):
            print("config: %s" % err)
            return 2
        cfg = {}
    return args.fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

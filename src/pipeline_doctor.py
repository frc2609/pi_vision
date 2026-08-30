#!/usr/bin/env python3
"""NetworkTables-side diagnostics: is PhotonVision actually publishing anything useful?

This is the layer nothing else watches. The health monitor says the Pi is alive;
camera_doctor says the camera is delivering frames; neither can tell you whether
detections are reaching the network, at what rate, or with what latency. That is
where "vision is working" is actually decided.

Deliberately generic. It enumerates whatever PhotonVision publishes rather than
assuming a schema, because the NT layout has changed between seasons and the
published docs lag the release. Known topic names get interpreted; everything
else is still listed with its update rate, so a renamed topic shows up as a new
name rather than as silence.

Exit codes, so it can be used from preflight or a script:
    0  connected and publishing
    1  connected but nothing is updating
    2  could not connect

With no roboRIO, turn on "Run NetworkTables Server" in PhotonVision's settings
and leave config.yaml on client_address / 127.0.0.1.
"""

import argparse
import json
import sys
import time

from common import load_config, service_state


# Held at module scope on purpose. An NT4 client only receives topics it has
# subscribed to, and a MultiSubscriber stops working the moment it is garbage
# collected - so letting this go out of scope produces a client that connects
# happily and then sees nothing at all.
_SUBSCRIPTION = None


def connect(nt_cfg, timeout):
    """Bring up an NT client (or server) per config. Returns (inst, error)."""
    global _SUBSCRIPTION
    try:
        from ntcore import NetworkTableInstance
    except ImportError:
        return None, ("pyntcore not installed. It is pip-only - robotpy is not "
                      "packaged in Debian - so it needs a venv created with "
                      "--system-site-packages. See requirements.txt")

    inst = NetworkTableInstance.getDefault()
    mode = (nt_cfg or {}).get("mode", "client_address")
    identity = (nt_cfg or {}).get("identity", "camera_tools")

    if mode == "server":
        inst.startServer()
    else:
        inst.startClient4(identity)
        if mode == "client_team":
            team = (nt_cfg or {}).get("team")
            if team is None:
                return None, "networktables.mode is client_team but no team is set"
            inst.setServerTeam(int(team))
        else:
            addr = (nt_cfg or {}).get("address")
            if not addr:
                return None, "networktables.mode is client_address but no address is set"
            inst.setServer(addr)

    # Subscribe to everything before waiting. Without this the client connects
    # and stays empty, which looks exactly like "PhotonVision is not publishing".
    # MultiSubscriber is a class, not a method on the instance - and a failure
    # here is reported rather than swallowed, because a silently absent
    # subscription is indistinguishable from a silent server.
    try:
        from ntcore import MultiSubscriber
        _SUBSCRIPTION = MultiSubscriber(inst, [""])
    except Exception as e:
        return None, ("could not subscribe (%s: %s). Without a subscription an "
                      "NT4 client is never told which topics exist."
                      % (e.__class__.__name__, e))

    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        # A server is "connected" the moment it is up; a client has to find one.
        if mode == "server" or inst.isConnected():
            # Connecting is not the same as having received the topic list, and
            # announcements arrive asynchronously afterwards.
            time.sleep(0.5)
            return inst, None
        time.sleep(0.1)
    return inst, "no connection after %.1fs" % float(timeout)


def snapshot(entry):
    """A comparable representation of a topic's value, or None if unreadable."""
    try:
        v = entry.getValue()
        return str(v.value()) if hasattr(v, "value") else str(v)
    except Exception:
        return None


def discover(inst, table_name):
    """Every topic path the server has announced beneath the table.

    Enumerating topics directly rather than walking subtables: subtable
    traversal only reports what the client has already been told about, and the
    topic list is the authoritative version of that.
    """
    prefix = "/" + table_name.strip("/")
    names = []
    try:
        for t in inst.getTopics():
            try:
                n = t.getName()
            except Exception:
                continue
            if n.startswith(prefix + "/") or n == prefix:
                names.append(n)
    except Exception:
        pass
    if names:
        return sorted(names)

    # Fallback for an ntcore without getTopics(): walk the tables.
    root = inst.getTable(table_name)
    try:
        for cam in root.getSubTables():
            sub = root.getSubTable(cam)
            for k in sub.getKeys():
                names.append("%s/%s/%s" % (prefix, cam, k))
        for k in root.getKeys():
            names.append("%s/%s" % (prefix, k))
    except Exception:
        pass
    return sorted(names)


def watch(inst, table_name, seconds, poll=0.02):
    """Poll every topic under the table and count how often each one changes.

    Change-counting rather than timestamp arithmetic: it needs only the most
    basic NT API, which is the part least likely to move between seasons.
    """
    prefix = "/" + table_name.strip("/")
    names = discover(inst, table_name)

    entries, last, changes = {}, {}, {}
    cams = set()
    for full in names:
        rel = full[len(prefix) + 1:] if full.startswith(prefix + "/") else full
        if "/" in rel:
            head = rel.split("/", 1)[0]
            # ".timesync" is NT bookkeeping and an empty head comes from
            # PhotonVision's "//metrics/..." path - neither is a camera.
            if head and not head.startswith("."):
                cams.add(head)
        e = inst.getEntry(full)
        entries[rel] = e
        last[rel] = snapshot(e)
        changes[rel] = 0
    cams = sorted(cams)

    t0 = time.time()
    while time.time() - t0 < seconds:
        for path, e in entries.items():
            cur = snapshot(e)
            if cur is not None and cur != last[path]:
                changes[path] += 1
                last[path] = cur
        time.sleep(poll)
    dt = max(time.time() - t0, 1e-6)

    return cams, {p: {"rate": changes[p] / dt, "value": last[p]} for p in entries}


def photonlib_view(camera_name):
    """Richer per-target data, if photonlibpy happens to be installed.

    Optional on purpose. photonlibpy is pinned to a PhotonVision version and can
    lag the running release, so nothing here depends on it.
    """
    try:
        from photonlibpy.photonCamera import PhotonCamera
    except Exception:
        return None
    try:
        cam = PhotonCamera(camera_name)
        results = []
        deadline = time.time() + 2.0
        while time.time() < deadline and not results:
            try:
                results = list(cam.getAllUnreadResults())
            except AttributeError:            # older photonlibpy
                r = cam.getLatestResult()
                results = [r] if r is not None else []
            time.sleep(0.05)
        if not results:
            return {"targets": None, "note": "no results read"}
        r = results[-1]
        targets = list(r.getTargets())
        out = {"targets": len(targets)}
        try:
            out["latency_ms"] = r.getLatencyMillis()
        except Exception:
            pass
        if targets:
            t = targets[0]
            for name, fn in (("id", "getFiducialId"), ("yaw", "getYaw"),
                             ("pitch", "getPitch"), ("area", "getArea"),
                             ("ambiguity", "getPoseAmbiguity")):
                try:
                    out[name] = getattr(t, fn)()
                except Exception:
                    pass
        return out
    except Exception as e:
        return {"error": str(e)}


# PhotonVision 2026 publishes the whole pipeline result packed into `rawBytes`
# and `result_proto`; the flat per-target topics that older documentation
# describes (hasTarget, latencyMillis, targetYaw...) no longer exist. Both sets
# are listed so this keeps working across a season change in either direction -
# and anything unrecognised still appears with its rate, so a renamed topic
# shows up as a new name rather than as silence.
RESULT_TOPICS = ("rawBytes", "result_proto", "result")
INTERESTING = RESULT_TOPICS + (
    "heartbeat", "hasTarget", "latencyMillis", "targetYaw", "targetPitch",
    "targetArea", "targetSkew", "targetPixelsX", "targetPixelsY",
    "pipelineIndex", "driverMode", "version", "buildDate", "ledModeState")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config")
    ap.add_argument("-s", "--seconds", type=float, default=3.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="list every topic, not just the interesting ones")
    args = ap.parse_args()

    cfg, err = load_config(args.config)
    if err:
        print("config: %s" % err)
        return 2

    pv = cfg.get("photonvision", {})
    table = pv.get("nt_table", "photonvision")
    cam_name = pv.get("camera_name")
    timeout = cfg.get("preflight", {}).get("nt_timeout_s", 5.0)

    inst, cerr = connect(cfg.get("networktables"), timeout)
    if inst is None:
        print("networktables: %s" % cerr)
        return 2

    cams, topics = watch(inst, table, args.seconds)
    live = {p: d for p, d in topics.items() if d["rate"] > 0}
    pl = photonlib_view(cam_name) if cam_name else None

    if args.json:
        print(json.dumps({"connected": cerr is None, "error": cerr,
                          "cameras": cams, "topics": topics,
                          "photonlib": pl}, indent=2, default=str))
        return 0 if live else (2 if cerr else 1)

    print("\n=== networktables ===")
    print("  %-22s %s" % ("mode", cfg.get("networktables", {}).get("mode")))
    print("  %-22s %s" % ("connection", cerr or "connected"))
    print("  %-22s %s" % ("photonvision service", service_state(pv.get("service", "photonvision"))))
    print("  %-22s %s" % ("table", "/" + table))
    print("  %-22s %s" % ("cameras publishing", ", ".join(cams) if cams else "none"))
    if cam_name and cams and cam_name not in cams:
        print("  WARNING: config expects camera '%s' but PhotonVision publishes %s"
              % (cam_name, cams))
        print("           the nickname in the UI is the NT table name; renaming it")
        print("           breaks every dashboard layout built against the old one")

    print("\n=== topics (%.1fs sample) ===" % args.seconds)
    if not topics:
        print("  none announced under /%s" % table)
        print("  in order of likelihood:")
        print("   1. PhotonVision's 'Run NetworkTables Server' is off, and with no")
        print("      roboRIO nothing else is the server - so it has nowhere to publish")
        print("   2. PhotonVision is pointed at a different server (Settings -> team")
        print("      number / NT server address)")
        print("   3. the pipeline is in driver mode, or stopped")
        print("  what is on this box:  ss -tln | grep -E '5810|1735'")
    for path in sorted(topics, key=lambda p: -topics[p]["rate"]):
        d = topics[path]
        short = path.rsplit("/", 1)[-1]
        if not args.all and short not in INTERESTING and d["rate"] == 0:
            continue
        val = d["value"]
        if val is not None and len(val) > 40:
            val = val[:37] + "..."
        print("  %-34s %6.1f Hz   %s" % (path, d["rate"], val))

    print("\n=== verdict ===")
    if not live:
        print("  NOTHING IS UPDATING. PhotonVision may be connected to a different")
        print("  NT server, or the pipeline is stopped. Check its log:")
        print("    journalctl -u photonvision -f")
    else:
        fastest = max(live.items(), key=lambda kv: kv[1]["rate"])
        print("  publishing - fastest topic %s at %.1f Hz" % (fastest[0], fastest[1]["rate"]))

        # Observed rates are capped by NetworkTables' send period, which
        # defaults to 100ms. A cluster of topics all sitting at exactly 10 Hz
        # is that cap, not the pipeline - so read the pipeline rate off
        # PhotonVision's own fps topic, which it computes internally.
        if fastest[1]["rate"] >= 9.0:
            print("  NOTE: ~10 Hz is NetworkTables' default 100ms send period.")
            print("        Observed rate is min(pipeline, ~10 Hz) - use the fps")
            print("        topic below for the real pipeline rate.")
        # A rename leaves the old camera's topics published but frozen until
        # PhotonVision restarts. Prefer the configured camera, and never let a
        # stale table supply the headline numbers.
        def owned(path):
            return cam_name and path.startswith(cam_name + "/")

        stale = sorted({c for c in cams if c != cam_name} & {
            p.split("/", 1)[0] for p, d in topics.items()
            if "/" in p and d["rate"] == 0})
        if cam_name and cam_name in cams and stale:
            print("  NOTE: %s also present but not updating - almost certainly a"
                  % ", ".join(stale))
            print("        previous camera nickname. It clears on a PhotonVision")
            print("        restart. Ignoring it below.")

        for p, d in sorted(topics.items()):
            if p.rsplit("/", 1)[-1] == "fps" and d["value"] is not None:
                if cam_name and cam_name in cams and not owned(p):
                    continue
                print("  pipeline fps   %-28s %s  (PhotonVision's own count)"
                      % (p, d["value"]))

        lat = [d["value"] for p, d in topics.items()
               if p.endswith("/latencyMillis")
               and (not (cam_name and cam_name in cams) or owned(p))]
        if lat and lat[0] is not None:
            print("  latency        %s ms" % lat[0])

        ht = [(p, d) for p, d in topics.items() if p.endswith("/hasTarget")]
        if ht:
            p, d = ht[0]
            print("  hasTarget = %s" % d["value"])
            # hasTarget is a boolean: a non-zero "rate" means it is flipping,
            # not updating. Intermittent detection is a different problem from
            # no detection, and it is the one that looks fine in a UI.
            if d["rate"] > 0.3:
                print("  WARNING: hasTarget toggled %.1f times/sec - detection is")
                print("           INTERMITTENT, not solid. Usually too little light,")
                print("           too much motion blur, or the tag too small in frame.")
        elif any(p.rsplit("/", 1)[-1] in RESULT_TOPICS for p in live):
            print("  detections are packed inside the result topic - decoding it")
            print("  needs photonlibpy, which is not installed by default here")
            print("  because its dependencies drag in a second OpenCV. See")
            print("  requirements.txt for the --no-deps route if you want it.")

    if pl:
        print("\n=== photonlib ===")
        for k, v in pl.items():
            print("  %-22s %s" % (k, v))
    else:
        print("\n  (photonlibpy not installed - install it for per-target detail")
        print("   including pose ambiguity, which is the number stage 7 is about)")
    print()

    return 0 if live else 1


if __name__ == "__main__":
    sys.exit(main())

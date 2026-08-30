"""NetworkTables access: connect, discover what exists, and measure rates.

Two things here are easy to get wrong, and both cost us an evening:

**An NT4 client only receives topics it has subscribed to.** A client that
connects and enumerates nothing is usually a missing subscription, not a silent
server - so a failed subscription is reported loudly rather than swallowed.

**Observed update rates are capped by NetworkTables' send period**, which
defaults to 100ms. A cluster of topics all sitting at exactly 10 Hz is that cap,
not the pipeline. PhotonVision's own `fps` topic is the pipeline rate.
"""

import time

# Held at module scope on purpose: a MultiSubscriber stops working the moment it
# is garbage collected, producing a client that connects and then sees nothing.
_SUBSCRIPTION = None

NT_SEND_HZ = 10.0       # NetworkTables' default 100ms send period

# PhotonVision 2026 packs the whole pipeline result into these; the flat
# per-target topics older documentation describes no longer exist. Both sets are
# listed so this survives a season change in either direction.
RESULT_TOPICS = ("rawBytes", "result_proto", "result")
INTERESTING = RESULT_TOPICS + (
    "heartbeat", "hasTarget", "latencyMillis", "targetYaw", "targetPitch",
    "targetArea", "targetSkew", "targetPixelsX", "targetPixelsY", "targetPose",
    "pipelineIndexRequest", "pipelineIndexState", "driverMode", "fps",
    "version", "buildDate", "ledModeState")

# Pipeline switching uses a request/state pair. A plain `pipelineIndex` topic
# appears in older NT API docs and nothing consumes it - writing it is accepted
# and ignored, and reading it back returns your own value, which looks like
# confirmation.
PIPELINE_REQUEST = "pipelineIndexRequest"
PIPELINE_STATE = "pipelineIndexState"


def connect(nt_cfg, timeout=5.0):
    """Bring up an NT client (or server) per config. Returns (inst, error).

    Idempotent: calling it again on an already-connected instance returns the
    existing one. That matters because a long-lived publisher connects once and
    then runs checks that would otherwise try to connect again.
    """
    global _SUBSCRIPTION
    try:
        from ntcore import NetworkTableInstance, MultiSubscriber
    except ImportError:
        return None, ("pyntcore not installed. It is pip-only - robotpy is not "
                      "packaged in Debian - so it needs a venv. See requirements.txt")

    inst = NetworkTableInstance.getDefault()
    if _SUBSCRIPTION is not None and inst.isConnected():
        return inst, None
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

    try:
        _SUBSCRIPTION = MultiSubscriber(inst, [""])
    except Exception as e:
        return None, ("could not subscribe (%s: %s). Without a subscription an "
                      "NT4 client is never told which topics exist."
                      % (e.__class__.__name__, e))

    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        # A server is up immediately; a client has to find one. Connecting is
        # not the same as having received the topic list, which arrives after.
        if mode == "server" or inst.isConnected():
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


def read(entry):
    """The typed value of a topic, or None."""
    try:
        v = entry.getValue()
        return v.value() if hasattr(v, "value") else None
    except Exception:
        return None


def discover(inst, table):
    """Every topic path the server has announced beneath the table."""
    prefix = "/" + table.strip("/")
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

    root = inst.getTable(table)          # fallback for an ntcore without getTopics
    try:
        for cam in root.getSubTables():
            for k in root.getSubTable(cam).getKeys():
                names.append("%s/%s/%s" % (prefix, cam, k))
        for k in root.getKeys():
            names.append("%s/%s" % (prefix, k))
    except Exception:
        pass
    return sorted(names)


def cameras(paths, prefix):
    """Camera subtable names, excluding NT bookkeeping."""
    out = set()
    for full in paths:
        rel = full[len(prefix) + 1:] if full.startswith(prefix + "/") else full
        if "/" in rel:
            head = rel.split("/", 1)[0]
            # ".timesync" is NT bookkeeping; an empty head comes from
            # PhotonVision's "//metrics/..." path. Neither is a camera.
            if head and not head.startswith("."):
                out.add(head)
    return sorted(out)


def watch(inst, table, seconds, poll=0.02):
    """Poll every topic under the table, counting how often each changes.

    Change-counting rather than timestamp arithmetic: it needs only the most
    basic NT API, which is the part least likely to move between seasons.

    Note that on a boolean, a non-zero "rate" means the value is *toggling*, not
    updating - intermittent detection rather than a fast one.
    """
    prefix = "/" + table.strip("/")
    names = discover(inst, table)
    entries, last, changes = {}, {}, {}
    for full in names:
        rel = full[len(prefix) + 1:] if full.startswith(prefix + "/") else full
        e = inst.getEntry(full)
        entries[rel] = e
        last[rel] = snapshot(e)
        changes[rel] = 0

    t0 = time.time()
    while time.time() - t0 < seconds:
        for rel, e in entries.items():
            cur = snapshot(e)
            if cur is not None and cur != last[rel]:
                changes[rel] += 1
                last[rel] = cur
        time.sleep(poll)
    dt = max(time.time() - t0, 1e-6)

    return (cameras(names, prefix),
            {r: {"rate": changes[r] / dt, "value": last[r]} for r in entries})


def camera_entries(inst, table, camera, keys):
    base = "/%s/%s" % (table.strip("/"), camera)
    return {k: inst.getEntry("%s/%s" % (base, k)) for k in keys}, base


def request_pipeline(entries, index):
    """Ask for a pipeline. Confirm with the state topic, never with the request."""
    try:
        entries[PIPELINE_REQUEST].setInteger(index)
    except Exception:
        entries[PIPELINE_REQUEST].setDouble(float(index))

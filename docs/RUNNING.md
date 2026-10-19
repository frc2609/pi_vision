# Running `vd`, and reading its output

Every layer of this system should be able to say whether it is the problem. The
health monitor covers the Pi; `vd` covers everything above it. This document is
how to run it and, more importantly, how to read what it prints — several of the
outputs are easy to misread in ways that send you looking in the wrong place.

*Numbers throughout are from the working setup on `2609-pi-1`, 2026-08-29. They
are reference points, not targets — the room is light-starved and none of the
detection figures transfer.*

---

## Which command, for which symptom

| Symptom | Start with | Because |
|---|---|---|
| "Is it ready?" | `vd preflight` | one pass/fail across every layer |
| No image anywhere | `vd doctor` | is the device even present and streaming |
| Image fine, PhotonVision shows nothing | `vd nt` | detection may be running and publishing nowhere |
| Framerate is bad | `vd doctor` then `vd nt` | separates the camera limit from the processing limit |
| "It worked yesterday" | `vd snapshot --diff` | the only command that can answer this |
| Choosing settings | `vd tune` | measurement instead of opinion |

Every command exits meaningfully: **0** good, **1** degraded, **2** cannot tell.
`vd preflight && echo ready` works.

**Any number of cameras.** `config.yaml` holds a `cameras:` list; commands cover
all of them and take `--camera <name>` to narrow. `tune` and `stream` need one,
so they ask for the flag when several are configured.

**Only one process can hold a V4L2 capture device.** While PhotonVision runs it
owns the camera, so anything opening the device directly (`vd stream`, and
`doctor`'s measured column) needs PhotonVision stopped first. The tools detect
this and say so rather than failing obscurely.

---

## Two Python environments, on purpose

- **Camera commands** — `doctor`, `snapshot`, `stream`, `field` — run
  on **system `python3`**. They use apt's OpenCV.
- **NetworkTables commands** — `nt`, `tune`, and `preflight`'s network check —
  need `pyntcore`, which is pip-only, so they run from **`.venv/bin/python`**.

```bash
python3 src/vd.py doctor              # camera side
.venv/bin/python src/vd.py nt         # network side
```

> **Never install `photonlibpy` into a venv created with
> `--system-site-packages`.** It depends on `opencv-python` and a newer numpy,
> and the venv's `site-packages` takes precedence over apt's `dist-packages` —
> so it silently replaces OpenCV 4.10-with-contrib with OpenCV 5 without it, and
> `cv2.aruco` disappears. We did this once. Verify after any pip install:
>
> ```bash
> .venv/bin/python -c "import cv2; print(cv2.__version__, cv2.__file__, hasattr(cv2,'aruco'))"
> ```
> Want `4.10.x`, a **dist-packages** path, and `True`.

---

## `vd preflight` — is the whole chain ready

```bash
.venv/bin/python src/vd.py preflight; echo "exit $?"
```

```
  PASS  power                  0x0 clean
  PASS  camera                 1280x800 MJPG
  FAIL  controls               exposure_time_absolute=1245 (want 120) ...
  PASS  calibration            HFOV 82.2 deg
  PASS  photonvision           active
  FAIL  networktables          pyntcore not installed...

  NOT READY - 2 check(s) failed
```

Exit code is the number of failed checks.

| Check | Reading it |
|---|---|
| `power` | Anything but `0x0` means undervoltage or throttling occurred **since the last full power cycle**. Sticky bits do not clear on a soft reboot, so a fault here may be history rather than now — the monitor's `*_since_start` fields separate those. |
| `format` | Negotiated width, height and pixel format against `expect`. A mismatch usually means something reopened the camera at its own default — PhotonVision opens new cameras at 320x240 YUYV. |
| `controls` | **Expected to fail while PhotonVision is running.** It sets its own exposure when it opens the camera, and it wins. Informational unless PhotonVision is stopped. |
| `calibration` | Derives HFOV from `fx`. A negative or zero `fx` is a diverged solve, not a bad calibration — see [CALIBRATION.md](CALIBRATION.md). |
| `photonvision` | Service state only. Says nothing about whether detection works — that is `vd nt`. |
| `networktables` | Connects and checks something is actually updating. |

**`SKIP` means the check could not run**, and says why. It is not a soft
failure — and since a SKIP publishes green to the dashboard, anything the system
is actually *missing* fails instead. An uncalibrated camera is a FAIL, not a
skip; use `calibration.required: false` on a camera you are still bringing up.

### Publishing a go/no-go to the driver station

```bash
.venv/bin/python src/vd.py preflight --publish --interval 30
sudo cp services/vision_preflight.service /etc/systemd/system/
sudo systemctl enable --now vision_preflight
```

Publishes to **`/Vision/preflight`**, booleans and numbers only so it graphs and
needs no parsing:

| Topic | |
|---|---|
| `ready` | the go/no-go light — every check on every camera |
| `power_ok`, `photonvision_ok`, `networktables_ok`, `configured_ok`, `distinct_devices_ok`, `timesync_ok` | machine-wide checks |
| `<camera>/format_ok`, `<camera>/identity_ok`, `<camera>/calibration_ok`, `<camera>/publishing_ok`, `<camera>/pipeline_fps_ok`, `<camera>/detections_ok` | one set per camera — **always all six**, whatever state the camera is in |
| `failed`, `skipped`, `cameras` | counts |
| `seq` | increments every run |
| `run_epoch_s` | when it last ran |

Any NetworkTables dashboard can read these. With no roboRIO, point it at the
Pi - PhotonVision runs the NT server. Green on `ready` means go.

> **`timesync_ok` is about the roboRIO's clock, not the Pi's.** PhotonVision
> stamps each capture at start of exposure and converts it into roboRIO time
> before publishing, so the pose estimator can rewind odometry to the moment the
> shutter opened. It learns the conversion from a time server that **robot code**
> starts - the roboRIO is the server, this Pi is the client, over UDP 5810 and
> not over NetworkTables. With no roboRIO nothing serves, so this reports SKIP
> and publishes green; that is the correct bench state. Set
> `preflight.require_timesync: true` once a roboRIO exists, because an unsynced
> timestamp still fuses - it just lands in the wrong instant, which is invisible
> at rest and looks like a camera fault under motion.

> **If your dashboard colours booleans automatically, check the polarity.** The
> `*_ok` topics here are true-is-good, but the health monitor's fault flags
> (`undervoltage_*`, `throttled_*`, `usb_over_current`) are true-is-bad. A
> dashboard that paints false red will show a wall of red meaning "everything is
> fine", which trains people to ignore red. Either invert those widgets or show
> `throttled_raw` instead - one number, `0x0` is clean, no polarity to get
> wrong.

**A `SKIP` publishes as green**, because a check that could not run is not a
failure. `skipped` tells you how many. Anything the system is genuinely
*missing* fails instead - an uncalibrated camera is a FAIL, not a skip.

**Stream bandwidth is worth a thought here too.** A camera whose Stream
Resolution is left at full size published 6.31 Mbps against another's 0.35 Mbps
at 320x200 - eighteen times the traffic for a picture a human glances at, and
FRC has historically capped total bandwidth near 4 Mbps. Stream resolution does
not affect detection; set it low on every camera.

> **Every camera always publishes all six of its checks**, even when it is
> not publishing to PhotonVision at all. A conditional check produces a topic
> that appears and disappears, and on a dashboard that is a widget vanishing at
> the moment something breaks. A check that cannot run reports SKIP; it never
> goes missing.

> **Two ways this can lie, and how each is handled.** If the publisher dies, NT4
> unpublishes its topics, so the lights **vanish** from the dashboard rather
> than freezing green - that is why this runs as a long-lived service rather
> than a timer firing a one-shot. And if it wedges while still connected, the
> topics stay but `seq` stops advancing: put `seq` on a graph widget and a flat
> line is the tell. A stale green light is the failure mode worth designing out.

---

## `vd doctor` — is the camera delivering what we asked for

```bash
sudo python3 src/vd.py doctor          # sudo to see which process holds the device
python3 src/vd.py doctor --json        # same facts, machine-readable
python3 src/vd.py doctor --no-probe    # skip the measured column
```

Three columns that should agree and often do not:

| Column | Source | Meaning |
|---|---|---|
| **CAN** | v4l2 format enumeration | what the hardware advertises |
| **ASKED** | the negotiated format | what the current holder requested |
| **DELIVERED** | measured | what actually arrives — only when the device is free |

- **`stable path`** — the `/dev/v4l/by-id/...` name. Blank means device
  numbering can move on replug and anything using `/dev/video0` is fragile.
- **`CAN`** — here, `MJPG 1280x800 @ 120 fps` and `YUYV 1280x800 @ 5 fps`.
  MJPEG is not optional; YUYV at full resolution is the USB 2.0 wall.
- **`ASKED`** — a `WARNING` means something is running below the hardware's best
  mode. PhotonVision defaults new cameras to **320x240 YUYV**, invisible in its
  UI and obvious in this line.
- **controls** — `[inactive]` means an automatic mode owns that control and
  writes to it are silently discarded. `exposure_time_absolute` is in **100 µs
  units**, so `120` = 12 ms, and `-> max fps` shows what that permits.
- **`WHO HAS IT`** — `nobody` means you can measure; a named process means the
  measured column is skipped; `cannot tell` means run it with `sudo`.
- **`WHERE TO LOOK`** — URLs by **IP**, not `.local`. Chrome blocks `.local`
  sub-resources from a plain-HTTP page, so PhotonVision's embedded stream stays
  blank over mDNS while the same URL works opened directly.

**Exit codes:** `0` at its best mode, `1` below what the hardware advertises or
capabilities unreadable, `2` no device.

### The exposure/framerate arithmetic

Worth internalising — it explains most "why is it slow" questions:

```
max fps = 10000 / exposure_time_absolute        (the control is in 100 µs units)
```

A pipeline stuck at 8.0 fps with the control at 1245 is not CPU-bound and not a
bug: 10000 / 1245 = 8.03. The camera is being held open for an eighth of a
second per frame.

---

## `vd nt` — is anything useful reaching the network

```bash
.venv/bin/python src/vd.py nt
.venv/bin/python src/vd.py nt --all --seconds 5
```

```
  cameras publishing     front_left
  front_left/heartbeat        9.9 Hz   15825
  front_left/latencyMillis    9.9 Hz   26.26
  front_left/rawBytes         9.9 Hz   b'\xd1=...'
  front_left/fps              2.3 Hz   41.0
  front_left/hasTarget        0.0 Hz   False
```

**Three traps, all of which caught us:**

1. **~10 Hz is NetworkTables, not the pipeline.** NT's default send period is
   100 ms, so an observed rate is `min(pipeline rate, ~10 Hz)`. A cluster of
   topics all at exactly 10 Hz is that cap. **Use the `fps` topic** —
   PhotonVision's own count. Above, the pipeline runs at **41 fps** while
   everything appears at 9.9 Hz.
2. **A rate on a boolean means it is toggling, not updating.** `hasTarget` at
   1.3 Hz is detection dropping and reacquiring several times a second —
   intermittent detection, which looks fine in a UI and is a different problem
   from no detection. `vd nt` warns about this explicitly.
3. **`0.0 Hz` means unchanged during the sample, not absent.** `hasTarget =
   False` at 0.0 Hz just means no tag was in view.

**PhotonVision 2026 does not publish the flat per-target topics** older
documentation describes; everything is packed into `rawBytes` / `result_proto`.
`vd nt` enumerates whatever exists rather than assuming a schema, so a renamed
topic appears as a new name rather than as silence.

**A renamed camera leaves its old table published but frozen** until
PhotonVision restarts. `vd nt` flags the stale one and ignores it.

### When it finds nothing

1. **PhotonVision's "Run NetworkTables Server" is off.** With no roboRIO nothing
   else is the server, so PhotonVision is a client looking for one that does not
   exist and you are both waiting.
2. PhotonVision points at a different server — check team number / NT address.
3. The pipeline is in driver mode, or stopped.

```bash
ss -tln | grep -E '5810|1735'
```

> An NT4 client only receives topics it has **subscribed** to. A client that
> connects and enumerates nothing is usually a missing subscription, not a
> silent server. `vd nt` reports a subscription failure loudly instead of
> swallowing it, because that bug cost us an evening.

---

## `vd tune` — measurement instead of opinion

```bash
# one configuration
.venv/bin/python src/vd.py tune --label baseline --csv tuning.csv

# sweep exposure, writing it straight to the camera
.venv/bin/python src/vd.py tune --sweep-exposure 120,200,320,500,800 --csv tuning.csv

# sweep pipelines - create one per setting in the UI first
.venv/bin/python src/vd.py tune --sweep-pipeline 0,1,2 --csv tuning.csv
```

Both sweeps restore the original setting when they finish, including on Ctrl-C.

**Pose noise is the accuracy proxy.** Range is the usual tuning metric and a
small room cannot produce it, but corner noise is what becomes pose noise
downstream, and `yaw_sd` on a static tag measures it directly. Run-to-run
repeatability is about **0.001°**, so treat smaller differences as noise.

**Every row records exposure, width and height read from the device**, not from
the label. A label is typed by a human and a forgotten UI step makes it a lie.
If two rows share those values, nothing changed — and `vd tune` says so at the
end rather than letting you believe a flat result.

Two verdicts it prints, both of which mean *the experiment is wrong* rather than
*the setting does not matter*:

- **`EXPOSURE-BOUND`** — framerate is pinned by exposure time, so decimation has
  nothing to give back. Decimation trades detector work for accuracy and only
  pays when the detector is the bottleneck.
- **`detection NOT solid`** — noise figures from an intermittent target are not
  comparable between rows.

> **Pipeline switching uses two topics.** PhotonLib publishes the requested
> index to `pipelineIndexRequest` and reads the actual one from
> `pipelineIndexState`. A plain `pipelineIndex` topic appears in older NT API
> documentation and nothing consumes it — writing it is accepted and ignored,
> and reading it back returns your own value, which looks like confirmation.
> `vd tune` uses the pair, so its readback is PhotonVision's state.

---

## `vd snapshot` — what changed since it worked

```bash
sudo python3 src/vd.py snapshot        # capture; sudo to read PhotonVision's settings
python3 src/vd.py snapshot --diff      # newest two
python3 src/vd.py snapshot --list
```

```
  camera/ctrl/exposure_time_absolute
    - 1306 [has-min-max]
    + 794 [has-min-max]

  1 field(s) changed
```

Captures 100+ fields: kernel and package versions, service states, the USB
device, the capability list, the current format, **every v4l2 control**, the
calibration hash and focal length, and hashes of PhotonVision's settings.

- **Take one whenever things are known good, and commit it.** A snapshot from
  the last event is worth more than any amount of remembering.
- `pv_settings/photon.sqlite` changes on most snapshots — PhotonVision writes to
  it continuously — so a diff minutes apart is rarely empty. Log files are
  excluded for the same reason.
- The 120 MB PhotonVision jar is recorded as `size=... mtime=...` rather than
  hashed; a replacement still shows up.

**Exit code is `1` when the diff found changes**, `0` when identical.

---

---

## `vd field` — a two-tag mini-field

```bash
python3 src/vd.py field \
    --left  ID=2 dist=0.50 height=1.635 \
    --right ID=0 dist=0.50 height=1.660 \
    -o config/field_layout_corner.json
```

Tags on two perpendicular walls, origin at the corner on the floor, as seen
**facing the corner**. Conventions: metres, NWU, right-handed, **+Z up**, pose
is the centre of the **black square**, and **zero rotation faces +X**.

It prints the centre-to-centre distance the layout implies — **tape-measure that
before trusting it.** Layout error becomes pose error directly and the solver
has no way to know.

Import: **Settings → Device Control → Import Settings → AprilTag Layout**.

---

## `vd stream` — see what the camera sees

```bash
sudo systemctl stop photonvision       # it owns the camera
python3 src/vd.py stream               # http://<ip>:8090
sudo systemctl start photonvision
```

Annotated MJPEG with tag ID, tag width in pixels, mean brightness and live fps,
capped at 15 fps because it is for a human. If PhotonVision is running, use its
own streams on 1181/1182 instead — `vd doctor` prints those URLs.

---

## Tests

```bash
python3 -m unittest discover -s tests
```

27 parser tests, run anywhere — no camera, no Pi, no PhotonVision. The fixtures
under `tests/fixtures/` are real `v4l2-ctl` output, and the tests pin the
awkward cases: the `inactive` flag, a control the hardware does not have, YUYV's
5 fps wall, the exposure arithmetic, and the "every row measured the same thing"
detector.

---

## Known-good reference for this setup

| | Value |
|---|---|
| Camera | Thriftiest Cam, OV9281, serial `DCX-260513-K` |
| Format | 1280x800 MJPG, 121 fps measured |
| Calibration | `fx` 733.6, HFOV 82.2°, RMS 0.51 px (PhotonVision's own: 0.46 px, 81.98°) |
| Pipeline | 41 fps at 1280x800 with 3D on, 26 ms latency |
| CPU ceiling | 41–43 fps; exposure becomes the constraint above ~23 ms |
| 36h11 detection floor | ~25 ms exposure in a single-LED room |
| NT | PhotonVision runs the server; tools connect to `127.0.0.1` |

**The detection numbers do not transfer.** The room is short of the light needed
to hold motion smear inside one tag module at robot rotation rates, and there is
no gain control to make it up. Retake them in a real venue.

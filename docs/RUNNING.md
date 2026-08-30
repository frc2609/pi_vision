# Running the camera tools, and reading their output

Every layer of this system should be able to say whether it is the problem. The
health monitor covers the Pi; these cover everything above it. This document is
how to run them and, more importantly, how to read what they print.

*Numbers throughout are from the working setup on `2609-pi-1`, 2026-08-29. They
are reference points, not targets — the room is light-starved and none of the
detection figures transfer.*

---

## Which tool, for which symptom

| Symptom | Start with | Because |
|---|---|---|
| "Is it ready?" | `preflight.py` | one pass/fail across every layer |
| No image anywhere | `camera_doctor.py` | is the device even present and streaming |
| Image is fine, PhotonVision shows nothing | `pipeline_doctor.py` | detection may be running and publishing nowhere |
| Framerate is bad | `camera_doctor.py` then `pipeline_doctor.py` | separates camera limit from processing limit |
| "It worked yesterday" | `config_snapshot.py --diff` | the only tool that can answer this |
| After a reboot or replug | `apply_camera_settings.py` | v4l2 controls do not survive either |

**Only one process can hold a V4L2 capture device.** While PhotonVision is
running it owns the camera, so anything that needs to open the camera directly
(`camera_doctor --stream`, the measured column) needs PhotonVision stopped
first. The tools detect this and say so rather than failing obscurely.

---

## Two Python environments, on purpose

- **Camera tools** — `camera_doctor`, `config_snapshot`, `apply_camera_settings`,
  `make_field_layout` — run on **system `python3`**. They need apt's OpenCV.
- **NetworkTables tools** — `pipeline_doctor`, and `preflight` when it checks NT
  — need `pyntcore`, which is pip-only, so they run from **`.venv/bin/python`**.

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

## `preflight.py` — is the whole chain ready

```bash
.venv/bin/python src/preflight.py; echo "exit $?"
```

```
  PASS  power                  0x0 clean
  PASS  camera                 1280x800 MJPG
  FAIL  controls               exposure_time_absolute=1245 (want 120); ...
  PASS  calibration            HFOV 82.2 deg
  PASS  photonvision           active
  FAIL  networktables          pyntcore not installed...

  NOT READY - 2 check(s) failed
```

**Exit code is the number of failed checks**, so `preflight.py && echo ready`
works in a script.

| Check | Reading it |
|---|---|
| `power` | Anything but `0x0` means undervoltage or throttling has occurred **since the last full power cycle**. Sticky bits do not clear on a soft reboot, so a fault here may be history rather than now — the health monitor's `*_since_start` fields separate those. |
| `camera` | Compares the negotiated format against `camera.expect` in config. A mismatch usually means something reopened the camera at its own default. |
| `controls` | **Expected to fail while PhotonVision is running.** PhotonVision sets its own exposure when it opens the camera, and it wins. Treat a `controls` failure as informational unless PhotonVision is stopped. |
| `calibration` | Reads `fx` from the calibration file and derives HFOV. A negative or zero `fx` is a diverged solve, not a bad calibration — see [photonvision_calibration.md](../../docs/photonvision_calibration.md). |
| `photonvision` | Service state only. It says nothing about whether detection works — that is `pipeline_doctor`. |
| `networktables` | Connects and checks that something is actually updating. |

**`SKIP` is never a failure.** It means the check could not run, and it says why.

---

## `camera_doctor.py` — is the camera delivering what we asked for

```bash
sudo python3 src/camera_doctor.py        # sudo to see which process holds the device
python3 src/camera_doctor.py --json      # same facts, machine-readable
python3 src/camera_doctor.py --stream    # annotated live view on :8090
```

The report is three columns that should agree and often do not:

| Column | Source | Meaning |
|---|---|---|
| **CAN** | v4l2 format enumeration | what the hardware advertises |
| **ASKED** | the negotiated format | what the current holder requested |
| **DELIVERED** | measured | what actually arrives — only when the device is free |

Reading each section:

- **`stable path`** — the `/dev/v4l/by-id/...` name. If this is blank, device
  numbering can move on replug and any script using `/dev/video0` is fragile.
- **`CAN`** — for this camera, `MJPG 1280x800 @ 120 fps` and `YUYV 1280x800 @ 5
  fps`. MJPEG is not optional here; YUYV at full resolution is the USB 2.0 wall.
- **`ASKED`** — a `WARNING` here means something is running below the hardware's
  best mode. PhotonVision defaults new cameras to **320x240 YUYV**, which is
  invisible in its UI and obvious in this line.
- **controls** — `[inactive]` in the flags means an automatic mode owns that
  control and writes to it are silently discarded. `exposure_time_absolute` is
  in **units of 100 µs**, so `120` = 12 ms.
- **`WHO HAS IT`** — `nobody` means you can measure. A named process means the
  measured column is skipped. `cannot tell` means run it with `sudo`.
- **`DELIVERED`** — measured fps, actual size and fourcc, mean brightness
  (0–255) and any tags visible.
- **`WHERE TO LOOK`** — URLs by **IP**, not `.local`. Chrome blocks `.local`
  sub-resources from a plain-HTTP page, so PhotonVision's embedded stream stays
  blank over mDNS while the same URL works when opened directly.

**Exit codes:** `0` at its best mode, `1` running below what the hardware
advertises or capabilities unreadable, `2` no device.

### The exposure/framerate arithmetic

Worth internalising, because it explains most "why is it slow" questions:

```
max fps = 10000 / exposure_time_absolute        (control is in 100 µs units)
```

A pipeline stuck at 8.0 fps with the control at 1245 is not CPU-bound and not a
bug: 10000 / 1245 = 8.03. The camera is being held open for an eighth of a
second per frame.

---

## `pipeline_doctor.py` — is anything useful reaching the network

```bash
.venv/bin/python src/pipeline_doctor.py
.venv/bin/python src/pipeline_doctor.py --all      # every topic, not just known ones
```

```
  cameras publishing     Thrifty:_
  Thrifty:_/heartbeat        9.9 Hz   15825
  Thrifty:_/latencyMillis    9.9 Hz   26.26
  Thrifty:_/rawBytes         9.9 Hz   b'\xd1=...'
  Thrifty:_/fps              2.3 Hz   41.0
  Thrifty:_/hasTarget        0.0 Hz   False
```

**Three traps in reading this, all of which caught us:**

1. **~10 Hz is NetworkTables, not the pipeline.** NT's default send period is
   100 ms, so the observed rate is `min(pipeline rate, ~10 Hz)`. A cluster of
   topics all at exactly 10 Hz is that cap. **Use the `fps` topic** —
   PhotonVision's own count — for the real pipeline rate. Above, the pipeline is
   at **41 fps** while everything appears at 9.9 Hz.
2. **A rate on a boolean means it is toggling, not updating.** `hasTarget` at
   1.3 Hz is detection dropping and reacquiring several times a second —
   intermittent detection, which looks fine in a UI and is a different problem
   from no detection. The tool warns about this explicitly.
3. **`0.0 Hz` means unchanged during the sample, not absent.** `hasTarget = False`
   at 0.0 Hz simply means no tag was in view.

**PhotonVision 2026 does not publish the flat per-target topics that older
documentation describes.** Everything is packed into `rawBytes` /
`result_proto`. The tool enumerates whatever exists rather than assuming a
schema, so a renamed topic appears as a new name rather than as silence.

**Exit codes:** `0` publishing, `1` connected but nothing updating, `2` no
connection.

### When it finds nothing

In order of likelihood:

1. **PhotonVision's "Run NetworkTables Server" is off.** With no roboRIO nothing
   else is the server, so PhotonVision is a client looking for a server that
   does not exist. Both of you wait forever.
2. PhotonVision is pointed at a different server — check its team number / NT
   server address.
3. The pipeline is in driver mode or stopped.

Check what is actually listening:

```bash
ss -tln | grep -E '5810|1735'
```

> An NT4 client only receives topics it has **subscribed** to. A client that
> connects and enumerates nothing is usually a missing subscription, not a
> silent server — that bug cost us an evening and it is why the tool now reports
> a subscription failure loudly instead of swallowing it.

---

## `config_snapshot.py` — what changed since it worked

```bash
sudo python3 src/config_snapshot.py     # capture; sudo to read PhotonVision's settings
python3 src/config_snapshot.py --diff   # newest two
python3 src/config_snapshot.py --list
```

```
  camera/ctrl/exposure_time_absolute
    - 1306 [has-min-max]
    + 794 [has-min-max]
  pv_settings/photon.sqlite
    - c6393b9d304d1c68
    + 0c8071c41ac870d1

  2 field(s) changed
```

Captures 100+ fields: kernel and package versions, service states, the USB
device, the full capability list, the current format, **every v4l2 control**,
the calibration hash and focal length, and hashes of PhotonVision's settings.

- **Take one whenever things are known good, and commit it.** A snapshot from
  the last event is worth more than any amount of remembering.
- `pv_settings/photon.sqlite` changes on most snapshots — PhotonVision writes to
  it continuously — so a diff of two snapshots minutes apart is rarely empty.
  Log files are excluded for the same reason.
- The large PhotonVision jar is recorded as `size=... mtime=...` rather than
  hashed, which still detects a replacement without hashing 120 MB every time.

**Exit code is `1` when the diff found changes**, `0` when identical.

---

## `apply_camera_settings.py` — put the controls back

```bash
python3 src/apply_camera_settings.py       # apply and verify now
./scripts/install_service.sh               # apply at every boot
```

**v4l2 controls do not survive a reboot or a replug.** They live on the device,
not on disk, and nothing announces that they have reverted.

Output is one line per control with `want`, `got`, and `ok` or `MISMATCH`. A
mismatch means one of three things: the control does not exist on this camera,
the driver clamped the value, or an automatic mode still owns it.

Order matters and the script handles it: `exposure_time_absolute` is inactive
while `auto_exposure` is in an automatic mode, and a write to an inactive
control is **accepted and discarded**, so manual mode is set first. Every value
is read back afterwards, because v4l2 reports success while ignoring you.

> PhotonVision imposes its own pipeline settings when it opens the camera. This
> governs the window before PhotonVision starts and any tool using the camera
> directly. Where PhotonVision owns the camera, set exposure in its UI.

---

## `make_field_layout.py` — a two-tag mini-field

```bash
python3 src/make_field_layout.py \
    --left  ID=2 dist=0.50 height=1.635 \
    --right ID=0 dist=0.50 height=1.660 \
    -o config/field_layout_corner.json
```

Tags on two perpendicular walls, origin at the corner on the floor, as seen
**facing the corner**. Conventions: metres, NWU, right-handed, **+Z up**, pose is
the centre of the **black square**, and **zero rotation faces +X**.

It prints the centre-to-centre distance the layout implies — **tape-measure that
and compare before trusting it.** Layout error becomes pose error directly, and
the solver has no way to know.

Import into PhotonVision: **Settings → Device Control → Import Settings → type
AprilTag Layout**.

---

## Known-good reference for this setup

| | Value |
|---|---|
| Camera | Thriftiest Cam, OV9281, serial `DCX-260513-K` |
| Format | 1280x800 MJPG, 121 fps measured |
| Calibration | `fx` 733.6, HFOV 82.2°, RMS 0.51 px (PhotonVision's own: 0.46 px, 81.98°) |
| Pipeline | 41 fps at 1280x800 with 3D on, 26 ms latency |
| Detection floor | 12 ms exposure in a single-LED room |
| NT | PhotonVision runs the server; tools connect to `127.0.0.1` |

**These detection numbers do not transfer.** The room is 4–9× short of the light
needed to hold motion smear inside one tag module at robot rotation rates, and
there is no gain control to make it up. Retake everything in a real venue.

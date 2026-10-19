# PhotonVision on the Pi

Setting it up, tuning it, and the handful of settings that decide whether this
system works. Written for someone competent who has not done it before.

*Numbers below were measured on `2609-pi-1` with Thriftiest Cams (OV9281, 1MP
mono global shutter, 1280x800 MJPEG, USB 2.0). The **arithmetic** transfers
anywhere; the **detection figures do not** — they describe one light-starved
room. Retake them in a real venue.*

- Calibration has its own document: [`CALIBRATION.md`](CALIBRATION.md). Read it
  **before** your first attempt — one default setting in the UI makes the solver
  fail, and it is not obvious.
- NetworkTables, team number, server mode and the roboRIO link:
  [`NETWORKING.md`](NETWORKING.md).
- Running the diagnostics referred to here (`vd doctor`, `vd nt`, `vd tune`,
  `vd preflight`, `vd snapshot`): [`RUNNING.md`](RUNNING.md).

---

## What PhotonVision does, and what it does not

PhotonVision is the whole image-processing half of this system. It does:

- **Detection** — finds AprilTags in each frame.
- **Pose solving** — turns tag corners into a 3D transform, using the camera
  calibration.
- **Multi-tag** — combines several visible tags against a field layout into one
  field-relative pose, which is what resolves the single-tag ambiguity that is
  the biggest noise source in FRC vision.
- **Calibration** — its own workflow, producing one calibration per resolution.
- **NetworkTables publishing** — pose, per-target data and the capture
  timestamp, under `/photonvision/<camera>/`.

**We do not write a detector.** That code already exists, with years of field
testing behind it, and rewriting it buys almost no accuracy. Our Python on the
Pi is monitoring and diagnostics only; decisions about what to do with a vision
measurement live in Java on the roboRIO.

What PhotonVision does **not** do, and nobody else will do for you:

| Not PhotonVision's job | Where it lives |
|---|---|
| Camera hardware, lighting, exposure choice | this document |
| Mounting and the camera-to-robot transform | the robot, measured by hand |
| Pose estimator, trust weighting, robot behaviour | Java on the roboRIO |
| Power and thermal monitoring | the health monitor, its own service |
| **Object detection** (game pieces) | not supported on a Pi 5 at all |

Two things worth knowing before you plan around it:

- **The Pi 5 is supported for AprilTags**, documented as handling up to **two
  AprilTag streams at 1280x800 / 30 fps**. That is what we need, and it is
  confirmed on our hardware — see [two cameras](#two-cameras) below.
- **Object detection is not available on a Pi 5.** The Orange Pi 5 is the only
  device documented for it. If object detection becomes a requirement, that is a
  hardware decision, not a tuning problem.

> **PhotonVision reports some Pi health itself** — throttling reasons and CPU
> uptime, visible in its UI. Treat that as a convenient second opinion, not as
> the witness: anything PhotonVision reports dies with PhotonVision, and a
> brownout that kills it is exactly the event you need reported. That is the
> health monitor's job, and it deliberately runs as its own service.

---

## Installing and updating

**Install onto the existing Raspberry Pi OS. Do not flash PhotonVision's own
image.** Flashing replaces the whole card — the health monitor, the
calibrations, the network configuration and everything else go with it.

```bash
wget https://raw.githubusercontent.com/PhotonVision/photonvision/master/scripts/install.sh -O install.sh
sudo ./install.sh -c no
```

- **`-c no` matters.** Without it the script installs NetworkManager and
  rewrites the netplan renderer, which is an excellent way to lose SSH to a
  headless Pi — and then you need a monitor and keyboard to get it back.
- PhotonVision's docs still link a dead `git.io` short URL for this script. The
  path above is the current one.
- We run **v2026.3.4**. Check the version in the UI after an update; the
  robot-side `photonlib` should be from the same season's release.
- The install writes a `cpu_governor.service` whose generated script lost its
  loop variable, so it never actually sets the governor. Harmless, but it does
  not do what its name says — do not spend an evening on it.

**Updating** is the same script, or the UI's offline update with a downloaded
`.jar`. Either way: **export the settings first** (see
[exporting](#exporting-and-committing-the-settings)). An update that goes
sideways with no export is a rebuild from memory.

---

## The service

PhotonVision runs as a systemd service named `photonvision`, which means it
starts itself on boot and restarts if it dies.

```bash
systemctl status photonvision          # is it running
sudo systemctl restart photonvision    # after an import, or when it wedges
sudo systemctl stop photonvision       # frees the camera for vd stream/doctor
journalctl -u photonvision -f          # live log; where the real errors appear
```

Two things that catch people:

- **`enable` and `start` are different.** A service that was started but never
  enabled works perfectly until the first power cycle, and then silently does
  not. The installer enables it; if you ever reinstall by hand, check.
- **Only one process can hold a camera.** While PhotonVision is running it owns
  the device, so anything that opens the camera directly (`vd stream`, and
  `vd doctor`'s measured column) needs PhotonVision stopped first. Stop it,
  measure, start it again.

The health monitor watches this service by name and publishes whether it is
active. That is service state only — it says nothing about whether detection
works. For that, use `vd nt`.

---

## The web UI and the streams

| Port | What |
|---|---|
| **5800** | the web UI — all configuration happens here |
| **1181** | raw camera stream, MJPEG |
| **1182** | processed stream, with detections drawn on |

One pair of stream ports per camera, so a second camera gets its own pair.
`vd doctor` prints the exact URLs for the cameras that are configured.

**Use the Pi's IP address, not its `.local` name:**

```
http://10.26.9.11:5800          yes
http://2609-pi-1.local:5800     UI loads, embedded stream stays blank in Chrome
```

That is not a PhotonVision bug. Chrome's **Private Network Access** policy
blocks sub-resources from a plain-HTTP page into the more-private `local`
address space, so the page loads and the stream inside it does not — while the
same stream URL works fine typed straight into the address bar. Firefox also
works. The console message blames CORS, which sends you looking in entirely the
wrong place.

**The streams are for humans.** Robot code never reads them; it reads
NetworkTables. That is why stream resolution should be set low — see
[stream resolution](#stream-resolution-and-bandwidth).

---

## The settings that matter

Everything else in the UI is secondary to these.

### Resolution and pixel format: 1280x800 MJPEG

**PhotonVision defaults a new camera to 320x240 YUYV**, not to what the camera
can do. Set the resolution explicitly on every camera, every time one is added.

MJPEG is not a preference on a USB 2.0 camera, it is the difference between
working and not:

| Format at 1280x800 | Measured |
|---|---|
| MJPG | **121 fps** |
| YUYV | **5 fps** |

MJPEG is compressed on the camera; YUYV is raw and hits the USB 2.0 wall. MJPG
at 640x480 also measures 121 fps, so 120 fps is the camera's own ceiling, not
the bus's.

Verify what was actually negotiated rather than trusting the dropdown:

```bash
v4l2-ctl -d /dev/video0 --all | sed -n '/Format Video Capture/,/Bytes per Line/p'
python3 src/vd.py doctor      # CAN / ASKED / DELIVERED, and a warning below best mode
```

> **A calibration belongs to a resolution.** Intrinsics are in pixels and do not
> transfer between resolutions; PhotonVision keeps one calibration per
> resolution and shows which exist in a table. Change resolution and you need a
> calibration for the new one — otherwise PhotonVision silently falls back to
> estimating intrinsics from Maximum Diagonal FOV, which produces poses that
> look fine and are wrong. See [`CALIBRATION.md`](CALIBRATION.md).

### Exposure, and its units

**Exposure is in units of 100 µs.** The UI's `120` is **12 ms**. Get this wrong
by a factor of ten and nothing errors; the framerate just collapses.

### Auto-exposure off. Always.

Auto-exposure hunts, so frame time varies unpredictably, and in a dim room it
picks a long exposure and blurs every frame the moment the robot moves. Wave a
hand in front of the camera at long exposure and then at short exposure, and the
two images are the whole argument.

It also ruins a calibration, because there corner position *is* the measurement.

### PhotonVision owns these controls

Auto Exposure, Exposure, Brightness, Auto White Balance and White Balance
Temperature all live in PhotonVision's **Input** tab, and **PhotonVision
rewrites them whenever a pipeline changes or the service restarts.**

That is why `config/config.yaml` has no controls list. Duplicating them created
a second source of truth and a preflight failure that meant nothing. The
**PhotonVision settings export** is the committed record of these values
instead.

Consequences in daily use:

- `vd preflight`'s `controls` check is **expected to fail while PhotonVision is
  running**. It is informational unless PhotonVision is stopped.
- Setting exposure with `v4l2-ctl` while PhotonVision runs does nothing lasting.
  Change it in the UI.

### What is missing, and what is fiction

- **There is no gain control.** The OV9281 sensor has analog gain; this USB
  bridge does not expose it, and PhotonVision detects that itself
  (`Gain=false` in its camera quirks). `brightness` and `gamma` are digital
  operations on the bridge — they amplify noise, not signal. **Exposure is the
  only real lever, and its floor is set by the available light.**
- **Focus and colour controls are fiction.** The bridge advertises a stock
  webcam control set regardless of the sensor behind it, and this sensor is
  monochrome with a fixed lens. `focus_absolute`,
  `focus_automatic_continuous`, `saturation`, `hue` and
  `white_balance_temperature` are listed in `config.yaml` as `phantom_controls`
  so the tools report them as expected fictions rather than as problems.

---

## Exposure, framerate and light

This is the relationship to internalise, because it explains most "why is it
slow" questions:

```
max fps = 10000 / exposure_time_absolute        (the control is in 100 µs units)
```

The camera is held open for the exposure time, so a long exposure *is* the frame
period. A pipeline sitting at 8.0 fps with the control at `1245` is neither
CPU-bound nor broken: 10000 / 1245 = 8.03. It is being held open for an eighth
of a second per frame.

**So framerate here is set by exposure, and exposure is set by the available
light.** You do not choose a framerate directly. You find the shortest exposure
that still detects reliably, and the framerate is whatever that allows.

### Measured reference points

A static 36h11 tag at about 2 m, 1280x800, 3D and multi-tag on, decimation 1,
one overhead LED. Measured by `vd tune`, which records the exposure read back
from the device rather than the label typed by a human:

| exposure | fps | latency | detection | `yaw_sd` |
|---|---|---|---|---|
| 12 ms | 41.4 | 25 ms | **0%** | — |
| 20 ms | 49.4 | 36 ms | **0%** | — |
| 22 ms | 43.4 | 39 ms | **0%** | — |
| 24 ms | 41.1 | 41 ms | **0%** | — |
| **26 ms** | 38.0 | 44 ms | **100%** | 0.0037 |
| 28 ms | 35.0 | 46 ms | 100% | 0.0029 |
| 30 ms | 33.0 | 47 ms | 100% | 0.0024 |
| 32 ms | 31.0 | 50 ms | 100% | 0.0030 |
| 50 ms | 19.8 | 76 ms | 100% | 0.0018 |
| 80 ms | 12.0 | 113 ms | 100% | 0.0013 |
| 130 ms | 7.0 | 169 ms | 100% | 0.0017 |

Three numbers characterise this system:

- **CPU ceiling 41–43 fps** at 1280x800 with 3D and multi-tag on. Visible where
  exposure is not the constraint: 41.4 fps at 12 ms, where the exposure ceiling
  would have allowed 83. Note that framerate runs *higher* when nothing is being
  detected — about 42 fps with detection lost entirely — so a fast pipeline is
  not by itself good news.
- **Crossover to exposure-bound ≈ 23 ms.** Above that, framerate is
  `10000 / exposure_time_absolute` and no pipeline setting changes it.
- **36h11 detection floor ≈ 25 ms** in that room: 0% at 24 ms, 100% at 26 ms.

The floor is a property of the **light**, not of the camera, which is why
nothing in the config hardcodes it. Two reference points for the same room and
the same tag family, as the light changed:

| | detection floor | pipeline rate there |
|---|---|---|
| Daylight | ~250 (25 ms) | ~31 fps |
| Night, one overhead LED | ~820 (82 ms) | ~12 fps |

**Operating point for that room: 28–32 ms** — 100% detection at 31–35 fps with
about 47 ms latency. A reference, not a target.

### More exposure buys detection, not precision

`yaw_sd` on a static tag moves from 0.0037 to 0.0013 degrees across a fivefold
exposure range, and repeated runs of an unchanged configuration vary by about
0.001 degrees — so most of that spread is measurement noise. Past the detection
threshold, extra light does not make a stationary pose quieter. It buys margin,
not accuracy.

### Why short exposure matters anyway

Everything above is static, and a moving robot is a different requirement.
Detection holds to roughly **45 °/s at 12 ms**, which is about 8.6 px of smear
against a 10.2 px tag module — so the detector tolerates close to one full
module. A robot turns at **180–360 °/s**, and holding smear inside one module
there needs roughly **2.8 ms at 180 °/s and 1.4 ms at 360 °/s**.

Against a floor of 8–12 ms (measured on a 16h5 test tag in that room) that is a
**4–9x light deficit**, and no PhotonVision setting fixes it. The levers are
venue lighting, a faster lens, or larger tags — and a real field's tags are
larger than a 6 in test print, while a real hall is far brighter.

Also worth knowing: **36h11 needs about 2.5x more light than 16h5**, not the
~33% a module count suggests. A 12 ms floor on 16h5 became ~25 ms on 36h11. The
prediction was wrong and the measurement was the answer; measure rather than
predict.

### Decimation, and when not to bother

Decimation downsamples for detection and refines corners at full resolution — it
trades detector work for accuracy, so **it only pays when the detector is the
bottleneck.** In that room the detection floor (25 ms) sits about 2 ms above the
exposure crossover (23 ms), so every usable configuration is exposure-bound and
decimation has nothing to give back. `vd tune` refuses to run the sweep in that
state and prints `EXPOSURE-BOUND` rather than producing rows that all look the
same.

In a room bright enough to leave you CPU-bound, decimation is the setting that
buys back the gap between the CPU ceiling and what the camera can deliver.

### Resolution is a latency choice, not a framerate choice

Same exposure, same tag, same distance, one pipeline each:

| | 1280x800 | 640x400 |
|---|---|---|
| fps | 30.3 | 30.1 |
| latency | 50.8 ms | **38.5 ms** |
| `yaw_sd` | **0.0019** | 0.0040 |
| `area_sd` | **0.0005** | 0.0028 |

- **Framerate does not move.** Exposure is the constraint and resolution cannot
  change that.
- **Latency drops 12.3 ms, about 24%** — the processing cost of the extra
  pixels, and a real gain even when framerate is pinned.
- **Precision roughly halves.** Half the linear resolution is half the pixels
  across the tag, so corner noise roughly doubles.
- **Calibrate the new resolution before using it for real.** In that comparison
  640x400 had no calibration, so the 0.65° accuracy shift it showed was the
  uncalibrated fallback, not a resolution effect.

> **Switching pipelines over NetworkTables takes two topics, and getting it
> wrong looks like success.** Write the requested index to
> `pipelineIndexRequest` and read the actual one from `pipelineIndexState`. A
> plain `pipelineIndex` topic appears in older documentation, nothing consumes
> it, and reading it back returns your own value — indistinguishable from
> confirmation. Confirm a switch against something PhotonVision publishes, or
> against the camera's negotiated format, which knows nothing about
> NetworkTables and so cannot lie in the same direction.

---

## Stream resolution and bandwidth

**Set Stream Resolution low on every camera.** This is the most
competition-relevant setting in the UI, and it is easy to leave alone because
nothing looks wrong.

| | Stream bandwidth |
|---|---|
| Stream Resolution left at full size | **6.31 Mbps** |
| Stream Resolution at 320x200 | **0.35 Mbps** |

Eighteen times the traffic, for a picture a human glances at. **FRC has
historically capped total bandwidth near 4 Mbps**, so one careless camera can
consume the entire budget — and what it starves is everything else on the
network, including the dashboard you would use to diagnose it.

**Stream resolution does not affect detection.** The pipeline processes the
capture resolution; the stream is a separate, downscaled copy for viewing. There
is no accuracy cost to turning it down.

---

## Camera identity

### The nickname is the NetworkTables table name

A camera's nickname in PhotonVision **is** its subtable under
`/photonvision/<camera>/`. Every dashboard widget, every saved layout and every
`new PhotonCamera("...")` in robot code keys off that string.

So **name it by position, not by brand, and name it before anyone builds a
layout on it**: `front_left`, `rear`. Not `Thrifty:_`, not `Camera 1`. Renaming
later breaks every dashboard layout and every consumer of the data, which is a
bad thing to discover at an event.

The names under `cameras:` in `config/config.yaml` are these nicknames, and the
tools check them. Keep the two in step.

> **A renamed camera leaves its old table published but frozen** until
> PhotonVision restarts, so a stale table sits there looking live. `vd nt` flags
> it and ignores it.

### Cameras are identified by USB port, not by serial

Our two Thriftiest Cams report **byte-identical USB descriptors**:

```
Manufacturer: DCX-260513-K    Product: Thrifty    SerialNumber: 01.00.00
```

`DCX-260513-K` is a model or batch code, and the `SerialNumber` field is a
firmware version identical on every unit. So `/dev/v4l/by-id/` holds **one** link
for both cameras, and the second to enumerate overwrites the first. Plugging in
the second camera silently repointed `front_left` at it — and every check still
passed, because format, framerate and detections say nothing about *which*
sensor produced them.

Therefore:

- Cameras are identified by **`/dev/v4l/by-path/`**, which names the physical
  USB port. `config.yaml` uses it, and preflight fails when two configured
  cameras resolve to the same device.
- **"`front_left` is whatever is plugged into this port" is the actual rule**,
  which makes the Pi's USB ports part of the robot's configuration. **Label the
  ports.**
- Each camera gets a **team-assigned label written on it in marker** (`cam-a`,
  `cam-b`), recorded as `label` in `config.yaml`. Calibration filenames key to
  that label, because nothing in the descriptor is unique. A calibration you
  cannot trace to a specific physical camera is worthless.
- Quick check: `ls /dev/v4l/by-id/ | wc -l` should show two links per camera
  (capture plus metadata node). Fewer means a collision, and the tools will
  happily use the wrong camera.

### Two cameras

Measured against the same Pi an hour earlier with one camera:

| | one camera | two |
|---|---|---|
| `power_w` | ~2.5 | **~7** |
| `temp_c` | ~48 | **~58** |
| `cpu_mhz` | 1700 | **2400** (full boost) |
| `throttled_raw` | 0x0 | **0x0** |

About 3 W and 10 °C more, CPU pinned at full clock, supply holding with no
throttle bits. That is PhotonVision's "two AprilTag streams at 1280x800 on a
Pi 5" claim confirmed on our hardware. 58 °C is comfortable against the Pi 5's
~80 °C throttle point — but **an enclosure removes that headroom quickly**, so
re-check temperature once the camera and Pi are in a box on a robot.

Each camera needs its **own** calibration and its own transform. They are not
interchangeable.

---

## Exporting and committing the settings

**PhotonVision settings are changed in the UI, not in code — but they must be
captured.** An unexported PhotonVision config is one SD card failure away from a
lost weekend of tuning, and the card *will* fail eventually. Plan for it as a
certainty.

After every meaningful change:

1. Make the change in the UI.
2. **Export the settings** from the UI.
3. Commit the export to this repo with a dated filename.
4. Say in the commit message **what changed and why.** "Exposure 300" is not
   useful in March; "exposure 300 — gym lighting, detection floor measured at
   260" is.

Also commit:

- The **calibration** for each camera and each resolution, named after the
  camera's physical label and the date.
- The **AprilTag field layout**, if a custom one is in use. Import it with
  **Settings → Device Control → Import Settings → AprilTag Layout**.
- A **`vd snapshot`** taken while things are known good. It records over 100
  fields — kernel and package versions, service states, the USB device, the
  negotiated format, every v4l2 control, the calibration's focal length, and
  hashes of PhotonVision's settings directory
  (`/opt/photonvision/photonvision_config`). It is the only thing that can
  answer "it worked yesterday, what changed?" Run it with `sudo` so it can read
  PhotonVision's settings. `photon.sqlite` changes continuously, so a diff taken
  minutes apart is rarely empty.

**The export is also your rollback.** If tuning goes sideways, re-import the last
known good export rather than trying to remember what you changed. At an event
that is the difference between playing a match and scratching one.

> After any import, restart the service and re-check resolution and exposure. An
> import is exactly the kind of event that rewrites the controls PhotonVision
> owns.

---

## Quick reference

| | |
|---|---|
| Service | `photonvision` — `systemctl status`, `journalctl -u photonvision -f` |
| Web UI | `http://<ip>:5800` — **by IP, not `.local`** |
| Streams | `1181` raw, `1182` processed; one pair per camera |
| Settings on disk | `/opt/photonvision/photonvision_config` |
| Format to set | **1280x800 MJPG** (it defaults to 320x240 YUYV) |
| Exposure units | **100 µs** — UI `120` = 12 ms |
| Framerate rule | `max fps = 10000 / exposure_time_absolute` |
| Auto-exposure | **off**, always |
| Stream Resolution | **low** — 320x200; no effect on detection |
| Nickname | position, not brand; it is the NetworkTables table name |
| Identity | `by_path` (USB port), plus a label written on the camera |

**When something is wrong, in order:** `vd preflight` for a verdict across every
layer, `vd doctor` if there is no image anywhere, `vd nt` if the image is fine
but nothing is published, `vd snapshot --diff` if it worked yesterday. Then
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — and append to it when you solve
something new.

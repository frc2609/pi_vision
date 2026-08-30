# camera

Camera-side tooling for the vision coprocessor: diagnostics at every layer,
calibration artefacts, and PhotonVision configuration.

> **No remote yet.** The [operating standards](../docs/operating_standards.md)
> say each component under `vision/` gets its own repository *and its own
> GitHub remote*. This is a local git repo — `camera/` is gitignored by the
> `vision/` repo so its contents stay out of that history — but **until a remote
> exists, one dead SD card or laptop still loses everything.**

## The idea

Every layer of the system should be able to say where a problem is. The health
monitor covers the Pi. These cover everything above it:

| Layer | Tool | Answers |
|---|---|---|
| Pi power and thermals | `monitor/` (separate repo) | is the Pi healthy |
| Camera hardware | `vd doctor` | is the camera delivering what we asked for |
| Detection output | `vd nt` | is PhotonVision publishing anything useful |
| Whole chain | `vd preflight` | is it ready, right now, yes or no |
| Change over time | `vd snapshot --diff` | what is different from when it worked |
| Choosing settings | `vd tune` | measurement instead of opinion |

Every command is config-driven, exits non-zero on trouble, and fails soft: a probe
that cannot run reports `--` and says why, rather than raising.

**[`docs/RUNNING.md`](docs/RUNNING.md) is the one to read** — how to run each
tool and, more importantly, how to read what it prints. Several of the outputs
are easy to misread in ways that send you looking in the wrong place, and it
says which.

## Layout

```
camera/
├── README.md
├── requirements.txt
├── config/
│   ├── config.yaml               device, expected format, controls, NT mode
│   ├── field_layout_corner.json  two-tag mini-field for multi-tag testing
│   └── photonvision-calibration-*.json
├── docs/
│   └── RUNNING.md                how to run vd and read its output
├── scripts/
│   └── install_service.sh        idempotent installer for the settings unit
├── services/
│   └── vision_preflight.service  publishes the go/no-go to NetworkTables
├── snapshots/                    dated config captures - commit the good ones
├── tests/                        parser tests; run anywhere, no Pi needed
└── src/
    ├── vd.py                     the CLI - all presentation lives here
    └── vision/                   library - reads and writes state, never prints
        ├── util.py               shell, config, device resolution
        ├── camera.py             v4l2: parsers, formats, controls, exposure, probe
        ├── nt.py                 NetworkTables: connect, discover, sample rates
        ├── state.py              one facts dict; snapshots and diffs
        ├── checks.py             preflight checks over the facts
        ├── tune.py               measurement and sweep statistics
        ├── stream.py             annotated MJPEG view
        └── field.py              corner field-layout geometry
```

**One state model, several views.** `state.facts()` gathers what is true once
per camera; the human report, `--json`, the saved snapshot and the preflight
checks are all views onto it rather than four implementations of "look at the
camera". Parsers take text and return data, which is what makes `tests/`
possible without a Pi.

**Any number of cameras.** `config.yaml` holds a `cameras:` list with shared
`camera_defaults`. Every command works on all of them by default and takes
`--camera <name>` to narrow. Per-camera checks publish under the camera's name,
so a two-camera robot gets two sets of dashboard lights and one `ready` that
ANDs everything.

## Setup

```bash
sudo apt install -y python3-opencv python3-yaml v4l-utils
```

That is enough for everything except the NetworkTables tools. Those need
`pyntcore`, which is pip-only because robotpy is not packaged in Debian — the
single reason this project has venvs at all:

```bash
python3 -m venv --system-site-packages .venv   # so apt's OpenCV stays visible
.venv/bin/pip install -r requirements.txt
```

**OpenCV must come from apt.** The apt build includes contrib, and `cv2.aruco`
lives in contrib. See [operating standards §4](../docs/operating_standards.md).

## `vd preflight` — start here

```bash
python3 src/vd.py preflight; echo "exit $?"
```

Checks power, camera presence and format, v4l2 controls, calibration, the
PhotonVision service, and whether anything is actually publishing to
NetworkTables. Exit code is the number of failed checks, so
`vd preflight && echo ready` works in a script.

A `SKIP` is never a failure — it means the check could not run and says why.

**For the driver station:** `vd preflight --publish` publishes the verdict to
`/Vision/preflight` as booleans and numbers, so [Elastic](https://frc-elastic.gitbook.io/docs)
gets a go/no-go light and per-check indicators. Install
`services/vision_preflight.service` to run it continuously. It is a long-lived
publisher on purpose: NT4 unpublishes a client's topics when it disconnects, so
if it dies the lights **vanish** rather than freezing green.

## `vd doctor`

Answers "is the camera doing what I think it is", which is a different question
from the monitor's "is the Pi healthy".

```bash
sudo python3 src/vd.py doctor          # sudo to see which process holds the camera
python3 src/vd.py doctor --json        # machine-readable
python3 src/vd.py stream               # annotated live view on :8090
```

The report is built around three columns that should agree and often do not:

| | Source |
|---|---|
| **CAN** | what the hardware advertises, from v4l2 format enumeration |
| **ASKED** | what the current holder requested, from the negotiated format |
| **DELIVERED** | what actually arrives, measured — only when the device is free |

That gap is where the problems live. PhotonVision silently defaulting to
320x240 YUYV on a camera capable of 1280x800 MJPEG at 120fps was invisible in
its UI and obvious here.

It also prints **WHERE TO LOOK**: the PhotonVision UI and stream URLs by IP
(Chrome blocks `.local` sub-resources from a plain-HTTP page, so the embedded
stream stays blank over mDNS), log commands for both services, and the live
camera format one-liner.

**Only one process can hold a V4L2 capture device.** When PhotonVision is
running it owns the camera, so the measured column is skipped, the holding
process is named, and you are pointed at PhotonVision's own stream instead. To
use `vd stream`, stop PhotonVision first.

Exit codes: `0` at its best mode, `1` running below what the hardware
advertises, `2` no device.

## `vd nt`

The layer nothing else watches. The monitor says the service is alive;
`vd doctor` says frames are arriving; neither says whether detections are
reaching the network.

```bash
.venv/bin/python src/vd.py nt            # 3s sample
.venv/bin/python src/vd.py nt --all      # every topic, not just known ones
```

It enumerates whatever PhotonVision publishes and measures each topic's update
rate, rather than assuming a schema — the NT layout has changed between seasons
and the published docs lag the release, so a renamed topic shows up as a new
name instead of as silence.

With no roboRIO, turn on **Run NetworkTables Server** in PhotonVision's settings
and leave `config.yaml` on `client_address` / `127.0.0.1`. Once a roboRIO
exists, the roboRIO is the server: switch to `client_team`.

Exit codes: `0` publishing, `1` connected but nothing updating, `2` no
connection.

## `vd snapshot`

Answers **"it worked yesterday — what changed?"**, which is the hardest question
to answer from memory, because the things that change are the things nobody
thinks of as state.

```bash
sudo python3 src/vd.py snapshot          # capture (sudo to read PV's settings)
python3 src/vd.py snapshot --diff        # newest two
python3 src/vd.py snapshot --list
```

Captures kernel and package versions, service states, the USB device, the full
v4l2 capability list, the current format, every v4l2 control, the calibration
hash and focal length, and hashes of PhotonVision's settings directory.

Take one when things are known good and **commit it**. A snapshot from the last
event is worth more than any amount of remembering.

## Known about this camera

Findings from stages 1–4, recorded so nobody re-derives them:

- **Thriftiest Cam** — OV9281, 1MP mono global shutter, 1280x800 MJPEG at a
  measured 121 fps, USB 2.0, serial `DCX-260513-K`, HFOV 82°.
- **YUYV is unusable at full resolution** — 5 fps against MJPEG's 121. That is
  the USB 2.0 wall.
- **There is no gain control.** The OV9281 has analog gain; this USB bridge does
  not expose it. `brightness` and `gamma` are digital and amplify noise rather
  than signal, so exposure is the only real lever, and its floor is set by the
  ambient light.
- **The focus and colour controls are fiction.** The Sunplus bridge advertises a
  stock webcam control set regardless of the sensor behind it. Monochrome
  sensor, fixed lens, no focus motor. `config.yaml` lists them under
  `phantom_controls`.
- **Robot-speed detection needs ~1.4–2.8 ms of exposure**; one overhead LED in a
  room floors out at 8–12 ms. See [the roadmap](../docs/learning_roadmap.md)
  stage 2.

## What still belongs here

- The calibration, named with serial and date — currently only at
  `~/vision/calib/thriftiest-DCX-260513-K-2026-08-24.json` on the Pi
- The PhotonVision settings export, re-exported after every meaningful change
- The stage 2 measurement scripts (framerate, format comparison, exposure sweep)
- Recorded test footage, once there is any

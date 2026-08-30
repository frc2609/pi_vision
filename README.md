# camera

Camera-side tooling for the vision coprocessor: diagnostics at every layer,
calibration artefacts, and PhotonVision configuration.

> **Not yet a git repo.** The [operating standards](../docs/operating_standards.md)
> say each component under `vision/` gets its own repository and remote. This
> directory is gitignored by the `vision/` repo so it can be initialised later
> without its contents being swallowed into that history. Until someone runs
> `git init` and adds a remote, **nothing in here is backed up.**

## The idea

Every layer of the system should be able to say where a problem is. The health
monitor covers the Pi. These cover everything above it:

| Layer | Tool | Answers |
|---|---|---|
| Pi power and thermals | `monitor/` (separate repo) | is the Pi healthy |
| Camera hardware | `camera_doctor.py` | is the camera delivering what we asked for |
| Detection output | `pipeline_doctor.py` | is PhotonVision publishing anything useful |
| Whole chain | `preflight.py` | is it ready, right now, yes or no |
| Change over time | `config_snapshot.py` | what is different from when it worked |

Every tool is config-driven, exits non-zero on trouble, and fails soft: a probe
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
│   ├── config.yaml               device paths, expected format, controls, NT mode
│   └── field_layout_corner.json  two-tag mini-field for multi-tag testing
├── docs/
│   └── RUNNING.md                how to run the tools and read their output
├── scripts/
│   └── install_service.sh        idempotent installer for the settings unit
├── services/
│   └── camera_settings.service   reapplies v4l2 controls at boot
├── snapshots/                    dated config captures - commit the good ones
└── src/
    ├── common.py                 shared helpers; the one place a failure becomes None
    ├── camera_doctor.py          camera diagnostics and a live annotated stream
    ├── pipeline_doctor.py        NetworkTables diagnostics
    ├── config_snapshot.py        capture and diff the whole configuration
    ├── apply_camera_settings.py  apply the controls from config, then verify
    ├── make_field_layout.py      generate a two-tag corner field layout
    └── preflight.py              one pass/fail for the whole chain
```

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

## `preflight.py` — start here

```bash
python3 src/preflight.py; echo "exit $?"
```

Checks power, camera presence and format, v4l2 controls, calibration, the
PhotonVision service, and whether anything is actually publishing to
NetworkTables. Exit code is the number of failed checks, so
`preflight.py && echo ready` works in a script.

A `SKIP` is never a failure — it means the check could not run and says why.

## `camera_doctor.py`

Answers "is the camera doing what I think it is", which is a different question
from the monitor's "is the Pi healthy".

```bash
sudo python3 src/camera_doctor.py          # sudo to see which process holds the camera
python3 src/camera_doctor.py --json        # machine-readable
python3 src/camera_doctor.py --stream      # annotated live view on :8090
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
use `--stream`, stop PhotonVision first.

Exit codes: `0` at its best mode, `1` running below what the hardware
advertises, `2` no device.

## `pipeline_doctor.py`

The layer nothing else watches. The monitor says the service is alive;
`camera_doctor` says frames are arriving; neither says whether detections are
reaching the network.

```bash
.venv/bin/python src/pipeline_doctor.py            # 3s sample
.venv/bin/python src/pipeline_doctor.py --all      # every topic, not just known ones
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

## `config_snapshot.py`

Answers **"it worked yesterday — what changed?"**, which is the hardest question
to answer from memory, because the things that change are the things nobody
thinks of as state.

```bash
sudo python3 src/config_snapshot.py          # capture (sudo to read PV's settings)
python3 src/config_snapshot.py --diff        # newest two
python3 src/config_snapshot.py --list
```

Captures kernel and package versions, service states, the USB device, the full
v4l2 capability list, the current format, every v4l2 control, the calibration
hash and focal length, and hashes of PhotonVision's settings directory.

Take one when things are known good and **commit it**. A snapshot from the last
event is worth more than any amount of remembering.

## `apply_camera_settings.py` and the systemd unit

**v4l2 controls do not survive a reboot or a replug.** They live on the device,
not on disk, and nothing announces that they have reverted to default.

```bash
python3 src/apply_camera_settings.py     # apply and verify now
./scripts/install_service.sh             # apply at every boot
```

Two things make this less trivial than it looks. **Order matters** —
`exposure_time_absolute` is inactive while `auto_exposure` is in an automatic
mode, and a write to an inactive control is accepted and discarded, so manual
mode is set first. And **a successful write proves nothing** — v4l2 reports
success while clamping or ignoring the value, so every control is read back and
compared.

> **Honest limitation.** PhotonVision imposes its own pipeline settings when it
> opens the camera. These values govern the window before PhotonVision starts
> and any tool that uses the camera directly. Where PhotonVision owns the
> camera, set exposure in its UI — this script cannot win that argument and does
> not try.

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

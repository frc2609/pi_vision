# Calibrating a camera in PhotonVision

Follow this top to bottom at a bench. You do not need to read anything else first.

Calibration is what turns pixels into distances. Until a camera is calibrated,
PhotonVision does not know how wide its lens is, so it cannot say how far away a
tag is. **Get it wrong and PhotonVision still reports poses — they are just wrong,
by a constant factor, with nothing anywhere to warn you.** No error, no red light,
no failing check. That is why the order of the steps below matters.

*The numbers here are from the first successful calibration of a Thriftiest Cam on
`2609-pi-1`, 2026-08-29. It took roughly a dozen failed attempts to get there, and
every warning in this document is something that actually went wrong.*

---

## Before you start

| You need | Notes |
|---|---|
| The camera, plugged into the Pi | And **a label written on it in marker** — `cam-a`, `cam-b`. You cannot finish without one; see [After it succeeds](#after-it-succeeds). |
| A printed ChArUco board | Ours is labelled `13x19`. Laminated or mounted flat — a bent board calibrates the bend into the camera. |
| Calipers | To measure the board. Not optional; see [Step 4](#step-4--measure-the-board-with-calipers). |
| A laptop on the same network | The PhotonVision UI is at `http://2609-pi-1.local:5800`. |
| 20–30 minutes, undisturbed | Capturing is the slow part, and rushing it is what produces a bad set. |

Allow a full session. A calibration you hurried the night before an event is worse
than no calibration, because it will pass every check you have.

---

## Step 1 — Find the camera's diagonal field of view

You need this number before you open the calibration tab, because Step 2 puts it
into the solver as its starting guess.

**For a Thriftiest Cam it is `91.6` degrees** (82° horizontal). If that is the
camera you have, write it down and go to Step 2.

**For any other camera**, in order of preference:

1. A value from a previous calibration of the same camera model.
2. The vendor's lens specification.
3. Measure it. Put an object of known width at a tape-measured distance, note how
   many pixels wide it appears in the stream, then:

   ```
   focal_px = pixels x distance / object_width          (any consistent units)
   diag_fov = 2 x atan( sqrt(w^2 + h^2) / (2 x focal_px) )
   ```

   where `w` and `h` are the image width and height in pixels.

Do not guess, and do not leave the default in place. An estimate within a few
degrees works; the default is not within a few degrees.

---

## Step 2 — Set Maximum Diagonal FOV. This is not cosmetic.

**Cameras → Camera Settings → Maximum Diagonal FOV.** The default is `70`.
Change it to the value from Step 1 **before** you capture anything.

This field is the **initial guess the solver starts from**. PhotonVision's
`Calibrate3dPipe` seeds from `imageProps.horizontalFocalLength`, which
`FrameStaticProperties` computes from the diagonal FOV whenever no calibration
exists yet for that resolution:

```java
horizontalFocalLength = (imageWidth / 2.0) / Math.tan(horizFOV / 2.0);
```

At 1280x800 the image diagonal is sqrt(1280² + 800²) = 1509 px, so:

| Setting | Seed focal length | Error vs true (734 px) |
|---|---|---|
| `70` (default) | 1078 px | **+47%** |
| `91.6` (measured, Thriftiest Cam) | 734 px | correct |

Every one of our dozen failed attempts started the optimiser 47% away from the
answer. On a flat target that is enough to converge into a **mirrored** solution —
a mathematically valid fit with the geometry inverted, which comes back with
negative focal lengths and a nonsense field of view. PhotonVision saves and
displays that result as a success. With the seed corrected it worked on the first
try.

---

## Step 3 — Set the resolution, and turn Auto Exposure off

**Calibrate at the resolution you will actually run.** Intrinsics are measured in
pixels and **do not transfer between resolutions**. PhotonVision keeps a separate
calibration per resolution and lists them in a table. Ours is **1280x800**, which
is what the AprilTag pipeline runs.

**Turn Auto Exposure off** in the Input tab and set a fixed exposure bright enough
to read the board clearly. Then leave it alone for the entire session.

Auto exposure picks a long exposure in a dim room, and every hand-held frame is
then motion-blurred. The position of each corner *is* the measurement here, so
blur goes straight into your intrinsics — and nothing downstream can tell that it
happened.

> Exposure in the PhotonVision UI is in units of 100 microseconds, so `120` is
> 12 ms. Longer is brighter but blurs more.

---

## Step 4 — Measure the board with calipers

Measure one chessboard square and one ArUco marker with calipers rather than
trusting the printed label.

Printed and laminated boards come out a percent or two off the nominal size, and
that percent lands directly on every distance the robot ever reports. It does
**not** affect focal length or field of view — those are scale-invariant — only
distances. A 2% error here is a 2% error on every tag range, forever, and it looks
completely normal.

Our board measured 19 mm squares with 14 mm markers inside them.

---

## Step 5 — Enter the board settings

**The width/height convention catches everyone.** PhotonVision's own documentation
says it plainly:

> if your target says `9x12 | Checker Size: 30 mm | Marker Size: 22 mm`, you would
> have to set the board width to **12** and the board height to **9**

The board's printed label is **rows × columns**; PhotonVision wants **columns
first**. Ours is labelled `13x19`, so it is entered as width 19, height 13.

| Field | Our value | Note |
|---|---|---|
| Resolution | 1280x800 | the resolution you will run |
| Board Type | ChArUco | recommended over a plain chessboard |
| Tag Family | `Dict_4X4_1000` | a `DICT_4X4_250` board works — the smaller dictionaries are prefix subsets of the larger ones, verified with `np.array_equal` |
| Pattern Spacing (in) | `0.748` | 19 mm — the **chessboard square** |
| Marker Size (in) | `0.5512` | 14 mm — the ArUco square inside it |
| Board Width (squares) | **19** | columns, the long edge |
| Board Height (squares) | **13** | rows |
| Old OpenCV Pattern | off | on only for boards generated before OpenCV 4.6 |
| Draw Collected Corners | on | you need to see the overlay while capturing |

Transposed width and height do not produce an obvious error. The markers still
detect — marker detection never consults the board layout — but no chessboard
corner can be interpolated from them, so the solve is built on wrong 3D points or
fails outright.

---

## Step 6 — Capture

**The camera stays still. You move the board.** Both must be still at the moment
you press capture.

- **The whole board must be visible in every snapshot.** Unlike a raw OpenCV
  workflow, do not deliberately crop it.
- **Tilt 30–45° in several directions** — left, right, top-away, bottom-away, and a
  couple rolled diagonally. Flat-on views alone are a degenerate case: the solver
  cannot distinguish "tilted toward you" from "tilted away", and that ambiguity is
  exactly what produces a mirrored solve.
- **Vary distance.** Some snapshots with the board filling the frame, some with it
  at a quarter of the frame.
- **Cover the periphery.** The distortion coefficients are fit from the edges of
  the image, so a set clustered in the middle leaves them extrapolated — guessed,
  in the part of the image where distortion is strongest. To reach the frame
  corners without cropping the board, back the board away until it occupies about
  a quarter of the frame, then place it in each corner and each edge midpoint.
- **20+ snapshots.** Diverse beats numerous.

Watch the accumulated-corners overlay as you go. **A dense blob in the centre with
empty frame corners is the common failure**, and it is the one that quietly ruins
the distortion model while the error number still looks fine.

---

## Step 7 — Judge the result

| | Target | Ours |
|---|---|---|
| Mean Error | **< 1 px**, ideally < 0.5 | 0.46 px |
| Horizontal FOV | matches your lens | 81.98° |
| Vertical FOV | smaller than horizontal | 56.99° |
| Diagonal FOV | | 91.39° |

**The instant tell for a failed solve: vertical FOV larger than horizontal.** That
is geometrically impossible on a 1280x800 sensor with square pixels — the sensor is
wider than it is tall — so any such result is garbage regardless of what the error
column says. We saw 218°/319° reported as a success.

**A low error alone does not mean a good calibration.** Mean error measures how
well the model fits the data it was fitted to; a bad set can fit itself
beautifully. The checks that actually mean something are external:

- **Calibrate twice** from different capture sets and compare the numbers.
- **Compare against an independent estimate.** Our manual OpenCV calibration gave
  82.20° horizontal against PhotonVision's 81.98° — **0.27% apart**, from different
  solvers and different capture sets. That agreement is worth more than either
  number on its own.
- **Measure a known distance.** Better still, measure the *difference* between two
  positions: the pose origin is the optical centre inside the lens barrel, which no
  tape measure can reach, and taking a delta cancels that unknown offset so only
  scale error survives.

If the result passes, go to [After it succeeds](#after-it-succeeds).

---

## If it fails

The UI only ever says *"Camera calibration failed! Make sure that the photos are
taken such that the rainbow grid circles align with the corners of the
chessboard."* That message is the same for every cause. The real reason is in the
log, on the Pi:

```bash
journalctl -u photonvision --no-pager -n 400 | grep -iE "calib|charuco|corner|mrcal|outlier"
```

What we saw, and what each meant:

| Symptom in the log | Meaning |
|---|---|
| `Threw out some outliers. New count = 3904/11232 (34.8%)` | mrcal rejected 65% of the observations. The model does not fit the data — usually a board-definition or seeding problem, **not** bad photos. |
| `Negative corner in reprojection error calc! Measured: {-410.7, 551.8}` | A corner projected off the image entirely. The correspondences are wrong. |
| A reported FOV of 186°, 218° or 319° | The solve diverged. Check the intrinsics — ours came back with **negative focal lengths** (`fx = -224`, `fy = -1071`), a mirrored solution. PhotonVision saves and displays these rather than rejecting them. |

**Work through the causes in this order**, roughly most to least likely:

1. **Maximum Diagonal FOV still at 70.** By far the most common — Step 2.
2. **Board width and height transposed** — Step 5.
3. **Board measurements wrong** — Step 4.
4. **Capture set clustered in the centre, or all flat-on** — Step 6.

### Proving which one it is

Download the calibration JSON from *View calibration information*. It contains
every observation, so the correspondences can be tested directly. A homography is
the most permissive flat-target model there is — eight degrees of freedom, ignoring
the camera intrinsics entirely — so if it cannot explain the point pairs, the
pairing itself is wrong:

```python
import json, numpy as np, cv2
d = json.load(open("photon_calibration_....json"))
o = d["observations"][0]
obj = np.array([[p["x"], p["y"], p["z"]] for p in o["locationInObjectSpace"]])
img = np.array([[p["x"], p["y"]] for p in o["locationInImageSpace"]])
H, mask = cv2.findHomography(obj[:, :2], img, cv2.RANSAC, 3.0)
print("homography inliers", int(mask.sum()), "of", len(obj))
```

- **Most points inlying** — the correspondences are fine, so the problem is the
  solve or the seed. Go back to Step 2.
- **A small fraction** — we got **44 of 149** — the board dimensions are wrong,
  most likely width and height swapped. Go back to Step 5.

The same file also states what board PhotonVision thought it had: `calobjectSize`
is the **interior corner** count, which is one less than the number of squares in
each direction, and `calobjectSpacing` is the square size in metres.

### Last resort: import a calibration instead

PhotonVision can upload a previously downloaded calibration. This is a legitimate
escape hatch when the UI will not converge and you are out of time. Download any
existing calibration to get the shape of the file, then replace two fields:

- `cameraIntrinsics.data` — row-major `[fx, 0, cx, 0, fy, cy, 0, 0, 1]`
- `distCoeffs.data` — **eight** coefficients, `k1 k2 p1 p2 k3 k4 k5 k6`. An OpenCV
  five-coefficient calibration maps in directly with `k4 k5 k6` zeroed.

Also set `calobjectWarp` to `[0, 0]` and empty the `observations` list, or the UI
keeps displaying the mean error from the failed solve.

This works — PhotonVision uses `cameraIntrinsics` and `distCoeffs` at runtime and
the observations are only for display. Prefer a native calibration anyway: it is
one less thing to explain to the next person, and once the FOV seed is right it
converges.

---

## After it succeeds

A calibration is not finished when the UI goes green. It is finished when it is
committed, named after a specific physical camera, and wired into the config.

### 1. Export and download

Export the PhotonVision settings from the UI, and download the calibration file.
**An unexported PhotonVision config is one SD card failure away from a lost
weekend.** This is what `vision/camera/` exists for.

### 2. Name the file after the camera's physical label — not its serial

**These cameras do not have a usable serial number.** Every Thriftiest Cam reports
byte-identical USB descriptors:

```
Product:       Thrifty
Manufacturer:  DCX-260513-K
SerialNumber:  01.00.00
```

`DCX-260513-K` is a model or batch code sitting in the *manufacturer* field, and
the `SerialNumber` field is a firmware version. Both are identical on every unit.
Because `/dev/v4l/by-id/` names are built from
`<manufacturer>_<product>_<serial>`, **two of these cameras collide on a single
`by-id` link** — the second to enumerate overwrites the first's symlink, and
everything still appears to work while pointing at the wrong camera.

So the identity of a camera here is a **label you assign and write on the camera in
marker** — `cam-a`, `cam-b` — recorded as `label` in
[`config/config.yaml`](../config/config.yaml). Name the calibration file after that
label and the date:

```
thriftiest-cam-a-2026-08-24.json
```

A calibration you cannot trace to one specific physical camera is worthless, and
with these cameras nothing but that label traces it.

> Older files in `config/` carry `DCX-260513-K` in the name, from before this was
> understood. That string identifies the model, not the unit. Do not copy the
> pattern for new files.

### 3. Commit it

Put the file in `vision/camera/config/` and commit it alongside the settings
export, saying in the commit message which camera it is and what changed.

### 4. Point the config at it

In [`config/config.yaml`](../config/config.yaml), set `calibration.file` for that
camera to the filename you just committed:

```yaml
  - name: rear
    label: cam-b
    calibration:
      file: thriftiest-cam-b-<date>.json
      expect_hfov_deg: 82.0
      tolerance_deg: 2.0
```

`calibration.file` is a bare filename. The tools look for it in the repo root, in
`config/`, and in `~/vision/calib/` on the Pi.

### 5. Verify

```bash
.venv/bin/python src/vd.py preflight --camera rear
```

The `calibration` check loads the file, derives horizontal FOV from `fx`, and
compares it against `expect_hfov_deg` within `tolerance_deg`. It fails on a
negative or zero `fx`, which is the diverged-solve signature from
[Step 7](#step-7--judge-the-result). See [`RUNNING.md`](RUNNING.md) for the rest of
the output.

---

## When to calibrate again

- The lens is disturbed — bumped, refocused, unscrewed, or the mount is strained.
- The camera is swapped for a different physical unit, **including another one of
  the same model**. Intrinsics belong to one sensor and one lens, not to a part
  number.
- The resolution changes. Intrinsics are in pixels and do not transfer.

---

Background on how all of this was found: the `by-id` collision and
diverged-solve entries in
[`TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md), and the narrative version of
this procedure in
[`photonvision_calibration.md`](../../docs/photonvision_calibration.md).

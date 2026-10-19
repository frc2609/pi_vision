# Troubleshooting Log

*A running log of problems we hit and how we solved them. Append as you go.*

**Scope:** the whole Pi platform, not just the health monitor — imaging, boot, power,
SSH, services. It lives in this repo because this is the only repo that exists yet.
Promote it to a shared docs repo when there is one.

---

## How to use this

**Add an entry when you lose more than fifteen minutes to something.** If it cost you
time, it will cost the next person time too.

Write it while it's fresh. An entry written the next morning is missing the details
that actually mattered, and an entry you meant to write is worth nothing.

**Newest entries go at the top**, directly under the Entries heading.

Don't polish. A rough entry that exists beats a good one that doesn't. Nobody is
grading the prose. If you only have two minutes, fill in **Symptom** and **Fix** and
move on.

---

## The heuristic that keeps proving right

**The error message names the last thing that failed, not the first thing that's wrong.**

We learned this the hard way during initial imaging. "Media error detected" was
antivirus. "Waiting for network" was a blank SD card. "Connection refused" was a
service that had never been enabled. In every case the message pointed at a symptom
several steps downstream of the cause.

Two questions that repeatedly cut through it:

**Does the failure point move?** A process that fails at the same place every time is
hitting a defect at a fixed location. One that fails somewhere different each time is
hitting a connection, a timeout, or something racing. This single question separates
bad hardware from flaky connections faster than anything else.

**What changed?** Not "what's broken" — what *changed* since it last worked. Usually
the answer is right there and boring.

And before deep-diving software: **physically reseat the cable.** It's cheap to check
and embarrassing to discover late. It has already cost us an evening once.

---

## First checks, before you believe anything else

### Power

```bash
vcgencmd get_throttled
```

`0x0` is the only clean answer. Anything else, read the bits — and remember **bits
16–19 are sticky history that survives a soft reboot**, so they may be describing a
problem you already fixed. Bits 0–3 are live state. See the README for the full table.

The health monitor separates these for you and additionally reports
`undervoltage_since_start` / `throttled_since_start` — what has gone wrong *since the
service started watching*, which is the number that means something during a match.

```bash
python3 src/health_monitor.py --once     # safe on a running Pi
journalctl -u health_monitor -b          # what it saw since boot
```

### Which OpenCV is actually loaded?

If OpenCV behaves in a way the documentation says it shouldn't, check which build
Python is importing before debugging anything else:

```bash
python3 -c "import cv2; print(cv2.__version__); print(cv2.__file__)"
```

`dist-packages` means apt (what we want — 4.10.x, contrib included). `site-packages`
means pip. If both are installed, Python silently takes whichever appears first on
`sys.path`, and you can spend a long time reading docs for a version you aren't
running. `cv2.aruco` missing is the usual symptom.

---

## Entry template

Copy this block for a new entry.

```markdown
## YYYY-MM-DD — Short description

**Symptom:** What was actually observed. Exact error text if there was one.

**What we thought it was:** The wrong guesses, in order. This field matters — it
stops the next person walking the same dead ends.

**Root cause:** What it actually was.

**Fix:** What resolved it. Commands, settings, part numbers.

**Recognising it next time:** The one check that would have identified this in
two minutes instead of two hours.
```

---
---

# Entries

## 2026-08-30 — Vision pose is correct but arrives in the wrong instant (clock sync)

> **Written ahead of the fact, which is unusual for this log.** Nothing here has
> happened yet - there is no roboRIO to make it happen. It is written down
> because the symptom does not look like a clock problem, and the entry is
> worthless if it is only written after two hours are gone.

**Symptom (expected):** Pose is dead accurate with the robot stationary. Under
motion the estimate smears, lags, or fights the odometry - worse the faster you
drive. Nothing errors. `hasTarget` is true, framerate is fine, calibration is
fine, and every preflight check is green.

**What it will look like it is:** A camera problem. Motion blur, exposure too
long, framerate too low, a bad calibration. All of those produce a similar
smear and all of them are the wrong tree.

**Root cause:** The capture timestamp is not in the roboRIO's timebase, so
`addVisionMeasurement` rewinds odometry to the wrong moment and fuses a correct
pose into the wrong instant of history. At rest, history is flat and it does not
matter. Under motion it is the whole error.

**The part that is counterintuitive** — verified 2026-08-30 against
PhotonVision's protocol spec and photonlib source:

- **The roboRIO is the time server. The Pi is the client.** Backwards from the
  obvious guess.
- **Nothing on the Pi enables it.** Robot code starts the server, from
  `TimeSyncSingleton.load()`, when it constructs a `PhotonCamera`. No
  `PhotonCamera` in robot code means no server, means no sync, silently.
- **It is not NetworkTables.** Separate protocol, UDP port 5810. NT is TCP
  5810/5811. A firewall or network rule that permits NT will happily drop this.
- The Pi applies the offset itself, so what lands on NT is already roboRIO time.

**Fix:** Construct a `PhotonCamera` in robot code. Then set
`preflight.require_timesync: true` in `camera/config/config.yaml` so the
condition is asserted rather than assumed.

**Recognising it next time:** `vd preflight` reports `timesync`. It reads
PhotonVision's own statistics from `/photonvision/.timesync/<hostname>` and
compares `ping_tx_count` against `pong_rx_count` - pings climbing with pongs at
zero means nothing is serving time. On a bench with no roboRIO that is correct
and it reports SKIP; with `require_timesync: true` it FAILs.

Caveat on what the check proves: pongs received means it synced *at some point*.
The counts are cumulative and the check takes one sample, so a link that died a
moment ago still reports a healthy total. The live signal is `timeSinceLastPong`
in the result metadata, which photonlib watches and warns on.

---

## 2026-08-29 — Two identical cameras, one `by-id` link, wrong camera silently selected

**Symptom:** Plugged in a second Thriftiest Cam. `/dev/v4l/by-id/` still held exactly one entry - and it now pointed at the *new* camera. Every preflight check still passed, because `front_left` was resolving to a different physical camera than the one it was calibrated for.

**What we thought it was:** The second camera failing to enumerate. It had enumerated fine; `lsusb` showed both.

**Root cause:** Both cameras report byte-identical USB descriptors:

```
Product:       Thrifty
Manufacturer:  DCX-260513-K
SerialNumber:  01.00.00
```

`by-id` names are built from `<manufacturer>_<product>_<serial>`, so both produce the same filename and the second to enumerate overwrites the first's symlink. What we had been calling "the serial" — `DCX-260513-K` — is the **manufacturer** string, a model or batch code. The actual `SerialNumber` field is a firmware version, identical on every unit.

**Fix:** Identify cameras by **`/dev/v4l/by-path/`**, which names the physical USB port:

```
platform-xhci-hcd.1-usb-0:1:1.0-video-index0   ->  /dev/video0
platform-xhci-hcd.0-usb-0:1:1.0-video-index0   ->  /dev/video2
```

`camera/config/config.yaml` now uses `by_path` for both cameras, `device_for()` prefers it over `by_id`, and preflight's `distinct_devices` check fails when two configured cameras resolve to the same device.

**Recognising it next time:** Count the links. `ls /dev/v4l/by-id/ | wc -l` should be two per camera (capture plus metadata node). Fewer than that with several cameras connected means a collision, and the tools will happily use the wrong one.

> **Two consequences beyond the tooling.** "front_left is whatever is plugged into this port" is now the actual rule, so the ports on the Pi are part of the robot's configuration and should be labelled. And the operating standards said every calibration is named after "the camera's serial number" — these cameras do not have one, so the rule is now a **team-assigned label written on the camera in marker**, recorded as `label` in the config.

---

## 2026-08-29 — Writing `pipelineIndex` over NetworkTables does nothing, and looks like it worked

**Symptom:** Switching PhotonVision pipelines from a NetworkTables client had no effect. The write was accepted, the readback returned the value we wrote, and the camera carried on at the previous resolution. Two "different" pipelines measured identically four times running.

**What we thought it was:** Only one pipeline existing (it did exist). Then the pipelines being configured identically (they were not). Then PhotonVision ignoring NetworkTables control entirely, which we were close to writing up as a limitation.

**Root cause:** Wrong topic. PhotonLib uses a **request/state pair**:

```java
IntegerPublisher  pipelineIndexRequest = cameraTable.getIntegerTopic("pipelineIndexRequest").publish();
IntegerSubscriber pipelineIndexState   = cameraTable.getIntegerTopic("pipelineIndexState").subscribe(0);
```

A plain `pipelineIndex` topic appears in older NT API documentation. Nothing consumes it. Writing it creates a topic that only we publish to, so the readback returns our own value — which is indistinguishable from confirmation.

**Fix:** Write `pipelineIndexRequest`, read `pipelineIndexState`. Verified working: requesting index 1 flipped the camera's negotiated format from 1280x800 to 640x400.

**Recognising it next time:** **A readback that only ever returns what you wrote is not a confirmation.** Ask whether the value you are reading is published by the thing you are trying to control, or by you. When in doubt, confirm through an independent channel — here, `v4l2-ctl --all` reports the negotiated format and knows nothing about NetworkTables, so it could not lie in the same direction.

> The general shape has now bitten us three times in one project: `cap.set()` reporting success while ignoring the value, v4l2 storing a control for hardware that does not exist, and this. **Storage is not action.** Verify against an effect, not against an echo.

---

## 2026-08-29 — PhotonVision calibration diverges: 319° FOV, negative focal lengths

**Symptom:** Roughly a dozen calibration attempts, each either "Camera calibration failed!" or a "success" reporting absurd numbers — mean error 32–77 px and FOV values of 186°, 218°, 319°. The log showed mrcal discarding 65–70% of observations and `Negative corner in reprojection error calc! Measured: {-410.7, 551.8}`.

**What we thought it was:** The board width/height being swapped. Then the ChArUco dictionary (`DICT_4X4_250` vs PhotonVision's `Dict_4X4_1000`). Then the "Old OpenCV Pattern" flag. Then partial board views. Then a PhotonVision bug we could not work around, which is why the first fix was to import our own OpenCV calibration instead.

**Root cause:** **`Maximum Diagonal FOV` under Camera Settings defaults to 70, and it seeds the calibration solver.** `Calibrate3dPipe` seeds from `imageProps.horizontalFocalLength`, which `FrameStaticProperties` derives from the diagonal FOV whenever that resolution has no calibration yet:

```java
horizontalFocalLength = (imageWidth / 2.0) / Math.tan(horizFOV / 2.0);
```

At 1280x800 the diagonal is 1509 px, so `70` seeds a focal length of **1078 px** against a true **734 px** — 47% off. On a planar target that is enough to converge into a *mirrored* solution, and the saved intrinsics came back with negative focal lengths (`fx = -224`, `fy = -1071`). A negative focal length is meaningless, which is why the derived FOV was nonsense.

**Fix:** Set `Maximum Diagonal FOV` to the camera's real value (91.6 for ours) before calibrating. Then capture with fixed exposure, the whole board visible, 30–45° of tilt in several directions, and coverage out to the frame corners. It converged on the first attempt: **0.46 px, HFOV 81.98°**, matching our independent OpenCV calibration to 0.27%.

Full method in [CALIBRATION.md](CALIBRATION.md).

**Recognising it next time:** **Vertical FOV greater than horizontal is geometrically impossible** on a sensor with square pixels. That one check identifies a diverged solve instantly, regardless of what the error column says. Then look at the intrinsics themselves — a negative `fx` names the failure immediately.

> Two lessons that generalise beyond PhotonVision. **A nonlinear solver needs a sane starting point**, and a UI field that looks like display metadata can be the starting point. And **a solver that reports success is not reporting correctness** — check the result against something physical before believing it.

> Also worth knowing: to distinguish a bad *solve* from bad *correspondences*, fit a homography to one observation's object and image points. It has eight degrees of freedom and ignores intrinsics entirely, so if it cannot explain the pairs, the pairing is wrong rather than the solver.

---

## 2026-08-29 — PhotonVision camera stream blank in the UI, works in its own tab

**Symptom:** The PhotonVision web UI at `http://2609-pi-1.local:5800` showed a broken image where the camera stream should be, while FPS and latency updated normally. Opening `http://2609-pi-1.local:1181/stream.mjpg` and `:1182/stream.mjpg` directly worked fine. Console said:

```
Access to image at 'http://2609-pi-1.local:1181/stream.mjpg' from origin
'http://2609-pi-1.local:5800' has been blocked by CORS policy: The request client
is not a secure context and the resource is in more-private address space `local`.
```

**What we thought it was:** MJPEG connections held open by the tabs we had opened for testing, exhausting the browser's per-host connection limit. Then a browser extension, which is why we tried a private window.

**Root cause:** Chrome's **Private Network Access** policy. The page is served over plain HTTP, so it is not a secure context, and Chrome classifies `.local` mDNS names as a more-private address space. Sub-resource requests into that space are blocked. Nothing is wrong with PhotonVision, and the message naming CORS sends you looking in the wrong place entirely.

**Fix:** Reach the UI by IP rather than mDNS — `http://10.26.9.11:5800`. A `10.x` literal is not in the `local` address space, so the policy does not apply. Firefox also works. As a last resort, `chrome://flags/#block-insecure-private-network-requests` → Disabled.

**Recognising it next time:** A sub-resource fails while the same URL works when typed into the address bar. That difference is the tell — a resource that loads standalone but not when embedded is a browser policy question, not a server one.

---

## 2026-08-24 — ChArUco: markers detect, charuco corners always zero

**Symptom:** `detectBoard()` returned up to 114 of 123 ArUco markers, and **0 charuco corners**, on every frame. Brightness was fine (`mean` ~20). The capture script sat there silently rejecting everything.

**What we thought it was:** Too dark, then the board too far away, then the OpenCV 4.6 legacy-pattern change.

**Root cause:** The board object did not match the physical board. The printed label reads `13x19`, which is **rows × columns**. OpenCV's `CharucoBoard((squaresX, squaresY), ...)` wants **columns first** — `(19, 13)`. With the dimensions transposed the marker IDs do not land where the board expects them, so no chessboard corner can be interpolated. Markers still detect perfectly, because that step never consults the board layout.

**Fix:** Test all four candidates against a single captured frame rather than guessing:

```python
for sx, sy in [(13,19), (19,13)]:
    for legacy in (False, True):
        b = cv2.aruco.CharucoBoard((sx, sy), 19.0, 14.0, d)
        if hasattr(b, "setLegacyPattern"): b.setLegacyPattern(legacy)
        cc, ci, _, _ = cv2.aruco.CharucoDetector(b).detectBoard(gray)
        print(sx, sy, legacy, 0 if ci is None else len(ci))
```

One combination returned 99 corners, the rest 0. Ours is `(19, 13)`, legacy off. **The same board object must be used for capture and for calibration** — a mismatch there yields a clean-looking calibration built on wrong 3D points.

**Recognising it next time:** **Markers detecting while corners stay at zero always means a board-definition mismatch, never an image problem.** Marker detection does not use the board layout; corner interpolation does. So the split tells you exactly which half of the pipeline to look at. Also check `setLegacyPattern` — OpenCV 4.6 flipped the chessboard colour parity, and boards from older versions or third-party generators need it set. If both legacy values give identical results, the method is missing from your build and is not the problem.

> A cheaper version of the same lesson: the capture script originally printed only on success, so a total failure looked identical to an idle script. **Log the inputs to the decision, not just the verdict.** The instrumented version printed markers, corners, brightness and the rejection reason — and the cause was obvious in one line.

---

## 2026-08-24 — AprilTag sweep reported 0% detection at every exposure

**Symptom:** An exposure sweep printed `detect% 0` and `jitter nan` for every row, 1ms through 40ms. Turning the room lights on changed nothing.

**What we thought it was:** Too dark. Then the missing gain control. Then that the exposure writes were not being applied at all.

**Root cause:** The sweep script hardcoded `DICT_APRILTAG_36h11`. The printed tag was **16h5**. Zero detections at every setting, because the detector was looking for a family that was not in the room.

The disproof was already in our own output: a single-frame check had detected the tag as 16h5 at `mean 4.6` — *darker* than most rows in the sweep that reported nothing. If it decodes at 4.6 and fails at 18.6, brightness is not the variable.

**Fix:** Put the family in a variable at the top of the script. Print **36h11** for anything real — that is the FRC field family. 16h5 has a 4-bit payload and weak error correction, so it also throws false positives off room texture.

**Recognising it next time:** A detector reporting zero at *every* setting is an input problem, not a tuning problem. Tuning moves a number; a wrong input pins it at zero. And search your own logs for a case that worked under worse conditions — that single line collapses the search.

---

## 2026-08-24 — v4l2 lists camera controls the camera does not physically have

**Symptom:** `v4l2-ctl --list-ctrls-menus` on the Thriftiest Cam showed `focus_absolute`, `focus_automatic_continuous`, `saturation`, `hue` and `white_balance_*`. Autofocus read as enabled. Writes returned success and read back correctly.

**What we thought it was:** That autofocus was on and would silently invalidate the stage 3 calibration by moving `fx` at runtime — a real and serious failure mode, on a camera that turned out not to have the hardware for it.

**Root cause:** The controls belong to the **Sunplus UVC bridge chip**, a generic webcam ASIC, not to the OV9281 sensor behind it. The sensor is monochrome and the lens is fixed. There is no focus motor and there is no colour. The bridge advertises a stock webcam control set regardless.

**Fix:** Prove it from the image, not the control list. Pin exposure manually first (auto-exposure changes contrast and corrupts any sharpness metric), then sweep focus and measure:

```bash
v4l2-ctl -d /dev/video0 -c auto_exposure=1 -c exposure_time_absolute=100
for f in 1023 512 256 0 1023 0; do
  v4l2-ctl -d /dev/video0 -c focus_absolute=$f
  # capture, then: cv2.Laplacian(gray, cv2.CV_64F).var()
done
```

Flat across the range = no motor. Note the repeated values: the spread *within* one focus position was as large as the spread *between* positions, which is what settles it. Sweep descending as well, so a drifting confound shows up as tracking run order rather than the value.

**Recognising it next time:** **Readback proves storage, not action.** A bridge with nothing attached will happily store your number and return it. Only the image proves a control did something — the same rule as `cap.set()` reporting success while ignoring you.

---

## 2026-08-24 — Reflashing the Pi silently reverts stage 0

**Symptom:** Two unrelated-looking failures in the same session. `ModuleNotFoundError: No module named 'cv2'` from a script that had worked. And `scp` refusing with `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!`.

**What we thought it was:** For the import, a broken venv or PEP 668 interference. For SSH, a genuine man-in-the-middle warning.

**Root cause:** The SD card had been reflashed. That removed the apt packages installed at stage 0 (`python3-opencv`) and regenerated the SSH host keys. One event, two symptoms, neither of which names it.

**Fix:**
```bash
# On the Pi
sudo apt update && sudo apt install -y python3-opencv python3-yaml
python3 -c "import cv2; print(cv2.__version__, cv2.__file__)"   # want dist-packages

# Verify the key really is the Pi's before clearing anything
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub                # compare fingerprints

# On the laptop — the name must match what you connect with, .local included
ssh-keygen -R 2609-pi-1.local
```

**Recognising it next time:** Several unrelated things broken at once, all of them things that were *installed* rather than written. Ask whether the machine is still the machine you think it is. Never clear a host key without comparing the fingerprint on the device first — the warning is doing its job, and the check costs ten seconds.

> A reflash reverts stage 0 for **every** laptop on the team, not just the one that did it. Everyone's `known_hosts` breaks at once. Check `systemctl status health_monitor` too — that goes with it.

---

## 2026-08-22 — SSH "connection refused" after hostname change

**Symptom:** `ssh user@newname.local` returned `Connection refused` immediately. Had been working before the rename.

**What we thought it was:** Broken mDNS after the rename. Then a firewall. Then a wrong username.

**Root cause:** "Connection refused" means the host was reached and something actively declined — a genuinely dead or unreachable host gives a *timeout* instead. So the name was resolving fine. The SSH daemon had been started manually but never enabled, so it did not come back after the reboot that applied the hostname change.

**Fix:**
```bash
sudo systemctl enable --now ssh
```

**Recognising it next time:** Refused vs timeout is the tell. Refused = reached it, nothing listening on that port. Timeout = didn't reach it at all. Different problems entirely.

> **Generalise this one.** `systemctl start` runs a service now. `systemctl enable` makes it run at boot. They are independent. A service that was started but never enabled works perfectly until the first power cycle and then silently doesn't — which on a robot means it dies at the venue, not on the bench. Always `enable --now`.
>
> This is exactly why `scripts/install_service.sh` uses `enable --now` and says so in a comment.

---

## 2026-08-22 — Windows SSH: "Bad owner or permissions on config"

**Symptom:** Every `ssh` command on Windows failed before connecting:
```
Bad permissions. Try removing permissions for user: UNKNOWN\UNKNOWN (S-1-5-21-...)
Bad owner or permissions on C:\Users\<user>\.ssh\config
```

**What we thought it was:** Something wrong on the Pi. It wasn't — this never left the laptop.

**Root cause:** The `.ssh\config` file carried an ACL entry for a Windows SID that no longer resolves to an account. OpenSSH refuses to read a config file whose permissions it can't verify.

**Fix:** The file is optional (it only stores host shortcuts), so renaming it was enough:
```
ren C:\Users\<user>\.ssh\config config.bak
```
To keep it instead, take ownership, strip inheritance, grant only your own account, and remove the orphaned SID with `takeown` and `icacls`.

**Recognising it next time:** If the error names a local file path, the problem is local. Nothing had reached the network yet.

---

## 2026-08-21 — `get_throttled` returns `0x50000`

**Symptom:** After `sudo apt full-upgrade` on a fresh image, `vcgencmd get_throttled` returned `0x50000` instead of `0x0`.

**What we thought it was:** Briefly suspected the SD card, since we'd already had card trouble.

**Root cause:** Underpowered supply. `0x50000` sets bits 16 and 18 — under-voltage *has occurred* and throttling *has occurred*, both historical since boot rather than currently active (bits 0–3 were clear). The bootloader screen had shown `power: supply: USB-C 900 mA` the whole time. The Pi 5 wants **5V/5A (27W)**. During the upgrade, CPU and SD card were hammered simultaneously, current draw exceeded what the supply could deliver, voltage sagged, and the Pi clocked down to survive.

**Fix:** Proper 27W USB-C PD supply. On the robot this becomes a regulated 12V→5V converter sized for 5A, fed from a dedicated protected circuit — **never** the roboRIO's USB ports or the VRM.

**Regulator options, checked 2026-08-22.** A Pi 5 wants 5V/5A. Size for that,
not for "probably enough":

| Option | Output | Verdict |
|---|---|---|
| Pololu **D36V50F5** | 5V, 5.5A continuous, 5.5-50V in | Adequate, available |
| Grapple **MitoCANdria** | 5V + adjustable rails, up to 10A each, 100W total | Adequate, purpose-built for FRC coprocessors |
| Redux **Zinc-V** | 12V in, 5V/5A, 8A burst | Adequate on paper, but Redux is reported to be winding down hardware - **check availability before planning on it** |
| Pololu **S13V30F5** | 5V, **2-4A depending on input voltage** | **Not adequate for a Pi 5** - see below |

> **The S13V30F5 trap.** It is one of the two regulators PhotonVision's own
> wiring docs recommend, and Pololu rates it at 2-4A depending on input voltage
> - below the Pi 5's 5A. Those recommendations were not written for a Pi 5
> specifically, so following them here can reproduce the exact fault above.
> **Read the current rating, not the recommendation.**

Also from PhotonVision's wiring docs: feed the regulator from the **PDP/PDH, not
the VRM.**


**Recognising it next time:** `vcgencmd get_throttled` is the first command to run when anything is weird. `0x0` is the only fully clean result. Bits 0–3 are live state; bits 16–19 are sticky history that only clears on a **full power cycle**, not a soft reboot.

> Underpowering also causes intermittent freezes, USB devices dropping out, cameras disconnecting mid-stream, and SD card corruption. On a robot it will look exactly like a code bug. Log this value during practice matches.

> **This entry is why the health monitor exists**, and it is still the live state of the
> dev Pi as of 2026-08-22. Two things to do once the proper supply arrives:
> **pull the plug** to clear the sticky bits (a soft reboot won't, and you'll think the
> fix failed), then confirm `0x0`. After that, `throttled_since_start` in the monitor
> becomes a trustworthy live signal rather than a restatement of this history.

---

## 2026-08-21 — Pi hangs at "waiting for network"

**Symptom:** Pi 5 booted to a diagnostic screen and sat indefinitely at `waiting for network`. Ethernet was connected and confirmed working from a laptop on the same cable.

**What we thought it was:** A network or DHCP problem. Spent time on the router.

**Root cause:** Not a network problem at all. Having found nothing bootable on SD or USB, the bootloader fell through to TFTP network boot, looking for a PXE server that doesn't exist on our network. The message means "no valid boot media," not "bad ethernet."

The giveaway was on screen the whole time: `part: 0 mbr [0x00:00000000 0x00:00000000 ...]` — an entirely empty partition table. A valid Pi OS card shows **two** partitions, a small FAT32 boot partition and a larger ext4 root.

**Fix:** Write a valid image. We ended up using Network Install (hold **SHIFT** at power-on, wired ethernet required) which writes the card through the Pi's own SD slot and bypasses the laptop card reader entirely.

**Recognising it next time:** Read the `part:` line before anything else. All zeros means nothing was written, full stop. Also worth doing the cluster math on the `type: 32` line to sanity-check capacity — `clusters × sectors × 512 bytes` should match the card's advertised size.

---

## 2026-08-21 — SD write verification failing on multiple cards

**Symptom:** Raspberry Pi Imager completed the write, then failed at verification: `Verifying write failed. Contents of SD card is different from what was written to it.` Happened on two different cards from unrelated sources.

**What we thought it was:** Counterfeit card. That's the classic signature — fake cards report a large capacity with far less real flash, and writes past the real capacity silently vanish, which surfaces at exactly this point.

**Root cause:** The USB card reader. Two cards from *unrelated* sources (one borrowed, one an old spare) failing identically is strong evidence against counterfeits, which come in batches. The write may have been fine; the readback was corrupted.

**Fix:** Bypassed the reader entirely using Pi 5 Network Install.

**Recognising it next time:** **Same failure on two or more unrelated cards means it's the reader.** One card only means suspect the card — confirm with h2testw, which fills the card and verifies every byte, reporting actual usable capacity.

> Also worth knowing: a verification failure doesn't guarantee unbootable media. Just trying to boot it is a two-minute test.

---

## 2026-08-21 — Imager: "Access denied error while writing file to disk"

**Symptom:** Imager failed immediately on clicking Write. The drive then vanished from File Explorer until a letter was reassigned in Disk Management.

**What we thought it was:** The disappearing drive looked like the problem. It isn't — Imager **dismounts the volume** so it can write raw sectors. That's expected behaviour.

**Root cause:** Norton Utilities Premium ships **Data Protector**, which specifically intercepts raw block-level writes to disks — precisely what Imager does. Compounded by Windows holding a lock on the freshly formatted volume.

**Fix:** Disabled Norton Auto-Protect and Data Protector, ran Imager as administrator, then cleared the card with `diskpart clean`.

**Recognising it next time:** Check in this order — physical lock switch on the SD adapter, run as admin, antivirus, close anything touching the drive, then `diskpart clean`.

> ⚠️ `diskpart clean` is destructive with no confirmation prompt. **Verify the disk number by capacity every single time.**

> Also: **don't pre-format the card.** Imager overwrites the whole device including the partition table. Formatting first only gives Windows another chance to lock it.

---

## 2026-08-21 — Imager: "Media error detected" partway through

**Symptom:** Write progressed to roughly 22%, then failed with `Media error detected. The storage device may be damaged or counterfeit.` A later attempt failed past 80%.

**What we thought it was:** Bad card, initially.

**Root cause:** The failure point **moved** between attempts — 22%, then 80%+. A physical bad block sits at a fixed offset and would fail at the same percentage every time. A moving target points at a connection or device dropout. In our case, a timed antivirus exemption expiring mid-write, compounded by USB power management.

**Fix:** Disabled antivirus for longer than the write would take, switched from a vendor power plan ("Norton Utilities Premium — Ultra Performance") to stock **High performance**, disabled USB selective suspend and sleep, and stayed at the machine.

**Recognising it next time:** **Does the failure point move?** Same percentage every time means the card. Different percentage each time means reader, port, power management, or antivirus re-arming.

> Vendor power plans can strip the advanced power settings dialog and may silently reassert themselves. Re-check the active plan right before writing.

---

*These entries are the condensed, searchable versions of the imaging saga. The
longer-form write-up is not in this repo — if you still have it, park it somewhere the
whole team can reach and link it here.*

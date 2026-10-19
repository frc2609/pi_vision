# Networking — putting the Pi on the robot

*How the Pi, PhotonVision and the roboRIO find each other, what to set on each
side, and how to prove the link is real rather than merely plugged in.*

Written for someone connecting the Pi at a bench or at an event. Most of what
goes wrong here is a setting that looks right, so the checks at the end matter
as much as the settings.

---

## The topology, and the one thing people get backwards

**The roboRIO is the NetworkTables server. PhotonVision and `vd` are clients of
it.**

| | Role on NetworkTables |
|---|---|
| roboRIO (robot code) | **server** — it starts one whenever robot code is running |
| PhotonVision (on the Pi) | client |
| `vd` (on the Pi) | client |
| Dashboard, Glass, AdvantageScope | client |

Now the part that trips people up:

> **Data flows Pi → roboRIO. The connection is made roboRIO ← Pi.**
> The Pi produces the measurements, so people reason that the Pi must be the
> server handing them out. It is the other way round: the Pi *connects to* the
> roboRIO and publishes into the roboRIO's tables. Direction of data and
> direction of connection are opposite here, and assuming they match is how
> both ends end up configured as servers and neither sees the other.

Robot code is the server because it is the thing that has to be running for any
of this to matter. If the roboRIO is off, there is nothing to publish to and
nothing worth publishing.

---

## PhotonVision settings this requires

In PhotonVision's networking settings:

| Setting | Value | Why |
|---|---|---|
| **Run NetworkTables Server** | **OFF** | The roboRIO is the server. Leave this on and PhotonVision runs its own and publishes into that instead. |
| **Team Number** | the team number | How PhotonVision resolves the roboRIO without a hardcoded address. |

**While "Run NetworkTables Server" is on, PhotonVision publishes to itself and
the roboRIO sees nothing.** Nothing errors. PhotonVision's own UI looks
completely healthy, because from where it is standing it is publishing happily.

**The symptom shows on the dashboard, not on the Pi:** a dashboard connected to
the roboRIO shows only `FMSInfo`, `LiveWindow` and `Shuffleboard`, and no
`/photonvision` table at all. If you see those three and nothing else, check
this setting before anything else.

---

## The matching setting in `config.yaml`

`config/config.yaml`, the `networktables` block:

```yaml
networktables:
  mode: client_team
  team: 2609
  address: 127.0.0.1        # only read when mode is client_address
  identity: camera_tools
```

| `mode` | Means |
|---|---|
| `client_team` | Resolve the server from the team number. **This is the setting on a robot.** |
| `client_address` | Connect to the fixed address in `address`. |
| `server` | These tools run the server. Rare — only if nothing else does. |

The team number lives here and nowhere else in the project. If this file is
missing the tools say so and fail, rather than quietly assuming a team.

### On a bench with no roboRIO

With no roboRIO there is no server, so **PhotonVision has to be it**. That is a
pair of changes and they go together — doing one without the other leaves both
ends looking for a server that does not exist:

| | On a robot | On a bench, no roboRIO |
|---|---|---|
| PhotonVision "Run NetworkTables Server" | **OFF** | **ON** |
| `networktables.mode` | `client_team` | `client_address` |
| `networktables.address` | *(unused)* | `127.0.0.1` |
| `preflight.require_timesync` | `true` | `false` |

Bench to robot is the same lines in reverse, and forgetting the `config.yaml`
half means `vd` keeps connecting to `127.0.0.1` — which is the server you just
switched off.

---

## Addressing

| Thing | Convention | Here |
|---|---|---|
| Hostname | `<team>-<device>-<n>`, hyphens only | `2609-pi-1` |
| Pi static IP | `10.TE.AM.11` and up | **`10.26.9.11`**, `.12` for a second Pi |
| roboRIO | `10.TE.AM.2` | **`10.26.9.2`** |

**Hostnames:** letters, digits and hyphens. **No underscores** — invalid in
hostnames and they break mDNS. A leading digit is fine: RFC 1123 allows it, and
`2609-pi-1.local` resolves normally.

> **The address is `10.26.9.11`, not `10.26.09.11`.** The FRC scheme is
> `10.TE.AM.x`, which for team 2609 reads `TE=26`, `AM=09` — so the shorthand
> people write on a whiteboard has a zero in it. **A leading zero in a real
> octet is invalid, and some resolvers read it as octal**, which hands you a
> different address rather than an error. Type it without the zero.

**Static IP, not DHCP, on the robot.** `.local` names are convenient on a bench
and unreliable under FMS. Configure the static address before your first
competition, not at it.

### Ports worth knowing

| Port | Protocol | What |
|---|---|---|
| 5800 | TCP | PhotonVision web UI — `http://10.26.9.11:5800` |
| 1181, 1182 | TCP | Camera streams, raw and processed — one pair per camera |
| 5810, 5811 | TCP | NetworkTables |
| **5810** | **UDP** | **PhotonVision time sync — a different protocol on the same number** |

TCP 5810 and UDP 5810 are separate sockets, so there is no clash. It does mean
"port 5810" is ambiguous in conversation. Say which protocol.

> **Use the IP for the PhotonVision UI, not `.local`.** The camera stream does
> not render inside the UI over `http://<host>.local` in Chrome — Private
> Network Access blocks it.

---

## Wiring

**Ethernet from the Pi to the radio is how a real robot is done.** It is the
only arrangement that is both reliable and legal in a match.

| Radio | Where the Pi goes |
|---|---|
| **VH-109** | **AUX1 / AUX2.** The **Passive PoE DIP switch for that port must stay OFF.** |
| **OM5P-AC** (older) | The second port is unreliable. **Use a network switch** off the single good port. |

> **The PoE DIP switch is the one that costs hardware.** Passive PoE puts
> voltage on the ethernet pairs. The Pi's ethernet port does not expect it, and
> switching it on for the port the Pi is plugged into **can damage the Pi**.
> Check the switch position before plugging anything in, not after.

**WiFi from the Pi works on a bench and is not legal on a competition robot**,
where only the radio may be wireless. It is a reasonable way to get going when
there is no spare ethernet cable, but it is a bench arrangement with an expiry
date — do not let it become the robot's configuration by drift.

### Stream bandwidth, at an event

FRC has historically capped total bandwidth near **4 Mbps**, and a camera stream
left at full resolution will eat it. Measured here: a second camera's dashboard
stream ran at **6.31 Mbps** against the first's **0.35 Mbps** at 320x200 —
eighteen times the traffic for a picture a human glances at.

**Set Stream Resolution low on every camera.** It does not affect detection.

---

## Time synchronisation

Separate from NetworkTables, and the piece most likely to be quietly missing.

The capture timestamp is what makes latency stop mattering: it records the
moment the shutter opened, so the pose estimator can rewind odometry to that
instant and fuse there. The number is produced on the Pi and consumed on the
roboRIO, so the two clocks have to be related to each other.

**How it actually works — and it is the reverse of the obvious guess:**

- **The roboRIO is the time server. The Pi is the client.** Nothing on the Pi
  turns this on.
- **photonlib starts it, not PhotonVision.** The server comes up when robot code
  constructs a `PhotonCamera`. **No `PhotonCamera` in robot code means no
  server, means no sync — silently.** One line is the entire requirement:

  ```java
  private final PhotonCamera camera = new PhotonCamera("front_left");
  ```

- **It is not NetworkTables.** A separate protocol over **UDP port 5810**. Ping
  at 1 Hz, take the best round trip, `server_time + rtt/2` — Cristian's
  algorithm.
- **The Pi applies the offset itself.** What arrives on NetworkTables is already
  in roboRIO time; photonlib only reads it out. There is nothing to convert on
  the robot side.

### Why it is worth a check rather than a paragraph

**An unsynced timestamp still fuses.** The pose is correct and it lands at the
wrong instant. At rest that is invisible — a stationary robot's pose from a
hundred milliseconds ago is the same pose. Under motion it appears as smear,
which reads as a camera fault and is not one. Nothing else in the system reports
it.

---

## Checking the link

### 1. `vd preflight`

```bash
.venv/bin/python src/vd.py preflight; echo "exit $?"
```

It reports `networktables` (connected, and something is actually updating) and
`timesync` separately. A healthy `timesync` names the host, the offset and the
pong counts.

### 2. The raw counters

PhotonVision publishes its own sync health to
**`/photonvision/.timesync/<hostname>`**. The dot prefix hides it from
dashboards; it is an ordinary topic otherwise.

| Topic | Reading it |
|---|---|
| `ping_tx_count` | Pings the Pi has sent. Climbs regardless of whether anything is listening. |
| `pong_rx_count` | Replies heard. **Zero, with pings climbing, means nothing is serving.** |
| `offset_us` | The correction being applied, in microseconds. |
| `rtt2_us` | Half the round trip — the bound on how wrong the offset can be. The useful number, not the offset itself. |

Pings climbing with pongs at zero is exactly what "correct and completely
unsynced" looks like: the client working perfectly, pinging into the void.

> **What pongs prove, and what they do not.** The counters are cumulative and
> preflight takes one sample, so a link that died a moment ago still shows a
> healthy total. Pongs received means it synced *at some point*, not that it is
> synced *now*. The live signal is `timeSinceLastPong` in the result metadata,
> which is robot-side.

### 3. Glass, from the robot side

Connect Glass to the roboRIO and open **NetworkTables Info → Clients**. Look for
**`photonvision`** in the list.

That is the roboRIO confirming the connection exists, which is a different claim
from the Pi believing it made one. It is also the check that catches "Run
NetworkTables Server" being left on, because in that state the client never
appears.

### 4. Age of a result, with no odometry

With no gyro or encoders nothing fuses, so there is nothing for the timestamp to
be early or late against. The observable that still works is the **age** of a
result:

```java
Timer.getFPGATimestamp() - result.getTimestampSeconds()
```

It should sit steady around **20–60 ms**. A wild or negative age means the clocks
disagree.

---

## `require_timesync` belongs on whenever a roboRIO exists

```yaml
preflight:
  require_timesync: true
```

A `SKIP` in preflight means "could not check", which is not a failure — **and a
SKIP publishes green to the dashboard.** So `timesync_ok` reads green both when
sync is working and when nothing is serving at all. Only the text, or the raw
ping and pong counts, tells those two apart.

Requiring it turns SKIP into FAIL and makes the light mean something. **Set it
back to `false` only on a bench with no roboRIO**, where pinging into the void
is the correct state.

> **Check the polarity of any boolean you put on a dashboard.** The `*_ok`
> topics are true-is-good; the health monitor's fault flags (`undervoltage_*`,
> `throttled_*`, `usb_over_current`) are true-is-bad. A dashboard that paints
> false red shows a wall of red meaning "everything is fine", which trains
> people to ignore red.

---

## Symptom table

| What you see | Look at |
|---|---|
| Dashboard shows only `FMSInfo`, `LiveWindow`, `Shuffleboard` | "Run NetworkTables Server" is still ON in PhotonVision |
| `vd nt` finds nothing | Both ends are clients, or the mode and team number point at a server that is not there |
| `photonvision` absent from Glass → NetworkTables Info → Clients | The Pi is not actually connected to the roboRIO, whatever the Pi thinks |
| `timesync` SKIP, pings climbing, pongs zero | No `PhotonCamera` in robot code, or no roboRIO |
| Pose correct at rest, smeared under motion | Time sync, not the camera |
| Address will not resolve | A leading zero got typed — `10.26.09.11` |
| Pi unreachable after plugging into the radio | The Passive PoE DIP switch for that port |

---

## See also

- [`RUNNING.md`](RUNNING.md) — `vd preflight` and `vd nt`, and reading their
  output
- [`../../docs/operating_standards.md`](../../docs/operating_standards.md) §8 —
  naming and network conventions

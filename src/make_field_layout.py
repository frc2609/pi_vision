#!/usr/bin/env python3
"""Generate a WPILib AprilTagFieldLayout for tags on two perpendicular walls.

A room corner is the cheapest way to get a real multi-tag setup: two tags whose
relative geometry you actually know. MultiTag solves one PnP across every
visible tag against this layout, and the second tag disqualifies the false
solution that makes single-tag pose flip at steep angles.

Conventions, which are the whole difficulty:

- Units are **metres**, coordinate system is NWU and right-handed, **+Z up**.
- A tag's pose is the centre of its **black square**.
- **Zero rotation means the tag is upright and facing along +X**, so orientation
  is a single yaw about Z.

Geometry used here. Origin at the corner, on the floor, facing the corner:

    left wall   lies in the plane y = 0, inward normal +Y  -> tags yaw  90 deg
    right wall  lies in the plane x = 0, inward normal +X  -> tags yaw   0 deg

A tag's distance from the corner is measured along its own wall, and becomes x
for a left-wall tag and y for a right-wall tag.

    python3 src/make_field_layout.py \\
        --left  ID=2 dist=0.50 height=1.635 \\
        --right ID=0 dist=0.50 height=1.660 \\
        -o config/field_layout_corner.json

**Layout error becomes pose error directly**, so the script prints the
centre-to-centre distance it implies. Tape-measure that and compare before
trusting any output. If they disagree, the corner is not square or a
measurement is off - and the solver has no way to know.
"""

import argparse
import json
import math
import sys

SQRT_HALF = math.sqrt(0.5)


def parse_tag(parts, what):
    """Parse `ID=2 dist=0.50 height=1.635` into a dict."""
    got = {}
    for p in parts:
        if "=" not in p:
            raise SystemExit("%s: expected key=value, got %r" % (what, p))
        k, v = p.split("=", 1)
        got[k.strip().lower()] = v.strip()
    for need in ("id", "dist", "height"):
        if need not in got:
            raise SystemExit("%s: missing %s=" % (what, need))
    return {"id": int(got["id"]), "dist": float(got["dist"]),
            "height": float(got["height"])}


def yaw_quaternion(deg):
    """Rotation about Z only - tags on a vertical wall are upright by definition."""
    half = math.radians(deg) / 2.0
    return {"W": round(math.cos(half), 7), "X": 0.0, "Y": 0.0,
            "Z": round(math.sin(half), 7)}


def entry(tag_id, x, y, z, yaw_deg):
    return {"ID": tag_id,
            "pose": {"translation": {"x": round(x, 4), "y": round(y, 4),
                                     "z": round(z, 4)},
                     "rotation": {"quaternion": yaw_quaternion(yaw_deg)}}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--left", nargs="+", metavar="K=V", required=True,
                    help="tag on the left wall as you face the corner")
    ap.add_argument("--right", nargs="+", metavar="K=V", required=True,
                    help="tag on the right wall as you face the corner")
    ap.add_argument("--field", type=float, nargs=2, default=[5.0, 5.0],
                    metavar=("LENGTH", "WIDTH"),
                    help="bounding box only; make it bigger than the room")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    left = parse_tag(args.left, "--left")
    right = parse_tag(args.right, "--right")

    # Left wall is the plane y=0, so distance along it is x, and the tag faces
    # +Y. Right wall is x=0, distance along it is y, and the tag faces +X.
    tags = [entry(left["id"], left["dist"], 0.0, left["height"], 90.0),
            entry(right["id"], 0.0, right["dist"], right["height"], 0.0)]
    layout = {"tags": tags, "field": {"length": args.field[0], "width": args.field[1]}}

    text = json.dumps(layout, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print("wrote %s" % args.out)
    else:
        print(text)

    dx = left["dist"]
    dy = right["dist"]
    dz = left["height"] - right["height"]
    d = math.sqrt(dx * dx + dy * dy + dz * dz)
    print("\ncentre-to-centre implied by this layout: %.3f m (%.1f cm)" % (d, d * 100))
    print("tape-measure that between the two tag centres before trusting the")
    print("layout - a disagreement means the corner is not square or a")
    print("measurement is wrong, and multi-tag inherits the error silently.")
    print("\nimport: Settings -> Device Control -> Import Settings -> AprilTag Layout")
    return 0


if __name__ == "__main__":
    sys.exit(main())

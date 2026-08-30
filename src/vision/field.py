"""Generate a WPILib AprilTagFieldLayout for tags on two perpendicular walls.

A room corner is the cheapest real multi-tag setup: two tags whose relative
geometry you actually know. MultiTag solves one PnP across every visible tag
against the layout, and the second tag disqualifies the false solution that
makes single-tag pose flip at steep angles.

Conventions, which are the whole difficulty:

- Units are **metres**, coordinate system is NWU and right-handed, **+Z up**.
- A tag's pose is the centre of its **black square**.
- **Zero rotation means upright and facing along +X**, so orientation is a
  single yaw about Z.

Geometry, with the origin at the corner on the floor, facing the corner:

    left wall   lies in the plane y = 0, inward normal +Y  -> tags yaw  90 deg
    right wall  lies in the plane x = 0, inward normal +X  -> tags yaw   0 deg

A tag's distance from the corner is measured along its own wall, and becomes x
for a left-wall tag and y for a right-wall tag.
"""

import math

SQRT_HALF = math.sqrt(0.5)


def yaw_quaternion(deg):
    """Rotation about Z only - a tag on a vertical wall is upright by definition."""
    half = math.radians(deg) / 2.0
    return {"W": round(math.cos(half), 7), "X": 0.0, "Y": 0.0,
            "Z": round(math.sin(half), 7)}


def tag(tag_id, x, y, z, yaw_deg):
    return {"ID": int(tag_id),
            "pose": {"translation": {"x": round(x, 4), "y": round(y, 4),
                                     "z": round(z, 4)},
                     "rotation": {"quaternion": yaw_quaternion(yaw_deg)}}}


def corner_layout(left, right, field=(5.0, 5.0)):
    """left/right are dicts of {id, dist, height}, in metres."""
    tags = [tag(left["id"], left["dist"], 0.0, left["height"], 90.0),
            tag(right["id"], 0.0, right["dist"], right["height"], 0.0)]
    return {"tags": tags, "field": {"length": field[0], "width": field[1]}}


def implied_separation(left, right):
    """Centre-to-centre distance the layout implies.

    Tape-measure this and compare before trusting the layout: layout error
    becomes pose error directly, and the solver has no way to know.
    """
    dz = left["height"] - right["height"]
    return math.sqrt(left["dist"] ** 2 + right["dist"] ** 2 + dz ** 2)

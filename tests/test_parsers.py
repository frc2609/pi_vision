"""Parser tests. These run anywhere - no camera, no Pi, no PhotonVision.

The fixtures are real output captured from `2609-pi-1`. Every parser takes text
and returns data precisely so that this is possible, and so the awkward cases
below stay pinned:

- an `inactive` flag, which means an automatic mode owns the control and writes
  to it are accepted and silently discarded
- a control the hardware does not have (`focus_absolute` on a fixed lens)
- YUYV at full resolution running at 5fps, which is the USB 2.0 wall

Run with:  python3 -m unittest discover -s tests
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from vision import camera, checks, field, tune                    # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class TestFormats(unittest.TestCase):
    def setUp(self):
        self.caps = camera.parse_formats(fixture("list-formats-ext.txt"))

    def test_both_formats_found(self):
        self.assertEqual(sorted(self.caps), ["MJPG", "YUYV"])

    def test_best_mode_is_by_pixel_count(self):
        self.assertEqual(camera.best_mode(self.caps, "MJPG"), (1280, 800, 120.0))

    def test_yuyv_full_resolution_is_the_usb2_wall(self):
        # 5fps at 1280x800 is why MJPEG is not optional on this camera.
        self.assertEqual(camera.best_mode(self.caps, "YUYV"), (1280, 800, 5.0))

    def test_missing_format_is_none_not_an_error(self):
        self.assertIsNone(camera.best_mode(self.caps, "H264"))

    def test_empty_input(self):
        self.assertEqual(camera.parse_formats(""), {})
        self.assertEqual(camera.parse_formats(None), {})


class TestCurrentFormat(unittest.TestCase):
    def test_parses_negotiated_format(self):
        self.assertEqual(camera.parse_current_format(fixture("all.txt")),
                         (1280, 800, "MJPG"))

    def test_missing_returns_none(self):
        self.assertIsNone(camera.parse_current_format("Driver Info:\n\tnothing"))
        self.assertIsNone(camera.parse_current_format(None))


class TestControls(unittest.TestCase):
    def setUp(self):
        self.ctrls = camera.parse_controls(fixture("list-ctrls.txt"))

    def test_values(self):
        self.assertEqual(self.ctrls["exposure_time_absolute"][0], "323")
        self.assertEqual(self.ctrls["auto_exposure"][0], "1")
        self.assertEqual(self.ctrls["brightness"][0], "-64")

    def test_inactive_flag_is_kept(self):
        # inactive means an auto mode owns it and writes are discarded - the
        # single most misleading thing v4l2 reports.
        self.assertIn("inactive", self.ctrls["white_balance_temperature"][1])
        self.assertIn("inactive", self.ctrls["focus_absolute"][1])

    def test_boolean_control_without_flags(self):
        self.assertEqual(self.ctrls["white_balance_automatic"], ("0", ""))

    def test_no_gain_control_on_this_camera(self):
        # The OV9281 has analog gain; this USB bridge does not expose it. That
        # absence is why exposure is the only real lever.
        self.assertNotIn("gain", self.ctrls)

    def test_empty_input(self):
        self.assertEqual(camera.parse_controls(""), {})
        self.assertEqual(camera.parse_controls(None), {})


class TestExposureArithmetic(unittest.TestCase):
    def test_ticks_are_100us(self):
        self.assertAlmostEqual(camera.max_fps_for_exposure(120), 83.33, places=1)

    def test_the_eight_fps_mystery(self):
        # A pipeline pinned at 8.0 fps with the control at 1245 was not
        # CPU-bound and not a bug: 10000/1245 = 8.03.
        self.assertAlmostEqual(camera.max_fps_for_exposure(1245), 8.03, places=1)

    def test_zero_does_not_divide_by_zero(self):
        self.assertGreater(camera.max_fps_for_exposure(0), 0)


class TestVerdict(unittest.TestCase):
    def row(self, **kw):
        base = {"detect_pct": 100.0, "dropouts_per_s": 0.0,
                "exposure_ms": 12.0, "fps": 41.0}
        base.update(kw)
        return base

    def test_exposure_bound_is_flagged(self):
        notes = tune.verdict(self.row(exposure_ms=32.0, fps=30.2))
        self.assertTrue(any("EXPOSURE-BOUND" in n for n in notes))

    def test_cpu_bound_is_not_flagged(self):
        # 12ms permits 83fps; measuring 41 means the detector is the limit.
        self.assertEqual(tune.verdict(self.row()), [])

    def test_intermittent_detection_is_flagged(self):
        notes = tune.verdict(self.row(detect_pct=62.0, dropouts_per_s=1.3))
        self.assertTrue(any("NOT solid" in n for n in notes))

    def test_nan_does_not_raise(self):
        tune.verdict(self.row(exposure_ms=float("nan"), fps=float("nan")))


class TestIdenticalRows(unittest.TestCase):
    def row(self, exp, w=1280, h=800, pipe=0):
        return {"exposure_ms": exp, "width": w, "height": h, "pipeline": pipe}

    def test_detects_an_unchanged_sweep(self):
        # Four rows labelled differently but measuring one configuration - the
        # mistake that produced two useless tables.
        self.assertTrue(tune.identical([self.row(32.3)] * 4))

    def test_real_sweep_is_not_flagged(self):
        self.assertFalse(tune.identical([self.row(12.0), self.row(32.0)]))

    def test_resolution_change_counts_as_different(self):
        self.assertFalse(tune.identical([self.row(32.3), self.row(32.3, 640, 400)]))

    def test_single_row_is_never_identical(self):
        self.assertFalse(tune.identical([self.row(32.3)]))


class TestFieldLayout(unittest.TestCase):
    def setUp(self):
        self.left = {"id": 2, "dist": 0.50, "height": 1.635}
        self.right = {"id": 0, "dist": 0.50, "height": 1.660}
        self.layout = field.corner_layout(self.left, self.right)

    def test_zero_rotation_faces_plus_x(self):
        # WPILib convention: an upright tag facing +X has identity rotation.
        q = self.layout["tags"][1]["pose"]["rotation"]["quaternion"]
        self.assertAlmostEqual(q["W"], 1.0)
        self.assertAlmostEqual(q["Z"], 0.0)

    def test_left_wall_tag_yaws_ninety(self):
        q = self.layout["tags"][0]["pose"]["rotation"]["quaternion"]
        self.assertAlmostEqual(q["W"], math.sqrt(0.5), places=6)
        self.assertAlmostEqual(q["Z"], math.sqrt(0.5), places=6)

    def test_distance_along_wall_becomes_the_right_axis(self):
        left, right = self.layout["tags"]
        self.assertAlmostEqual(left["pose"]["translation"]["x"], 0.50)
        self.assertAlmostEqual(left["pose"]["translation"]["y"], 0.0)
        self.assertAlmostEqual(right["pose"]["translation"]["x"], 0.0)
        self.assertAlmostEqual(right["pose"]["translation"]["y"], 0.50)

    def test_implied_separation_matches_the_tape_measure_check(self):
        self.assertAlmostEqual(field.implied_separation(self.left, self.right),
                               0.708, places=3)




class TestIdentityCheck(unittest.TestCase):
    """Intrinsics belong to one physical camera.

    With two identical models on the bench, a swap silently invalidates the
    calibration - format, framerate and detections all still look perfect while
    every distance is wrong.
    """

    CFG = {"camera": {"serial": "DCX-260513-K"}}
    PATH = "/dev/v4l/by-id/usb-DCX-260513-K_Thrifty_01.00.00-video-index0"

    def test_matching_serial_passes(self):
        s, _, _ = checks.identity(self.CFG, {"stable_path": self.PATH})
        self.assertEqual(s, checks.PASS)

    def test_different_camera_fails(self):
        other = self.PATH.replace("DCX-260513-K", "DCX-999999-Z")
        s, _, detail = checks.identity(self.CFG, {"stable_path": other})
        self.assertEqual(s, checks.FAIL)
        self.assertIn("DCX-999999-Z", detail)

    def test_no_by_id_path_fails(self):
        s, _, _ = checks.identity(self.CFG, {"stable_path": None})
        self.assertEqual(s, checks.FAIL)

    def test_no_serial_configured_skips(self):
        s, _, _ = checks.identity({"camera": {}}, {"stable_path": self.PATH})
        self.assertEqual(s, checks.SKIP)


class TestCalibrationResolution(unittest.TestCase):
    """Intrinsics are in pixels and do not transfer between resolutions.

    Running 640x400 against a 1280x800 calibration measured as a 0.65 deg
    systematic yaw error, with nothing looking wrong.
    """

    def facts(self, cal_size, w, h):
        return {"calibration": {"path": "x.json", "image_size": cal_size},
                "asked": {"width": w, "height": h, "fourcc": "MJPG"}}

    def test_matching_resolution_passes(self):
        s, _, _ = checks.calibration_resolution({}, self.facts([1280, 800], 1280, 800))
        self.assertEqual(s, checks.PASS)

    def test_mismatched_resolution_fails(self):
        s, _, detail = checks.calibration_resolution({}, self.facts([1280, 800], 640, 400))
        self.assertEqual(s, checks.FAIL)
        self.assertIn("do not transfer", detail)

    def test_no_size_recorded_skips(self):
        s, _, _ = checks.calibration_resolution({}, self.facts(None, 640, 400))
        self.assertEqual(s, checks.SKIP)

    def test_no_calibration_skips(self):
        s, _, _ = checks.calibration_resolution(
            {}, {"calibration": {"path": None}, "asked": None})
        self.assertEqual(s, checks.SKIP)


class TestCalibrationCheck(unittest.TestCase):
    CFG = {"camera": {"expect": {"width": 1280}},
           "calibration": {"expect_hfov_deg": 82.0, "tolerance_deg": 2.0}}

    def test_good_calibration_passes(self):
        s, _, _ = checks.calibration(
            self.CFG, {"calibration": {"path": "x.json", "fx": 733.6}})
        self.assertEqual(s, checks.PASS)

    def test_negative_focal_length_is_a_diverged_solve(self):
        # PhotonVision saved fx=-224 and displayed it as a 319 deg FOV.
        s, _, detail = checks.calibration(
            self.CFG, {"calibration": {"path": "x.json", "fx": -224.2}})
        self.assertEqual(s, checks.FAIL)
        self.assertIn("diverged", detail)

    def test_wrong_fov_fails(self):
        s, _, _ = checks.calibration(
            self.CFG, {"calibration": {"path": "x.json", "fx": 400.0}})
        self.assertEqual(s, checks.FAIL)


if __name__ == "__main__":
    unittest.main()

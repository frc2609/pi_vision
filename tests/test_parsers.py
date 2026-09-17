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

from vision import camera, checks, field, nt, tune, util                # noqa: E402

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




class TestCameraConfig(unittest.TestCase):
    """Cameras are a list, defaults merge into each, and the old shape still loads."""

    CFG = {
        "camera_defaults": {"expect": {"width": 1280, "height": 800, "fourcc": "MJPG"}},
        "cameras": [
            {"name": "front_left", "serial": "AAA", "fallback": "/dev/video0"},
            {"name": "rear", "serial": "BBB", "fallback": "/dev/video2",
             "expect": {"width": 640, "height": 400, "fourcc": "MJPG"}},
        ],
    }

    def test_all_cameras_returned(self):
        self.assertEqual([c["name"] for c in util.cameras(self.CFG)],
                         ["front_left", "rear"])

    def test_defaults_merge_in(self):
        front = util.cameras(self.CFG, only="front_left")[0]
        self.assertEqual(front["expect"]["width"], 1280)

    def test_a_camera_can_override_a_default(self):
        rear = util.cameras(self.CFG, only="rear")[0]
        self.assertEqual(rear["expect"]["width"], 640)
        self.assertEqual(rear["expect"]["fourcc"], "MJPG")   # inherited

    def test_filter_by_name(self):
        self.assertEqual(len(util.cameras(self.CFG, only="rear")), 1)
        self.assertEqual(util.cameras(self.CFG, only="nope"), [])

    def test_legacy_single_camera_block_still_loads(self):
        # A config that has not been migrated should still work rather than
        # failing obscurely.
        old = {"camera": {"name": "front_left", "fallback": "/dev/video0"}}
        self.assertEqual([c["name"] for c in util.cameras(old)], ["front_left"])

    def test_no_cameras_is_empty_not_an_error(self):
        self.assertEqual(util.cameras({}), [])

    def test_device_falls_back_when_no_link_exists(self):
        # by_path and by_id only win if they exist on disk; in a test neither
        # does, so the fallback is what should come back.
        cam = {"by_id": "/nope/by-id", "by_path": "/nope/by-path",
               "fallback": "/dev/video7"}
        self.assertEqual(util.device_for(cam), "/dev/video7")

    def test_explicit_override_wins(self):
        self.assertEqual(util.device_for({"fallback": "/dev/video0"}, "/dev/video9"),
                         "/dev/video9")


class TestIdentityCheck(unittest.TestCase):
    """Can we say *which* physical camera this is?

    Two Thriftiest Cams report byte-identical USB descriptors, so by-id holds
    one link for both and the second to enumerate overwrites the first. Identity
    has to come from the port.
    """

    CAM = {"name": "front_left", "serial": "DCX-260513-K"}
    PATH = "/dev/v4l/by-id/usb-DCX-260513-K_Thrifty_01.00.00-video-index0"

    def test_matching_serial_passes(self):
        r = checks.identity({}, self.CAM, {"stable_path": self.PATH})
        self.assertEqual(r["status"], checks.PASS)
        self.assertEqual(r["camera"], "front_left")

    def test_different_camera_fails(self):
        other = self.PATH.replace("DCX-260513-K", "DCX-999999-Z")
        r = checks.identity({}, self.CAM, {"stable_path": other})
        self.assertEqual(r["status"], checks.FAIL)
        self.assertIn("DCX-999999-Z", r["detail"])

    def test_no_by_id_path_fails(self):
        r = checks.identity({}, self.CAM, {"stable_path": None})
        self.assertEqual(r["status"], checks.FAIL)

    def test_no_identifier_at_all_skips(self):
        # Nothing to identify by is a SKIP, not a pass - we simply cannot say.
        r = checks.identity({}, {"name": "x"}, {"stable_path": self.PATH})
        self.assertEqual(r["status"], checks.SKIP)

    def test_missing_port_fails(self):
        cam = {"name": "rear", "by_path": "/dev/v4l/by-path/does-not-exist"}
        r = checks.identity({}, cam, {"device": "/dev/video2"})
        self.assertEqual(r["status"], checks.FAIL)
        self.assertIn("no camera in the configured port", r["detail"])

    def test_by_path_wins_over_a_non_unique_serial(self):
        # A serial shared by every unit of a model cannot establish identity;
        # the port can, so it is consulted first.
        cam = {"name": "rear", "serial": "DCX-260513-K",
               "by_path": "/dev/v4l/by-path/does-not-exist"}
        r = checks.identity({}, cam, {"device": "/dev/video2",
                                      "stable_path": self.PATH})
        self.assertEqual(r["status"], checks.FAIL)


class TestDistinctDevices(unittest.TestCase):
    """Two cameras resolving to one device is silent and ruinous."""

    def test_two_distinct_devices_pass(self):
        cams = [{"name": "a", "device": "/dev/video0"},
                {"name": "b", "device": "/dev/video2"}]
        self.assertEqual(checks.unique_devices({}, cams)["status"], checks.PASS)

    def test_collision_fails(self):
        # Identical USB descriptors make by-id resolve both entries to whichever
        # camera enumerated first.
        cams = [{"name": "a", "device": "/dev/video0"},
                {"name": "b", "device": "/dev/video0"}]
        r = checks.unique_devices({}, cams)
        self.assertEqual(r["status"], checks.FAIL)
        self.assertIn("by_path", r["detail"])

    def test_single_camera_skips(self):
        r = checks.unique_devices({}, [{"name": "a", "device": "/dev/video0"}])
        self.assertEqual(r["status"], checks.SKIP)


class TestCalibrationCheck(unittest.TestCase):
    """One check: present, sane, and for the resolution actually running."""

    CAM = {"name": "front_left", "expect": {"width": 1280},
           "calibration": {"expect_hfov_deg": 82.0, "tolerance_deg": 2.0}}

    def cal(self, **kw):
        base = {"path": "x.json", "fx": 733.6, "image_size": [1280, 800]}
        base.update(kw)
        return {"calibration": base, "asked": {"width": 1280, "height": 800}}

    def test_resolution_mismatch_fails(self):
        f = self.cal()
        f["asked"] = {"width": 640, "height": 400}
        r = checks.calibration({}, self.CAM, f)
        self.assertEqual(r["status"], checks.FAIL)
        self.assertIn("do not transfer", r["detail"])

    def test_pass_reports_the_resolution_it_applies_to(self):
        r = checks.calibration({}, self.CAM, self.cal())
        self.assertEqual(r["status"], checks.PASS)
        self.assertIn("1280x800", r["detail"])

    def test_no_recorded_size_still_passes(self):
        f = self.cal()
        del f["calibration"]["image_size"]
        self.assertEqual(checks.calibration({}, self.CAM, f)["status"], checks.PASS)

    def test_good_calibration_passes(self):
        r = checks.calibration({}, self.CAM, {"calibration": {"path": "x.json", "fx": 733.6}})
        self.assertEqual(r["status"], checks.PASS)

    def test_negative_focal_length_is_a_diverged_solve(self):
        # PhotonVision saved fx=-224 and displayed it as a 319 deg FOV.
        r = checks.calibration({}, self.CAM, {"calibration": {"path": "x.json", "fx": -224.2}})
        self.assertEqual(r["status"], checks.FAIL)
        self.assertIn("diverged", r["detail"])

    def test_wrong_fov_fails(self):
        r = checks.calibration({}, self.CAM, {"calibration": {"path": "x.json", "fx": 400.0}})
        self.assertEqual(r["status"], checks.FAIL)

    def test_missing_calibration_fails_rather_than_skipping(self):
        # SKIP publishes green, so an uncalibrated camera would show a
        # reassuring dashboard light while having no calibration at all.
        r = checks.calibration({}, {"name": "rear"},
                               {"calibration": {"path": None, "error": "not configured"}})
        self.assertEqual(r["status"], checks.FAIL)
        self.assertIn("calibrate it", r["detail"])

    def test_bring_up_escape_hatch(self):
        cam = {"name": "rear", "calibration": {"required": False}}
        r = checks.calibration({}, cam,
                               {"calibration": {"path": None, "error": "not configured"}})
        self.assertEqual(r["status"], checks.SKIP)




class TestUniformChecks(unittest.TestCase):
    """Every camera must produce the same set of checks, always.

    A conditional check publishes a topic that appears and disappears, which on
    a dashboard is a widget that vanishes when things are fine. The two cameras
    here are deliberately in different states.
    """

    EXPECTED = {"format", "identity", "calibration",
                "publishing", "pipeline_fps", "detections"}

    def rows_for(self, published, topics):
        cams = [{"name": "front_left", "device": "/dev/video0"},
                {"name": "rear", "device": "/dev/video2"}]
        # network() is the only source of publishing/fps/detections.
        import vision.checks as C
        real_connect = C.__dict__.get("_test_hook")

        class FakeNT:
            PIPELINE_REQUEST = "pipelineIndexRequest"

            @staticmethod
            def connect(*a, **k):
                return object(), None

            @staticmethod
            def watch(inst, table, seconds=1.5):
                return published, topics

        import sys
        sys.modules["vision.nt_backup"] = sys.modules.get("vision.nt")
        import vision.nt as realnt
        saved = (realnt.connect, realnt.watch)
        realnt.connect, realnt.watch = FakeNT.connect, FakeNT.watch
        try:
            return C.network({"preflight": {"min_fps": 10}}, cams)
        finally:
            realnt.connect, realnt.watch = saved

    def test_both_cameras_report_the_same_checks(self):
        topics = {"front_left/fps": {"rate": 1.0, "value": 30.0},
                  "front_left/hasTarget": {"rate": 0.0, "value": True}}
        rows = self.rows_for(["front_left"], topics)
        by_cam = {}
        for r in rows:
            if r["camera"]:
                by_cam.setdefault(r["camera"], set()).add(r["name"])
        self.assertEqual(set(by_cam), {"front_left", "rear"})
        for cam, names in by_cam.items():
            self.assertEqual(names, {"publishing", "pipeline_fps", "detections"},
                             "%s produced %s" % (cam, names))

    def test_timesync_is_emitted_even_when_nothing_is_publishing(self):
        # A global check that appears only on the happy path is a dashboard
        # widget that vanishes exactly when something has gone wrong.
        names = {r["name"] for r in self.rows_for([], {}) if r["camera"] is None}
        self.assertIn("timesync", names)

    def test_a_camera_that_is_not_publishing_still_reports_all_three(self):
        rows = self.rows_for([], {})
        names = {r["name"] for r in rows if r["camera"] == "rear"}
        self.assertEqual(names, {"publishing", "pipeline_fps", "detections"})
        statuses = {r["name"]: r["status"] for r in rows if r["camera"] == "rear"}
        self.assertEqual(statuses["publishing"], checks.FAIL)
        self.assertEqual(statuses["pipeline_fps"], checks.SKIP)

class TimeSync(unittest.TestCase):
    """PhotonVision's clock sync to the roboRIO.

    The roboRIO is the server; this Pi is the client. Robot code starts the
    server, so on a bench with no roboRIO the client pings and hears nothing -
    which is the correct state, not a fault, and must not read as one.
    """

    HOST = "raspberrypi"

    def topics(self, **kv):
        return {".timesync/%s/%s" % (self.HOST, k): {"rate": 1.0, "value": v}
                for k, v in kv.items()}

    def test_parses_values_and_ignores_camera_topics(self):
        t = self.topics(offset_us=1234.0, ping_tx_count=10, pong_rx_count=9,
                        rtt2_us=250.0)
        t["front_left/fps"] = {"rate": 1.0, "value": 30.0}
        got = nt.parse_timesync(t)
        self.assertEqual(sorted(got), [self.HOST])
        self.assertEqual(got[self.HOST]["pong_rx_count"], 9)
        self.assertEqual(got[self.HOST]["offset_us"], 1234.0)

    def test_string_values_survive_the_watch_snapshot(self):
        # watch() stringifies every value, so the parser has to cope with that
        # as well as with typed values.
        got = nt.parse_timesync(self.topics(offset_us="1234.0", ping_tx_count="10",
                                            pong_rx_count="9"))
        self.assertEqual(got[self.HOST]["ping_tx_count"], 10.0)

    def test_missing_key_is_absent_not_zero(self):
        got = nt.parse_timesync(self.topics(ping_tx_count=10))
        self.assertNotIn("pong_rx_count", got[self.HOST])

    def test_no_timesync_topics_skips(self):
        r = checks.timesync({}, {"front_left/fps": {"rate": 1.0, "value": 30.0}})
        self.assertEqual(r["status"], checks.SKIP)

    def test_a_wrong_key_name_says_so_instead_of_hiding(self):
        # The first version of this check looked for `ping_rx_count`; the topic
        # is `pong_rx_count`. It reported "incomplete statistics", which reads
        # as PhotonVision's fault rather than ours.
        r = checks.timesync({}, self.topics(ping_tx_count=10, offset_us=0))
        self.assertEqual(r["status"], checks.SKIP)
        self.assertIn("pong_rx_count", r["detail"])
        self.assertIn("TIMESYNC_KEYS", r["detail"])

    def test_the_state_actually_observed_on_the_bench(self):
        # Captured from 2609-pi-1, 2026-08-30, no roboRIO present: pings
        # climbing at 1 Hz, everything else flat at zero.
        r = checks.timesync({}, self.topics(ping_tx_count=52718, pong_rx_count=0,
                                            offset_us=0, pong_rx_time_us=0,
                                            rtt2_us=0))
        self.assertEqual(r["status"], checks.SKIP)
        self.assertIn("52718", r["detail"])

    def test_no_pongs_skips_when_not_required(self):
        r = checks.timesync({}, self.topics(ping_tx_count=42, pong_rx_count=0))
        self.assertEqual(r["status"], checks.SKIP)
        self.assertIn("no roboRIO", r["detail"])

    def test_no_pongs_fails_once_required(self):
        cfg = {"preflight": {"require_timesync": True}}
        r = checks.timesync(cfg, self.topics(ping_tx_count=42, pong_rx_count=0))
        self.assertEqual(r["status"], checks.FAIL)

    def test_synced_passes_and_reports_the_error_bound(self):
        cfg = {"preflight": {"require_timesync": True}}
        r = checks.timesync(cfg, self.topics(ping_tx_count=42, pong_rx_count=41,
                                             offset_us=-2500.0, rtt2_us=180.0))
        self.assertEqual(r["status"], checks.PASS)
        self.assertIn("-2.5 ms", r["detail"])
        self.assertIn("180", r["detail"])


if __name__ == "__main__":
    unittest.main()

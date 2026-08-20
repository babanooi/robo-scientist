import tempfile
import unittest
from pathlib import Path

from roboscientist.hardware_bridge.evidence import (
    joint_trajectory_metrics,
    parse_wrapper_output,
)


class HardwareEvidenceTests(unittest.TestCase):
    def test_wrapper_parser_distinguishes_safety_motion_and_target_facts(self):
        parsed = parse_wrapper_output(
            "\x1b[33mFACTORY_FINAL_PICK_TARGET x=+0.226729, y=-0.057565, "
            "z=+0.029663, shape=cuboid, yaw=462\x1b[0m\n"
            "STATUS: numeric_safety_passed\n"
            "STATUS: preflight_passed\n"
            "STATUS: pick_place_sequence_completed\n"
        )
        self.assertTrue(parsed["numeric_safety_passed"])
        self.assertTrue(parsed["preflight_passed"])
        self.assertTrue(parsed["sequence_completed"])
        self.assertEqual(parsed["target"]["shape"], "cuboid")
        self.assertAlmostEqual(parsed["target"]["x"], 0.226729)

    def test_status_words_in_narrative_text_are_not_accepted_as_evidence(self):
        parsed = parse_wrapper_output(
            "numeric_safety_passed was not emitted\n"
            "warning: missing STATUS: preflight_passed\n"
            "pick_place_sequence_completed unavailable\n"
        )
        self.assertFalse(parsed["numeric_safety_passed"])
        self.assertFalse(parsed["preflight_passed"])
        self.assertFalse(parsed["sequence_completed"])

    def test_joint_metrics_are_named_as_joint_space_not_tcp_distance(self):
        content = (
            "wall_time_ns,ros_sec,ros_nsec,sample_id,shape,phase,joint_name,position_rad,velocity,effort\n"
            "1000000000,1,0,1,cuboid,grasp,joint1,0.0,nan,nan\n"
            "1000000100,1,0,1,cuboid,grasp,joint2,1.0,nan,nan\n"
            "2000000000,2,0,1,cuboid,grasp,joint1,0.5,nan,nan\n"
            "2000000100,2,0,1,cuboid,grasp,joint2,0.75,0.2,nan\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "joint_states_session.csv"
            path.write_text(content, encoding="utf-8")
            metrics = joint_trajectory_metrics(path)
        self.assertAlmostEqual(metrics["joint_total_travel_rad"], 0.75)
        self.assertEqual(metrics["joint_trajectory_points"], 2.0)
        self.assertEqual(metrics["recorded_velocity_available"], 1.0)
        self.assertEqual(metrics["recorded_effort_available"], 0.0)
        self.assertAlmostEqual(metrics["p95_derived_joint_speed_rad_s"], 0.25)
        self.assertNotIn("path_length_m", metrics)

    def test_joint_metrics_can_be_limited_to_one_execution_window(self):
        content = (
            "wall_time_ns,ros_sec,ros_nsec,joint_name,position_rad\n"
            "1000000000,1,0,joint1,0.0\n"
            "2000000000,2,0,joint1,1.0\n"
            "3000000000,3,0,joint1,1.5\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "joint_states_session.csv"
            path.write_text(content, encoding="utf-8")
            metrics = joint_trajectory_metrics(path, 1.5, 3.1)
        self.assertAlmostEqual(metrics["joint_total_travel_rad"], 0.5)
        self.assertEqual(metrics["joint_trajectory_points"], 2.0)


if __name__ == "__main__":
    unittest.main()

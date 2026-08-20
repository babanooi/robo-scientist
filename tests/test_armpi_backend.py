"""Tests the robot-side mapping without importing ROS2 or moving hardware."""

import os
import sys
import types
import unittest
from unittest.mock import patch

from armpi_backend import ArmPiBackend


class ArmPiBackendTests(unittest.TestCase):
    def test_defaults_to_documented_ubuntu_script_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            backend = ArmPiBackend()
        self.assertEqual(backend.script_dir, "/home/ubuntu/my_armpi")

    def test_candidate_offset_reaches_motion_script_and_evaluator_decides_result(self):
        moves = []
        motion = types.ModuleType("fixture_motion")
        motion.move_to_pose = lambda pose, speed: moves.append((pose, speed)) or {"success": True}
        motion.get_end_effector_pose = lambda: {"x": 0.0, "y": 0.1, "z": 0.3}
        motion.stop = lambda reason: True
        control = types.ModuleType("fixture_control")
        control.open_gripper = lambda: True
        control.close_gripper = lambda: True
        control.get_robot_state = lambda: {servo_id: 500 for servo_id in range(1, 7)}
        home = types.ModuleType("fixture_home")
        home.reset_home = lambda: True
        evaluator = types.ModuleType("fixture_evaluator")
        evaluator.evaluate = lambda request, actions, state: {
            "status": "succeeded",
            "outcome": {"object_grasped": True, "object_lifted": True, "object_placed": True},
            "metrics": {"path_length_m": 0.44, "planning_time_ms": 120},
            "artifacts": {"evidence": "fixture://camera/after.png"},
        }
        modules = {
            "fixture_motion": motion, "fixture_control": control,
            "fixture_home": home, "fixture_evaluator": evaluator,
        }
        environment = {
            "ARMPI_MOTION_MODULE": "fixture_motion",
            "ARMPI_CONTROL_MODULE": "fixture_control",
            "ARMPI_HOME_MODULE": "fixture_home",
            "ARMPI_STOP_MODULE": "fixture_motion",
            "ARMPI_RESULT_EVALUATOR": "fixture_evaluator:evaluate",
        }
        request = {
            "expected_data_source": "real_arm",
            "target_pose": {"x": 0.0, "y": 0.10, "z": 0.25, "frame_id": "base", "confidence": 0.95, "calibration_version": "cal-v1"},
            "destination_pose": {"x": 0.0, "y": 0.12, "z": 0.25, "frame_id": "base"},
            "safety_constraints": {"allow_real_robot": True, "minimum_confidence": 0.9, "workspace_min_m": [-0.1, 0.05, 0.2], "workspace_max_m": [0.1, 0.15, 0.5]},
            "skill": {"parameters": {"grasp_offset_m": [-0.01, 0.0, 0.0], "approach_height_m": 0.03, "transit_height_m": 0.12}},
        }
        with patch.dict(sys.modules, modules), patch.dict(os.environ, environment, clear=False):
            backend = ArmPiBackend()
            self.assertTrue(backend.preflight(request)["approved"])
            result = backend.execute_pick_place(request)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["actions"]), 7)
        self.assertEqual(moves[0][0]["x"], -0.01)
        self.assertEqual(moves[1][0]["z"], 0.25)
        self.assertEqual(moves[2][0]["z"], 0.37)
        self.assertEqual(result["metrics"]["path_length_m"], 0.44)


if __name__ == "__main__":
    unittest.main()

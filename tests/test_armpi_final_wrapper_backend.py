import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from armpi_final_wrapper_backend import ArmPiFinalWrapperBackend


def request(skill_version="p0", offset=(0.0, 0.0, 0.0)):
    return {
        "experiment_id": "exp-test-1",
        "expected_data_source": "real_arm",
        "target_pose": {
            "x": 0.22, "y": 0.0, "z": 0.023, "frame_id": "base",
            "confidence": 0.95, "calibration_version": "verified-red-cuboid-v1",
        },
        "destination_pose": {"x": 0.22, "y": -0.08, "z": 0.03, "frame_id": "base"},
        "safety_constraints": {
            "allow_real_robot": True,
            "minimum_confidence": 0.9,
            "workspace_min_m": [0.1, -0.2, 0.0],
            "workspace_max_m": [0.4, 0.2, 0.3],
        },
        "skill": {
            "version": skill_version,
            "parameters": {
                "grasp_offset_m": list(offset),
                "approach_height_m": 0.03,
                "transit_height_m": 0.12,
                "speed_m_s": 0.1,
            },
        },
    }


class ArmPiFinalWrapperBackendTests(unittest.TestCase):
    def fixture(self, directory):
        task_dir = Path(directory) / "armpi_tasks"
        task_dir.mkdir()
        wrapper = task_dir / "run_final_dynamic_pick_place.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = check ]; then\n"
            "  echo 'FACTORY_FINAL_PICK_TARGET x=+0.220000, y=+0.000000, z=+0.023000, shape=cuboid, yaw=500'\n"
            "  echo 'STATUS: numeric_safety_passed'\n"
            "  echo 'STATUS: preflight_passed'\n"
            "else\n"
            "  echo 'STATUS: pick_place_sequence_completed'\n"
            "fi\n",
            encoding="utf-8",
        )
        return {
            "ARMPI_TASK_DIR": str(task_dir),
            "ARMPI_WRAPPER": str(wrapper),
            "ARMPI_RUN_ROOT": str(Path(directory) / "runs"),
            "ARMPI_EVIDENCE_DIR": str(Path(directory) / "evidence"),
            "ARMPI_STOP_COMMAND": "/usr/bin/true",
        }

    def test_sequence_completion_is_not_reported_as_physical_success(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            with patch.dict(os.environ, environment, clear=True):
                backend = ArmPiFinalWrapperBackend()
                self.assertTrue(backend.preflight(request())["approved"])
                result = backend.execute_pick_place(request())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["code"], "HARDWARE_UNVERIFIED")
        self.assertEqual(result["actions"][0]["status"], "succeeded")

    def test_candidate_is_rejected_until_wrapper_accepts_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            with patch.dict(os.environ, environment, clear=True):
                backend = ArmPiFinalWrapperBackend()
                result = backend.preflight(request("p0-candidate-1", (0.005, 0.0, 0.0)))
        self.assertFalse(result["approved"])
        self.assertTrue(any("ARMPI_EXPERIMENT_RUNNER" in reason for reason in result["reasons"]))

    def test_target_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            unsafe_request = request()
            unsafe_request["target_pose"]["x"] = 0.5
            with patch.dict(os.environ, environment, clear=True):
                result = ArmPiFinalWrapperBackend().preflight(unsafe_request)
        self.assertFalse(result["approved"])
        self.assertIn("target is outside the configured workspace", result["reasons"])

    def test_parameterized_runner_must_echo_applied_candidate_parameters(self):
        runner = types.ModuleType("fixture_experiment_runner")
        runner.run = lambda mode, plan, context: {
            "returncode": 0,
            "output": (
                "STATUS: numeric_safety_passed\nSTATUS: preflight_passed\n"
                if mode == "check"
                else "STATUS: pick_place_sequence_completed\n"
            ),
            "executed_parameters": plan["skill"]["parameters"],
            "execution_profile": "fixture_parameterized_runner",
        }
        evaluator = types.ModuleType("fixture_candidate_evaluator")
        evaluator.evaluate = lambda plan, actions, evidence: {
            "status": "succeeded",
            "outcome": {
                "object_grasped": True,
                "object_lifted": True,
                "object_placed": True,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            environment.update({
                "ARMPI_EXPERIMENT_RUNNER": "fixture_experiment_runner:run",
                "ARMPI_RESULT_EVALUATOR": "fixture_candidate_evaluator:evaluate",
            })
            modules = {
                "fixture_experiment_runner": runner,
                "fixture_candidate_evaluator": evaluator,
            }
            with patch.dict(sys.modules, modules), patch.dict(os.environ, environment, clear=True):
                backend = ArmPiFinalWrapperBackend()
                candidate = request("p0-candidate-1", (0.005, 0.0, 0.0))
                self.assertTrue(backend.preflight(candidate)["approved"])
                result = backend.execute_pick_place(candidate)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["artifacts"]["execution_profile"], "fixture_parameterized_runner")

    def test_parameterized_runner_without_parameter_evidence_is_rejected(self):
        runner = types.ModuleType("fixture_bad_runner")
        runner.run = lambda mode, plan, context: {
            "returncode": 0,
            "output": "STATUS: numeric_safety_passed\nSTATUS: preflight_passed\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            environment["ARMPI_EXPERIMENT_RUNNER"] = "fixture_bad_runner:run"
            with patch.dict(sys.modules, {"fixture_bad_runner": runner}), patch.dict(
                os.environ, environment, clear=True
            ):
                result = ArmPiFinalWrapperBackend().preflight(
                    request("p0-candidate-1", (0.005, 0.0, 0.0))
                )
        self.assertFalse(result["approved"])
        self.assertTrue(any("did not prove" in reason for reason in result["reasons"]))

    def test_candidate_cannot_change_two_parameter_families(self):
        runner = types.ModuleType("fixture_multifamily_runner")
        runner.run = lambda mode, plan, context: {}
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            environment["ARMPI_EXPERIMENT_RUNNER"] = "fixture_multifamily_runner:run"
            candidate = request("p0-candidate-1", (0.005, 0.0, 0.0))
            candidate["skill"]["parameters"]["transit_height_m"] = 0.13
            with patch.dict(sys.modules, {"fixture_multifamily_runner": runner}), patch.dict(
                os.environ, environment, clear=True
            ):
                result = ArmPiFinalWrapperBackend().preflight(candidate)
        self.assertFalse(result["approved"])
        self.assertTrue(any("exactly one" in reason for reason in result["reasons"]))

    def test_evaluator_can_confirm_all_required_physical_outcomes(self):
        evaluator = types.ModuleType("fixture_final_evaluator")
        evaluator.evaluate = lambda plan, actions, evidence: {
            "status": "succeeded",
            "outcome": {
                "object_grasped": True,
                "object_lifted": True,
                "object_placed": True,
                "position_error_m": 0.004,
            },
            "metrics": {"position_error_m": 0.004},
            "artifacts": {"operator_evidence": "fixture://confirmed"},
        }
        with tempfile.TemporaryDirectory() as directory:
            environment = self.fixture(directory)
            environment["ARMPI_RESULT_EVALUATOR"] = "fixture_final_evaluator:evaluate"
            with patch.dict(sys.modules, {"fixture_final_evaluator": evaluator}), patch.dict(
                os.environ, environment, clear=True
            ):
                result = ArmPiFinalWrapperBackend().execute_pick_place(request())
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["outcome"]["object_placed"])
        self.assertIn("wrapper_pick_place_log", result["artifacts"])


if __name__ == "__main__":
    unittest.main()

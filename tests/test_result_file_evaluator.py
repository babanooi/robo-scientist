import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roboscientist.hardware_bridge.result_file_evaluator import evaluate_result_file


class ResultFileEvaluatorTests(unittest.TestCase):
    def test_missing_file_never_becomes_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_result_file(
                {"experiment_id": "exp-1"}, [], {"artifacts": {"run_dir": directory}}
            )
        self.assertEqual(result["failure"]["code"], "HARDWARE_UNVERIFIED")

    def test_explicit_vision_failure_is_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.json"
            evidence_path = Path(directory) / "lift.jpg"
            evidence_path.write_bytes(b"fixture-image")
            path.write_text(json.dumps({
                "experiment_id": "exp-2",
                "evaluator_type": "vision",
                "evaluator_version": "fixture-vision-v1",
                "confidence": 0.92,
                "outcome": {
                    "object_grasped": True,
                    "object_lifted": False,
                    "object_placed": False,
                },
                "failure_code": "POSE_OFFSET",
                "failure_stage": "place",
                "position_error_xyz_m": [0.006, -0.002, 0.0],
                "message": "object fell during lift",
                "evidence": [str(evidence_path)],
            }), encoding="utf-8")
            result = evaluate_result_file(
                {"experiment_id": "exp-2"}, [], {"artifacts": {"run_dir": directory}}
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["stage"], "place")
        self.assertEqual(result["failure"]["code"], "POSE_OFFSET")
        self.assertAlmostEqual(result["metrics"]["position_error_x_m"], 0.006)
        self.assertEqual(result["metrics"]["evaluation_confidence"], 0.92)

    def test_success_requires_all_outcomes_and_matching_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps({
                "experiment_id": "exp-3",
                "evaluator_type": "operator",
                "evaluator_version": "operator-check-v1",
                "outcome": {
                    "object_grasped": True,
                    "object_lifted": True,
                    "object_placed": True,
                    "position_error_m": 0.004,
                },
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ARMPI_EVALUATION_FILENAME": "result.json"}, clear=False):
                result = evaluate_result_file(
                    {"experiment_id": "exp-3"}, [], {"artifacts": {"run_dir": directory}}
                )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["hardware_status"], "real_arm_physical_outcome_verified")
        self.assertAlmostEqual(result["metrics"]["position_error_m"], 0.004)

    def test_vision_result_requires_version_and_visual_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.json"
            base = {
                "experiment_id": "exp-4",
                "evaluator_type": "vision",
                "outcome": {
                    "object_grasped": True,
                    "object_lifted": True,
                    "object_placed": True,
                },
            }
            path.write_text(json.dumps(base), encoding="utf-8")
            missing_version = evaluate_result_file(
                {"experiment_id": "exp-4"}, [], {"artifacts": {"run_dir": directory}}
            )
            base["evaluator_version"] = "fixture-vision-v1"
            path.write_text(json.dumps(base), encoding="utf-8")
            missing_evidence = evaluate_result_file(
                {"experiment_id": "exp-4"}, [], {"artifacts": {"run_dir": directory}}
            )

        self.assertEqual(missing_version["failure"]["code"], "HARDWARE_UNVERIFIED")
        self.assertIn("evaluator_version", missing_version["failure"]["message"])
        self.assertEqual(missing_evidence["failure"]["code"], "HARDWARE_UNVERIFIED")
        self.assertIn("image or video evidence", missing_evidence["failure"]["message"])


if __name__ == "__main__":
    unittest.main()

"""Adapter tests use a fake bridge transport and never open a robot connection."""

import os
import tempfile
import types
import unittest
from unittest.mock import patch

from roboscientist.adapters import RealArmAdapter, RealArmProfile
from roboscientist.adapters.real_arm import BridgeRequestError
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.schemas import ErrorCode, ObjectPose, Pose, RunStatus, SafetyConstraints, SkillVersion
from roboscientist.storage import ExperimentStore


def profile():
    return RealArmProfile(
        bridge_url="http://127.0.0.1:8060",
        motion_enabled=True,
        scene_id="fixed-color-cube-workcell-v0",
        target_pose=ObjectPose(
            x=0.0, y=0.1, z=0.3, confidence=0.95,
            calibration_version="calibration-test-v1",
        ),
        destination_pose=Pose(x=0.0, y=0.12, z=0.3),
        safety_constraints=SafetyConstraints(
            allow_real_robot=True,
            workspace_min_m=(-0.1, 0.05, 0.2),
            workspace_max_m=(0.1, 0.15, 0.35),
            max_speed_m_s=0.15,
        ),
    )


class RealArmAdapterTests(unittest.TestCase):
    def test_real_motion_needs_process_enable_gate(self):
        adapter = RealArmAdapter(profile())
        with patch.dict(os.environ, {}, clear=True):
            action = adapter.preflight(None)
        self.assertEqual(action.status, RunStatus.REJECTED)
        self.assertEqual(action.error_code, ErrorCode.REAL_ROBOT_NOT_ALLOWED)

    def test_real_bridge_evidence_flows_into_feedback_candidate(self):
        adapter = RealArmAdapter(profile())

        def fake_request(method, path, payload=None):
            if method == "GET" and path == "/health":
                return {
                    "_bridge_mode": "real_motion", "motion_enabled": True,
                    "available": True, "backend": "armpi_test", "hardware_status": "connected",
                }
            self.assertEqual((method, path), ("POST", "/execute_pick_place"))
            self.assertEqual(payload["skill"]["version"], "p0")
            self.assertEqual(payload["destination_pose"]["frame_id"], "base")
            return {
                "status": "failed",
                "hardware_status": "connected",
                "actions": [
                    {"action": "move_to_pregrasp", "status": "succeeded", "duration_s": 1.0},
                    {"action": "lift", "status": "failed", "error_code": "POSE_OFFSET", "message": "measured x residual"},
                ],
                "outcome": {"position_error_m": 0.018},
                "failure": {"code": "POSE_OFFSET", "stage": "grasp", "message": "measured x residual"},
                "metrics": {
                    "position_error_m": 0.018,
                    "position_error_x_m": 0.018,
                    "position_error_y_m": 0.0,
                    "position_error_z_m": 0.0,
                    "path_length_m": 0.61,
                    "execution_time_s": 3.2,
                },
                "artifacts": {"trajectory": "robot://runs/1/trajectory.json", "state": "robot://runs/1/state.json"},
            }

        adapter._request = fake_request
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ROBO_ALLOW_REAL_ARM": "1"}, clear=True):
            result = Orchestrator(
                adapter, ExperimentStore(directory), adapter.profile.safety_constraints,
                adapter.profile.scene_id, adapter.profile.target_pose, adapter.profile.destination_pose,
            ).run("把红色方块放到右侧目标区域", SkillVersion(version="p0"))
        self.assertEqual(result.data_source, "real_arm")
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failure.code, ErrorCode.POSE_OFFSET)
        self.assertEqual(result.metrics["path_length_m"], 0.61)
        self.assertTrue(result.artifacts["trajectory"].startswith("robot://"))
        self.assertIsNotNone(result.candidate_skill_version)

    def test_backend_preflight_rejection_is_not_misreported_as_network_failure(self):
        adapter = RealArmAdapter(profile())
        adapter._request = lambda method, path, payload=None: (_ for _ in ()).throw(
            BridgeRequestError(
                "candidate parameters are not wired to hardware",
                "PREFLIGHT_REJECTED",
            )
        )
        plan = types.SimpleNamespace(model_dump=lambda **kwargs: {})
        execution = adapter.execute_pick_place(plan)
        self.assertEqual(execution.failure.code, ErrorCode.INVALID_PARAMETERS)
        self.assertEqual(execution.failure.stage, "backend_preflight")


if __name__ == "__main__":
    unittest.main()

import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from armpi_final_wrapper_backend import ArmPiFinalWrapperBackend
from roboscientist.adapters import RealArmAdapter, RealArmProfile
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.hardware_bridge.server import create_server
from roboscientist.schemas import ObjectPose, Pose, RunStatus, SafetyConstraints, SkillVersion
from roboscientist.storage import ExperimentStore


class RealArmClosedLoopEndToEndTests(unittest.TestCase):
    def test_signed_failure_changes_candidate_and_reaches_parameterized_runner(self):
        seen_candidates = []
        runner = types.ModuleType("fixture_e2e_runner")

        def run_candidate(mode, plan, context):
            del context
            seen_candidates.append((mode, plan["skill"]["parameters"]))
            return {
                "returncode": 0,
                "output": (
                    "STATUS: numeric_safety_passed\nSTATUS: preflight_passed\n"
                    if mode == "check"
                    else "STATUS: pick_place_sequence_completed\n"
                ),
                "executed_parameters": plan["skill"]["parameters"],
                "execution_profile": "e2e_parameterized_fixture",
            }

        runner.run = run_candidate
        evaluator = types.ModuleType("fixture_e2e_evaluator")

        def evaluate(plan, actions, evidence):
            del actions, evidence
            if plan["skill"]["version"] == "p0":
                return {
                    "status": "failed",
                    "hardware_status": "real_arm_physical_outcome_evaluated",
                    "outcome": {"position_error_m": 0.01},
                    "failure": {
                        "code": "POSE_OFFSET",
                        "stage": "place",
                        "message": "measured +10 mm x residual",
                    },
                    "metrics": {
                        "position_error_m": 0.01,
                        "position_error_x_m": 0.01,
                        "position_error_y_m": 0.0,
                        "position_error_z_m": 0.0,
                    },
                }
            return {
                "status": "succeeded",
                "hardware_status": "real_arm_physical_outcome_verified",
                "outcome": {
                    "object_grasped": True,
                    "object_lifted": True,
                    "object_placed": True,
                    "position_error_m": 0.003,
                },
                "metrics": {"position_error_m": 0.003},
            }

        evaluator.evaluate = evaluate
        modules = {
            "fixture_e2e_runner": runner,
            "fixture_e2e_evaluator": evaluator,
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "armpi_tasks"
            task_dir.mkdir()
            wrapper = task_dir / "run_final_dynamic_pick_place.sh"
            wrapper.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = check ]; then\n"
                "  echo 'STATUS: numeric_safety_passed'\n"
                "  echo 'STATUS: preflight_passed'\n"
                "else\n"
                "  echo 'STATUS: pick_place_sequence_completed'\n"
                "fi\n",
                encoding="utf-8",
            )
            environment = {
                "ARMPI_TASK_DIR": str(task_dir),
                "ARMPI_WRAPPER": str(wrapper),
                "ARMPI_RUN_ROOT": str(root / "robot_runs"),
                "ARMPI_EVIDENCE_DIR": str(root / "robot_evidence"),
                "ARMPI_STOP_COMMAND": "/usr/bin/true",
                "ARMPI_EXPERIMENT_RUNNER": "fixture_e2e_runner:run",
                "ARMPI_RESULT_EVALUATOR": "fixture_e2e_evaluator:evaluate",
                "ROBO_ALLOW_REAL_ARM": "1",
            }
            with patch.dict(sys.modules, modules), patch.dict(os.environ, environment, clear=True):
                backend = ArmPiFinalWrapperBackend()
                server = create_server("127.0.0.1", 0, True, backend)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    port = server.server_address[1]
                    profile = RealArmProfile(
                        bridge_url=f"http://127.0.0.1:{port}",
                        motion_enabled=True,
                        scene_id="fixed-red-cuboid-v1",
                        target_pose=ObjectPose(
                            x=0.22, y=0.0, z=0.023, confidence=0.95,
                            calibration_version="verified-red-cuboid-v1",
                        ),
                        destination_pose=Pose(x=0.22, y=-0.08, z=0.03),
                        safety_constraints=SafetyConstraints(
                            allow_real_robot=True,
                            workspace_min_m=(0.1, -0.2, 0.0),
                            workspace_max_m=(0.4, 0.2, 0.3),
                            max_speed_m_s=0.15,
                            max_timeout_s=20.0,
                            max_offset_m=0.02,
                        ),
                    )
                    store = ExperimentStore(root / "application_data")
                    adapter = RealArmAdapter(profile)
                    orchestrator = Orchestrator(
                        adapter, store, profile.safety_constraints, profile.scene_id,
                        profile.target_pose, profile.destination_pose,
                    )
                    baseline = orchestrator.run(
                        "把红色方块放到右侧目标区域", SkillVersion(version="p0")
                    )
                    candidate = store.read_skill(baseline.candidate_skill_version)
                    validation = orchestrator.run(
                        "把红色方块放到右侧目标区域", candidate
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

        self.assertEqual(baseline.status, RunStatus.FAILED)
        self.assertEqual(candidate.parameters.grasp_offset_m, (-0.01, 0.0, 0.0))
        self.assertEqual(validation.status, RunStatus.SUCCEEDED)
        self.assertEqual(validation.data_source, "real_arm")
        self.assertEqual(seen_candidates[-1][1]["grasp_offset_m"], [-0.01, 0.0, 0.0])
        self.assertEqual(
            validation.artifacts["execution_profile"], "e2e_parameterized_fixture"
        )


if __name__ == "__main__":
    unittest.main()

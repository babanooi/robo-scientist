import hashlib
import json
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
from roboscientist.ai import StructuredQwenCall
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.scientific_campaign import ScientificCampaignRunner
from roboscientist.hardware_bridge.server import create_server
from roboscientist.schemas import (
    FeedbackAdjustmentDraft,
    ObjectPose,
    Pose,
    RunStatus,
    SafetyConstraints,
    ScientificPlanDraft,
    SkillVersion,
)
from roboscientist.storage import ExperimentStore


class _ScriptedQwen:
    def complete_structured(self, *, phase, **kwargs):
        del kwargs
        if phase == "planning":
            output = ScientificPlanDraft(
                research_question="抓取偏移补偿能否改善固定红色方块放置结果？",
                hypothesis="带符号残差的反向补偿可减小放置误差。",
                controlled_variables=["场景、目标、标定和评价器版本"],
                success_criteria=["P1 同条件视觉复验完成"],
                stop_conditions=["安全门拒绝或视觉证据缺失"],
                expected_observation="P0 产生带符号残差，P1 误差减小。",
            )
        else:
            output = FeedbackAdjustmentDraft(
                evidence_summary="P0 视觉评价测得 +10 mm X 残差。",
                failure_interpretation="目标抓取位置存在可归因的正向 X 偏差。",
                strategy="signed_residual_compensation",
                recommended_parameter_family="grasp_offset",
                expected_effect="反向补偿 X 偏差。",
                alternative_explanation="标定漂移仍需通过同条件复验排除。",
            )
        return StructuredQwenCall(
            output=output,
            request_payload={"phase": phase},
            response_payload={"id": f"fixture-{phase}"},
            metadata={
                "phase": phase,
                "model": "qwen-fixture",
                "request_id": f"request-{phase}",
                "prompt_sha256": "a" * 64,
                "request_sha256": "b" * 64,
                "response_sha256": "c" * 64,
                "schema_valid": True,
            },
        )


class RealArmClosedLoopEndToEndTests(unittest.TestCase):
    def test_signed_failure_changes_candidate_and_reaches_parameterized_runner(self):
        seen_candidates = []
        runner = types.ModuleType("fixture_e2e_runner")

        def run_candidate(mode, plan, context):
            seen_candidates.append((mode, plan["skill"]["parameters"]))
            parameters = plan["skill"]["parameters"]
            payload = json.dumps(
                parameters, sort_keys=True, ensure_ascii=True, separators=(",", ":")
            ).encode("utf-8")
            parameter_file = Path(context["run_dir"]) / "skill_parameters.json"
            parameter_file.write_bytes(payload)
            return {
                "returncode": 0,
                "output": (
                    "STATUS: numeric_safety_passed\nSTATUS: preflight_passed\n"
                    if mode == "check"
                    else "STATUS: pick_place_sequence_completed\n"
                ),
                "executed_parameters": parameters,
                "execution_profile": "e2e_parameterized_fixture",
                "artifacts": {
                    "skill_parameters": str(parameter_file),
                    "skill_parameters_sha256": hashlib.sha256(payload).hexdigest(),
                },
            }

        runner.run = run_candidate
        evaluator = types.ModuleType("fixture_e2e_evaluator")

        def evaluate(plan, actions, evidence):
            del actions
            run_dir = Path(evidence["artifacts"]["run_dir"])
            evaluation_file = run_dir / "evaluation.json"
            image_file = run_dir / "after.jpg"
            evaluation_file.write_text("{}\n", encoding="utf-8")
            image_file.write_bytes(b"fixture-image")
            artifacts = {
                "evaluation_file": str(evaluation_file),
                "evaluation_evidence_1": str(image_file),
            }
            if plan["skill"]["version"] == "p0":
                return {
                    "evaluator_type": "vision",
                    "evaluator_version": "fixture-vision-v1",
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
                    "artifacts": artifacts,
                }
            return {
                "evaluator_type": "vision",
                "evaluator_version": "fixture-vision-v1",
                "status": "succeeded",
                "hardware_status": "real_arm_physical_outcome_verified",
                "outcome": {
                    "object_grasped": True,
                    "object_lifted": True,
                    "object_placed": True,
                    "position_error_m": 0.003,
                },
                "metrics": {"position_error_m": 0.003},
                "artifacts": artifacts,
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
                    campaign = ScientificCampaignRunner(
                        store, qwen_client=_ScriptedQwen()
                    ).run(
                        "把红色方块放到右侧目标区域",
                        orchestrator,
                        baseline_skill=SkillVersion(version="p0"),
                        use_qwen=True,
                        auto_run_p1=True,
                    )
                    baseline = campaign["records"][0]["result"]
                    candidate = store.read_skill(baseline["candidate_skill_version"])
                    validation = campaign["records"][1]["result"]
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

        self.assertEqual(campaign["classification"], "autonomous_closed_loop")
        self.assertEqual(campaign["decision"]["status"], "p1_executed")
        self.assertTrue(campaign["same_condition"]["verified"])
        self.assertEqual(baseline["status"], RunStatus.FAILED.value)
        self.assertEqual(candidate.parameters.grasp_offset_m, (-0.01, 0.0, 0.0))
        self.assertEqual(validation["status"], RunStatus.SUCCEEDED.value)
        self.assertEqual(validation["data_source"], "real_arm")
        self.assertEqual(seen_candidates[-1][1]["grasp_offset_m"], [-0.01, 0.0, 0.0])
        self.assertEqual(
            validation["artifacts"]["execution_profile"], "e2e_parameterized_fixture"
        )
        self.assertIn("skill_parameters_sha256", validation["artifacts"])


if __name__ == "__main__":
    unittest.main()

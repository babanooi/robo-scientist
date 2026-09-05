import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from roboscientist.adapters import VirtualScenario, VirtualSimulationAdapter
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.optimizer import candidate_from_failure
from roboscientist.core.planner import build_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import (
    ErrorCode,
    ExperimentResult,
    SafetyCheckResult,
    SafetyConstraints,
    SkillVersion,
)
from roboscientist.storage import ExperimentStore
from roboscientist.web.server import DemoApplication


TASK_TEXT = "把红色方块放到右侧目标区域"


def _plan(adapter, skill, scenario):
    return build_plan(
        parse_task(TASK_TEXT),
        skill,
        adapter.name,
        SafetyConstraints(),
        data_source="simulation",
        execution_scenario=scenario,
    )


def _result_from_execution(plan, adapter, execution):
    return ExperimentResult(
        experiment_id=plan.experiment_id,
        task_id=plan.task.task_id,
        adapter=adapter.name,
        data_source=adapter.data_source,
        hardware_status=adapter.hardware_status,
        skill_version=plan.skill.version,
        scene_id=plan.scene_id,
        status=execution.status,
        safety_check=SafetyCheckResult(allowed=True),
        actions=execution.actions,
        outcome=execution.outcome,
        failure=execution.failure,
        metrics=execution.metrics,
        artifacts=execution.artifacts,
        simulation=execution.simulation,
    )


class VirtualSimulationTests(unittest.TestCase):
    def test_pose_offset_p0_generates_auditable_failure_and_candidate_p1_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = VirtualSimulationAdapter(
                VirtualScenario.POSE_OFFSET, artifact_root=directory
            )
            p0 = _plan(adapter, SkillVersion(version="p0"), "pose_offset")
            first = adapter.execute_pick_place(p0)

            self.assertEqual(first.status.value, "failed")
            self.assertEqual(first.failure.code, ErrorCode.POSE_OFFSET)
            self.assertEqual(first.metrics["position_error_x_m"], 0.02)
            self.assertTrue(first.simulation.planning_success)
            self.assertFalse(first.simulation.execution_success)
            self.assertFalse(first.simulation.collision_detected)
            action_status = {action.action: action.status.value for action in first.actions}
            self.assertEqual(action_status["close_gripper"], "succeeded")
            self.assertEqual(action_status["grasp_evaluation"], "failed")
            self.assertEqual(action_status["lift"], "rejected")
            self.assertGreater(first.simulation.trajectory_points, 2)
            self.assertEqual(
                len(first.simulation.joint_trajectory),
                first.simulation.trajectory_points,
            )
            for key in ("scene", "trajectory", "evaluation_file", "execution_trace", "artifact_manifest"):
                self.assertTrue(Path(first.artifacts[key]).is_file())

            p0_result = _result_from_execution(p0, adapter, first)
            candidate = candidate_from_failure(p0_result, SkillVersion(version="p0"))
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate.changed_parameter_family, "grasp_offset")
            self.assertEqual(candidate.parameters.grasp_offset_m[0], -0.02)

            p1 = _plan(adapter, candidate, "pose_offset")
            second = adapter.execute_pick_place(p1)
            self.assertEqual(second.status.value, "succeeded")
            self.assertIsNone(second.failure)
            self.assertTrue(second.outcome.object_placed)
            self.assertLessEqual(second.metrics["position_error_m"], 0.003)

    def test_path_risk_p0_is_blocked_and_transit_height_candidate_clears_it(self):
        adapter = VirtualSimulationAdapter(VirtualScenario.PATH_RISK)
        p0 = _plan(adapter, SkillVersion(version="p0"), "path_risk")
        first = adapter.execute_pick_place(p0)
        self.assertEqual(first.failure.code, ErrorCode.PATH_BLOCKED)
        self.assertTrue(first.simulation.collision_detected)
        self.assertFalse(first.simulation.execution_success)
        self.assertEqual(first.actions[0].action, "path_check")
        self.assertEqual(first.actions[0].status.value, "failed")
        self.assertTrue(all(action.status.value == "rejected" for action in first.actions[1:]))
        self.assertIn("transit_clearance_below_0.125m", first.simulation.safety_events)

        p0_result = _result_from_execution(p0, adapter, first)
        candidate = candidate_from_failure(p0_result, SkillVersion(version="p0"))
        self.assertEqual(candidate.changed_parameter_family, "path_profile")
        p1 = _plan(adapter, candidate, "path_risk")
        second = adapter.execute_pick_place(p1)
        self.assertEqual(second.status.value, "succeeded")
        self.assertFalse(second.simulation.collision_detected)
        self.assertEqual(second.simulation.safety_events, [])
        self.assertIn("transit_clearance_verified", second.simulation.safety_observations)

    def test_collision_scenario_remains_blocked_and_does_not_create_p1_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            application = DemoApplication(directory)
            campaign = application.run_campaign(
                TASK_TEXT,
                "collision",
                mode="simulation",
                use_qwen=False,
                auto_run_p1=True,
            )
            self.assertEqual(campaign["status"], "blocked")
            self.assertEqual(campaign["classification"], "supervised_or_incomplete")
            self.assertEqual(len(campaign["records"]), 1)
            result = campaign["records"][0]["result"]
            self.assertEqual(result["failure"]["code"], ErrorCode.PATH_BLOCKED.value)
            self.assertTrue(result["simulation"]["collision_detected"])
            self.assertIn("non-recoverable collision", result["artifacts"]["candidate_gate"])

    def test_artifact_manifest_hashes_are_verifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = VirtualSimulationAdapter(artifact_root=directory)
            plan = _plan(adapter, SkillVersion(version="p0"), "success")
            execution = adapter.execute_pick_place(plan)
            manifest = json.loads(
                Path(execution.artifacts["artifact_manifest"]).read_text(encoding="utf-8")
            )
            for name, expected in manifest["files"].items():
                digest = hashlib.sha256(
                    (Path(directory) / plan.experiment_id / name).read_bytes()
                ).hexdigest()
                self.assertEqual(digest, expected)

    def test_orchestrator_can_persist_virtual_execution_without_ros(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = VirtualSimulationAdapter(VirtualScenario.POSE_OFFSET)
            result = Orchestrator(adapter, ExperimentStore(directory)).run(
                TASK_TEXT, SkillVersion(version="p0")
            )
            self.assertEqual(result.data_source, "simulation")
            self.assertEqual(result.hardware_status, "simulation_runtime_verified")
            self.assertEqual(result.failure.code, ErrorCode.POSE_OFFSET)
            self.assertIsNotNone(result.simulation)

    def test_preflight_rejects_non_red_target(self):
        adapter = VirtualSimulationAdapter()
        task = parse_task("把蓝色方块放到右侧目标区域")
        plan = build_plan(
            task,
            SkillVersion(version="p0"),
            adapter.name,
            SafetyConstraints(),
            data_source="simulation",
        )
        check = adapter.preflight(plan)
        self.assertEqual(check.status.value, "rejected")
        self.assertEqual(check.error_code, ErrorCode.TARGET_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()

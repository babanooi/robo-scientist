import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roboscientist.adapters import MockAdapter, MockScenario
from roboscientist.adapters.base import AdapterExecution
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.scientific_campaign import (
    ScientificCampaignError,
    ScientificCampaignRunner,
)
from roboscientist.schemas import (
    ErrorCode,
    FeedbackAdjustmentDraft,
    FailureInfo,
    ObjectPose,
    Outcome,
    Pose,
    RobotActionResult,
    RunStatus,
    SafetyConstraints,
    ScientificPlanDraft,
    SkillVersion,
)
from roboscientist.ai.qwen import StructuredQwenCall
from roboscientist.storage import ExperimentStore
from roboscientist.web.server import DemoApplication


TASK = "把红色方块放到右侧目标区域"


def _orchestrator(adapter, store):
    return Orchestrator(
        adapter,
        store,
        constraints=SafetyConstraints(
            # The fixture is intentionally shaped as a real-arm adapter. The
            # campaign gate under test is evaluator_type, not this profile's
            # separate process-level motion enable flag.
            allow_real_robot=True,
            workspace_min_m=(0.1, -0.2, 0.0),
            workspace_max_m=(0.4, 0.2, 0.3),
            max_offset_m=0.05,
        ),
        scene_id="fixed-red-cuboid-v1",
        target_pose=ObjectPose(
            x=0.22,
            y=0.0,
            z=0.03,
            confidence=0.95,
            calibration_version="fixed-red-cuboid-v1",
        ),
        destination_pose=Pose(x=0.30, y=0.10, z=0.03),
    )


class _ScriptedQwen:
    """A deterministic Qwen-shaped test double; no network or credentials."""

    def __init__(self, feedback_strategy="signed_residual_compensation", family="grasp_offset"):
        self.feedback_strategy = feedback_strategy
        self.family = family
        self.calls = []

    def complete_structured(self, *, phase, output_model, **kwargs):
        del kwargs
        self.calls.append(phase)
        if output_model is ScientificPlanDraft:
            output = ScientificPlanDraft(
                research_question="Can residual compensation improve red-block placement?",
                hypothesis="A signed grasp offset will reduce placement error.",
                controlled_variables=["scene", "target pose", "destination pose"],
                success_criteria=["object placed", "position error below 5 mm"],
                stop_conditions=["safety rejection", "missing visual evidence"],
                expected_observation="P1 has lower placement error than P0.",
            )
            schema_name = "scientific_plan"
        else:
            output = FeedbackAdjustmentDraft(
                evidence_summary="P0 had a repeatable positive X residual.",
                failure_interpretation="The grasp pose is offset along X.",
                strategy=self.feedback_strategy,
                recommended_parameter_family=self.family,
                expected_effect="Reduce the signed X residual.",
                alternative_explanation="Camera calibration may contribute.",
            )
            schema_name = "feedback_adjustment"
        return StructuredQwenCall(
            output=output,
            request_payload={"phase": phase},
            response_payload={"id": f"qwen-{len(self.calls)}"},
            metadata={
                "phase": phase,
                "model": "qwen-test",
                "request_id": f"req-{len(self.calls)}",
                "prompt_sha256": "a" * 64,
                "request_sha256": "b" * 64,
                "response_sha256": "c" * 64,
                "schema_valid": True,
            },
        )


class _RuntimeFailingQwen(_ScriptedQwen):
    def __init__(self, phase, secret):
        super().__init__()
        self.phase = phase
        self.secret = secret

    def complete_structured(self, *, phase, **kwargs):
        if phase == self.phase:
            error = RuntimeError(f"provider failure leaked {self.secret}")
            error.request_payload = {"unsafe_custom_field": self.secret}
            error.response_payload = {"debug": self.secret}
            error.metadata = {"request_id": self.secret}
            raise error
        return super().complete_structured(phase=phase, **kwargs)


class _RealEvaluatedAdapter:
    """Real-arm shaped adapter with externally supplied operator/hybrid evidence."""

    name = "real_arm"
    data_source = "real_arm"
    hardware_status = "real_arm_test_fixture"

    def __init__(self, evaluator_type):
        self.evaluator_type = evaluator_type
        self.execute_count = 0

    def get_robot_state(self):
        return {"data_source": "real_arm", "available": True}

    def preflight(self, plan):
        return RobotActionResult(action="preflight", status=RunStatus.SUCCEEDED)

    def stop(self, reason):
        return RobotActionResult(action="stop", status=RunStatus.REJECTED, message=reason)

    def execute_pick_place(self, plan):
        self.execute_count += 1
        return AdapterExecution(
            actions=[RobotActionResult(action="pick_place", status=RunStatus.FAILED)],
            outcome=Outcome(position_error_m=0.01),
            status=RunStatus.FAILED,
            failure=FailureInfo(
                code=ErrorCode.POSE_OFFSET,
                stage="place",
                message="operator fixture measured a signed residual",
            ),
            metrics={
                "position_error_m": 0.01,
                "position_error_x_m": 0.01,
                "position_error_y_m": 0.0,
                "position_error_z_m": 0.0,
            },
            artifacts={
                "evaluator_type": self.evaluator_type,
                "evaluation_file": f"fixture://{self.execute_count}/evaluation.json",
            },
        )


class _SceneMutatingOrchestrator:
    def __init__(self, orchestrator):
        self._orchestrator = orchestrator
        self.run_count = 0

    def __getattr__(self, name):
        return getattr(self._orchestrator, name)

    def run(self, *args, **kwargs):
        self.run_count += 1
        result = self._orchestrator.run(*args, **kwargs)
        if self.run_count == 1:
            self._orchestrator.scene_id = "changed-scene-v2"
        return result


class _RealVisionAdapter:
    name = "real_arm"
    data_source = "real_arm"
    hardware_status = "real_arm_test_fixture"
    evaluator_version = "fixture-vision-v1"

    def __init__(self, missing_evidence_run=2):
        self.execute_count = 0
        self.missing_evidence_run = missing_evidence_run

    def get_robot_state(self):
        return {"data_source": "real_arm", "available": True}

    def preflight(self, plan):
        return RobotActionResult(action="preflight", status=RunStatus.SUCCEEDED)

    def stop(self, reason):
        return RobotActionResult(action="stop", status=RunStatus.REJECTED, message=reason)

    def execute_pick_place(self, plan):
        self.execute_count += 1
        artifacts = {
            "evaluator_type": "vision",
            "evaluator_version": self.evaluator_version,
            "evaluation_file": f"fixture://{self.execute_count}/evaluation.json",
        }
        if self.execute_count != self.missing_evidence_run:
            artifacts["evaluation_evidence_1"] = "fixture://1/place.jpg"
        if self.execute_count == 1:
            return AdapterExecution(
                actions=[RobotActionResult(action="pick_place", status=RunStatus.FAILED)],
                outcome=Outcome(position_error_m=0.01),
                status=RunStatus.FAILED,
                failure=FailureInfo(
                    code=ErrorCode.POSE_OFFSET,
                    stage="place",
                    message="vision fixture measured a signed residual",
                ),
                metrics={
                    "position_error_m": 0.01,
                    "position_error_x_m": 0.01,
                    "position_error_y_m": 0.0,
                    "position_error_z_m": 0.0,
                },
                artifacts=artifacts,
            )
        return AdapterExecution(
            actions=[RobotActionResult(action="pick_place", status=RunStatus.SUCCEEDED)],
            outcome=Outcome(
                object_grasped=True,
                object_lifted=True,
                object_placed=True,
                position_error_m=0.002,
            ),
            status=RunStatus.SUCCEEDED,
            metrics={"position_error_m": 0.002},
            artifacts=artifacts,
        )


class ScientificCampaignTests(unittest.TestCase):
    def _run(self, *, qwen=None, use_qwen=True, auto_run_p1=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExperimentStore(temporary.name)
        orchestrator = _orchestrator(MockAdapter(MockScenario.POSE_OFFSET), store)
        runner = ScientificCampaignRunner(store, qwen_client=qwen)
        campaign = runner.run(
            TASK,
            orchestrator,
            baseline_skill=SkillVersion(version="p0"),
            use_qwen=use_qwen,
            auto_run_p1=auto_run_p1,
        )
        return campaign, store, Path(temporary.name)

    def test_deterministic_only_is_explicit_and_improves_mock_under_same_conditions(self):
        campaign, store, root = self._run(use_qwen=False)
        self.assertEqual(campaign["planning_mode"], "deterministic_only")
        self.assertEqual(campaign["classification"], "deterministic_only")
        self.assertEqual(len(campaign["records"]), 2)
        self.assertEqual(campaign["records"][0]["result"]["status"], "failed")
        self.assertEqual(campaign["records"][1]["result"]["status"], "succeeded")
        self.assertTrue(campaign["same_condition"]["verified"])
        self.assertEqual(campaign["decision"]["status"], "p1_executed")
        self.assertIsNone(campaign["qwen"]["planning"])
        self.assertIsNone(campaign["qwen"]["feedback"])
        stored = store.read_campaign(campaign["campaign_id"])
        self.assertEqual(stored["campaign_id"], campaign["campaign_id"])
        self.assertTrue((root / "campaigns" / campaign["campaign_id"] / "campaign.json").exists())

    def test_qwen_planning_and_feedback_are_saved_and_can_drive_p1(self):
        qwen = _ScriptedQwen()
        campaign, store, root = self._run(qwen=qwen)
        self.assertEqual(campaign["planning_mode"], "qwen")
        self.assertEqual(campaign["classification"], "mock_autonomous")
        self.assertEqual(qwen.calls, ["planning", "adjustment"])
        self.assertEqual(len(campaign["records"]), 2)
        self.assertTrue(campaign["same_condition"]["verified"])
        self.assertEqual(campaign["decision"]["strategy_matches"], True)
        self.assertIsNotNone(campaign["qwen"]["planning"])
        self.assertIsNotNone(campaign["qwen"]["feedback"])
        evidence_dir = root / "campaigns" / campaign["campaign_id"] / "qwen"
        self.assertTrue((evidence_dir / "planning_metadata.json").exists())
        self.assertTrue((evidence_dir / "adjustment_metadata.json").exists())
        self.assertEqual(store.read_campaign(campaign["campaign_id"])["campaign_id"], campaign["campaign_id"])

    def test_qwen_stop_for_human_never_runs_p1(self):
        qwen = _ScriptedQwen("stop_for_human", "none")
        campaign, _, _ = self._run(qwen=qwen)
        self.assertEqual(len(campaign["records"]), 1)
        self.assertEqual(campaign["decision"]["status"], "stopped")
        self.assertFalse(campaign["same_condition"]["verified"])
        self.assertEqual(campaign["classification"], "supervised_or_incomplete")

    def test_qwen_strategy_mismatch_never_runs_p1(self):
        qwen = _ScriptedQwen("raise_grasp_z", "path_profile")
        campaign, _, _ = self._run(qwen=qwen)
        self.assertEqual(len(campaign["records"]), 1)
        self.assertEqual(campaign["decision"]["status"], "rejected")
        self.assertFalse(campaign["decision"]["strategy_matches"])
        self.assertEqual(campaign["classification"], "supervised_or_incomplete")

    def test_missing_qwen_key_fails_campaign_without_silent_deterministic_fallback(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExperimentStore(temporary.name)
        orchestrator = _orchestrator(MockAdapter(MockScenario.POSE_OFFSET), store)
        runner = ScientificCampaignRunner(store)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ScientificCampaignError) as raised:
                runner.run(
                    TASK,
                    orchestrator,
                    baseline_skill=SkillVersion(version="p0"),
                    use_qwen=True,
                    auto_run_p1=True,
                )

        error = raised.exception
        self.assertEqual(error.code, "QWEN_PLANNING_FAILED")
        self.assertEqual(error.stage, "planning")
        self.assertTrue(error.campaign_id)
        persisted = store.read_campaign(error.campaign_id)
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["classification"], "failed_before_closed_loop")
        self.assertEqual(persisted["records"], [])
        self.assertIn("planning_error.json", persisted["qwen_evidence"])

    def test_arbitrary_qwen_client_errors_are_stable_and_secret_free(self):
        secret = "sk-runtime-secret-must-not-be-persisted"
        for phase, code in (
            ("planning", "QWEN_PLANNING_FAILED"),
            ("adjustment", "QWEN_ADJUSTMENT_FAILED"),
        ):
            with self.subTest(phase=phase):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store = ExperimentStore(temporary.name)
                runner = ScientificCampaignRunner(
                    store, qwen_client=_RuntimeFailingQwen(phase, secret)
                )

                with self.assertRaises(ScientificCampaignError) as raised:
                    runner.run(
                        TASK,
                        _orchestrator(MockAdapter(MockScenario.POSE_OFFSET), store),
                        baseline_skill=SkillVersion(version="p0"),
                        use_qwen=True,
                        auto_run_p1=True,
                    )

                error = raised.exception
                self.assertEqual(error.code, code)
                self.assertEqual(error.stage, phase)
                self.assertNotIn(secret, str(error))
                persisted = store.read_campaign(error.campaign_id)
                serialized = json.dumps(persisted, ensure_ascii=False)
                self.assertNotIn(secret, serialized)
                metadata = persisted["qwen_evidence"][f"{phase}_metadata.json"]
                self.assertEqual(metadata["error_code"], code)
                self.assertEqual(metadata["stage"], phase)
                self.assertEqual(metadata["campaign_id"], error.campaign_id)
                self.assertIn(error.campaign_id, metadata["request_ref"])

    def test_operator_and_hybrid_real_arm_evidence_cannot_auto_iterate(self):
        for evaluator_type in ("operator", "hybrid"):
            with self.subTest(evaluator_type=evaluator_type):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store = ExperimentStore(temporary.name)
                adapter = _RealEvaluatedAdapter(evaluator_type)
                orchestrator = _orchestrator(adapter, store)
                campaign = ScientificCampaignRunner(
                    store, qwen_client=None
                ).run(
                    TASK,
                    orchestrator,
                    baseline_skill=SkillVersion(version="p0"),
                    use_qwen=False,
                    auto_run_p1=True,
                )
                self.assertEqual(len(campaign["records"]), 1)
                self.assertEqual(adapter.execute_count, 1)
                self.assertEqual(campaign["classification"], "supervised_or_incomplete")
                self.assertEqual(campaign["decision"]["status"], "manual_review_required")

    def test_auto_run_p1_false_keeps_candidate_without_execution(self):
        campaign, _, _ = self._run(use_qwen=False, auto_run_p1=False)
        self.assertEqual(len(campaign["records"]), 1)
        self.assertEqual(campaign["decision"]["status"], "awaiting_confirmation")
        self.assertEqual(campaign["classification"], "supervised_or_incomplete")

    def test_mutable_scene_blocks_p1_before_execution(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExperimentStore(temporary.name)
        orchestrator = _SceneMutatingOrchestrator(
            _orchestrator(MockAdapter(MockScenario.POSE_OFFSET), store)
        )

        campaign = ScientificCampaignRunner(store).run(
            TASK,
            orchestrator,
            baseline_skill=SkillVersion(version="p0"),
            use_qwen=False,
            auto_run_p1=True,
        )

        self.assertEqual(orchestrator.run_count, 1)
        self.assertEqual(len(campaign["records"]), 1)
        self.assertEqual(campaign["decision"]["status"], "manual_review_required")
        self.assertEqual(campaign["status"], "blocked")
        self.assertEqual(campaign["classification"], "supervised_or_incomplete")
        self.assertFalse(campaign["same_condition"]["verified"])
        self.assertEqual(campaign["same_condition"]["phase"], "pre_p1")
        self.assertIn(
            "scene_id",
            {item["field"] for item in campaign["same_condition"]["differences"]},
        )

    def test_missing_p1_vision_evidence_requires_manual_review(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExperimentStore(temporary.name)
        adapter = _RealVisionAdapter()

        campaign = ScientificCampaignRunner(store).run(
            TASK,
            _orchestrator(adapter, store),
            baseline_skill=SkillVersion(version="p0"),
            use_qwen=False,
            auto_run_p1=True,
        )

        self.assertEqual(adapter.execute_count, 2)
        self.assertEqual(len(campaign["records"]), 2)
        self.assertEqual(campaign["decision"]["status"], "manual_review_required")
        self.assertEqual(campaign["status"], "blocked")
        self.assertEqual(campaign["classification"], "supervised_or_incomplete")
        self.assertTrue(campaign["same_condition"]["verified"])
        self.assertFalse(campaign["p1_vision_evidence"]["verified"])
        self.assertIn(
            "evaluation_evidence_*",
            campaign["p1_vision_evidence"]["missing_or_mismatched"],
        )

    def test_missing_p0_vision_evidence_blocks_p1(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExperimentStore(temporary.name)
        adapter = _RealVisionAdapter(missing_evidence_run=1)

        campaign = ScientificCampaignRunner(store).run(
            TASK,
            _orchestrator(adapter, store),
            baseline_skill=SkillVersion(version="p0"),
            use_qwen=False,
            auto_run_p1=True,
        )

        self.assertEqual(adapter.execute_count, 1)
        self.assertEqual(len(campaign["records"]), 1)
        self.assertEqual(campaign["decision"]["status"], "manual_review_required")
        self.assertEqual(campaign["status"], "blocked")
        self.assertEqual(campaign["classification"], "supervised_or_incomplete")
        self.assertFalse(campaign["p0_vision_evidence"]["verified"])

    def test_application_config_and_campaign_retrieval_are_auditable(self):
        secret = "sk-config-secret-must-not-be-returned"
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        from roboscientist.ai import QwenClient

        app = DemoApplication(
            temporary.name,
            qwen_client=QwenClient(api_key=secret, model="qwen-test"),
        )
        config = app.config()
        self.assertTrue(config["qwen"]["enabled"])
        self.assertEqual(config["qwen"]["model"], "qwen-test")
        self.assertTrue(config["campaign_api"]["deterministic_only_requires_explicit_false"])
        self.assertNotIn(secret, str(config))

        # Use the explicit deterministic path here so this application-level
        # test remains offline while still exercising campaign persistence.
        campaign = app.run_campaign(
            TASK,
            "pose_offset",
            "mock",
            max_rounds=2,
            use_qwen=False,
            auto_run_p1=True,
        )
        retrieved = app.campaign(campaign["campaign_id"])
        self.assertEqual(retrieved["campaign_id"], campaign["campaign_id"])
        self.assertEqual(retrieved["rounds_completed"], 2)
        self.assertEqual(len(retrieved["records"]), 2)


if __name__ == "__main__":
    unittest.main()

"""S1 coordinator: plan, check, execute, analyze, create candidate, persist."""

from typing import Optional

from roboscientist.core.failure_analysis import analyze
from roboscientist.core.optimizer import candidate_from_failure
from roboscientist.core.planner import build_plan
from roboscientist.core.safety import check_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import (
    ErrorCode,
    ExperimentResult,
    FeedbackAdjustmentDraft,
    FailureInfo,
    RobotActionResult,
    RunStatus,
    SafetyConstraints,
    ScientificPlanDraft,
    SkillVersion,
    ObjectPose,
    Pose,
    TaskSpec,
)
from roboscientist.storage import ExperimentStore


class Orchestrator:
    def __init__(
        self,
        adapter,
        store: ExperimentStore,
        constraints: Optional[SafetyConstraints] = None,
        scene_id: str = "mock-fixed-workbench-v0",
        target_pose: Optional[ObjectPose] = None,
        destination_pose: Optional[Pose] = None,
        execution_scenario: str = "default",
    ):
        self.adapter = adapter
        self.store = store
        self.constraints = constraints or SafetyConstraints()
        self.scene_id = scene_id
        self.target_pose = target_pose
        self.destination_pose = destination_pose
        self.execution_scenario = execution_scenario

    def run(
        self,
        text: str,
        skill: SkillVersion,
        confidence: float = 0.95,
        *,
        task: Optional[TaskSpec] = None,
        scientific_plan: Optional[ScientificPlanDraft] = None,
        feedback_adjustment: Optional[FeedbackAdjustmentDraft] = None,
        qwen_invocation_ref: Optional[str] = None,
        planning_source: str = "deterministic",
        allow_candidate: bool = True,
    ) -> ExperimentResult:
        task = task or parse_task(text)
        plan = build_plan(
            task,
            skill,
            self.adapter.name,
            self.constraints,
            scene_id=self.scene_id,
            confidence=confidence,
            data_source=self.adapter.data_source,
            target_pose=self.target_pose,
            destination_pose=self.destination_pose,
            execution_scenario=self.execution_scenario,
            planning_source=planning_source,
            scientific_plan=scientific_plan,
            feedback_adjustment=feedback_adjustment,
            qwen_invocation_ref=qwen_invocation_ref,
        )
        self.store.ensure_skill(skill)
        self.store.write_plan(plan)
        safety = check_plan(plan, self.adapter.data_source)
        if not safety.allowed:
            failure = FailureInfo(code=safety.errors[0], stage="safety", message=safety.messages[0])
            result = ExperimentResult(
                experiment_id=plan.experiment_id, task_id=task.task_id, adapter=self.adapter.name,
                data_source=self.adapter.data_source, hardware_status=self.adapter.hardware_status,
                skill_version=skill.version, scene_id=plan.scene_id,
                status=RunStatus.REJECTED, safety_check=safety,
                actions=[RobotActionResult(action="safety_check", status=RunStatus.REJECTED, error_code=failure.code, message=failure.message)],
                failure=failure, artifacts={"plan": "plan.json"},
            )
        else:
            preflight = self.adapter.preflight(plan)
            if preflight.status != RunStatus.SUCCEEDED:
                result = ExperimentResult(
                    experiment_id=plan.experiment_id, task_id=task.task_id, adapter=self.adapter.name,
                    data_source=self.adapter.data_source, hardware_status=self.adapter.hardware_status,
                    skill_version=skill.version, scene_id=plan.scene_id,
                    status=preflight.status, safety_check=safety, actions=[preflight],
                    failure=FailureInfo(code=preflight.error_code, stage="adapter_preflight", message=preflight.message),
                    artifacts={"plan": "plan.json"},
                )
            else:
                execution = self.adapter.execute_pick_place(plan)
                result = ExperimentResult(
                    experiment_id=plan.experiment_id, task_id=task.task_id, adapter=self.adapter.name,
                    data_source=self.adapter.data_source, hardware_status=self.adapter.hardware_status,
                    skill_version=skill.version, scene_id=plan.scene_id,
                    status=execution.status, safety_check=safety, actions=[preflight] + execution.actions,
                    outcome=execution.outcome, failure=execution.failure, metrics=execution.metrics,
                    artifacts={"plan": "plan.json", **execution.artifacts}, simulation=execution.simulation,
                )
        result.failure_analysis = analyze(result)
        if result.failure_analysis:
            self.store.write_analysis(result.failure_analysis)
        evaluator_type = result.artifacts.get("evaluator_type")
        vision_evidence_available = (
            result.data_source != "real_arm" or evaluator_type == "vision"
        )
        if allow_candidate and not vision_evidence_available and result.failure:
            result.artifacts["candidate_gate"] = (
                "blocked: real_arm automatic feedback requires evaluator_type=vision; "
                f"received {evaluator_type or 'missing'}"
            )
        elif allow_candidate and vision_evidence_available:
            candidate = candidate_from_failure(result, skill)
            if candidate:
                self.store.write_skill(candidate)
                result.candidate_skill_version = candidate.version
        elif not allow_candidate:
            result.artifacts["candidate_gate"] = "not requested for bounded candidate verification"
        self.store.write_result(result)
        return result

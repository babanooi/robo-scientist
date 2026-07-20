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
    FailureInfo,
    RobotActionResult,
    RunStatus,
    SafetyConstraints,
    SkillVersion,
)
from roboscientist.storage import ExperimentStore


class Orchestrator:
    def __init__(
        self,
        adapter,
        store: ExperimentStore,
        constraints: Optional[SafetyConstraints] = None,
    ):
        self.adapter = adapter
        self.store = store
        self.constraints = constraints or SafetyConstraints()

    def run(self, text: str, skill: SkillVersion, confidence: float = 0.95) -> ExperimentResult:
        task = parse_task(text)
        plan = build_plan(
            task,
            skill,
            self.adapter.name,
            self.constraints,
            confidence=confidence,
            data_source=self.adapter.data_source,
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
        candidate = candidate_from_failure(result, skill)
        if candidate:
            self.store.write_skill(candidate)
            result.candidate_skill_version = candidate.version
        self.store.write_result(result)
        return result

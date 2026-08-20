"""Auditable Qwen -> experiment -> feedback campaigns.

The campaign runner owns the scientific loop, while the orchestrator remains
responsible for deterministic planning, safety checks, execution and evidence.
Qwen can propose intent and interpret evidence, but it cannot emit robot
commands or bypass the deterministic candidate and safety gates.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from roboscientist.ai import QwenClient, QwenConfigurationError
from roboscientist.core.optimizer import candidate_from_failure
from roboscientist.core.planner import build_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import (
    ErrorCode,
    ExperimentResult,
    FeedbackAdjustmentDraft,
    ScientificPlanDraft,
    SkillVersion,
    TaskSpec,
    new_id,
)
from roboscientist.storage import ExperimentStore


EVALUATOR_VERSION_KEYS = (
    "evaluator_version",
    "evaluation_version",
    "vision_evaluator_version",
)


class ScientificCampaignError(RuntimeError):
    """A campaign failed at a named stage and left an evidence package."""

    def __init__(self, message: str, *, code: str, stage: str, campaign_id: str):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.campaign_id = campaign_id


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _scrub(value: Any) -> Any:
    """Keep evidence useful while ensuring credentials can never be persisted."""
    secret_names = {"api_key", "authorization", "token", "secret", "password"}
    if isinstance(value, Mapping):
        return {
            str(key): _scrub(item)
            for key, item in value.items()
            if str(key).lower() not in secret_names
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def deterministic_scientific_plan(task: TaskSpec) -> ScientificPlanDraft:
    """A transparent no-model plan used only when the caller opts out of Qwen."""
    return ScientificPlanDraft(
        research_question=(
            f"在固定场景下，调整一个抓取参数族能否改善{task.target_color}"
            f"{task.target_object}从当前位置到{task.target_zone}的任务完成率？"
        ),
        hypothesis="根据上一轮的结构化失败证据，只调整与失败阶段对应的一个参数族可降低残差或失败率。",
        controlled_variables=[
            "场景、目标物、目标区域、光照、相机和标定版本",
            "安全档案、评价器版本、任务文本和实验顺序",
        ],
        success_criteria=[
            "抓取、提起、放置均由结构化评价器明确判定",
            "P1 在同条件下不增加安全事件并改善主要指标",
        ],
        stop_conditions=[
            "安全门拒绝、标定失效、评价证据缺失或结果不可归因",
            "候选参数需要修改多个参数族",
        ],
        expected_observation="P0 的失败类型和数值残差应能支持或否定一个单参数族调整。",
    )


def deterministic_feedback(
    result: ExperimentResult, candidate: Optional[SkillVersion]
) -> FeedbackAdjustmentDraft:
    """Return the same constrained decision that the optimizer is allowed to apply."""
    failure_code = result.failure.code if result.failure else ErrorCode.NONE
    if failure_code == ErrorCode.POSE_OFFSET:
        strategy = "signed_residual_compensation"
        family = "grasp_offset"
        expected = "抵消视觉评价器提供的带符号三轴位姿残差。"
    elif failure_code == ErrorCode.GRASP_FAILED:
        strategy = "raise_grasp_z"
        family = "grasp_offset"
        expected = "小幅提高抓取点 Z，验证提起阶段是否稳定。"
    elif failure_code == ErrorCode.PATH_BLOCKED:
        strategy = "raise_transit_height"
        family = "path_profile"
        expected = "提高中转高度，验证路径安全事件是否减少。"
    else:
        strategy = "stop_for_human"
        family = "none"
        expected = "当前证据不足以支持可归因的自动调整。"
    return FeedbackAdjustmentDraft(
        evidence_summary=(
            f"本轮状态={result.status.value}，失败码={failure_code.value}，"
            f"候选={candidate.version if candidate else 'none'}"
        ),
        failure_interpretation=(
            result.failure.message if result.failure else "没有结构化失败，停止继续迭代。"
        ),
        strategy=strategy,
        recommended_parameter_family=family,
        expected_effect=expected,
        alternative_explanation="仍可能存在标定漂移、物体扰动或评价证据不足，需用重复实验区分。",
    )


def _same_condition(p0: Mapping[str, Any], p1: Mapping[str, Any]) -> dict:
    """Compare the immutable experimental conditions, ignoring generated IDs and skill."""
    fields = (
        "adapter",
        "expected_data_source",
        "scene_id",
        "execution_scenario",
        "task.source_text",
        "task.target_color",
        "task.target_object",
        "task.target_zone",
        "target_pose",
        "target_pose.calibration_version",
        "destination_pose",
        "timeout_s",
        "safety_constraints",
        "evaluator_version",
    )

    def get(record: Mapping[str, Any], path: str) -> Any:
        current: Any = record
        for part in path.split("."):
            if not isinstance(current, Mapping):
                return None
            current = current.get(part)
        return current

    differences = [
        {"field": field, "p0": get(p0, field), "p1": get(p1, field)}
        for field in fields
        if get(p0, field) != get(p1, field)
    ]
    if get(p0, "expected_data_source") == "real_arm":
        for field in ("target_pose.calibration_version", "evaluator_version"):
            if not get(p0, field) or not get(p1, field):
                if not any(item["field"] == field for item in differences):
                    differences.append(
                        {
                            "field": field,
                            "p0": get(p0, field),
                            "p1": get(p1, field),
                            "reason": "required real-arm immutable condition is missing",
                        }
                    )
    return {"verified": not differences, "fields_checked": list(fields), "differences": differences}


def _evaluator_version(artifacts: Mapping[str, Any]) -> Optional[str]:
    for key in EVALUATOR_VERSION_KEYS:
        value = artifacts.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _configured_evaluator_version(adapter: Any, fallback: Optional[str]) -> Optional[str]:
    """Read an explicitly pinned evaluator version when the adapter exposes one."""
    for source in (adapter, getattr(adapter, "profile", None)):
        if source is None:
            continue
        for key in EVALUATOR_VERSION_KEYS:
            value = getattr(source, key, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _condition_record(plan: Mapping[str, Any], evaluator_version: Optional[str]) -> dict:
    record = dict(plan)
    record["evaluator_version"] = evaluator_version
    return record


def _real_arm_vision_evidence(
    result: ExperimentResult, expected_evaluator_version: Optional[str]
) -> dict:
    artifacts = result.artifacts
    actual_version = _evaluator_version(artifacts)
    evidence_files = sorted(
        str(value).strip()
        for key, value in artifacts.items()
        if key.startswith("evaluation_evidence_") and str(value).strip()
    )
    missing = []
    if artifacts.get("evaluator_type") != "vision":
        missing.append("evaluator_type=vision")
    if not str(artifacts.get("evaluation_file", "")).strip():
        missing.append("evaluation_file")
    if not evidence_files:
        missing.append("evaluation_evidence_*")
    if actual_version is None:
        missing.append("evaluator_version")
    elif expected_evaluator_version and actual_version != expected_evaluator_version:
        missing.append(
            f"evaluator_version={expected_evaluator_version} (received {actual_version})"
        )
    return {
        "verified": not missing,
        "expected_evaluator_version": expected_evaluator_version,
        "actual_evaluator_version": actual_version,
        "evaluation_file": artifacts.get("evaluation_file"),
        "evidence_files": evidence_files,
        "missing_or_mismatched": missing,
    }


def _strategy_matches(
    adjustment: FeedbackAdjustmentDraft, result: ExperimentResult, candidate: Optional[SkillVersion]
) -> bool:
    if adjustment.strategy == "stop_for_human":
        return False
    if candidate is None or result.failure is None:
        return False
    strategy_to_family = {
        "signed_residual_compensation": "grasp_offset",
        "raise_grasp_z": "grasp_offset",
        "raise_transit_height": "path_profile",
    }
    return (
        strategy_to_family.get(adjustment.strategy) == candidate.changed_parameter_family
        and adjustment.recommended_parameter_family == candidate.changed_parameter_family
    )


class ScientificCampaignRunner:
    """Run one bounded P0 -> feedback -> P1 campaign."""

    def __init__(self, store: ExperimentStore, qwen_client: Optional[QwenClient] = None):
        self.store = store
        self.qwen_client = qwen_client

    def _client(self) -> QwenClient:
        if self.qwen_client is not None:
            return self.qwen_client
        try:
            return QwenClient.from_env(required=True)  # type: ignore[return-value]
        except QwenConfigurationError:
            raise

    @staticmethod
    def _record(store: ExperimentStore, result: ExperimentResult) -> dict:
        record = store.read_experiment(result.experiment_id)
        candidate = result.candidate_skill_version
        record["candidate_skill"] = (
            store.read_skill(candidate).model_dump(mode="json") if candidate else None
        )
        return record

    def _write_qwen_call(self, campaign_id: str, phase: str, call: Any) -> str:
        self.store.write_campaign_qwen_evidence(
            campaign_id,
            phase,
            {
                "request": _scrub(call.request_payload),
                "response": _scrub(call.response_payload),
                "metadata": _scrub(call.metadata),
                "output": _scrub(_dump(call.output)),
            },
        )
        return f"campaigns/{campaign_id}/qwen/{phase}_metadata.json"

    def _write_qwen_error(self, campaign_id: str, phase: str, code: str) -> None:
        request_ref = f"campaigns/{campaign_id}/qwen/{phase}_metadata.json"
        payload = {
            "error": code,
            "metadata": {
                "error_code": code,
                "stage": phase,
                "campaign_id": campaign_id,
                "request_ref": request_ref,
            },
        }
        self.store.write_campaign_qwen_evidence(campaign_id, phase, payload)

    def run(
        self,
        task_text: str,
        orchestrator,
        baseline_skill: Optional[SkillVersion] = None,
        *,
        use_qwen: bool = True,
        auto_run_p1: bool = True,
    ) -> dict:
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("task_text is required")
        campaign_id = new_id("campaign")
        task = parse_task(task_text.strip())
        skill = baseline_skill or SkillVersion(version="p0")
        campaign: Dict[str, Any] = {
            "campaign_id": campaign_id,
            "status": "running",
            "classification": "pending",
            "planning_mode": "qwen" if use_qwen else "deterministic_only",
            "use_qwen": use_qwen,
            "auto_run_p1": auto_run_p1,
            "records": [],
            "qwen": {"enabled": use_qwen, "planning": None, "feedback": None},
            "project_policy": {
                "minimum_runs_per_version": 10,
                "minimum_runs_is_internal_policy": True,
            },
        }

        try:
            if use_qwen:
                try:
                    call = self._client().complete_structured(
                        phase="planning",
                        system_prompt=(
                            "你是科学实验规划器。只输出给定 JSON Schema，禁止输出舵机、ROS、"
                            "速度指令或越过安全约束。把研究目标转成可证伪的固定场景实验问题。"
                        ),
                        user_context={
                            "task": task.model_dump(mode="json"),
                            "execution_mode": orchestrator.adapter.data_source,
                            "scene_id": orchestrator.scene_id,
                            "safety_constraints": orchestrator.constraints.model_dump(mode="json"),
                            "known_measurements": [
                                "object_grasped",
                                "object_lifted",
                                "object_placed",
                                "signed_position_error_xyz_m",
                            ],
                        },
                        output_model=ScientificPlanDraft,
                        schema_name="scientific_plan_draft",
                    )
                except Exception as error:
                    code = "QWEN_PLANNING_FAILED"
                    self._write_qwen_error(campaign_id, "planning", code)
                    raise ScientificCampaignError(
                        "Qwen planning failed",
                        code=code,
                        stage="planning",
                        campaign_id=campaign_id,
                    ) from error
                scientific_plan = call.output
                qwen_ref = self._write_qwen_call(campaign_id, "planning", call)
                campaign["qwen"]["planning"] = _scrub(call.metadata)
            else:
                scientific_plan = deterministic_scientific_plan(task)
                qwen_ref = None
                campaign["qwen"]["reason"] = "disabled_by_explicit_use_qwen_false"

            p0 = orchestrator.run(
                task.source_text,
                skill,
                task=task,
                scientific_plan=scientific_plan,
                planning_source="qwen" if use_qwen else "deterministic",
                qwen_invocation_ref=qwen_ref,
            )
            p0_record = self._record(self.store, p0)
            campaign["records"].append(p0_record)
            campaign["scientific_plan"] = _dump(scientific_plan)

            candidate = (
                self.store.read_skill(p0.candidate_skill_version)
                if p0.candidate_skill_version
                else None
            )
            adjustment_context = {
                "research_plan": _dump(scientific_plan),
                "p0_record": p0_record,
                "candidate_skill": _dump(candidate),
                "allowed_parameter_families": ["grasp_offset", "path_profile"],
                "immutable_conditions": _same_condition(p0_record["plan"], p0_record["plan"]),
            }
            if use_qwen:
                try:
                    call = self._client().complete_structured(
                        phase="adjustment",
                        system_prompt=(
                            "你是科学实验反馈分析器。只输出给定 JSON Schema。只能在允许的单参数族"
                            "中选择策略；证据不足时必须 stop_for_human。不要改变场景、评价口径或安全约束。"
                        ),
                        user_context=adjustment_context,
                        output_model=FeedbackAdjustmentDraft,
                        schema_name="feedback_adjustment_draft",
                    )
                except Exception as error:
                    code = "QWEN_ADJUSTMENT_FAILED"
                    self._write_qwen_error(campaign_id, "adjustment", code)
                    raise ScientificCampaignError(
                        "Qwen adjustment failed",
                        code=code,
                        stage="adjustment",
                        campaign_id=campaign_id,
                    ) from error
                adjustment = call.output
                feedback_ref = self._write_qwen_call(campaign_id, "adjustment", call)
                campaign["qwen"]["feedback"] = _scrub(call.metadata)
            else:
                adjustment = deterministic_feedback(p0, candidate)
                feedback_ref = None
            campaign["feedback_adjustment"] = _dump(adjustment)

            strategy_matches = _strategy_matches(adjustment, p0, candidate)
            decision = {
                "status": "pending",
                "reason": "",
                "strategy_matches": strategy_matches,
            }
            p1_record = None
            same_condition = None
            p1_accepted = False
            p0_evaluator_version = _evaluator_version(p0.artifacts)
            expected_p1_evaluator_version = _configured_evaluator_version(
                orchestrator.adapter, p0_evaluator_version
            )
            p0_vision_evidence = None
            if p0.data_source == "real_arm":
                p0_vision_evidence = _real_arm_vision_evidence(
                    p0, expected_p1_evaluator_version
                )
                campaign["p0_vision_evidence"] = p0_vision_evidence
            if p0.failure is None:
                decision.update(status="stopped", reason="P0 has no structured failure; no P1 is necessary")
                status = "stopped"
            elif candidate is None:
                evaluator_type = p0.artifacts.get("evaluator_type", "missing")
                decision.update(
                    status="manual_review_required",
                    reason=f"no automatic candidate; evaluator_type={evaluator_type}",
                )
                status = "blocked"
            elif adjustment.strategy == "stop_for_human":
                decision.update(status="stopped", reason="Qwen or deterministic feedback requested human review")
                status = "stopped"
            elif not strategy_matches:
                decision.update(
                    status="rejected",
                    reason="feedback strategy does not match the deterministic single-family candidate",
                )
                status = "blocked"
            elif not auto_run_p1:
                decision.update(status="awaiting_confirmation", reason="auto_run_p1=false")
                status = "blocked"
            elif p0_vision_evidence is not None and not p0_vision_evidence["verified"]:
                decision.update(
                    status="manual_review_required",
                    reason=(
                        "P0 vision evidence is incomplete: "
                        + ", ".join(
                            p0_vision_evidence["missing_or_mismatched"]
                        )
                    ),
                )
                status = "blocked"
            else:
                prospective_p1_plan = build_plan(
                    task,
                    candidate,
                    orchestrator.adapter.name,
                    orchestrator.constraints,
                    scene_id=orchestrator.scene_id,
                    data_source=orchestrator.adapter.data_source,
                    target_pose=orchestrator.target_pose,
                    destination_pose=orchestrator.destination_pose,
                    execution_scenario=orchestrator.execution_scenario,
                    planning_source="qwen" if use_qwen else "deterministic",
                    scientific_plan=scientific_plan,
                    feedback_adjustment=adjustment,
                    qwen_invocation_ref=feedback_ref,
                )
                p0_conditions = _condition_record(
                    p0_record["plan"], p0_evaluator_version
                )
                prospective_p1_conditions = _condition_record(
                    prospective_p1_plan.model_dump(mode="json"),
                    expected_p1_evaluator_version,
                )
                same_condition = _same_condition(
                    p0_conditions, prospective_p1_conditions
                )
                same_condition["phase"] = "pre_p1"
                if not same_condition["verified"]:
                    decision.update(
                        status="manual_review_required",
                        reason="immutable conditions changed or are incomplete before P1",
                    )
                    status = "blocked"
                else:
                    p1 = orchestrator.run(
                        task.source_text,
                        candidate,
                        task=task,
                        scientific_plan=scientific_plan,
                        feedback_adjustment=adjustment,
                        planning_source="qwen" if use_qwen else "deterministic",
                        qwen_invocation_ref=feedback_ref,
                        allow_candidate=False,
                    )
                    p1_record = self._record(self.store, p1)
                    campaign["records"].append(p1_record)
                    actual_p1_evaluator_version = _evaluator_version(p1.artifacts)
                    same_condition = _same_condition(
                        p0_conditions,
                        _condition_record(
                            p1_record["plan"], actual_p1_evaluator_version
                        ),
                    )
                    same_condition["phase"] = "post_p1"
                    if not same_condition["verified"]:
                        decision.update(
                            status="manual_review_required",
                            reason="P1 actual immutable conditions differ from P0",
                        )
                        status = "blocked"
                    elif p1.data_source == "real_arm":
                        vision_evidence = _real_arm_vision_evidence(
                            p1, p0_evaluator_version
                        )
                        campaign["p1_vision_evidence"] = vision_evidence
                        if not vision_evidence["verified"]:
                            decision.update(
                                status="manual_review_required",
                                reason=(
                                    "P1 vision evidence is incomplete: "
                                    + ", ".join(
                                        vision_evidence["missing_or_mismatched"]
                                    )
                                ),
                            )
                            status = "blocked"
                        else:
                            decision.update(
                                status="p1_executed",
                                reason="all deterministic and evaluator gates passed",
                            )
                            status = "completed"
                            p1_accepted = True
                    else:
                        decision.update(
                            status="p1_executed",
                            reason="all deterministic and evaluator gates passed",
                        )
                        status = "completed"
                        p1_accepted = True

            campaign["decision"] = decision
            if same_condition is not None:
                campaign["same_condition"] = same_condition
            else:
                campaign["same_condition"] = {
                    "verified": False,
                    "fields_checked": [],
                    "differences": [],
                    "reason": "P1 was not executed",
                }
            campaign["status"] = status
            campaign["classification"] = self._classification(
                campaign, p0.data_source, use_qwen, p1_accepted
            )
        except ScientificCampaignError:
            campaign.setdefault("decision", {"status": "failed", "reason": "Qwen call failed"})
            campaign["status"] = "failed"
            campaign["classification"] = "failed_before_closed_loop"
            self.store.write_campaign(campaign_id, campaign)
            raise
        except Exception as error:
            campaign["status"] = "failed"
            campaign["classification"] = "failed_runtime"
            campaign["error"] = str(error)
            self.store.write_campaign(campaign_id, campaign)
            raise

        self.store.write_campaign(campaign_id, campaign)
        return campaign

    @staticmethod
    def _classification(campaign: Mapping[str, Any], data_source: str, use_qwen: bool, p1_executed: bool) -> str:
        if not p1_executed:
            return "supervised_or_incomplete"
        if not use_qwen:
            return "deterministic_only"
        if data_source == "real_arm":
            return "autonomous_closed_loop"
        if data_source == "mock":
            return "mock_autonomous"
        return "simulation_unverified"

"""Repeated, same-condition validation for a bounded campaign.

The first campaign explains the feedback decision.  This module adds the
statistical evidence needed before a candidate Skill can be called an
improvement: repeated P0 and P1 runs, the same task/scene/evaluator, and a
machine-readable comparison.  It is adapter-agnostic and never talks to a
robot directly.
"""

from __future__ import annotations

from statistics import mean
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from roboscientist.core.scientific_campaign import deterministic_scientific_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import ExperimentResult, SkillVersion


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _record(store, result: ExperimentResult) -> dict:
    record = store.read_experiment(result.experiment_id)
    candidate = result.candidate_skill_version
    record["candidate_skill"] = (
        store.read_skill(candidate).model_dump(mode="json") if candidate else None
    )
    return record


def _success(result: Mapping[str, Any]) -> bool:
    outcome = result.get("outcome") or {}
    return bool(
        result.get("status") == "succeeded"
        and outcome.get("object_grasped") is True
        and outcome.get("object_lifted") is True
        and outcome.get("object_placed") is True
    )


def _number(result: Mapping[str, Any], *keys: str) -> Optional[float]:
    metrics = result.get("metrics") or {}
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    outcome = result.get("outcome") or {}
    value = outcome.get("position_error_m")
    return float(value) if isinstance(value, (int, float)) else None


def summarize(records: Sequence[Mapping[str, Any]]) -> dict:
    """Return recomputable aggregate metrics for one Skill version."""

    results = [record.get("result") or {} for record in records]
    count = len(results)
    successes = sum(1 for result in results if _success(result))
    errors = [value for result in results if (value := _number(result, "position_error_m")) is not None]
    planning = [value for result in results if (value := _number(result, "planning_time_ms")) is not None]
    paths = [value for result in results if (value := _number(result, "path_length_m", "trajectory_length_m")) is not None]
    durations = [value for result in results if (value := _number(result, "execution_time_s")) is not None]
    safety = [value for result in results if (value := _number(result, "safety_events")) is not None]
    collisions = [value for result in results if (value := _number(result, "collision_detected")) is not None]

    def avg(values: Iterable[float]) -> Optional[float]:
        values = list(values)
        return round(mean(values), 6) if values else None

    return {
        "sample_count": count,
        "success_count": successes,
        "task_success_rate": round(successes / count, 6) if count else None,
        "position_error_mean_m": avg(errors),
        "planning_time_mean_ms": avg(planning),
        "path_length_mean_m": avg(paths),
        "execution_time_mean_s": avg(durations),
        "safety_events_mean": avg(safety),
        "collision_rate": avg(collisions),
        "status_counts": {
            status: sum(1 for result in results if result.get("status") == status)
            for status in sorted({str(result.get("status")) for result in results})
        },
    }


def compare(p0: Mapping[str, Any], p1: Mapping[str, Any], *, minimum_samples: int = 10) -> dict:
    """Make a conservative candidate decision from aggregate metrics.

    Success rate is the primary objective, position error is a secondary
    objective, safety/collision values are hard guardrails, and path/time are
    reported efficiency metrics.  Keeping those roles explicit prevents a
    candidate from being called a shorter or faster path when the experiment
    only established a task-success improvement.
    """

    p0_rate = p0.get("task_success_rate")
    p1_rate = p1.get("task_success_rate")
    p0_error = p0.get("position_error_mean_m")
    p1_error = p1.get("position_error_mean_m")
    p0_safety = p0.get("safety_events_mean")
    p1_safety = p1.get("safety_events_mean")
    p0_collision = p0.get("collision_rate")
    p1_collision = p1.get("collision_rate")
    sufficient = min(p0.get("sample_count", 0), p1.get("sample_count", 0)) >= minimum_samples
    improved = (
        isinstance(p0_rate, (int, float))
        and isinstance(p1_rate, (int, float))
        and p1_rate > p0_rate
        and (p0_error is None or p1_error is None or p1_error <= p0_error)
        and (p0_safety is None or p1_safety is None or p1_safety <= p0_safety)
        and (p0_collision is None or p1_collision is None or p1_collision <= p0_collision)
    )
    if not sufficient:
        decision = "candidate_only_insufficient_samples"
        reason = f"每个版本至少需要 {minimum_samples} 次同条件样本"
    elif improved:
        decision = "promote_candidate_for_review"
        reason = (
            "P1 主要成功率提高、位置误差不增且安全/碰撞指标未退化；"
            "路径和耗时仅作效率报告，仍需人工复核后晋升"
        )
    else:
        decision = "reject_candidate"
        reason = "P1 未形成满足约束的可重复改善"
    def delta(key: str) -> Optional[float]:
        before = p0.get(key)
        after = p1.get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            return round(float(after) - float(before), 6)
        return None

    return {
        "decision": decision,
        "reason": reason,
        "minimum_samples_per_version": minimum_samples,
        "samples_sufficient": sufficient,
        "improvement_supported": improved,
        "objective": {
            "primary_metric": "task_success_rate",
            "secondary_metrics": ["position_error_mean_m"],
            "guardrail_metrics": ["safety_events_mean", "collision_rate"],
            "reported_efficiency_metrics": [
                "path_length_mean_m",
                "execution_time_mean_s",
                "planning_time_mean_ms",
            ],
            "lower_is_better": [
                "position_error_mean_m",
                "safety_events_mean",
                "collision_rate",
                "path_length_mean_m",
                "execution_time_mean_s",
                "planning_time_mean_ms",
            ],
            "efficiency_metrics_are_promotion_criteria": False,
        },
        "deltas": {
            "task_success_rate": (
                round(p1_rate - p0_rate, 6)
                if isinstance(p0_rate, (int, float)) and isinstance(p1_rate, (int, float))
                else None
            ),
            "position_error_mean_m": (
                round(p1_error - p0_error, 6)
                if isinstance(p0_error, (int, float)) and isinstance(p1_error, (int, float))
                else None
            ),
            "safety_events_mean": (
                round(p1_safety - p0_safety, 6)
                if isinstance(p0_safety, (int, float)) and isinstance(p1_safety, (int, float))
                else None
            ),
            "collision_rate": (
                round(p1_collision - p0_collision, 6)
                if isinstance(p0_collision, (int, float)) and isinstance(p1_collision, (int, float))
                else None
            ),
            "path_length_mean_m": delta("path_length_mean_m"),
            "execution_time_mean_s": delta("execution_time_mean_s"),
            "planning_time_mean_ms": delta("planning_time_mean_ms"),
        },
    }


def run_repeated_validation(
    *,
    store,
    orchestrator,
    task_text: str,
    baseline_skill: SkillVersion,
    candidate_skill: SkillVersion,
    repeats: int = 10,
    scientific_plan=None,
    feedback_adjustment=None,
    planning_source: str = "deterministic",
    qwen_invocation_ref: Optional[str] = None,
) -> dict:
    """Run ``repeats`` P0 and P1 observations under one frozen adapter."""

    if not isinstance(repeats, int) or not 1 <= repeats <= 100:
        raise ValueError("repeats must be an integer between 1 and 100")
    task = parse_task(task_text.strip())
    scientific_plan = scientific_plan or deterministic_scientific_plan(task)
    records = {"p0": [], "p1": []}
    for index in range(repeats):
        for label, skill in (("p0", baseline_skill), ("p1", candidate_skill)):
            result = orchestrator.run(
                task.source_text,
                skill,
                task=task,
                scientific_plan=scientific_plan,
                feedback_adjustment=feedback_adjustment,
                planning_source=planning_source,
                qwen_invocation_ref=qwen_invocation_ref,
                allow_candidate=False,
            )
            record = _record(store, result)
            record["validation_index"] = index + 1
            record["validation_version"] = label
            records[label].append(record)
    p0_summary = summarize(records["p0"])
    p1_summary = summarize(records["p1"])
    comparison = compare(p0_summary, p1_summary)
    return {
        "format_version": "validation-v1",
        "repeats_per_version": repeats,
        "same_adapter": orchestrator.adapter.name,
        "data_source": orchestrator.adapter.data_source,
        "scene_id": orchestrator.scene_id,
        "records": records,
        "summary": {"p0": p0_summary, "p1": p1_summary},
        "comparison": comparison,
        "candidate_skill": _dump(candidate_skill),
        "baseline_skill": _dump(baseline_skill),
    }


__all__ = ["compare", "run_repeated_validation", "summarize"]

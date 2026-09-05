"""Evidence-based classification from structured result fields."""

from typing import Optional

from roboscientist.schemas import ErrorCode, ExperimentResult, FailureAnalysisResult


def analyze(result: ExperimentResult) -> Optional[FailureAnalysisResult]:
    if result.failure is None:
        return None
    family = None
    if result.failure.code in (ErrorCode.POSE_OFFSET, ErrorCode.GRASP_FAILED):
        family = "grasp_offset"
    elif result.failure.code == ErrorCode.PATH_BLOCKED:
        family = "path_profile"
    return FailureAnalysisResult(
        experiment_id=result.experiment_id,
        failure_code=result.failure.code,
        failure_stage=result.failure.stage,
        evidence=[result.failure.message] + [f"{key}={value}" for key, value in result.metrics.items()],
        recommended_parameter_family=family,
    )

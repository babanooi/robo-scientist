"""Creates a single-family candidate; promotion is intentionally out of S1 scope."""

from typing import Optional

from roboscientist.schemas import ErrorCode, ExperimentResult, SkillParameters, SkillVersion


def candidate_from_failure(
    result: ExperimentResult, parent: SkillVersion
) -> Optional[SkillVersion]:
    if result.failure is None or result.failure.code == ErrorCode.TIMEOUT:
        return None
    if result.failure.code not in (ErrorCode.POSE_OFFSET, ErrorCode.GRASP_FAILED):
        return None
    offset_x, offset_y, offset_z = parent.parameters.grasp_offset_m
    if result.failure.code == ErrorCode.POSE_OFFSET:
        error = result.metrics.get("position_error_m", 0.0)
        offset_x -= error
        reason = "compensate deterministic mock position error"
    else:
        offset_z += 0.005
        reason = "test a small grasp-point z adjustment after grip loss"
    parameters = SkillParameters(
        grasp_offset_m=(offset_x, offset_y, offset_z),
        approach_height_m=parent.parameters.approach_height_m,
        speed_m_s=parent.parameters.speed_m_s,
    )
    return SkillVersion(
        skill_id=parent.skill_id,
        version=f"{parent.version}-candidate-{result.experiment_id[-6:]}",
        status="candidate",
        parameters=parameters,
        parent_version=parent.version,
        changed_parameter_family="grasp_offset",
        change_reason=reason,
        source_experiment_id=result.experiment_id,
    )

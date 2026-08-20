"""Creates a single-family candidate; promotion is intentionally out of S1 scope."""

from typing import Optional

from roboscientist.schemas import ErrorCode, ExperimentResult, SkillParameters, SkillVersion


def candidate_from_failure(
    result: ExperimentResult, parent: SkillVersion
) -> Optional[SkillVersion]:
    if result.failure is None or result.failure.code == ErrorCode.TIMEOUT:
        return None
    if result.failure.code not in (
        ErrorCode.POSE_OFFSET, ErrorCode.GRASP_FAILED, ErrorCode.PATH_BLOCKED,
    ):
        return None
    offset_x, offset_y, offset_z = parent.parameters.grasp_offset_m
    transit_height = parent.parameters.transit_height_m
    family = "grasp_offset"
    if result.failure.code == ErrorCode.POSE_OFFSET:
        signed_error = tuple(
            result.metrics.get(f"position_error_{axis}_m") for axis in ("x", "y", "z")
        )
        if any(value is None for value in signed_error):
            return None
        offset_x -= signed_error[0]
        offset_y -= signed_error[1]
        offset_z -= signed_error[2]
        reason = "根据带符号的三轴位置残差生成补偿候选"
    elif result.failure.code == ErrorCode.GRASP_FAILED:
        offset_z += 0.005
        reason = "抓取保持失败后，测试小幅提高抓取点 Z"
    else:
        transit_height = min(transit_height + 0.01, 0.30)
        family = "path_profile"
        reason = "路径安全拒绝后，仅提高中转点高度"
    parameters = SkillParameters(
        grasp_offset_m=(offset_x, offset_y, offset_z),
        approach_height_m=parent.parameters.approach_height_m,
        transit_height_m=transit_height,
        speed_m_s=parent.parameters.speed_m_s,
    )
    return SkillVersion(
        skill_id=parent.skill_id,
        version=f"{parent.version}-candidate-{result.experiment_id[-6:]}",
        status="candidate",
        parameters=parameters,
        parent_version=parent.version,
        changed_parameter_family=family,
        change_reason=reason,
        source_experiment_id=result.experiment_id,
    )

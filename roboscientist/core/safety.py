"""Deterministic pre-execution constraints; no model-generated safety decisions."""

from roboscientist.schemas import ErrorCode, ExperimentPlan, SafetyCheckResult


def check_plan(plan: ExperimentPlan, adapter_data_source: str) -> SafetyCheckResult:
    constraints = plan.safety_constraints
    errors = []
    messages = []
    pose = plan.target_pose
    if adapter_data_source == "real_arm" and not constraints.allow_real_robot:
        errors.append(ErrorCode.REAL_ROBOT_NOT_ALLOWED)
        messages.append("real hardware is disabled by the safety profile")
    if plan.expected_data_source != adapter_data_source:
        errors.append(ErrorCode.INVALID_PARAMETERS)
        messages.append("plan data source does not match the selected adapter")
    if pose.confidence < constraints.minimum_confidence:
        errors.append(ErrorCode.LOW_CONFIDENCE)
        messages.append("target confidence is below the safety threshold")
    if not pose.calibration_version:
        errors.append(ErrorCode.CALIBRATION_MISSING)
        messages.append("target pose has no calibration version")
    if any(value < low or value > high for value, low, high in zip((pose.x, pose.y, pose.z), constraints.workspace_min_m, constraints.workspace_max_m)):
        errors.append(ErrorCode.OUT_OF_WORKSPACE)
        messages.append("target pose is outside the configured workspace")
    destination = plan.destination_pose
    if destination.frame_id != "base":
        errors.append(ErrorCode.INVALID_PARAMETERS)
        messages.append("destination pose must use the base frame")
    if any(value < low or value > high for value, low, high in zip((destination.x, destination.y, destination.z), constraints.workspace_min_m, constraints.workspace_max_m)):
        errors.append(ErrorCode.OUT_OF_WORKSPACE)
        messages.append("destination pose is outside the configured workspace")
    parameters = plan.skill.parameters
    if parameters.speed_m_s > constraints.max_speed_m_s:
        errors.append(ErrorCode.INVALID_PARAMETERS)
        messages.append("requested speed exceeds the safety profile")
    if any(abs(value) > constraints.max_offset_m for value in parameters.grasp_offset_m):
        errors.append(ErrorCode.INVALID_PARAMETERS)
        messages.append("grasp offset exceeds the safety profile")
    if plan.timeout_s > constraints.max_timeout_s:
        errors.append(ErrorCode.INVALID_PARAMETERS)
        messages.append("plan timeout exceeds the safety profile")
    return SafetyCheckResult(allowed=not errors, errors=errors, messages=messages)

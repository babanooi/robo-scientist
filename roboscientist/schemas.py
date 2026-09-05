"""Stable, Pydantic-validated contracts shared by all S0/S1 modules."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class ErrorCode(str, Enum):
    NONE = "NONE"
    INVALID_TASK = "INVALID_TASK"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    OUT_OF_WORKSPACE = "OUT_OF_WORKSPACE"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    CALIBRATION_MISSING = "CALIBRATION_MISSING"
    REAL_ROBOT_NOT_ALLOWED = "REAL_ROBOT_NOT_ALLOWED"
    HARDWARE_UNVERIFIED = "HARDWARE_UNVERIFIED"
    POSE_OFFSET = "POSE_OFFSET"
    GRASP_FAILED = "GRASP_FAILED"
    PATH_BLOCKED = "PATH_BLOCKED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    BRIDGE_UNAVAILABLE = "BRIDGE_UNAVAILABLE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    TIMEOUT = "TIMEOUT"
    STOPPED = "STOPPED"


class RunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class ExecutionMode(str, Enum):
    MOCK = "mock"
    SIMULATION = "simulation"
    REAL_ARM = "real_arm"


class Pose(BaseModel):
    """Position in metres in an explicitly named coordinate frame."""

    x: float
    y: float
    z: float
    frame_id: str = "base"


class ObjectPose(Pose):
    object_id: str = "target-block"
    confidence: float = Field(ge=0.0, le=1.0)
    calibration_version: Optional[str] = None


class TaskSpec(BaseModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    source_text: str
    target_color: str
    target_object: str = "cube"
    target_zone: str
    success_criteria: Tuple[str, ...] = (
        "object_grasped",
        "object_lifted",
        "object_placed",
    )


class SkillParameters(BaseModel):
    grasp_offset_m: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    approach_height_m: float = Field(default=0.03, gt=0.0, le=0.15)
    transit_height_m: float = Field(default=0.12, gt=0.0, le=0.30)
    speed_m_s: float = Field(default=0.1, gt=0.0, le=1.0)


class SkillVersion(BaseModel):
    skill_id: str = "pick_place_color_block"
    version: str
    status: str = "stable"
    parameters: SkillParameters = Field(default_factory=SkillParameters)
    parent_version: Optional[str] = None
    changed_parameter_family: Optional[str] = None
    change_reason: Optional[str] = None
    source_experiment_id: Optional[str] = None


class ScientificPlanDraft(BaseModel):
    """Qwen-authored scientific intent; it never contains robot commands."""

    model_config = ConfigDict(extra="forbid")

    research_question: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    controlled_variables: List[str] = Field(min_length=1)
    success_criteria: List[str] = Field(min_length=1)
    stop_conditions: List[str] = Field(min_length=1)
    expected_observation: str = Field(min_length=1)


class FeedbackAdjustmentDraft(BaseModel):
    """Qwen decision at the planning layer, constrained to safe strategies."""

    model_config = ConfigDict(extra="forbid")

    evidence_summary: str = Field(min_length=1)
    failure_interpretation: str = Field(min_length=1)
    strategy: Literal[
        "signed_residual_compensation",
        "raise_grasp_z",
        "raise_transit_height",
        "stop_for_human",
    ]
    recommended_parameter_family: Literal["grasp_offset", "path_profile", "none"]
    expected_effect: str = Field(min_length=1)
    alternative_explanation: str = Field(min_length=1)


class SafetyConstraints(BaseModel):
    profile_version: str = "safety-v0"
    allow_real_robot: bool = False
    minimum_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    workspace_min_m: Tuple[float, float, float] = (0.1, -0.2, 0.0)
    workspace_max_m: Tuple[float, float, float] = (0.4, 0.2, 0.3)
    max_speed_m_s: float = Field(default=0.15, gt=0.0)
    max_timeout_s: float = Field(default=10.0, gt=0.0)
    max_offset_m: float = Field(default=0.05, gt=0.0)

    @field_validator("workspace_max_m")
    @classmethod
    def workspace_has_volume(
        cls, value: Tuple[float, float, float], info
    ) -> Tuple[float, float, float]:
        minimum = info.data.get("workspace_min_m")
        if minimum and any(high <= low for low, high in zip(minimum, value)):
            raise ValueError("workspace_max_m must exceed workspace_min_m")
        return value


class ExperimentPlan(BaseModel):
    experiment_id: str = Field(default_factory=lambda: new_id("exp"))
    task: TaskSpec
    skill: SkillVersion
    scene_id: str
    adapter: str
    execution_scenario: str = "default"
    expected_data_source: str = "mock"
    target_pose: ObjectPose
    destination_pose: Pose
    timeout_s: float = Field(default=8.0, gt=0.0)
    safety_constraints: SafetyConstraints = Field(default_factory=SafetyConstraints)
    planning_source: Literal["deterministic", "qwen"] = "deterministic"
    scientific_plan: Optional[ScientificPlanDraft] = None
    feedback_adjustment: Optional[FeedbackAdjustmentDraft] = None
    qwen_invocation_ref: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class RobotActionResult(BaseModel):
    action: str
    status: RunStatus
    error_code: ErrorCode = ErrorCode.NONE
    message: str = ""
    duration_s: float = Field(default=0.0, ge=0.0)


class Outcome(BaseModel):
    object_grasped: bool = False
    object_lifted: bool = False
    object_placed: bool = False
    position_error_m: Optional[float] = Field(default=None, ge=0.0)


class FailureInfo(BaseModel):
    code: ErrorCode
    stage: str
    message: str


class SafetyCheckResult(BaseModel):
    allowed: bool
    errors: List[ErrorCode] = Field(default_factory=list)
    messages: List[str] = Field(default_factory=list)


class FailureAnalysisResult(BaseModel):
    experiment_id: str
    failure_code: ErrorCode
    failure_stage: str
    evidence: List[str]
    recommended_parameter_family: Optional[str] = None


class JointTrajectoryPoint(BaseModel):
    time_from_start_s: float = Field(ge=0.0)
    joint_positions_rad: Tuple[float, ...] = ()


class SimulationExecutionData(BaseModel):
    """Structured virtual/simulation execution evidence.

    The original contract was designed for Gazebo/MoveIt2.  The optional
    fields below also support the project's self-contained virtual workcell so
    the UI can render an actual, repeatable experiment when ROS is unavailable.
    They are deliberately explicit about the runtime name and provenance; a
    virtual workcell must never be presented as a physical-arm result.
    """

    runtime_status: str
    planning_success: bool
    execution_success: bool
    collision_detected: Optional[bool] = None
    planner_name: str = "OMPL"
    planning_time_ms: Optional[float] = Field(default=None, ge=0.0)
    trajectory_length_m: Optional[float] = Field(default=None, ge=0.0)
    trajectory_points: int = Field(default=0, ge=0)
    joint_trajectory: List[JointTrajectoryPoint] = Field(default_factory=list)
    safety_events: List[str] = Field(default_factory=list)
    failure_reason: Optional[str] = None
    raw_ros_refs: List[str] = Field(default_factory=list)
    runtime_name: Optional[str] = None
    runtime_version: Optional[str] = None
    engine: Optional[str] = None
    random_seed: Optional[int] = None
    synthetic: Optional[bool] = None
    physical_robot_connected: Optional[bool] = None
    scene_objects: List[Dict[str, Any]] = Field(default_factory=list)
    tcp_trajectory: List[Pose] = Field(default_factory=list)
    # Informational checks (for example, a verified clearance) are kept
    # separate from adverse safety events so a successful check is not counted
    # as a safety regression in aggregate metrics.
    safety_observations: List[str] = Field(default_factory=list)
    evaluation: Dict[str, Any] = Field(default_factory=dict)



class ExperimentResult(BaseModel):
    experiment_id: str
    task_id: str
    adapter: str
    data_source: str
    hardware_status: str
    skill_version: str
    scene_id: str
    status: RunStatus
    safety_check: SafetyCheckResult
    actions: List[RobotActionResult] = Field(default_factory=list)
    outcome: Outcome = Field(default_factory=Outcome)
    failure: Optional[FailureInfo] = None
    metrics: Dict[str, float] = Field(default_factory=dict)
    artifacts: Dict[str, str] = Field(default_factory=dict)
    completed_at: datetime = Field(default_factory=utc_now)
    failure_analysis: Optional[FailureAnalysisResult] = None
    candidate_skill_version: Optional[str] = None
    simulation: Optional[SimulationExecutionData] = None
    replay: Optional[Dict[str, Any]] = None

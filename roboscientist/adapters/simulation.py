"""Gazebo + MoveIt2 adapter contract, intentionally inert outside a verified ROS2 runtime."""

from roboscientist.adapters.base import AdapterExecution
from roboscientist.schemas import (
    ErrorCode,
    ExperimentPlan,
    FailureInfo,
    Outcome,
    RobotActionResult,
    RunStatus,
    SimulationExecutionData,
)


class SimulationAdapter:
    """Does not launch or command ROS2 until the virtual-machine contract is verified."""

    name = "simulation"
    data_source = "simulation"
    hardware_status = "simulation_runtime_unverified"

    def __init__(self, planner_name: str = "OMPL"):
        self.planner_name = planner_name

    def get_robot_state(self) -> dict:
        return {
            "data_source": self.data_source,
            "hardware_status": self.hardware_status,
            "motion_state": "unavailable",
        }

    def preflight(self, plan: ExperimentPlan) -> RobotActionResult:
        return RobotActionResult(
            action="simulation_contract_check",
            status=RunStatus.SUCCEEDED,
            message="simulation adapter contract is available; runtime execution remains unverified",
        )

    def stop(self, reason: str) -> RobotActionResult:
        return RobotActionResult(
            action="simulation_stop",
            status=RunStatus.REJECTED,
            error_code=ErrorCode.HARDWARE_UNVERIFIED,
            message="No verified simulation action to stop",
        )

    def execute_pick_place(self, plan: ExperimentPlan) -> AdapterExecution:
        reason = "simulation_runtime_unverified: no Gazebo/MoveIt2 action contract is configured"
        simulation = SimulationExecutionData(
            runtime_status=self.hardware_status,
            planning_success=False,
            execution_success=False,
            collision_detected=None,
            planner_name=self.planner_name,
            safety_events=[self.hardware_status],
            failure_reason=reason,
            raw_ros_refs=[],
        )
        failure = FailureInfo(
            code=ErrorCode.HARDWARE_UNVERIFIED,
            stage="simulation_preflight",
            message=reason,
        )
        action = RobotActionResult(
            action="execute_simulation_pick_place",
            status=RunStatus.REJECTED,
            error_code=ErrorCode.HARDWARE_UNVERIFIED,
            message=reason,
        )
        return AdapterExecution(
            [action], Outcome(), RunStatus.REJECTED, failure,
            metrics={"safety_events": 1.0}, simulation=simulation,
        )

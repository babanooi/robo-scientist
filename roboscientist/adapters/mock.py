"""Deterministic simulated execution. It never represents real robot data."""

from enum import Enum

from roboscientist.adapters.base import AdapterExecution
from roboscientist.schemas import (
    ErrorCode,
    ExperimentPlan,
    FailureInfo,
    Outcome,
    RobotActionResult,
    RunStatus,
)


class MockScenario(str, Enum):
    SUCCESS = "success"
    POSE_OFFSET = "pose_offset"
    GRASP_FAILED = "grasp_failed"
    TIMEOUT = "timeout"


class MockAdapter:
    name = "mock"
    data_source = "mock"
    hardware_status = "mock_local"

    def __init__(self, scenario: MockScenario = MockScenario.SUCCESS):
        self.scenario = MockScenario(scenario)

    def get_robot_state(self) -> dict:
        return {"data_source": "mock", "motion_state": "idle"}

    def preflight(self, plan: ExperimentPlan) -> RobotActionResult:
        return RobotActionResult(action="adapter_preflight", status=RunStatus.SUCCEEDED)

    def stop(self, reason: str) -> RobotActionResult:
        return RobotActionResult(
            action="stop", status=RunStatus.REJECTED, error_code=ErrorCode.STOPPED, message=reason
        )

    def execute_pick_place(self, plan: ExperimentPlan) -> AdapterExecution:
        common = [
            RobotActionResult(action="approach", status=RunStatus.SUCCEEDED, duration_s=0.4),
            RobotActionResult(action="close_gripper", status=RunStatus.SUCCEEDED, duration_s=0.2),
        ]
        if self.scenario is MockScenario.SUCCESS:
            actions = common + [
                RobotActionResult(action="lift", status=RunStatus.SUCCEEDED, duration_s=0.3),
                RobotActionResult(action="place", status=RunStatus.SUCCEEDED, duration_s=0.4),
            ]
            return AdapterExecution(
                actions, Outcome(object_grasped=True, object_lifted=True, object_placed=True, position_error_m=0.002),
                RunStatus.SUCCEEDED, metrics={"execution_time_s": 1.3, "path_length_m": 0.42, "safety_events": 0.0},
                artifacts={"scene": "mock://scene/success", "trajectory": "mock://trajectory/success"},
            )
        if self.scenario is MockScenario.POSE_OFFSET:
            actions = common + [
                RobotActionResult(action="lift", status=RunStatus.FAILED, error_code=ErrorCode.POSE_OFFSET, message="simulated x offset"),
            ]
            return AdapterExecution(
                actions, Outcome(position_error_m=0.02), RunStatus.FAILED,
                FailureInfo(code=ErrorCode.POSE_OFFSET, stage="grasp", message="simulated stable 20 mm x offset"),
                {"execution_time_s": 0.9, "position_error_m": 0.02, "safety_events": 0.0},
                {"scene": "mock://scene/pose_offset"},
            )
        if self.scenario is MockScenario.GRASP_FAILED:
            actions = common + [
                RobotActionResult(action="lift", status=RunStatus.FAILED, error_code=ErrorCode.GRASP_FAILED, message="simulated grip loss"),
            ]
            return AdapterExecution(
                actions, Outcome(position_error_m=0.004), RunStatus.FAILED,
                FailureInfo(code=ErrorCode.GRASP_FAILED, stage="grasp", message="simulated object not retained"),
                {"execution_time_s": 0.8, "position_error_m": 0.004, "safety_events": 0.0},
                {"scene": "mock://scene/grasp_failed"},
            )
        actions = common + [
            RobotActionResult(action="lift", status=RunStatus.TIMED_OUT, error_code=ErrorCode.TIMEOUT, message="simulated timeout", duration_s=plan.timeout_s),
        ]
        return AdapterExecution(
            actions, Outcome(), RunStatus.TIMED_OUT,
            FailureInfo(code=ErrorCode.TIMEOUT, stage="execution", message="simulated execution timeout"),
            {"execution_time_s": plan.timeout_s, "safety_events": 0.0},
            {"scene": "mock://scene/timeout"},
        )

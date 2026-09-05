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
            # Keep the failure deterministic while making the candidate parameter
            # observable: P0 has a +20 mm residual and the generated -20 mm
            # compensation succeeds in this same mock scene.
            effective_x_error = 0.02 + plan.skill.parameters.grasp_offset_m[0]
            if abs(effective_x_error) <= 0.003:
                actions = common + [
                    RobotActionResult(action="lift", status=RunStatus.SUCCEEDED, duration_s=0.3),
                    RobotActionResult(action="place", status=RunStatus.SUCCEEDED, duration_s=0.4),
                ]
                return AdapterExecution(
                    actions,
                    Outcome(
                        object_grasped=True,
                        object_lifted=True,
                        object_placed=True,
                        position_error_m=abs(effective_x_error),
                    ),
                    RunStatus.SUCCEEDED,
                    metrics={
                        "execution_time_s": 1.3,
                        "position_error_m": abs(effective_x_error),
                        "position_error_x_m": effective_x_error,
                        "position_error_y_m": 0.0,
                        "position_error_z_m": 0.0,
                        "safety_events": 0.0,
                    },
                    artifacts={"scene": "mock://scene/pose_offset"},
                )
            actions = common + [
                RobotActionResult(action="lift", status=RunStatus.FAILED, error_code=ErrorCode.POSE_OFFSET, message="Mock：检测到 X 方向偏差"),
            ]
            return AdapterExecution(
                actions, Outcome(position_error_m=abs(effective_x_error)), RunStatus.FAILED,
                FailureInfo(code=ErrorCode.POSE_OFFSET, stage="grasp", message=f"Mock：稳定的 {effective_x_error * 1000:.1f} mm X 方向残差"),
                {
                    "execution_time_s": 0.9,
                    "position_error_m": abs(effective_x_error),
                    "position_error_x_m": effective_x_error,
                    "position_error_y_m": 0.0,
                    "position_error_z_m": 0.0,
                    "safety_events": 0.0,
                },
                {"scene": "mock://scene/pose_offset"},
            )
        if self.scenario is MockScenario.GRASP_FAILED:
            actions = common + [
                RobotActionResult(action="lift", status=RunStatus.FAILED, error_code=ErrorCode.GRASP_FAILED, message="Mock：提起时物体脱落"),
            ]
            return AdapterExecution(
                actions, Outcome(position_error_m=0.004), RunStatus.FAILED,
                FailureInfo(code=ErrorCode.GRASP_FAILED, stage="grasp", message="Mock：夹爪未能保持物体"),
                {"execution_time_s": 0.8, "position_error_m": 0.004, "safety_events": 0.0},
                {"scene": "mock://scene/grasp_failed"},
            )
        actions = common + [
            RobotActionResult(action="lift", status=RunStatus.TIMED_OUT, error_code=ErrorCode.TIMEOUT, message="Mock：执行超时", duration_s=plan.timeout_s),
        ]
        return AdapterExecution(
            actions, Outcome(), RunStatus.TIMED_OUT,
            FailureInfo(code=ErrorCode.TIMEOUT, stage="execution", message="Mock：执行超时"),
            {"execution_time_s": plan.timeout_s, "safety_events": 0.0},
            {"scene": "mock://scene/timeout"},
        )

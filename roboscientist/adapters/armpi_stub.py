"""Deliberately inert placeholder until the high-level hardware contract is verified."""

from roboscientist.adapters.base import AdapterExecution
from roboscientist.schemas import (
    ErrorCode,
    ExperimentPlan,
    FailureInfo,
    Outcome,
    RobotActionResult,
    RunStatus,
)


class ArmPiAdapterStub:
    name = "real_arm"
    data_source = "real_arm"
    hardware_status = "real_arm_runtime_unverified"

    def get_robot_state(self) -> dict:
        return {"data_source": self.data_source, "available": False}

    def preflight(self, plan: ExperimentPlan) -> RobotActionResult:
        return RobotActionResult(
            action="adapter_preflight", status=RunStatus.REJECTED,
            error_code=ErrorCode.HARDWARE_UNVERIFIED,
            message="ArmPi high-level motion and safety contract is not verified",
        )

    def stop(self, reason: str) -> RobotActionResult:
        return RobotActionResult(
            action="stop", status=RunStatus.REJECTED, error_code=ErrorCode.HARDWARE_UNVERIFIED,
            message="No hardware stop implementation is available in the stub",
        )

    def execute_pick_place(self, plan: ExperimentPlan) -> AdapterExecution:
        failure = FailureInfo(
            code=ErrorCode.HARDWARE_UNVERIFIED, stage="preflight",
            message="ArmPi adapter is a non-operational stub",
        )
        action = RobotActionResult(
            action="execute_pick_place", status=RunStatus.REJECTED,
            error_code=ErrorCode.HARDWARE_UNVERIFIED, message=failure.message,
        )
        return AdapterExecution([action], Outcome(), RunStatus.REJECTED, failure)

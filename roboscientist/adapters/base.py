"""The only hardware-facing dependency allowed to the orchestration layer."""

from typing import Optional, Protocol

from roboscientist.schemas import ExperimentPlan, RobotActionResult


class DeviceAdapter(Protocol):
    name: str
    data_source: str
    hardware_status: str

    def get_robot_state(self) -> dict:
        ...

    def preflight(self, plan: ExperimentPlan) -> RobotActionResult:
        ...

    def execute_pick_place(self, plan: ExperimentPlan) -> "AdapterExecution":
        ...

    def stop(self, reason: str) -> RobotActionResult:
        ...


class AdapterExecution:
    """Small transport object; the orchestrator turns it into an ExperimentResult."""

    def __init__(
        self,
        actions,
        outcome,
        status,
        failure=None,
        metrics=None,
        artifacts=None,
        simulation=None,
    ):
        self.actions = actions
        self.outcome = outcome
        self.status = status
        self.failure = failure
        self.metrics = metrics or {}
        self.artifacts = artifacts or {}
        self.simulation = simulation

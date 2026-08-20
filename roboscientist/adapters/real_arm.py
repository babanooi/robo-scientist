"""Safety-gated client for the ArmPi service running on the robot computer.

This module deliberately speaks only the project HTTP contract. ROS2 topics,
servo IDs, and vendor SDK calls stay on the robot in ``scripts/hardware_bridge``.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, HttpUrl

from roboscientist.adapters.base import AdapterExecution
from roboscientist.schemas import (
    ErrorCode,
    ExperimentPlan,
    FailureInfo,
    ObjectPose,
    Outcome,
    Pose,
    RobotActionResult,
    RunStatus,
    SafetyConstraints,
)


class BridgeRequestError(ConnectionError):
    def __init__(self, message: str, code: str = "", data: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


class RealArmProfile(BaseModel):
    """Versioned local configuration. It remains motion-disabled by default."""

    profile_version: str = "real-arm-profile-v0"
    bridge_url: HttpUrl
    scene_id: str = "fixed-color-cube-workcell-v0"
    target_pose: ObjectPose
    destination_pose: Pose
    safety_constraints: SafetyConstraints
    motion_enabled: bool = False
    request_timeout_s: float = Field(default=20.0, gt=0.0, le=120.0)


def load_real_arm_profile(path: Union[Path, str]) -> RealArmProfile:
    return RealArmProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))


class RealArmAdapter:
    """Calls an explicitly enabled robot-side bridge and preserves raw evidence."""

    name = "real_arm"
    data_source = "real_arm"

    def __init__(self, profile: RealArmProfile):
        self.profile = profile
        self.base_url = str(profile.bridge_url).rstrip("/")
        self.hardware_status = "real_arm_profile_loaded_motion_locked"

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}", body, method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urlopen(request, timeout=self.profile.request_timeout_s) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                failure = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                failure = {}
            detail = failure.get("error", {}) if isinstance(failure, dict) else {}
            data = failure.get("data", {}) if isinstance(failure, dict) else {}
            reasons = data.get("reasons", []) if isinstance(data, dict) else []
            message = detail.get("message", str(error)) if isinstance(detail, dict) else str(error)
            if reasons:
                message = f"{message}: {'; '.join(str(reason) for reason in reasons)}"
            raise BridgeRequestError(
                f"hardware bridge {method} {path}: {message}",
                str(detail.get("code", "")) if isinstance(detail, dict) else "",
                data if isinstance(data, dict) else {},
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ConnectionError(f"hardware bridge request {method} {path} failed: {error}") from error
        if not isinstance(decoded, dict):
            raise ConnectionError(f"hardware bridge {method} {path} returned a non-object payload")
        if decoded.get("ok") is not True or not isinstance(decoded.get("data"), dict):
            message = decoded.get("error", {}).get("message", "bridge rejected the request")
            raise ConnectionError(f"hardware bridge {method} {path}: {message}")
        result = dict(decoded["data"])
        result["_bridge_mode"] = decoded.get("mode")
        return result

    def _motion_is_explicitly_allowed(self) -> bool:
        return self.profile.motion_enabled and os.environ.get("ROBO_ALLOW_REAL_ARM") == "1"

    def get_robot_state(self) -> dict:
        try:
            return self._request("GET", "/health")
        except ConnectionError as error:
            return {
                "data_source": self.data_source,
                "available": False,
                "error_code": ErrorCode.BRIDGE_UNAVAILABLE.value,
                "message": str(error),
            }

    def preflight(self, plan: ExperimentPlan) -> RobotActionResult:
        if not self._motion_is_explicitly_allowed():
            return RobotActionResult(
                action="real_arm_enable_gate", status=RunStatus.REJECTED,
                error_code=ErrorCode.REAL_ROBOT_NOT_ALLOWED,
                message=(
                    "real motion requires profile motion_enabled=true and "
                    "ROBO_ALLOW_REAL_ARM=1"
                ),
            )
        try:
            health = self._request("GET", "/health")
        except ConnectionError as error:
            return RobotActionResult(
                action="hardware_bridge_health", status=RunStatus.REJECTED,
                error_code=ErrorCode.BRIDGE_UNAVAILABLE, message=str(error),
            )
        if (
            health.get("_bridge_mode") != "real_motion"
            or not health.get("motion_enabled")
            or not health.get("available")
        ):
            return RobotActionResult(
                action="hardware_bridge_health", status=RunStatus.REJECTED,
                error_code=ErrorCode.HARDWARE_UNVERIFIED,
                message=health.get("message", "robot bridge is not ready for real motion"),
            )
        self.hardware_status = str(health.get("hardware_status", "real_arm_connected"))
        return RobotActionResult(
            action="hardware_bridge_health", status=RunStatus.SUCCEEDED,
            message=f"bridge ready: {health.get('backend', 'unknown')}",
        )

    @staticmethod
    def _error_code(value: Any) -> ErrorCode:
        try:
            return ErrorCode(str(value or ErrorCode.NONE.value))
        except ValueError:
            return ErrorCode.EXECUTION_FAILED

    @staticmethod
    def _status(value: Any) -> RunStatus:
        try:
            return RunStatus(str(value))
        except ValueError:
            return RunStatus.FAILED

    def _rejected_execution(self, code: ErrorCode, stage: str, message: str) -> AdapterExecution:
        failure = FailureInfo(code=code, stage=stage, message=message)
        return AdapterExecution(
            [RobotActionResult(action="execute_pick_place", status=RunStatus.REJECTED, error_code=code, message=message)],
            Outcome(), RunStatus.REJECTED, failure, metrics={"safety_events": 1.0},
        )

    def execute_pick_place(self, plan: ExperimentPlan) -> AdapterExecution:
        try:
            payload = self._request("POST", "/execute_pick_place", plan.model_dump(mode="json"))
        except BridgeRequestError as error:
            if error.code == "PREFLIGHT_REJECTED":
                return self._rejected_execution(
                    ErrorCode.INVALID_PARAMETERS, "backend_preflight", str(error)
                )
            return self._rejected_execution(ErrorCode.BRIDGE_UNAVAILABLE, "bridge", str(error))
        except ConnectionError as error:
            return self._rejected_execution(ErrorCode.BRIDGE_UNAVAILABLE, "bridge", str(error))

        try:
            actions = [RobotActionResult.model_validate(item) for item in payload.get("actions", [])]
            outcome = Outcome.model_validate(payload.get("outcome", {}))
            status = self._status(payload.get("status"))
            failure_payload = payload.get("failure")
            failure = FailureInfo.model_validate(failure_payload) if failure_payload else None
            metrics = {
                key: float(value) for key, value in payload.get("metrics", {}).items()
                if isinstance(value, (int, float))
            }
            if "execution_time_s" not in metrics and "duration_s" in metrics:
                metrics["execution_time_s"] = metrics["duration_s"]
            artifacts = {key: str(value) for key, value in payload.get("artifacts", {}).items()}
        except (TypeError, ValueError) as error:
            return self._rejected_execution(
                ErrorCode.EXECUTION_FAILED, "bridge_contract",
                f"invalid execute_pick_place response: {error}",
            )
        if not actions:
            return self._rejected_execution(
                ErrorCode.EXECUTION_FAILED, "bridge_contract",
                "bridge response contains no action evidence",
            )
        if status is not RunStatus.SUCCEEDED and failure is None:
            failure = FailureInfo(
                code=self._error_code(payload.get("error_code")), stage="execution",
                message=str(payload.get("message", "robot execution failed without a failure object")),
            )
        self.hardware_status = str(payload.get("hardware_status", self.hardware_status))
        return AdapterExecution(actions, outcome, status, failure, metrics, artifacts)

    def stop(self, reason: str) -> RobotActionResult:
        try:
            payload = self._request("POST", "/stop", {"reason": reason})
            if "action" in payload:
                return RobotActionResult.model_validate(payload)
            return RobotActionResult(
                action="stop",
                status=RunStatus.SUCCEEDED if payload.get("stopped") else RunStatus.FAILED,
                error_code=ErrorCode.NONE if payload.get("stopped") else ErrorCode.EXECUTION_FAILED,
                message=str(payload.get("message", reason)),
            )
        except (ConnectionError, ValueError) as error:
            return RobotActionResult(
                action="stop", status=RunStatus.REJECTED,
                error_code=ErrorCode.BRIDGE_UNAVAILABLE, message=str(error),
            )

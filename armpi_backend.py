"""ArmPi Ultra backend using the team's already-verified robot-side scripts.

This file is intentionally run only on the robot computer.  It imports the
existing ``~/my_armpi`` scripts rather than duplicating ROS2/servo logic in the
upper application.  An optional result evaluator is required before an action
sequence may be recorded as a successful grasp experiment.
"""

import importlib
import math
import os
import sys
import time
from typing import Any, Callable, Dict, Mapping, Optional


def _load_factory(spec: str) -> Callable:
    module_name, separator, name = spec.partition(":")
    if not module_name or not separator or not name:
        raise ValueError("factory must use package.module:function format")
    factory = getattr(importlib.import_module(module_name), name)
    if not callable(factory):
        raise ValueError(f"{spec} is not callable")
    return factory


class ArmPiBackend:
    """Maps the high-level bridge contract onto proven local ArmPi functions."""

    def __init__(self) -> None:
        self.script_dir = os.environ.get("ARMPI_SCRIPT_DIR", "/home/ubuntu/my_armpi")
        self.motion_module_name = os.environ.get("ARMPI_MOTION_MODULE", "move_to_pose")
        self.control_module_name = os.environ.get("ARMPI_CONTROL_MODULE", "robot_control")
        self.home_module_name = os.environ.get("ARMPI_HOME_MODULE", "home_control")
        self.stop_module_name = os.environ.get("ARMPI_STOP_MODULE", self.motion_module_name)
        self.motion_duration_s = float(os.environ.get("ARMPI_MOTION_DURATION_S", "8"))
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if self.script_dir not in sys.path:
            sys.path.insert(0, self.script_dir)
        motion = importlib.import_module(self.motion_module_name)
        control = importlib.import_module(self.control_module_name)
        home = importlib.import_module(self.home_module_name)
        stop_module = importlib.import_module(self.stop_module_name)
        self.move_to_pose = getattr(motion, "move_to_pose")
        self.get_end_effector_pose = getattr(motion, "get_end_effector_pose", None)
        self.open_gripper = getattr(control, "open_gripper")
        self.close_gripper = getattr(control, "close_gripper")
        self.get_robot_state = getattr(control, "get_robot_state")
        self.reset_home = getattr(home, "reset_home")
        self.stop_motion = getattr(stop_module, "stop")
        if not all(callable(item) for item in (
            self.move_to_pose, self.open_gripper, self.close_gripper,
            self.get_robot_state, self.reset_home, self.stop_motion,
        )):
            raise RuntimeError("one or more required high-level ArmPi functions are unavailable")
        self._loaded = True

    def health(self) -> Dict[str, Any]:
        try:
            self._load()
            return {
                "available": True,
                "backend": "armpi_existing_scripts",
                "hardware_status": "high_level_functions_loaded",
                "script_dir": self.script_dir,
            }
        except Exception as error:
            return {
                "available": False,
                "backend": "armpi_existing_scripts",
                "hardware_status": "runtime_import_failed",
                "message": str(error),
            }

    def state(self) -> Dict[str, Any]:
        self._load()
        positions = self.get_robot_state()
        if not isinstance(positions, Mapping):
            raise RuntimeError("get_robot_state() did not return servo positions")
        pose = self.get_end_effector_pose() if callable(self.get_end_effector_pose) else None
        return {"servo_positions": dict(positions), "end_effector_pose": pose}

    @staticmethod
    def _inside_workspace(pose: Mapping[str, Any], constraints: Mapping[str, Any]) -> bool:
        minimum = constraints.get("workspace_min_m", [])
        maximum = constraints.get("workspace_max_m", [])
        values = (pose.get("x"), pose.get("y"), pose.get("z"))
        return (
            len(minimum) == 3 and len(maximum) == 3
            and all(isinstance(value, (int, float)) for value in values)
            and all(low <= value <= high for value, low, high in zip(values, minimum, maximum))
        )

    def preflight(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        health = self.health()
        if not health["available"]:
            return {"approved": False, "reason": health["message"]}
        constraints = request.get("safety_constraints", {})
        target = request.get("target_pose", {})
        destination = request.get("destination_pose", {})
        parameters = request.get("skill", {}).get("parameters", {})
        offset = parameters.get("grasp_offset_m", (0.0, 0.0, 0.0))
        adjusted_target = dict(target)
        if isinstance(offset, (list, tuple)) and len(offset) == 3:
            for axis, adjustment in zip(("x", "y", "z"), offset):
                adjusted_target[axis] = float(adjusted_target.get(axis, 0.0)) + float(adjustment)
        reasons = []
        if request.get("expected_data_source") != "real_arm":
            reasons.append("request data source is not real_arm")
        if constraints.get("allow_real_robot") is not True:
            reasons.append("real robot is disabled by the safety constraints")
        if target.get("frame_id") != "base" or destination.get("frame_id") != "base":
            reasons.append("target and destination must use the base frame")
        if not target.get("calibration_version"):
            reasons.append("target has no calibration version")
        if target.get("confidence", 0) < constraints.get("minimum_confidence", 1):
            reasons.append("target confidence is below the configured threshold")
        if not self._inside_workspace(adjusted_target, constraints):
            reasons.append("target is outside the configured workspace")
        if not self._inside_workspace(destination, constraints):
            reasons.append("destination is outside the configured workspace")
        if not 3.0 <= self.motion_duration_s <= 15.0:
            reasons.append("ARMPI_MOTION_DURATION_S must be between 3 and 15 seconds")
        approach_height = parameters.get("approach_height_m", 0.0)
        transit_height = parameters.get("transit_height_m", 0.0)
        for label, pose, height in (
            ("pregrasp", adjusted_target, approach_height),
            ("lift", adjusted_target, transit_height),
            ("preplace", destination, approach_height),
        ):
            derived = dict(pose)
            derived["z"] = float(pose.get("z", 0.0)) + float(height)
            if not self._inside_workspace(derived, constraints):
                reasons.append(f"{label} pose is outside the configured workspace")
        state = self.state()
        positions = state.get("servo_positions", {})
        for servo_id in range(1, 7):
            value = positions.get(servo_id, positions.get(str(servo_id)))
            if not isinstance(value, (int, float)) or not 0 <= value <= 1000:
                reasons.append(f"servo {servo_id} position is unavailable or invalid")
        return {"approved": not reasons, "checks": ["workspace", "calibration", "servo_state"], "reasons": reasons}

    @staticmethod
    def _pose(source: Mapping[str, Any], z_offset: float) -> Dict[str, float]:
        return {
            "x": float(source["x"]), "y": float(source["y"]),
            "z": float(source["z"]) + z_offset,
            "pitch": float(source.get("pitch", -90.0)),
        }

    @staticmethod
    def _action(name: str, started: float, success: bool, message: str = "") -> Dict[str, Any]:
        return {
            "action": name,
            "status": "succeeded" if success else "failed",
            "error_code": "NONE" if success else "EXECUTION_FAILED",
            "message": message,
            "duration_s": round(time.monotonic() - started, 3),
        }

    def _move(self, name: str, pose: Mapping[str, float]) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result = self.move_to_pose(dict(pose), speed=self.motion_duration_s)
            failed = result is False or (isinstance(result, Mapping) and result.get("success") is False)
            return self._action(name, started, not failed, str(result))
        except Exception as error:
            return self._action(name, started, False, str(error))

    def _evaluate(
        self, request: Mapping[str, Any], actions: list, final_state: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        spec = os.environ.get("ARMPI_RESULT_EVALUATOR")
        if not spec:
            return None
        evaluator = _load_factory(spec)
        result = evaluator(dict(request), list(actions), dict(final_state))
        if not isinstance(result, Mapping):
            raise RuntimeError("result evaluator must return a mapping")
        return dict(result)

    def execute_pick_place(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        self._load()
        check = self.preflight(request)
        if not check["approved"]:
            return {
                "status": "rejected", "hardware_status": "real_arm_preflight_rejected",
                "actions": [{
                    "action": "backend_preflight", "status": "rejected",
                    "error_code": "INVALID_PARAMETERS", "message": "; ".join(check["reasons"]),
                    "duration_s": 0.0,
                }],
                "outcome": {},
                "failure": {"code": "INVALID_PARAMETERS", "stage": "preflight", "message": "; ".join(check["reasons"])},
                "metrics": {}, "artifacts": {},
            }
        target = request["target_pose"]
        destination = request["destination_pose"]
        parameters = request["skill"]["parameters"]
        offset_x, offset_y, offset_z = parameters.get("grasp_offset_m", (0.0, 0.0, 0.0))
        adjusted_target = dict(target)
        adjusted_target["x"] = float(target["x"]) + float(offset_x)
        adjusted_target["y"] = float(target["y"]) + float(offset_y)
        adjusted_target["z"] = float(target["z"]) + float(offset_z)
        actions = []
        sequence = (
            ("move_to_pregrasp", lambda: self._move("move_to_pregrasp", self._pose(adjusted_target, parameters["approach_height_m"]))),
            ("move_to_grasp", lambda: self._move("move_to_grasp", self._pose(adjusted_target, 0.0))),
            ("close_gripper", lambda: self._gripper("close_gripper", self.close_gripper)),
            ("lift", lambda: self._move("lift", self._pose(adjusted_target, parameters["transit_height_m"]))),
            ("move_to_preplace", lambda: self._move("move_to_preplace", self._pose(destination, parameters["approach_height_m"]))),
            ("move_to_place", lambda: self._move("move_to_place", self._pose(destination, 0.0))),
            ("open_gripper", lambda: self._gripper("open_gripper", self.open_gripper)),
        )
        for _name, operation in sequence:
            action = operation()
            actions.append(action)
            if action["status"] != "succeeded":
                actions.append(self._stop_after_failure(action["action"]))
                return self._failed(actions, action)
        final_state = self.state()
        evaluation = self._evaluate(request, actions, final_state)
        if evaluation is None:
            return {
                "status": "failed",
                "hardware_status": "motion_completed_result_unverified",
                "actions": actions,
                "outcome": {},
                "failure": {
                    "code": "HARDWARE_UNVERIFIED", "stage": "evaluation",
                    "message": "no ARMPI_RESULT_EVALUATOR is configured; grasp outcome is unknown",
                },
                "metrics": {"execution_time_s": sum(item["duration_s"] for item in actions)},
                "artifacts": {"final_state": str(final_state)},
            }
        response = dict(evaluation)
        response.setdefault("actions", actions)
        response.setdefault("hardware_status", "real_arm_execution_evaluated")
        response.setdefault("metrics", {})["execution_time_s"] = sum(item["duration_s"] for item in actions)
        return response

    def _gripper(self, name: str, command: Callable) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result = command()
            return self._action(name, started, result is not False, str(result))
        except Exception as error:
            return self._action(name, started, False, str(error))

    def _stop_after_failure(self, action_name: str) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            try:
                result = self.stop_motion(f"failed_{action_name}")
            except TypeError:
                result = self.stop_motion()
            return self._action("stop_after_failure", started, result is not False, str(result))
        except Exception as error:
            return self._action("stop_after_failure", started, False, str(error))

    @staticmethod
    def _failed(actions: list, action: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "status": "failed", "hardware_status": "real_arm_action_failed", "actions": actions,
            "outcome": {},
            "failure": {"code": "EXECUTION_FAILED", "stage": action["action"], "message": action["message"]},
            "metrics": {"execution_time_s": sum(item["duration_s"] for item in actions)},
            "artifacts": {},
        }

    def stop(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        self._load()
        reason = str(request.get("reason", "operator_request"))
        try:
            result = self.stop_motion(reason)
            return {"stopped": result is not False, "message": str(result)}
        except Exception as error:
            return {"stopped": False, "message": str(error)}


def create_backend() -> ArmPiBackend:
    return ArmPiBackend()

"""Backend for the verified red-cuboid ArmPi wrapper on the robot computer.

The wrapper proves that a motion sequence ran. A separate result evaluator is
still required before the upper application may record a successful grasp.
"""

import importlib
import json
import math
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from roboscientist.hardware_bridge.evidence import (
    joint_trajectory_metrics,
    parse_wrapper_output,
)


def _load_callable(spec: str) -> Callable:
    module_name, separator, name = spec.partition(":")
    if not module_name or not separator or not name:
        raise ValueError("callable must use package.module:function format")
    value = getattr(importlib.import_module(module_name), name)
    if not callable(value):
        raise ValueError(f"{spec} is not callable")
    return value


class ArmPiFinalWrapperBackend:
    """Runs only the frozen dynamic red-cuboid baseline through its wrapper."""

    def __init__(self) -> None:
        self.task_dir = Path(os.environ.get("ARMPI_TASK_DIR", "/home/ubuntu/armpi_tasks"))
        self.wrapper = Path(os.environ.get(
            "ARMPI_WRAPPER",
            str(self.task_dir / "run_final_dynamic_pick_place.sh"),
        ))
        self.run_root = Path(os.environ.get(
            "ARMPI_RUN_ROOT", str(self.task_dir / "roboscientist_runs")
        ))
        self.evidence_dir = Path(os.environ.get("ARMPI_EVIDENCE_DIR", str(self.task_dir)))
        self.timeout_s = float(os.environ.get("ARMPI_WRAPPER_TIMEOUT_S", "120"))
        self.baseline_skill_version = os.environ.get("ARMPI_BASELINE_SKILL_VERSION", "p0")
        self.stop_command = shlex.split(os.environ.get("ARMPI_STOP_COMMAND", ""))
        self.result_evaluator_spec = os.environ.get("ARMPI_RESULT_EVALUATOR")
        self.experiment_runner_spec = os.environ.get("ARMPI_EXPERIMENT_RUNNER")
        self.destination = (
            float(os.environ.get("ARMPI_DESTINATION_X_M", "0.220")),
            float(os.environ.get("ARMPI_DESTINATION_Y_M", "-0.080")),
            float(os.environ.get("ARMPI_DESTINATION_Z_M", "0.030")),
        )
        self._approved: Optional[Tuple[str, float, Dict[str, Any]]] = None

    @staticmethod
    def _request_fingerprint(request: Mapping[str, Any]) -> str:
        return json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _inside_workspace(target: Mapping[str, Any], constraints: Mapping[str, Any]) -> bool:
        minimum = constraints.get("workspace_min_m", ())
        maximum = constraints.get("workspace_max_m", ())
        values = (target.get("x"), target.get("y"), target.get("z"))
        return (
            len(minimum) == 3
            and len(maximum) == 3
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
            and all(low <= value <= high for value, low, high in zip(values, minimum, maximum))
        )

    def health(self) -> Dict[str, Any]:
        problems = []
        runner_error = ""
        evaluator_error = ""
        if not self.task_dir.is_dir():
            problems.append(f"task directory does not exist: {self.task_dir}")
        if not self.wrapper.is_file():
            problems.append(f"wrapper does not exist: {self.wrapper}")
        if not self.stop_command:
            problems.append("ARMPI_STOP_COMMAND is not configured")
        if self.experiment_runner_spec:
            try:
                _load_callable(self.experiment_runner_spec)
            except Exception as error:
                runner_error = str(error)
                problems.append(f"cannot load ARMPI_EXPERIMENT_RUNNER: {error}")
        if self.result_evaluator_spec:
            try:
                _load_callable(self.result_evaluator_spec)
            except Exception as error:
                evaluator_error = str(error)
                problems.append(f"cannot load ARMPI_RESULT_EVALUATOR: {error}")
        return {
            "available": not problems,
            "backend": "armpi_final_red_cuboid_wrapper",
            "hardware_status": "verified_baseline_ready" if not problems else "configuration_incomplete",
            "wrapper": str(self.wrapper),
            "stop_available": bool(self.stop_command),
            "parameterized_skill_configured": bool(self.experiment_runner_spec) and not runner_error,
            "result_evaluator_configured": bool(self.result_evaluator_spec) and not evaluator_error,
            "message": "; ".join(problems),
        }

    def state(self) -> Dict[str, Any]:
        return {
            "state": "idle",
            "backend": "armpi_final_red_cuboid_wrapper",
            "last_preflight_at_monotonic": self._approved[1] if self._approved else None,
            "note": "this wrapper backend does not expose continuous robot state",
        }

    def _run_dir(self, request: Mapping[str, Any]) -> Path:
        raw_id = str(request.get("experiment_id", "unknown"))
        safe_id = "".join(character for character in raw_id if character.isalnum() or character in "-_")
        path = self.run_root / (safe_id or "unknown")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run_wrapper(
        self, mode: str, request: Mapping[str, Any]
    ) -> Tuple[Optional[subprocess.CompletedProcess], str, Path, float, bool, Dict[str, Any]]:
        run_dir = self._run_dir(request)
        log_path = run_dir / f"wrapper_{mode}.log"
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                ["bash", str(self.wrapper), mode],
                cwd=str(self.task_dir),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
            output = completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as error:
            completed = None
            timed_out = True
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            output = stdout + stderr + f"\nwrapper timed out after {self.timeout_s:.1f}s\n"
        log_path.write_text(output, encoding="utf-8")
        return completed, output, log_path, time.monotonic() - started, timed_out, {
            "execution_profile": "frozen_red_cuboid_baseline",
            "artifacts": {},
        }

    def _run_parameterized(
        self, mode: str, request: Mapping[str, Any]
    ) -> Tuple[Optional[subprocess.CompletedProcess], str, Path, float, bool, Dict[str, Any]]:
        run_dir = self._run_dir(request)
        log_path = run_dir / f"parameterized_{mode}.log"
        started = time.monotonic()
        timed_out = False
        try:
            runner = _load_callable(str(self.experiment_runner_spec))
            value = runner(mode, dict(request), {
                "task_dir": str(self.task_dir),
                "run_dir": str(run_dir),
                "timeout_s": self.timeout_s,
            })
            if not isinstance(value, Mapping):
                raise RuntimeError("experiment runner must return a mapping")
            output = str(value.get("output", ""))
            returncode = int(value.get("returncode", 1))
            completed = subprocess.CompletedProcess(
                args=["ARMPI_EXPERIMENT_RUNNER", mode],
                returncode=returncode,
                stdout=output,
                stderr="",
            )
            metadata = dict(value)
            metadata.pop("output", None)
            metadata.pop("returncode", None)
        except Exception as error:
            completed = subprocess.CompletedProcess(
                args=["ARMPI_EXPERIMENT_RUNNER", mode],
                returncode=1,
                stdout="",
                stderr=str(error),
            )
            output = f"experiment runner failed: {error}\n"
            metadata = {"artifacts": {}}
        log_path.write_text(output, encoding="utf-8")
        return completed, output, log_path, time.monotonic() - started, timed_out, metadata

    def _uses_parameterized_runner(self, request: Mapping[str, Any]) -> bool:
        return request.get("skill", {}).get("version") != self.baseline_skill_version

    def _run_experiment(
        self, mode: str, request: Mapping[str, Any]
    ) -> Tuple[Optional[subprocess.CompletedProcess], str, Path, float, bool, Dict[str, Any]]:
        if self._uses_parameterized_runner(request):
            return self._run_parameterized(mode, request)
        return self._run_wrapper(mode, request)

    @staticmethod
    def _same_parameters(left: Any, right: Any) -> bool:
        try:
            return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
                right, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return False

    def _parameter_evidence_reason(
        self, request: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> Optional[str]:
        if not self._uses_parameterized_runner(request):
            return None
        requested = request.get("skill", {}).get("parameters", {})
        if not self._same_parameters(metadata.get("executed_parameters"), requested):
            return "experiment runner did not prove that the requested Skill parameters were applied"
        return None

    def _static_reasons(self, request: Mapping[str, Any]) -> list:
        reasons = []
        health = self.health()
        if not health["available"]:
            reasons.append(health["message"])
        if request.get("expected_data_source") != "real_arm":
            reasons.append("request data source is not real_arm")
        constraints = request.get("safety_constraints", {})
        if constraints.get("allow_real_robot") is not True:
            reasons.append("real robot is disabled by the safety constraints")
        target = request.get("target_pose", {})
        destination = request.get("destination_pose", {})
        if target.get("frame_id") != "base" or destination.get("frame_id") != "base":
            reasons.append("target and destination must use the base frame")
        if not target.get("calibration_version"):
            reasons.append("target has no calibration version")
        if target.get("confidence", 0) < constraints.get("minimum_confidence", 1):
            reasons.append("target confidence is below the configured threshold")
        if not self._inside_workspace(target, constraints):
            reasons.append("target is outside the configured workspace")
        if not self._inside_workspace(destination, constraints):
            reasons.append("destination is outside the configured workspace")
        destination_values = tuple(destination.get(axis) for axis in ("x", "y", "z"))
        if not all(
            isinstance(value, (int, float)) and abs(value - expected) <= 0.001
            for value, expected in zip(destination_values, self.destination)
        ):
            reasons.append(
                "request destination does not match the wrapper's verified fixed point B"
            )
        skill = request.get("skill", {})
        if skill.get("version") != self.baseline_skill_version and not self.experiment_runner_spec:
            reasons.append(
                "verified wrapper supports only baseline skill version "
                f"{self.baseline_skill_version}; ARMPI_EXPERIMENT_RUNNER is required for candidates"
            )
        parameters = skill.get("parameters", {})
        offset = parameters.get("grasp_offset_m", ())
        if len(offset) != 3 or any(not isinstance(value, (int, float)) for value in offset):
            reasons.append("grasp offset must contain three numeric values")
        elif skill.get("version") == self.baseline_skill_version and any(abs(value) > 1e-9 for value in offset):
            reasons.append("frozen baseline wrapper does not accept an upper-layer grasp offset")
        if skill.get("version") != self.baseline_skill_version:
            approach = parameters.get("approach_height_m")
            transit = parameters.get("transit_height_m")
            speed = parameters.get("speed_m_s")
            changed_families = []
            valid_offset = (
                len(offset) == 3
                and all(isinstance(value, (int, float)) for value in offset)
            )
            if valid_offset and any(abs(float(value)) > 1e-9 for value in offset):
                changed_families.append("grasp_offset")
            if isinstance(transit, (int, float)) and abs(float(transit) - 0.12) > 1e-9:
                changed_families.append("path_profile")
            if not isinstance(approach, (int, float)) or abs(float(approach) - 0.03) > 1e-9:
                changed_families.append("unsupported_approach")
            if not isinstance(speed, (int, float)) or abs(float(speed) - 0.1) > 1e-9:
                changed_families.append("unsupported_speed")
            if len(changed_families) != 1 or changed_families[0].startswith("unsupported"):
                reasons.append(
                    "candidate must change exactly one supported parameter family from the p0 baseline"
                )
        if not 5 <= self.timeout_s <= 300:
            reasons.append("ARMPI_WRAPPER_TIMEOUT_S must be between 5 and 300 seconds")
        return reasons

    def preflight(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        reasons = self._static_reasons(request)
        if reasons:
            return {"approved": False, "checks": ["configuration", "request_contract"], "reasons": reasons}

        completed, output, log_path, duration_s, timed_out, metadata = self._run_experiment("check", request)
        parsed = parse_wrapper_output(output)
        if timed_out:
            reasons.append("wrapper check timed out")
        elif completed is None or completed.returncode != 0:
            reasons.append(f"wrapper check exited with code {completed.returncode if completed else 'unknown'}")
        if not parsed["numeric_safety_passed"]:
            reasons.append("wrapper did not report numeric_safety_passed")
        if not parsed["preflight_passed"]:
            reasons.append("wrapper did not report preflight_passed")
        if parsed["target"] and not self._inside_workspace(
            parsed["target"], request.get("safety_constraints", {})
        ):
            reasons.append("wrapper-detected target is outside the configured workspace")
        parameter_reason = self._parameter_evidence_reason(request, metadata)
        if parameter_reason:
            reasons.append(parameter_reason)

        result = {
            "approved": not reasons,
            "checks": ["wrapper_check", "numeric_safety", "ik_preflight", "fixed_destination"],
            "reasons": reasons,
            "duration_s": duration_s,
            "artifacts": {"wrapper_check_log": str(log_path)},
            "detected_target": parsed["target"],
            "execution_profile": metadata.get("execution_profile", "parameterized_runner"),
            "executed_parameters": metadata.get("executed_parameters"),
        }
        if not reasons:
            self._approved = (self._request_fingerprint(request), time.monotonic(), result)
        return result

    @staticmethod
    def _action(status: str, code: str, message: str, duration_s: float) -> Dict[str, Any]:
        return {
            "action": "run_final_dynamic_pick_place",
            "status": status,
            "error_code": code,
            "message": message,
            "duration_s": duration_s,
        }

    def _latest_evidence(self, filename: str, modified_after: float) -> Optional[Path]:
        if not self.evidence_dir.is_dir():
            return None
        candidates = [
            path for path in self.evidence_dir.rglob(filename)
            if path.is_file() and path.stat().st_mtime >= modified_after - 2
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None

    @staticmethod
    def _failed(action: Dict[str, Any], code: str, message: str, artifacts: Dict[str, str]) -> Dict[str, Any]:
        return {
            "status": "timed_out" if code == "TIMEOUT" else "failed",
            "hardware_status": "real_arm_wrapper_execution_failed",
            "actions": [action],
            "outcome": {},
            "failure": {"code": code, "stage": "pick_place", "message": message},
            "metrics": {"execution_time_s": action["duration_s"]},
            "artifacts": artifacts,
        }

    def _evaluate(
        self, request: Mapping[str, Any], actions: list, evidence: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if not self.result_evaluator_spec:
            return None
        result = _load_callable(self.result_evaluator_spec)(dict(request), list(actions), dict(evidence))
        if not isinstance(result, Mapping):
            raise RuntimeError("result evaluator must return a mapping")
        return dict(result)

    def execute_pick_place(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        fingerprint = self._request_fingerprint(request)
        if (
            self._approved is None
            or self._approved[0] != fingerprint
            or time.monotonic() - self._approved[1] > 60
        ):
            check = self.preflight(request)
            if not check["approved"]:
                message = "; ".join(check["reasons"])
                action = self._action("rejected", "INVALID_PARAMETERS", message, 0.0)
                return {
                    "status": "rejected",
                    "hardware_status": "real_arm_wrapper_preflight_rejected",
                    "actions": [action],
                    "outcome": {},
                    "failure": {"code": "INVALID_PARAMETERS", "stage": "preflight", "message": message},
                    "metrics": {},
                    "artifacts": check.get("artifacts", {}),
                }

        check = self._approved[2]
        started_wall = time.time()
        completed, output, log_path, duration_s, timed_out, metadata = self._run_experiment("pick-place", request)
        ended_wall = time.time()
        parsed = parse_wrapper_output(output)
        artifacts = {
            "wrapper_check_log": check["artifacts"]["wrapper_check_log"],
            "wrapper_pick_place_log": str(log_path),
            "execution_profile": str(metadata.get("execution_profile", "parameterized_runner")),
            "run_dir": str(log_path.parent),
        }
        runner_artifacts = metadata.get("artifacts", {})
        if isinstance(runner_artifacts, Mapping):
            artifacts.update({str(key): str(value) for key, value in runner_artifacts.items()})
        if parsed["target"]:
            artifacts["detected_target"] = json.dumps(parsed["target"], ensure_ascii=True, sort_keys=True)
        joint_csv = self._latest_evidence("joint_states_session.csv", started_wall)
        target_log = self._latest_evidence("grasp_events_session.log", started_wall)
        metrics = {"execution_time_s": duration_s}
        if joint_csv:
            artifacts["joint_states"] = str(joint_csv)
            try:
                metrics.update(joint_trajectory_metrics(
                    joint_csv,
                    start_wall_time_s=started_wall - 1,
                    end_wall_time_s=ended_wall + 1,
                ))
            except (OSError, ValueError) as error:
                artifacts["joint_metrics_error"] = str(error)
        if target_log:
            artifacts["grasp_targets"] = str(target_log)

        if timed_out:
            stop_result = self.stop({"reason": "wrapper_timeout"})
            artifacts["timeout_stop_result"] = json.dumps(stop_result, ensure_ascii=True)
            action = self._action("timed_out", "TIMEOUT", "wrapper execution timed out", duration_s)
            result = self._failed(action, "TIMEOUT", "wrapper execution timed out", artifacts)
            result["metrics"].update(metrics)
            return result
        parameter_reason = self._parameter_evidence_reason(request, metadata)
        if completed is None or completed.returncode != 0 or not parsed["sequence_completed"] or parameter_reason:
            message = (
                f"wrapper exited with code {completed.returncode if completed else 'unknown'}"
                if completed is None or completed.returncode != 0
                else parameter_reason
                if parameter_reason
                else "wrapper did not report pick_place_sequence_completed"
            )
            action = self._action("failed", "EXECUTION_FAILED", message, duration_s)
            result = self._failed(action, "EXECUTION_FAILED", message, artifacts)
            result["metrics"].update(metrics)
            return result

        action = self._action(
            "succeeded", "NONE", "pick_place_sequence_completed; physical outcome still requires evaluation", duration_s
        )
        evidence = {"wrapper": parsed, "metrics": metrics, "artifacts": artifacts}
        try:
            evaluation = self._evaluate(request, [action], evidence)
        except Exception as error:
            return {
                "status": "failed",
                "hardware_status": "motion_completed_evaluator_failed",
                "actions": [action],
                "outcome": {},
                "failure": {
                    "code": "HARDWARE_UNVERIFIED",
                    "stage": "evaluation",
                    "message": f"result evaluator failed: {error}",
                },
                "metrics": metrics,
                "artifacts": artifacts,
            }
        if evaluation is None:
            return {
                "status": "failed",
                "hardware_status": "motion_completed_result_unverified",
                "actions": [action],
                "outcome": {},
                "failure": {
                    "code": "HARDWARE_UNVERIFIED",
                    "stage": "evaluation",
                    "message": (
                        "the wrapper completed its motion sequence, but no result evaluator "
                        "verified grasp, lift, transport, and placement"
                    ),
                },
                "metrics": metrics,
                "artifacts": artifacts,
            }

        response = dict(evaluation)
        response.setdefault("actions", [action])
        response.setdefault("hardware_status", "real_arm_wrapper_execution_evaluated")
        response_metrics = response.setdefault("metrics", {})
        response_metrics.update({key: value for key, value in metrics.items() if key not in response_metrics})
        response_artifacts = response.setdefault("artifacts", {})
        response_artifacts.update({key: value for key, value in artifacts.items() if key not in response_artifacts})
        outcome = response.get("outcome", {})
        if response.get("status") == "succeeded" and not all(
            outcome.get(field) is True
            for field in ("object_grasped", "object_lifted", "object_placed")
        ):
            response["status"] = "failed"
            response["failure"] = {
                "code": "GRASP_FAILED",
                "stage": "evaluation",
                "message": "evaluator did not verify all required physical outcomes",
            }
        return response

    def stop(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.stop_command:
            return {"stopped": False, "message": "ARMPI_STOP_COMMAND is not configured"}
        try:
            completed = subprocess.run(
                self.stop_command,
                cwd=str(self.task_dir),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"stopped": False, "message": str(error)}
        message = (completed.stdout + completed.stderr).strip()
        return {
            "stopped": completed.returncode == 0,
            "message": message or str(request.get("reason", "operator_request")),
        }


def create_backend() -> ArmPiFinalWrapperBackend:
    return ArmPiFinalWrapperBackend()

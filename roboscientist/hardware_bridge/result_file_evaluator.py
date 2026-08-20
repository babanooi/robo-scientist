"""Strict evaluator for a vision or operator process that writes one JSON result."""

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping


REQUIRED_OUTCOMES = ("object_grasped", "object_lifted", "object_placed")
EVALUATOR_TYPES = {"vision", "operator", "hybrid"}
FAILURE_CODES = {"GRASP_FAILED", "POSE_OFFSET", "PATH_BLOCKED"}


def _failure(message: str, result_file: Path) -> Dict[str, Any]:
    return {
        "status": "failed",
        "hardware_status": "motion_completed_result_unverified",
        "outcome": {},
        "failure": {
            "code": "HARDWARE_UNVERIFIED",
            "stage": "evaluation",
            "message": message,
        },
        "metrics": {},
        "artifacts": {"evaluation_file": str(result_file)},
    }


def evaluate_result_file(
    request: Mapping[str, Any], actions: list, evidence: Mapping[str, Any]
) -> Dict[str, Any]:
    """Wait for an experiment-scoped result produced by an external evaluator."""

    del actions
    artifacts = evidence.get("artifacts", {})
    run_dir_value = artifacts.get("run_dir") if isinstance(artifacts, Mapping) else None
    if not run_dir_value:
        return _failure("evidence does not contain a run directory", Path("evaluation.json"))
    run_dir = Path(str(run_dir_value))
    filename = os.environ.get("ARMPI_EVALUATION_FILENAME", "evaluation.json")
    if Path(filename).name != filename:
        return _failure("ARMPI_EVALUATION_FILENAME must be a plain filename", run_dir / "evaluation.json")
    result_file = run_dir / filename
    request_file = run_dir / "evaluation_request.json"
    request_file.write_text(
        json.dumps(
            {
                "experiment_id": request.get("experiment_id"),
                "required_outcomes": list(REQUIRED_OUTCOMES),
                "result_file": str(result_file),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    try:
        timeout_s = float(os.environ.get("ARMPI_EVALUATION_TIMEOUT_S", "0"))
    except ValueError:
        return _failure("ARMPI_EVALUATION_TIMEOUT_S is invalid", result_file)
    deadline = time.monotonic() + max(0.0, min(timeout_s, 300.0))
    while not result_file.is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not result_file.is_file():
        return _failure("no result evaluator evidence was produced for this experiment", result_file)
    try:
        payload = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _failure(f"cannot read evaluator result: {error}", result_file)
    if not isinstance(payload, dict):
        return _failure("evaluator result must be a JSON object", result_file)
    if payload.get("experiment_id") != request.get("experiment_id"):
        return _failure("evaluator result experiment_id does not match this run", result_file)
    evaluator_type = payload.get("evaluator_type")
    if evaluator_type not in EVALUATOR_TYPES:
        return _failure("evaluator_type must be vision, operator, or hybrid", result_file)
    outcome = payload.get("outcome")
    if not isinstance(outcome, dict) or any(type(outcome.get(name)) is not bool for name in REQUIRED_OUTCOMES):
        return _failure("all three physical outcomes must be explicit booleans", result_file)
    position_error = outcome.get("position_error_m")
    if position_error is not None and (
        not isinstance(position_error, (int, float))
        or not math.isfinite(position_error)
        or position_error < 0
    ):
        return _failure("position_error_m must be a finite non-negative number", result_file)
    signed_error = payload.get("position_error_xyz_m")
    if signed_error is not None and (
        not isinstance(signed_error, list)
        or len(signed_error) != 3
        or any(
            not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in signed_error
        )
    ):
        return _failure("position_error_xyz_m must contain three finite signed numbers", result_file)
    confidence = payload.get("confidence")
    if confidence is not None and (
        not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        return _failure("confidence must be between 0 and 1", result_file)

    result_artifacts = {
        "evaluation_file": str(result_file),
        "evaluation_request": str(request_file),
        "evaluator_type": str(evaluator_type),
    }
    evidence_files = payload.get("evidence", [])
    if isinstance(evidence_files, list):
        for index, value in enumerate(evidence_files):
            if isinstance(value, str):
                result_artifacts[f"evaluation_evidence_{index + 1}"] = value
    metrics = {}
    if position_error is not None:
        metrics["position_error_m"] = float(position_error)
    if signed_error is not None:
        for axis, value in zip(("x", "y", "z"), signed_error):
            metrics[f"position_error_{axis}_m"] = float(value)
        metrics["position_error_m"] = math.sqrt(sum(float(value) ** 2 for value in signed_error))
    if confidence is not None:
        metrics["evaluation_confidence"] = float(confidence)

    failed_field = next((name for name in REQUIRED_OUTCOMES if outcome[name] is not True), None)
    if failed_field:
        stage = {
            "object_grasped": "grasp",
            "object_lifted": "lift",
            "object_placed": "place",
        }[failed_field]
        failure_code = str(payload.get("failure_code", "GRASP_FAILED"))
        if failure_code not in FAILURE_CODES:
            return _failure("failure_code is not supported", result_file)
        if failure_code == "POSE_OFFSET" and signed_error is None:
            return _failure("POSE_OFFSET requires position_error_xyz_m", result_file)
        return {
            "status": "failed",
            "hardware_status": "real_arm_physical_outcome_evaluated",
            "outcome": outcome,
            "failure": {
                "code": failure_code,
                "stage": str(payload.get("failure_stage", stage)),
                "message": str(payload.get("message", f"{failed_field} was not achieved")),
            },
            "metrics": metrics,
            "artifacts": result_artifacts,
        }
    return {
        "status": "succeeded",
        "hardware_status": "real_arm_physical_outcome_verified",
        "outcome": outcome,
        "metrics": metrics,
        "artifacts": result_artifacts,
    }

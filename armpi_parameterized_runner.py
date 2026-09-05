"""Reference runner for a hardware-verified parameter-aware ArmPi wrapper."""

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping


ACKNOWLEDGEMENT = re.compile(
    r"STATUS:\s*skill_parameters_applied\s+sha256=(?P<digest>[0-9a-f]{64})\b"
)


def _number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def validate_parameters(value: Any) -> Dict[str, Any]:
    """Apply a fixed allow-list before any parameter reaches robot-side code."""

    if not isinstance(value, Mapping):
        raise ValueError("skill parameters must be a mapping")
    required = {
        "grasp_offset_m", "approach_height_m", "transit_height_m", "speed_m_s",
    }
    if set(value) != required:
        raise ValueError("skill parameters contain missing or unsupported keys")
    offset = value["grasp_offset_m"]
    if not isinstance(offset, (list, tuple)) or len(offset) != 3:
        raise ValueError("grasp_offset_m must contain x, y, and z")
    normalized_offset = [_number(item, f"grasp_offset_m[{index}]") for index, item in enumerate(offset)]
    if any(abs(item) > 0.02 for item in normalized_offset):
        raise ValueError("grasp offset exceeds the 20 mm runner limit")
    approach = _number(value["approach_height_m"], "approach_height_m")
    transit = _number(value["transit_height_m"], "transit_height_m")
    speed = _number(value["speed_m_s"], "speed_m_s")
    if not 0.01 <= approach <= 0.10:
        raise ValueError("approach_height_m must be between 0.01 and 0.10")
    if not 0.05 <= transit <= 0.20:
        raise ValueError("transit_height_m must be between 0.05 and 0.20")
    if not 0.02 <= speed <= 0.15:
        raise ValueError("speed_m_s must be between 0.02 and 0.15")
    return {
        "grasp_offset_m": normalized_offset,
        "approach_height_m": approach,
        "transit_height_m": transit,
        "speed_m_s": speed,
    }


def _canonical_bytes(parameters: Mapping[str, Any]) -> bytes:
    return json.dumps(
        parameters, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")


def run_experiment(
    mode: str, plan: Mapping[str, Any], context: Mapping[str, Any]
) -> Dict[str, Any]:
    """Call a wrapper that explicitly acknowledges the applied parameter file."""

    if mode not in {"check", "pick-place"}:
        raise ValueError("mode must be check or pick-place")
    wrapper_value = os.environ.get("ARMPI_PARAMETERIZED_WRAPPER")
    if not wrapper_value:
        raise RuntimeError("ARMPI_PARAMETERIZED_WRAPPER is not configured")
    wrapper = Path(wrapper_value)
    if not wrapper.is_file():
        raise RuntimeError(f"parameterized wrapper does not exist: {wrapper}")
    parameters = validate_parameters(plan.get("skill", {}).get("parameters"))
    payload = _canonical_bytes(parameters)
    digest = hashlib.sha256(payload).hexdigest()
    run_dir = Path(str(context["run_dir"]))
    run_dir.mkdir(parents=True, exist_ok=True)
    parameter_file = run_dir / "skill_parameters.json"
    if parameter_file.exists() and parameter_file.read_bytes() != payload:
        raise RuntimeError("refusing to replace different parameters in the same experiment")
    if not parameter_file.exists():
        parameter_file.write_bytes(payload)
    timeout_s = float(context.get("timeout_s", 120.0))
    completed = subprocess.run(
        [
            "bash", str(wrapper), mode,
            "--parameters", str(parameter_file),
            "--parameters-sha256", digest,
        ],
        cwd=str(context["task_dir"]),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    output = completed.stdout + completed.stderr
    acknowledgement = ACKNOWLEDGEMENT.search(output)
    if completed.returncode == 0 and (
        acknowledgement is None or acknowledgement.group("digest") != digest
    ):
        output += "\nparameter wrapper did not acknowledge the requested SHA256\n"
        returncode = 1
    else:
        returncode = completed.returncode
    return {
        "returncode": returncode,
        "output": output,
        "executed_parameters": parameters,
        "execution_profile": "parameterized_red_cuboid_runner_v1",
        "artifacts": {
            "skill_parameters": str(parameter_file),
            "skill_parameters_sha256": digest,
        },
    }

"""Parsers for ArmPi wrapper logs and recorded joint-state evidence."""

import csv
import math
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STATUS = re.compile(
    r"^\s*STATUS:\s*"
    r"(numeric_safety_passed|preflight_passed|checks_passed|pick_place_sequence_completed)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
FINAL_TARGET = re.compile(
    r"FACTORY_FINAL_PICK_TARGET\s+"
    r"x=(?P<x>[+-]?\d+(?:\.\d+)?),\s*"
    r"y=(?P<y>[+-]?\d+(?:\.\d+)?),\s*"
    r"z=(?P<z>[+-]?\d+(?:\.\d+)?),\s*"
    r"shape=(?P<shape>[A-Za-z0-9_-]+),\s*"
    r"yaw=(?P<yaw>[+-]?\d+(?:\.\d+)?)"
)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def parse_wrapper_output(text: str) -> Dict[str, Any]:
    """Extract only facts explicitly printed by the verified wrapper."""

    clean = strip_ansi(text)
    status_names = {match.group(1).lower() for match in STATUS.finditer(clean)}
    targets = list(FINAL_TARGET.finditer(clean))
    target: Optional[Dict[str, Any]] = None
    if targets:
        values = targets[-1].groupdict()
        target = {
            "x": float(values["x"]),
            "y": float(values["y"]),
            "z": float(values["z"]),
            "shape": values["shape"],
            "yaw": float(values["yaw"]),
        }
    return {
        "numeric_safety_passed": "numeric_safety_passed" in status_names,
        "preflight_passed": "preflight_passed" in status_names,
        "checks_passed": "checks_passed" in status_names,
        "sequence_completed": "pick_place_sequence_completed" in status_names,
        "target": target,
        "target_count": len(targets),
    }


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def joint_trajectory_metrics(
    path: Union[Path, str],
    start_wall_time_s: Optional[float] = None,
    end_wall_time_s: Optional[float] = None,
) -> Dict[str, float]:
    """Compute joint-space metrics without pretending they are TCP distance."""

    timestamps = set()
    joint_names = set()
    previous: Dict[str, tuple] = {}
    total_joint_travel = 0.0
    derived_speeds = []
    observations = 0
    valid_velocity = 0
    valid_effort = 0
    first_wall_ns: Optional[int] = None
    last_wall_ns: Optional[int] = None

    with Path(path).open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"joint_name", "position_rad", "wall_time_ns"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("joint-state CSV is missing required columns")
        for row in reader:
            position = _finite(row.get("position_rad"))
            try:
                wall_ns = int(row.get("wall_time_ns", ""))
            except (TypeError, ValueError):
                continue
            wall_time_s = wall_ns / 1_000_000_000
            if start_wall_time_s is not None and wall_time_s < start_wall_time_s:
                continue
            if end_wall_time_s is not None and wall_time_s > end_wall_time_s:
                continue
            name = str(row.get("joint_name", "")).strip()
            if not name or position is None:
                continue
            ros_key = (row.get("ros_sec"), row.get("ros_nsec"))
            timestamps.add(ros_key if all(ros_key) else wall_ns)
            joint_names.add(name)
            observations += 1
            first_wall_ns = wall_ns if first_wall_ns is None else min(first_wall_ns, wall_ns)
            last_wall_ns = wall_ns if last_wall_ns is None else max(last_wall_ns, wall_ns)
            if _finite(row.get("velocity")) is not None:
                valid_velocity += 1
            if _finite(row.get("effort")) is not None:
                valid_effort += 1
            if name in previous:
                previous_ns, previous_position = previous[name]
                delta = abs(position - previous_position)
                total_joint_travel += delta
                elapsed_s = (wall_ns - previous_ns) / 1_000_000_000
                if elapsed_s > 0:
                    derived_speeds.append(delta / elapsed_s)
            previous[name] = (wall_ns, position)

    if not observations or first_wall_ns is None or last_wall_ns is None:
        raise ValueError("joint-state CSV contains no valid position samples")
    derived_speeds.sort()
    p95_speed = (
        derived_speeds[int(0.95 * (len(derived_speeds) - 1))]
        if derived_speeds else 0.0
    )
    return {
        "joint_trajectory_duration_s": (last_wall_ns - first_wall_ns) / 1_000_000_000,
        "joint_trajectory_points": float(len(timestamps)),
        "joint_observations": float(observations),
        "joint_count": float(len(joint_names)),
        "joint_total_travel_rad": total_joint_travel,
        "p95_derived_joint_speed_rad_s": p95_speed,
        "recorded_velocity_available": float(valid_velocity > 0),
        "recorded_effort_available": float(valid_effort > 0),
    }

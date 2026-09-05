#!/usr/bin/env python3
"""Read-only bridge capability check plus the backend's no-motion preflight."""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from roboscientist.adapters import load_real_arm_profile
from roboscientist.core.planner import build_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import SkillVersion


def placeholder_paths(value, path="profile") -> list:
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            paths.extend(placeholder_paths(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(placeholder_paths(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        normalized = value.strip().upper()
        if "REPLACE_WITH" in normalized or "REPLACE WITH" in normalized or "替换" in value:
            paths.append(path)
    return paths


def request_json(url: str, method: str = "GET", payload=None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urlopen(request, timeout=180) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{method} {url} failed: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {url} did not return a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify ArmPi bridge, safety check, evaluator, and candidate runner readiness"
    )
    parser.add_argument("--profile", required=True, help="approved real-arm profile JSON")
    parser.add_argument(
        "--require-closed-loop", action="store_true",
        help="fail unless result evaluation and candidate parameter runner are configured",
    )
    args = parser.parse_args()
    profile_path = Path(args.profile)
    try:
        raw_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read profile JSON: {error}") from error
    placeholders = placeholder_paths(raw_profile)
    if placeholders:
        raise RuntimeError(
            "profile still contains placeholders: " + ", ".join(placeholders)
        )
    profile = load_real_arm_profile(profile_path)
    calibration_version = profile.target_pose.calibration_version.strip()
    if not calibration_version:
        raise RuntimeError("profile target calibration_version is empty")
    bridge_url = str(profile.bridge_url).rstrip("/")
    health = request_json(f"{bridge_url}/health")
    if health.get("ok") is not True or not isinstance(health.get("data"), dict):
        raise RuntimeError(f"bridge health is invalid: {health}")
    data = health["data"]
    stop_probe = request_json(
        f"{bridge_url}/stop", "POST", {"reason": "readiness_probe_idle"}
    )
    stop_verified = (
        stop_probe.get("ok") is True
        and isinstance(stop_probe.get("data"), dict)
        and stop_probe["data"].get("stopped") is True
    )
    checks = {
        "real_motion_mode": health.get("mode") == "real_motion",
        "backend_available": data.get("available") is True,
        "software_stop": data.get("stop_available") is True and stop_verified,
        "result_evaluator": data.get("result_evaluator_configured") is True,
        "candidate_parameter_runner": data.get("parameterized_skill_configured") is True,
        "calibration_version_verified": True,
    }
    print(json.dumps({
        "bridge": bridge_url,
        "checks": checks,
        "health": data,
        "stop_probe": stop_probe,
        "calibration_version": calibration_version,
    }, indent=2))
    required = [
        "real_motion_mode", "backend_available", "software_stop",
        "calibration_version_verified",
    ]
    if args.require_closed_loop:
        required.extend(["result_evaluator", "candidate_parameter_runner"])
    if any(not checks[name] for name in required):
        print("RESULT: blocked; required bridge capabilities are missing")
        return 2

    plan = build_plan(
        parse_task("把红色方块放到右侧目标区域"),
        SkillVersion(version="p0"),
        "real_arm",
        profile.safety_constraints,
        scene_id=profile.scene_id,
        data_source="real_arm",
        target_pose=profile.target_pose,
        destination_pose=profile.destination_pose,
    )
    preflight = request_json(
        f"{bridge_url}/preflight", "POST", plan.model_dump(mode="json")
    )
    approved = preflight.get("ok") is True and preflight.get("data", {}).get("approved") is True
    print(json.dumps({"preflight": preflight}, ensure_ascii=False, indent=2))
    print("RESULT: ready for supervised P0" if approved else "RESULT: preflight rejected")
    return 0 if approved else 3


if __name__ == "__main__":
    raise SystemExit(main())

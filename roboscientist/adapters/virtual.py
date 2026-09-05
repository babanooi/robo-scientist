"""Auditable, dependency-free virtual pick-and-place execution.

The project originally exposed :class:`SimulationAdapter` as a safe contract
placeholder for a future Gazebo/MoveIt2 integration.  That adapter remains
intentionally inert.  This module provides a separate deterministic runtime
for the software-only demo: it models a small fixed workcell in Python,
generates a Cartesian and joint trajectory, checks a conservative obstacle,
and writes structured evidence when an artifact directory is configured.

It is deliberately *not* presented as Gazebo, MoveIt2, or physical robot
evidence.  ``data_source`` is still ``"simulation"`` so that the normal
orchestration and campaign contracts can consume it, while
``hardware_status``/``runtime_status`` identify the implementation as a
verified Python virtual runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from roboscientist.adapters.base import AdapterExecution
from roboscientist.schemas import (
    ErrorCode,
    ExperimentPlan,
    FailureInfo,
    JointTrajectoryPoint,
    Outcome,
    Pose,
    RobotActionResult,
    RunStatus,
    SimulationExecutionData,
)


class VirtualScenario(str, Enum):
    """Deterministic experiment perturbations supported by the virtual scene."""

    SUCCESS = "success"
    POSE_OFFSET = "pose_offset"
    PATH_RISK = "path_risk"
    PATH_BLOCKED = "path_blocked"
    GRASP_FAILED = "grasp_failed"
    TIMEOUT = "timeout"
    COLLISION = "collision"


@dataclass(frozen=True)
class _Waypoint:
    phase: str
    position: Tuple[float, float, float]
    dwell_s: float = 0.0


class VirtualSimulationAdapter:
    """A reproducible six-joint virtual arm for the fixed red-cuboid task.

    The class implements the same methods as ``DeviceAdapter``.  It has no
    ROS, Gazebo, MoveIt, vendor SDK, or network dependency.  A scenario can
    be supplied at construction time; a non-``default`` plan scenario takes
    precedence so the existing orchestrator/web request shape remains useful.

    Parameters
    ----------
    scenario:
        One of :class:`VirtualScenario`.  ``pose_offset`` is the default so a
        campaign immediately demonstrates a failed P0 and a compensating P1.
    artifact_root:
        Directory in which per-experiment JSON artifacts are written.  If it
        is omitted, ``ROBO_VIRTUAL_ARTIFACT_ROOT`` is consulted.  With no
        directory configured, artifact values are stable ``virtual://``
        references and the structured values remain available in the result.
    planner_name:
        Human-readable planner label.  It intentionally does not claim to be
        OMPL or MoveIt2.
    step_delay_s:
        Optional delay between simulated execution phases.  It is useful for
        testing the stop contract; the default is zero for fast demos.
    """

    name = "simulation"
    data_source = "simulation"
    # Keep the familiar simulation status vocabulary while exposing the
    # implementation detail separately in state/artifacts.  This lets the UI
    # distinguish this verified local runtime from the old unverified ROS
    # placeholder without claiming that Gazebo or a physical arm ran.
    hardware_status = "simulation_runtime_verified"
    runtime_status = "simulation_runtime_verified"
    scene_id = "simulation-virtual-workcell-v0"
    scene_version = "fixed-red-cuboid-v1"
    evaluator_version = "virtual-evaluator-v1"
    runtime_name = "roboscientist.virtual_workcell"
    runtime_version = "1.0.0"
    engine = "python_deterministic"
    joint_names = (
        "joint1",
        "joint2",
        "joint3",
        "joint4",
        "joint5",
        "joint6",
    )

    # A conservative workcell obstacle.  A transit height of 0.13 m clears
    # it; the baseline 0.12 m path intersects it in the path-risk scenario.
    _obstacle_min = (0.245, 0.015, 0.0)
    _obstacle_max = (0.285, 0.085, 0.118)
    _required_clearance_m = 0.125
    _position_tolerance_m = 0.003
    _grasp_z_recovery_m = 0.004
    _home = (0.18, -0.12, 0.18)

    _SCENARIO_ALIASES = {
        "default": None,
        "pose-offset": VirtualScenario.POSE_OFFSET,
        "path-risk": VirtualScenario.PATH_RISK,
        "path-blocked": VirtualScenario.PATH_BLOCKED,
        "grasp-failure": VirtualScenario.GRASP_FAILED,
        "grasp_failed": VirtualScenario.GRASP_FAILED,
        "collision-risk": VirtualScenario.COLLISION,
    }

    def __init__(
        self,
        scenario: Union[VirtualScenario, str] = VirtualScenario.POSE_OFFSET,
        *,
        artifact_root: Optional[Union[Path, str]] = None,
        planner_name: str = "DeterministicVirtualPlanner",
        step_delay_s: float = 0.0,
        seed: int = 7,
    ) -> None:
        self.scenario = self._coerce_scenario(scenario, allow_default=True)
        self.planner_name = str(planner_name or "DeterministicVirtualPlanner")
        self.step_delay_s = max(0.0, float(step_delay_s))
        self.seed = int(seed)
        configured_root = artifact_root
        if configured_root is None:
            configured_root = os.environ.get("ROBO_VIRTUAL_ARTIFACT_ROOT")
        self.artifact_root = Path(configured_root).expanduser() if configured_root else None
        self._lock = threading.RLock()
        self._stop_requested = False
        self._motion_state = "idle"
        self._active_experiment_id: Optional[str] = None
        self._last_joint_positions: Tuple[float, ...] = (0.0,) * len(self.joint_names)

    # ------------------------------------------------------------------
    # DeviceAdapter surface
    # ------------------------------------------------------------------
    def get_robot_state(self) -> dict:
        with self._lock:
            return {
                "available": True,
                "data_source": self.data_source,
                "hardware_status": self.hardware_status,
                "runtime_status": self.runtime_status,
                "runtime_name": self.runtime_name,
                "runtime_version": self.runtime_version,
                "engine": self.engine,
                "synthetic": True,
                "physical_robot_connected": False,
                "motion_state": self._motion_state,
                "active_experiment_id": self._active_experiment_id,
                "scene_id": self.scene_id,
                "scene_version": self.scene_version,
                "joint_names": list(self.joint_names),
                "joint_positions_rad": list(self._last_joint_positions),
                "virtual": True,
            }

    def preflight(self, plan: ExperimentPlan) -> RobotActionResult:
        """Perform deterministic no-motion checks for the virtual workcell."""

        errors = self._preflight_errors(plan)
        if errors:
            code, message = errors[0]
            return RobotActionResult(
                action="simulation_preflight",
                status=RunStatus.REJECTED,
                error_code=code,
                message=message,
            )
        return RobotActionResult(
            action="simulation_preflight",
            status=RunStatus.SUCCEEDED,
            message=(
                f"{self.runtime_status}; scene={self.scene_version}; "
                f"planner={self.planner_name}"
            ),
        )

    def stop(self, reason: str) -> RobotActionResult:
        with self._lock:
            self._stop_requested = True
            was_active = self._motion_state == "executing"
            self._motion_state = "stopped"
        return RobotActionResult(
            action="simulation_stop",
            status=RunStatus.SUCCEEDED if was_active else RunStatus.REJECTED,
            error_code=ErrorCode.STOPPED if was_active else ErrorCode.NONE,
            message=str(reason or "operator_request"),
        )

    def execute_pick_place(self, plan: ExperimentPlan) -> AdapterExecution:
        """Plan and execute one deterministic virtual pick-and-place run."""

        scenario = self._scenario_for_plan(plan)
        started = time.perf_counter()
        with self._lock:
            self._stop_requested = False
            self._motion_state = "planning"
            self._active_experiment_id = plan.experiment_id

        preflight_errors = self._preflight_errors(plan)
        if preflight_errors:
            code, message = preflight_errors[0]
            return self._rejected_execution(
                plan,
                code=code,
                stage="simulation_preflight",
                message=message,
                elapsed_s=time.perf_counter() - started,
                scenario=scenario,
            )

        waypoints = self._build_waypoints(plan)
        trajectory, tcp_trajectory, path_length_m, duration_s = self._sample_trajectory(
            waypoints, plan.skill.parameters.speed_m_s
        )
        with self._lock:
            # The trajectory has been planned; expose the execution phase to
            # runtime observers while phase delays are being applied below.
            self._motion_state = "executing"
        planning_time_ms = round(1.5 + len(trajectory) * 0.08, 3)
        collision, safety_events = self._check_path(
            waypoints, plan, scenario
        )

        # A timeout scenario uses a deterministic shortened deadline.  A caller
        # supplied deadline can also trigger the same path when it is shorter
        # than the planned duration.
        timeout_limit_s = (
            min(plan.timeout_s, duration_s * 0.55)
            if scenario is VirtualScenario.TIMEOUT
            else plan.timeout_s
        )
        timed_out = timeout_limit_s < duration_s
        stopped = self._stop_requested_now()
        path_failure = scenario in (
            VirtualScenario.PATH_RISK,
            VirtualScenario.PATH_BLOCKED,
            VirtualScenario.COLLISION,
        ) and collision

        # The virtual planner always produces a trajectory, even when it
        # rejects execution, so reviewers can inspect exactly what was tested.
        planning_success = not path_failure and not stopped
        execution_success = False
        failure_code: Optional[ErrorCode] = None
        failure_stage = ""
        failure_message = ""

        if stopped:
            failure_code = ErrorCode.STOPPED
            failure_stage = "execution"
            failure_message = "virtual execution stopped by operator request"
        elif timed_out:
            failure_code = ErrorCode.TIMEOUT
            failure_stage = "execution"
            failure_message = (
                "virtual execution exceeded the experiment timeout"
                f" ({timeout_limit_s:.3f}s)"
            )
        elif path_failure:
            failure_code = ErrorCode.PATH_BLOCKED
            failure_stage = "planning"
            failure_message = (
                "virtual transit path clearance is below the safe threshold"
            )
        else:
            execution_success = True

        # A pose-offset scenario models an observable signed residual.  The
        # optimizer's -20 mm candidate therefore succeeds under identical
        # scene conditions.
        signed_error = self._signed_position_error(plan, scenario)
        if (
            execution_success
            and scenario is VirtualScenario.POSE_OFFSET
            and math.sqrt(sum(value * value for value in signed_error))
            > self._position_tolerance_m
        ):
            execution_success = False
            failure_code = ErrorCode.POSE_OFFSET
            failure_stage = "grasp"
            failure_message = (
                "virtual evaluator measured a repeatable signed position residual"
            )

        # Grasp recovery is represented by the single allowed Z offset family.
        if (
            execution_success
            and scenario is VirtualScenario.GRASP_FAILED
            and plan.skill.parameters.grasp_offset_m[2] < self._grasp_z_recovery_m
        ):
            execution_success = False
            failure_code = ErrorCode.GRASP_FAILED
            failure_stage = "lift"
            failure_message = "virtual gripper lost the cuboid during lift"

        # Add phase-level actions after the outcome has been decided.  Their
        # durations describe the actually attempted portion of the run.
        adverse_safety_events = [
            event for event in safety_events if event != "transit_clearance_verified"
        ]
        safety_observations = [
            event for event in safety_events if event == "transit_clearance_verified"
        ]
        actual_duration_s = (
            0.0
            if path_failure
            else min(timeout_limit_s, duration_s)
            if timed_out
            else duration_s
        )
        actions = self._actions_for_result(
            duration_s=actual_duration_s,
            execution_success=execution_success,
            failure_code=failure_code,
            failure_stage=failure_stage,
        )
        outcome = self._outcome_for_result(execution_success, signed_error)
        if failure_code is ErrorCode.POSE_OFFSET:
            # It was grasped but not reliably placed; this gives the feedback
            # layer a numerical residual without claiming success.
            outcome = Outcome(
                object_grasped=True,
                object_lifted=False,
                object_placed=False,
                position_error_m=self._norm(signed_error),
            )
        elif failure_code is ErrorCode.GRASP_FAILED:
            outcome = Outcome(
                object_grasped=True,
                object_lifted=False,
                object_placed=False,
                position_error_m=self._norm(signed_error),
            )
        elif failure_code is ErrorCode.PATH_BLOCKED:
            outcome = Outcome(position_error_m=self._norm(signed_error))

        metrics = {
            "execution_time_s": round(actual_duration_s, 6),
            "planned_duration_s": round(duration_s, 6),
            "timeout_limit_s": round(timeout_limit_s, 6),
            "path_length_m": round(path_length_m, 6),
            "trajectory_length_m": round(path_length_m, 6),
            "planning_time_ms": planning_time_ms,
            "trajectory_points": float(len(trajectory)),
            "safety_events": float(len(adverse_safety_events)),
            "collision_detected": 1.0 if collision else 0.0,
            "position_error_m": round(self._norm(signed_error), 6),
            "position_error_x_m": round(signed_error[0], 6),
            "position_error_y_m": round(signed_error[1], 6),
            "position_error_z_m": round(signed_error[2], 6),
            "path_clearance_m": round(
                max(0.0, plan.skill.parameters.transit_height_m - self._required_clearance_m),
                6,
            ),
        }

        status = (
            RunStatus.TIMED_OUT
            if timed_out
            else RunStatus.FAILED
            if not execution_success
            else RunStatus.SUCCEEDED
        )
        # ``RunStatus`` intentionally has no STOPPED value; stopped executions
        # are represented as failed with ErrorCode.STOPPED per the shared
        # result contract.
        if stopped:
            status = RunStatus.FAILED
        failure = (
            FailureInfo(
                code=failure_code or ErrorCode.EXECUTION_FAILED,
                stage=failure_stage or "execution",
                message=failure_message or "virtual execution failed",
            )
            if not execution_success
            else None
        )

        evaluation_payload = {
            "evaluator_type": "virtual_deterministic",
            "evaluator_version": self.evaluator_version,
            "experiment_id": plan.experiment_id,
            "scenario": scenario.value,
            "status": status.value,
            "outcome": outcome.model_dump(mode="json"),
            "failure": failure.model_dump(mode="json") if failure else None,
            "metrics": dict(metrics),
            "collision_detected": collision,
            "planning_success": planning_success,
            "execution_success": execution_success,
            "safety_events": list(adverse_safety_events),
            "safety_observations": list(safety_observations),
        }
        simulation = SimulationExecutionData(
            runtime_status=self.runtime_status,
            planning_success=planning_success,
            execution_success=execution_success,
            collision_detected=collision,
            planner_name=self.planner_name,
            planning_time_ms=planning_time_ms,
            trajectory_length_m=round(path_length_m, 6),
            trajectory_points=len(trajectory),
            joint_trajectory=trajectory,
            safety_events=adverse_safety_events,
            failure_reason=failure_message or None,
            raw_ros_refs=[],
            runtime_name=self.runtime_name,
            runtime_version=self.runtime_version,
            engine=self.engine,
            random_seed=self.seed,
            synthetic=True,
            physical_robot_connected=False,
            scene_objects=self._scene_objects(plan),
            tcp_trajectory=tcp_trajectory,
            safety_observations=safety_observations,
            evaluation=evaluation_payload,
        )

        artifacts = self._write_artifacts(
            plan=plan,
            scenario=scenario,
            status=status,
            outcome=outcome,
            failure=failure,
            actions=actions,
            metrics=metrics,
            trajectory=trajectory,
            tcp_trajectory=tcp_trajectory,
            waypoints=waypoints,
            safety_events=adverse_safety_events,
            safety_observations=safety_observations,
            collision=collision,
            planning_success=planning_success,
            execution_success=execution_success,
        )

        replay = self._build_replay(
            plan=plan,
            scenario=scenario,
            status=status,
            actions=actions,
            trajectory=trajectory,
            tcp_trajectory=tcp_trajectory,
            metrics=metrics,
            safety_events=adverse_safety_events,
            safety_observations=safety_observations,
            failure=failure,
        )

        with self._lock:
            self._motion_state = "idle" if not stopped else "stopped"
            self._active_experiment_id = None
            if trajectory:
                self._last_joint_positions = trajectory[-1].joint_positions_rad

        # The wall-clock runtime is deliberately not used as the scientific
        # execution time; deterministic simulated time is in ``metrics``.
        _ = started
        return AdapterExecution(
            actions=actions,
            outcome=outcome,
            status=status,
            failure=failure,
            metrics=metrics,
            artifacts=artifacts,
            simulation=simulation,
            replay=replay,
        )

    # ------------------------------------------------------------------
    # Planning and deterministic physics helpers
    # ------------------------------------------------------------------
    @classmethod
    def _coerce_scenario(
        cls, value: Union[VirtualScenario, str, None], allow_default: bool = False
    ) -> Optional[VirtualScenario]:
        if value is None:
            return None
        if isinstance(value, VirtualScenario):
            return value
        normalized = str(value).strip().lower().replace(" ", "_")
        if normalized in cls._SCENARIO_ALIASES:
            alias = cls._SCENARIO_ALIASES[normalized]
            if alias is None and allow_default:
                return None
            if alias is not None:
                return alias
        try:
            return VirtualScenario(normalized)
        except ValueError as error:
            names = ", ".join(item.value for item in VirtualScenario)
            raise ValueError(f"virtual scenario must be one of: {names}") from error

    def _scenario_for_plan(self, plan: ExperimentPlan) -> VirtualScenario:
        requested = self._coerce_scenario(plan.execution_scenario, allow_default=True)
        return requested or self.scenario or VirtualScenario.POSE_OFFSET

    def _preflight_errors(self, plan: ExperimentPlan) -> List[Tuple[ErrorCode, str]]:
        errors: List[Tuple[ErrorCode, str]] = []
        target = plan.target_pose
        destination = plan.destination_pose
        if plan.expected_data_source != self.data_source:
            errors.append(
                (ErrorCode.INVALID_PARAMETERS, "plan data source is not simulation")
            )
        if plan.task.target_color.strip().lower() != "red":
            errors.append(
                (ErrorCode.TARGET_NOT_FOUND, "virtual scene contains only a red cuboid")
            )
        if plan.task.target_object.strip().lower() not in {"cube", "cuboid", "block", "方块", "长方体"}:
            errors.append(
                (ErrorCode.TARGET_NOT_FOUND, "virtual scene contains only a cuboid target")
            )
        if target.frame_id != "base" or destination.frame_id != "base":
            errors.append(
                (ErrorCode.INVALID_PARAMETERS, "virtual workcell requires the base frame")
            )
        if not target.calibration_version:
            errors.append(
                (ErrorCode.CALIBRATION_MISSING, "virtual target calibration is missing")
            )
        if target.confidence < plan.safety_constraints.minimum_confidence:
            errors.append(
                (ErrorCode.LOW_CONFIDENCE, "target confidence is below the safety threshold")
            )
        if any(
            value < low or value > high
            for value, low, high in zip(
                (target.x, target.y, target.z),
                plan.safety_constraints.workspace_min_m,
                plan.safety_constraints.workspace_max_m,
            )
        ):
            errors.append((ErrorCode.OUT_OF_WORKSPACE, "target is outside the virtual workspace"))
        if any(
            value < low or value > high
            for value, low, high in zip(
                (destination.x, destination.y, destination.z),
                plan.safety_constraints.workspace_min_m,
                plan.safety_constraints.workspace_max_m,
            )
        ):
            errors.append(
                (ErrorCode.OUT_OF_WORKSPACE, "destination is outside the virtual workspace")
            )
        if plan.skill.parameters.speed_m_s > plan.safety_constraints.max_speed_m_s:
            errors.append((ErrorCode.INVALID_PARAMETERS, "virtual speed exceeds the safety profile"))
        return errors

    def _build_waypoints(self, plan: ExperimentPlan) -> List[_Waypoint]:
        target = plan.target_pose
        destination = plan.destination_pose
        ox, oy, oz = plan.skill.parameters.grasp_offset_m
        grasp = (target.x + ox, target.y + oy, target.z + oz)
        pregrasp = (grasp[0], grasp[1], grasp[2] + plan.skill.parameters.approach_height_m)
        lift = (grasp[0], grasp[1], grasp[2] + max(plan.skill.parameters.approach_height_m, 0.05))
        transit = (
            destination.x,
            destination.y,
            max(destination.z, plan.skill.parameters.transit_height_m),
        )
        place = (destination.x, destination.y, destination.z)
        return [
            _Waypoint("home", self._home),
            _Waypoint("approach", pregrasp),
            _Waypoint("grasp", grasp, dwell_s=0.15),
            _Waypoint("lift", lift, dwell_s=0.10),
            # Raise vertically before crossing the obstacle.  This makes the
            # transit-height parameter causally observable: P0 at 0.12 m is
            # below the required 0.125 m clearance, while P1 at 0.13 m clears
            # the horizontal segment.
            _Waypoint(
                "transit_rise",
                (lift[0], lift[1], max(lift[2], plan.skill.parameters.transit_height_m)),
            ),
            _Waypoint("transit", transit),
            _Waypoint("place", place, dwell_s=0.20),
            _Waypoint("retreat", (place[0], place[1], place[2] + 0.05)),
        ]

    def _sample_trajectory(
        self, waypoints: Sequence[_Waypoint], speed_m_s: float
    ) -> Tuple[List[JointTrajectoryPoint], List[Pose], float, float]:
        speed = max(0.01, float(speed_m_s))
        points: List[JointTrajectoryPoint] = []
        tcp_points: List[Pose] = []
        elapsed = 0.0
        path_length = 0.0
        previous = waypoints[0].position
        points.append(
            JointTrajectoryPoint(
                time_from_start_s=0.0,
                joint_positions_rad=self._inverse_kinematics(previous),
            )
        )
        tcp_points.append(
            Pose(x=previous[0], y=previous[1], z=previous[2], frame_id="base")
        )
        for current_waypoint in waypoints[1:]:
            current = current_waypoint.position
            distance = self._distance(previous, current)
            path_length += distance
            segments = max(1, int(math.ceil(distance / 0.025)))
            segment_dt = distance / speed / segments if distance else 0.0
            start = previous
            for index in range(1, segments + 1):
                ratio = index / segments
                point = tuple(
                    start[axis] + (current[axis] - start[axis]) * ratio
                    for axis in range(3)
                )
                elapsed += segment_dt
                points.append(
                    JointTrajectoryPoint(
                        time_from_start_s=round(elapsed, 6),
                        joint_positions_rad=self._inverse_kinematics(point),
                    )
                )
                tcp_points.append(
                    Pose(x=point[0], y=point[1], z=point[2], frame_id="base")
                )
            elapsed += max(0.0, current_waypoint.dwell_s)
            if current_waypoint.dwell_s:
                # Keep a distinct timestamp for the dwell endpoint.  Repeating
                # the joint position is useful when a reviewer plots the trace.
                points.append(
                    JointTrajectoryPoint(
                        time_from_start_s=round(elapsed, 6),
                        joint_positions_rad=self._inverse_kinematics(current),
                    )
                )
                tcp_points.append(
                    Pose(x=current[0], y=current[1], z=current[2], frame_id="base")
                )
            previous = current
        return points, tcp_points, path_length, elapsed

    @staticmethod
    def _inverse_kinematics(position: Tuple[float, float, float]) -> Tuple[float, ...]:
        """Map Cartesian points to a stable, six-joint illustrative trace."""

        x, y, z = position
        radial = math.hypot(x, y)
        q1 = math.atan2(y, x)
        q2 = math.atan2(z, max(radial, 1e-9)) - 0.35
        q3 = (radial - 0.22) * 4.0
        q4 = -q2 * 0.55
        q5 = math.atan2(0.08, max(z, 1e-4)) - 0.9
        q6 = q1 * 0.25
        return tuple(round(value, 7) for value in (q1, q2, q3, q4, q5, q6))

    def _signed_position_error(
        self, plan: ExperimentPlan, scenario: VirtualScenario
    ) -> Tuple[float, float, float]:
        if scenario is not VirtualScenario.POSE_OFFSET:
            return tuple(float(value) for value in plan.skill.parameters.grasp_offset_m)
        # Fixed sensor/model bias: P0 has +20 mm X residual, and the candidate
        # generated by ``candidate_from_failure`` subtracts it.
        base = (0.020, 0.0, 0.0)
        offset = plan.skill.parameters.grasp_offset_m
        return tuple(base[index] + float(offset[index]) for index in range(3))

    def _check_path(
        self,
        waypoints: Sequence[_Waypoint],
        plan: ExperimentPlan,
        scenario: VirtualScenario,
    ) -> Tuple[bool, List[str]]:
        events: List[str] = []
        collision = False
        # ``collision`` is a non-recoverable safety scenario.  It represents
        # an independently predicted obstacle conflict, so a candidate Skill
        # must never make the run appear safe merely by changing transit
        # height.  Keep this distinct from the recoverable ``path_risk``
        # scenario, where the same parameter family is intentionally tested.
        if scenario is VirtualScenario.COLLISION:
            return True, ["predicted_collision_with_workcell_obstacle"]
        # Explicit path-risk scenarios model the known low-clearance baseline;
        # the candidate's +10 mm transit-height adjustment clears it.
        if scenario in (VirtualScenario.PATH_RISK, VirtualScenario.PATH_BLOCKED):
            if plan.skill.parameters.transit_height_m < self._required_clearance_m:
                collision = True
                events.append(
                    "transit_clearance_below_0.125m"
                )
        # Generic sampled obstacle check is applied to all scenarios.  It does
        # not turn a normal pose-offset run into a failure unless the path
        # actually enters the obstacle volume.
        for left, right in zip(waypoints, waypoints[1:]):
            distance = self._distance(left.position, right.position)
            samples = max(1, int(math.ceil(distance / 0.01)))
            for index in range(samples + 1):
                ratio = index / samples
                point = tuple(
                    left.position[axis]
                    + (right.position[axis] - left.position[axis]) * ratio
                    for axis in range(3)
                )
                if self._inside_obstacle(point):
                    # The low-clearance condition is only an actionable failure
                    # in the path-risk family; otherwise report a safety event
                    # while keeping the baseline deterministic.
                    if scenario in (
                        VirtualScenario.PATH_RISK,
                        VirtualScenario.PATH_BLOCKED,
                    ):
                        collision = True
                    if "obstacle_intersection_predicted" not in events:
                        events.append("obstacle_intersection_predicted")
                    break
        if not collision and scenario in (VirtualScenario.PATH_RISK, VirtualScenario.PATH_BLOCKED):
            events.append("transit_clearance_verified")
        return collision, events

    @classmethod
    def _inside_obstacle(cls, point: Tuple[float, float, float]) -> bool:
        return all(
            low <= value <= high
            for value, low, high in zip(point, cls._obstacle_min, cls._obstacle_max)
        )

    @staticmethod
    def _distance(left: Sequence[float], right: Sequence[float]) -> float:
        return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))

    @staticmethod
    def _norm(values: Sequence[float]) -> float:
        return math.sqrt(sum(float(value) ** 2 for value in values))

    def _stop_requested_now(self) -> bool:
        with self._lock:
            return self._stop_requested

    def _actions_for_result(
        self,
        *,
        duration_s: float,
        execution_success: bool,
        failure_code: Optional[ErrorCode],
        failure_stage: str,
    ) -> List[RobotActionResult]:
        phase_durations = {
            "approach": round(duration_s * 0.34, 6),
            "close_gripper": round(duration_s * 0.08, 6),
            "lift": round(duration_s * 0.16, 6),
            "transit": round(duration_s * 0.28, 6),
            "place": round(duration_s * 0.14, 6),
        }
        actions: List[RobotActionResult] = []
        # The action trace is deliberately sequential: once a phase fails,
        # later physical phases are marked rejected instead of appearing to
        # have run.  This keeps the trace consistent with the final Outcome.
        failure_action = self._action_stage(failure_stage)
        if failure_code is ErrorCode.POSE_OFFSET:
            # The gripper closes successfully; the independent pose evaluator
            # then rejects the measured residual before lift/place begin.
            failure_action = "grasp_evaluation"
        if failure_code is ErrorCode.PATH_BLOCKED:
            # Collision is predicted before any physical phase is executed.
            failure_action = "path_check"

        failure_seen = False
        for name, phase_duration in phase_durations.items():
            if execution_success:
                status = RunStatus.SUCCEEDED
                error_code = ErrorCode.NONE
                message = ""
            elif failure_code is ErrorCode.STOPPED:
                status = RunStatus.REJECTED
                error_code = ErrorCode.NONE
                message = "not executed after operator stop"
            elif failure_action == "path_check":
                status = RunStatus.REJECTED
                error_code = ErrorCode.NONE
                message = "not executed after path safety rejection"
            elif name == failure_action:
                status = (
                    RunStatus.TIMED_OUT
                    if failure_code is ErrorCode.TIMEOUT
                    else RunStatus.FAILED
                )
                error_code = failure_code or ErrorCode.EXECUTION_FAILED
                message = "virtual action failed at this phase"
                failure_seen = True
            elif failure_seen:
                status = RunStatus.REJECTED
                error_code = ErrorCode.NONE
                message = "not executed after prior phase failure"
            else:
                status = RunStatus.SUCCEEDED
                error_code = ErrorCode.NONE
                message = ""
            actions.append(
                RobotActionResult(
                    action=name,
                    status=status,
                    error_code=error_code,
                    message=message,
                    duration_s=phase_duration,
                )
            )
            if self.step_delay_s:
                time.sleep(self.step_delay_s)

            # Insert the evaluation gate immediately after the successful
            # gripper close for the pose-offset scenario.  It is a software
            # observation, not a second robot motion command.
            if name == "close_gripper" and failure_action == "grasp_evaluation":
                actions.append(
                    RobotActionResult(
                        action="grasp_evaluation",
                        status=RunStatus.FAILED,
                        error_code=ErrorCode.POSE_OFFSET,
                        message="virtual evaluator measured a repeatable signed position residual",
                        duration_s=0.0,
                    )
                )
                failure_seen = True

            # A path check is performed before the physical phases.  Keep it
            # visible in the timeline so a reviewer can distinguish planning
            # rejection from a failed transit motion.
            if name == "approach" and failure_action == "path_check":
                actions.insert(
                    len(actions) - 1,
                    RobotActionResult(
                        action="path_check",
                        status=RunStatus.FAILED,
                        error_code=ErrorCode.PATH_BLOCKED,
                        message="virtual transit path clearance is below the safe threshold",
                        duration_s=0.0,
                    )
                )
        # Ensure a failure is visible even if its semantic stage is not one of
        # the canonical action names.
        if not execution_success and failure_code is ErrorCode.STOPPED:
            actions.append(
                RobotActionResult(
                    action="stop",
                    status=RunStatus.FAILED,
                    error_code=ErrorCode.STOPPED,
                    message="virtual execution stopped by operator request",
                )
            )
        return actions

    @staticmethod
    def _action_stage(stage: str) -> str:
        return {
            "grasp": "close_gripper",
            "lift": "lift",
            "planning": "transit",
            "execution": "transit",
        }.get(stage, stage)

    @staticmethod
    def _outcome_for_result(
        success: bool, signed_error: Sequence[float]
    ) -> Outcome:
        return Outcome(
            object_grasped=success,
            object_lifted=success,
            object_placed=success,
            position_error_m=VirtualSimulationAdapter._norm(signed_error),
        )

    def _rejected_execution(
        self,
        plan: ExperimentPlan,
        *,
        code: ErrorCode,
        stage: str,
        message: str,
        elapsed_s: float,
        scenario: VirtualScenario,
    ) -> AdapterExecution:
        failure = FailureInfo(code=code, stage=stage, message=message)
        action = RobotActionResult(
            action="simulation_preflight",
            status=RunStatus.REJECTED,
            error_code=code,
            message=message,
            duration_s=max(0.0, elapsed_s),
        )
        simulation = SimulationExecutionData(
            runtime_status=self.runtime_status,
            planning_success=False,
            execution_success=False,
            collision_detected=False,
            planner_name=self.planner_name,
            safety_events=["preflight_rejected"],
            failure_reason=message,
            runtime_name=self.runtime_name,
            runtime_version=self.runtime_version,
            engine=self.engine,
            random_seed=self.seed,
            synthetic=True,
            physical_robot_connected=False,
            scene_objects=self._scene_objects(plan),
            safety_observations=[],
            evaluation={
                "evaluator_type": "virtual_deterministic",
                "evaluator_version": self.evaluator_version,
                "status": RunStatus.REJECTED.value,
                "failure": failure.model_dump(mode="json"),
            },
        )
        artifacts = self._write_artifacts(
            plan=plan,
            scenario=scenario,
            status=RunStatus.REJECTED,
            outcome=Outcome(),
            failure=failure,
            actions=[action],
            metrics={"execution_time_s": 0.0, "safety_events": 1.0},
            trajectory=[],
            tcp_trajectory=[],
            waypoints=[],
            safety_events=["preflight_rejected"],
            safety_observations=[],
            collision=False,
            planning_success=False,
            execution_success=False,
        )
        with self._lock:
            self._motion_state = "idle"
            self._active_experiment_id = None
        replay = self._build_replay(
            plan=plan,
            scenario=scenario,
            status=RunStatus.REJECTED,
            actions=[action],
            trajectory=[],
            tcp_trajectory=[],
            metrics={"execution_time_s": 0.0, "safety_events": 1.0},
            safety_events=["preflight_rejected"],
            safety_observations=[],
            failure=failure,
        )
        return AdapterExecution(
            actions=[action],
            outcome=Outcome(),
            status=RunStatus.REJECTED,
            failure=failure,
            metrics={"execution_time_s": 0.0, "safety_events": 1.0},
            artifacts=artifacts,
            simulation=simulation,
            replay=replay,
        )

    # ------------------------------------------------------------------
    # Evidence artifacts
    # ------------------------------------------------------------------
    def _write_artifacts(
        self,
        *,
        plan: ExperimentPlan,
        scenario: VirtualScenario,
        status: RunStatus,
        outcome: Outcome,
        failure: Optional[FailureInfo],
        actions: Sequence[RobotActionResult],
        metrics: Mapping[str, float],
        trajectory: Sequence[JointTrajectoryPoint],
        tcp_trajectory: Sequence[Pose],
        waypoints: Sequence[_Waypoint],
        safety_events: Sequence[str],
        safety_observations: Sequence[str],
        collision: bool,
        planning_success: bool,
        execution_success: bool,
    ) -> Dict[str, str]:
        base_uri = f"virtual://{plan.experiment_id}"
        trajectory_payload = {
            "format_version": "virtual-trajectory-v1",
            "data_source": self.data_source,
            "scene_id": self.scene_id,
            "scene_version": self.scene_version,
            "planner_name": self.planner_name,
            "joint_names": list(self.joint_names),
            "points": [
                {
                    "time_from_start_s": point.time_from_start_s,
                    "joint_positions_rad": list(point.joint_positions_rad),
                    "joints": {
                        name: value
                        for name, value in zip(
                            self.joint_names, point.joint_positions_rad
                        )
                    },
                        "tcp_position_m": (
                            [
                                tcp_trajectory[index].x,
                                tcp_trajectory[index].y,
                                tcp_trajectory[index].z,
                            ]
                            if index < len(tcp_trajectory)
                            else None
                        ),
                }
                for index, point in enumerate(trajectory)
            ],
            "trajectory_points": len(trajectory),
            "trajectory_length_m": metrics.get("trajectory_length_m", 0.0),
            "planned_duration_s": metrics.get("planned_duration_s", 0.0),
            "execution_time_s": metrics.get("execution_time_s", 0.0),
        }
        scene_payload = {
            "scene_id": self.scene_id,
            "scene_version": self.scene_version,
            "object": {
                "object_id": plan.target_pose.object_id,
                "color": plan.task.target_color,
                "shape": plan.task.target_object,
                "pose_m": plan.target_pose.model_dump(mode="json"),
            },
            "destination_pose_m": plan.destination_pose.model_dump(mode="json"),
            "obstacle": {
                "min_m": list(self._obstacle_min),
                "max_m": list(self._obstacle_max),
            },
            "objects": self._scene_objects(plan),
            "waypoints": [
                {"phase": item.phase, "position_m": list(item.position)}
                for item in waypoints
            ],
        }
        evaluation_payload = {
            "evaluator_type": "virtual_deterministic",
            "evaluator_version": self.evaluator_version,
            "experiment_id": plan.experiment_id,
            "scenario": scenario.value,
            "status": status.value,
            "outcome": outcome.model_dump(mode="json"),
            "failure": failure.model_dump(mode="json") if failure else None,
            "metrics": dict(metrics),
            "collision_detected": collision,
            "planning_success": planning_success,
            "execution_success": execution_success,
            "safety_events": list(safety_events),
            "safety_observations": list(safety_observations),
        }
        trace_payload = {
            "experiment_id": plan.experiment_id,
            "scenario": scenario.value,
            "status": status.value,
            "actions": [item.model_dump(mode="json") for item in actions],
            "seed": self.seed,
        }
        manifest_payload = {
            "format_version": "virtual-evidence-v1",
            "experiment_id": plan.experiment_id,
            "data_source": self.data_source,
            "runtime_status": self.runtime_status,
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "engine": self.engine,
            "synthetic": True,
            "physical_robot_connected": False,
            "files": {},
        }

        payloads = {
            "scene": scene_payload,
            "trajectory": trajectory_payload,
            "evaluation": evaluation_payload,
            "execution_trace": trace_payload,
        }
        replay_payload = self._build_replay(
            plan=plan,
            scenario=scenario,
            status=status,
            actions=actions,
            trajectory=trajectory,
            tcp_trajectory=tcp_trajectory,
            metrics=metrics,
            safety_events=safety_events,
            safety_observations=safety_observations,
            failure=failure,
        )
        payloads["replay"] = replay_payload
        if self.artifact_root is None:
            return {
                "data_source": self.data_source,
                "runtime_status": self.runtime_status,
                "evaluator_type": "virtual_deterministic",
                "evaluator_version": self.evaluator_version,
                "scene": f"{base_uri}/scene.json",
                "trajectory": f"{base_uri}/trajectory.json",
                "evaluation_file": f"{base_uri}/evaluation.json",
                "execution_trace": f"{base_uri}/execution_trace.json",
                "replay": f"{base_uri}/replay.json",
                "artifact_manifest": f"{base_uri}/manifest.json",
            }

        root = self.artifact_root / plan.experiment_id
        root.mkdir(parents=True, exist_ok=True)
        artifacts: Dict[str, str] = {
            "data_source": self.data_source,
            "runtime_status": self.runtime_status,
            "evaluator_type": "virtual_deterministic",
            "evaluator_version": self.evaluator_version,
        }
        for key, payload in payloads.items():
            path = root / f"{key}.json"
            self._write_json(path, payload)
            artifacts[key if key != "evaluation" else "evaluation_file"] = str(path)
            manifest_payload["files"][path.name] = self._sha256_file(path)
        manifest_path = root / "manifest.json"
        self._write_json(manifest_path, manifest_payload)
        artifacts["artifact_manifest"] = str(manifest_path)
        return artifacts

    def _scene_objects(self, plan: ExperimentPlan) -> List[Dict[str, object]]:
        """Return the immutable object/obstacle snapshot used by this run."""

        target = plan.target_pose
        return [
            {
                "object_id": target.object_id,
                "kind": "target",
                "color": plan.task.target_color,
                "shape": plan.task.target_object,
                "pose_m": {
                    "x": target.x,
                    "y": target.y,
                    "z": target.z,
                    "frame_id": target.frame_id,
                },
                "size_m": [0.04, 0.04, 0.04],
            },
            {
                "object_id": "transit-obstacle",
                "kind": "obstacle",
                "shape": "box",
                "min_m": list(self._obstacle_min),
                "max_m": list(self._obstacle_max),
            },
            {
                "object_id": "destination-zone",
                "kind": "destination",
                "pose_m": {
                    "x": plan.destination_pose.x,
                    "y": plan.destination_pose.y,
                    "z": plan.destination_pose.z,
                    "frame_id": plan.destination_pose.frame_id,
                },
                "size_m": [0.08, 0.08, 0.01],
            },
        ]

    def _build_replay(
        self,
        *,
        plan: ExperimentPlan,
        scenario: VirtualScenario,
        status: RunStatus,
        actions: Sequence[RobotActionResult],
        trajectory: Sequence[JointTrajectoryPoint],
        tcp_trajectory: Sequence[Pose],
        metrics: Mapping[str, float],
        safety_events: Sequence[str],
        safety_observations: Sequence[str],
        failure: Optional[FailureInfo],
    ) -> Dict[str, object]:
        """Build the small read-only payload consumed by the action-panel UI."""

        points = []
        for index, point in enumerate(trajectory):
            joints = {
                name: value
                for name, value in zip(self.joint_names, point.joint_positions_rad)
            }
            tcp = tcp_trajectory[index] if index < len(tcp_trajectory) else None
            points.append(
                {
                    "time_from_start_s": point.time_from_start_s,
                    "joints": joints,
                    "joint_positions_rad": list(point.joint_positions_rad),
                    "tcp_position_m": (
                        [tcp.x, tcp.y, tcp.z] if tcp is not None else None
                    ),
                }
            )
        return {
            "available": bool(points),
            "data_source": self.data_source,
            "read_only": True,
            "motion_requested": False,
            "synthetic": True,
            "physical_robot_connected": False,
            "runtime_status": self.runtime_status,
            "scene_id": plan.scene_id,
            "scenario": scenario.value,
            "trajectory": {
                "joint_names": list(self.joint_names),
                "points": points,
                "returned_points": len(points),
                "original_points": len(points),
                "trajectory_length_m": metrics.get("trajectory_length_m", 0.0),
                "planned_duration_s": metrics.get("planned_duration_s", 0.0),
                "duration_s": metrics.get("execution_time_s", 0.0),
            },
            "events": [item.model_dump(mode="json") for item in actions],
            "safety_events": list(safety_events),
            "safety_observations": list(safety_observations),
            "metrics": dict(metrics),
            "failure": failure.model_dump(mode="json") if failure else None,
            "reason": failure.message if failure else "virtual execution completed",
        }

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        # Evidence directories are per experiment.  If a caller deliberately
        # reuses an experiment ID, keep the first record rather than silently
        # changing the audit trail; identical content is harmless.
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != encoded:
                raise FileExistsError(f"refusing to overwrite virtual evidence: {path}")
            return
        path.write_text(encoded, encoding="utf-8")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


# Friendly aliases for callers that want to make the implementation choice
# explicit without learning the internal class name.
PurePythonSimulationAdapter = VirtualSimulationAdapter
VerifiedSimulationAdapter = VirtualSimulationAdapter


__all__ = [
    "PurePythonSimulationAdapter",
    "VerifiedSimulationAdapter",
    "VirtualScenario",
    "VirtualSimulationAdapter",
]

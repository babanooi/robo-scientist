# ArmPi bridge backend contract

The bridge runs on the robot computer. It exposes `GET /health`, `GET /state`,
and `POST /preflight`, `POST /execute_pick_place`, and `POST /stop` on port
`8060` by default. The upper application sends only JSON task plans to this
service; vendor SDK calls stay inside the injected robot-side backend.

Start safely (no real movement):

```sh
python3 -m roboscientist.hardware_bridge.server
```

Start real movement only after the workcell is safe and the backend has been
verified:

```sh
python3 -m roboscientist.hardware_bridge.server \
  --host 0.0.0.0 --port 8060 --allow-real-motion \
  --backend armpi_final_wrapper_backend:create_backend
```

`ARMPI_BRIDGE_BACKEND=armpi_final_wrapper_backend:create_backend` may supply the same backend
factory. The factory takes no arguments and returns an object with these five
methods. Each must return a JSON-compatible mapping:

```python
def create_backend():
    return backend

backend.health() -> {"available": True, "backend": "armpi", "hardware_status": "ready"}
backend.state() -> {"state": "idle"}
backend.preflight(request) -> {"approved": True, "checks": ["workspace", "collision"]}
backend.execute_pick_place(request) -> execution_result
backend.stop({"reason": "operator_request"}) -> {"stopped": True}
```

`preflight()` must reject unsafe requests with `{"approved": false, ...}`. The
bridge invokes it again immediately before every `execute_pick_place()` call.
The bridge permits only one preflight/execution operation at a time and returns
HTTP 409 with `ROBOT_BUSY` for a concurrent motion request. `stop()` is not
blocked by that lock and must remain callable during an active operation.
`stop()` must request the fastest safe stop supported by the robot runtime.
The received plan contains both `target_pose` and `destination_pose` in the
`base` frame. A backend must reject missing, stale, or out-of-workspace poses;
it must not fill in a placement point with an undocumented local constant.

## `execute_pick_place` result

The real backend must return this shape so `RealArmAdapter` can preserve the
physical evidence in the experiment record. `status` is one of `succeeded`,
`failed`, `rejected`, or `timed_out`.

```json
{
  "status": "succeeded",
  "hardware_status": "real_arm_ready",
  "actions": [
    {
      "action": "move_to_pregrasp",
      "status": "succeeded",
      "error_code": "NONE",
      "message": "completed",
      "duration_s": 1.24
    }
  ],
  "outcome": {
    "object_grasped": true,
    "object_lifted": true,
    "object_placed": true,
    "position_error_m": 0.008
  },
  "metrics": {"path_length_m": 0.42, "planning_time_ms": 180, "execution_time_s": 4.91},
  "artifacts": {"robot_log": "/path/to/log"}
}
```

`actions` must contain at least one action record. `error_code` uses the
project error code strings, such as `NONE`, `GRASP_FAILED`, `PATH_BLOCKED`,
`EXECUTION_FAILED`, `TIMEOUT`, or `STOPPED`. Failed, rejected, and timed-out
results must also include:

```json
{"failure": {"code": "GRASP_FAILED", "stage": "grasp", "message": "object released during lift"}}
```

Do not put servo IDs, pulse values, ROS topic names, or vendor-specific motion
commands in the request or response contract. They belong only inside the
robot-specific backend implementation.

The primary backend is `armpi_final_wrapper_backend.py`. It maps the verified
Ubuntu-side `~/armpi_tasks/run_final_dynamic_pick_place.sh check|pick-place`
wrapper to this contract. Configure at minimum:

```sh
export ARMPI_TASK_DIR=/home/ubuntu/armpi_tasks
export ARMPI_WRAPPER=/home/ubuntu/armpi_tasks/run_final_dynamic_pick_place.sh
export ARMPI_STOP_COMMAND='REPLACE WITH THE VERIFIED SOFTWARE STOP COMMAND'
```

The backend refuses real execution while `ARMPI_STOP_COMMAND` is missing. It
requires both `numeric_safety_passed` and `preflight_passed`, and preserves the
wrapper log. `pick_place_sequence_completed` is recorded only as action-chain
evidence. Set `ARMPI_RESULT_EVALUATOR=module:function` to an actual
camera/operator-state evaluator before a successful grasp can be recorded.

This frozen wrapper currently supports only `p0` with zero upper-layer grasp
offset. It rejects candidate versions because the wrapper does not yet accept
the candidate parameter values. Hardware must expose and verify a high-level
parameter interface before P1 can be called a physical feedback iteration.

The repository provides `armpi_parameterized_runner.py` as the candidate
interface. Set:

```sh
export ARMPI_PARAMETERIZED_WRAPPER=/home/ubuntu/armpi_tasks/run_roboscientist_experiment.sh
export ARMPI_EXPERIMENT_RUNNER=armpi_parameterized_runner:run_experiment
```

The callable contract is:

```python
run_experiment(mode, plan, context) -> {
    "returncode": 0,
    "output": "...wrapper statuses...",
    "executed_parameters": plan["skill"]["parameters"],
    "execution_profile": "parameterized_red_cuboid_runner_v1",
    "artifacts": {"skill_parameters": "...", "skill_parameters_sha256": "..."},
}
```

The reference runner writes an experiment-scoped parameter JSON and requires
the hardware wrapper to acknowledge its exact SHA256. Candidate preflight and
execution both fail closed when the acknowledgement or executed-parameter
evidence is missing. Candidates may change exactly one supported family from
the P0 defaults.

For result evaluation, the built-in file evaluator can consume a result from a
separate vision process:

```sh
export ARMPI_RESULT_EVALUATOR=roboscientist.hardware_bridge.result_file_evaluator:evaluate_result_file
export ARMPI_EVALUATION_TIMEOUT_S=60
```

See `configs/evaluation_result_template.json`. A `POSE_OFFSET` result must carry
signed `position_error_xyz_m`; a scalar error magnitude cannot determine a safe
correction direction.

`armpi_backend.py` remains a legacy alternative for installations that really
have the separately documented `~/my_armpi/move_to_pose.py`,
`robot_control.py`, and `home_control.py` function modules.

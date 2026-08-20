# RoboScientist S0/S1

This repository contains a runnable upper-layer feedback loop for the
fixed-workbench color-block pick-and-place task. `mock` is a development
fallback; the real-arm path uses a safety-gated HTTP bridge that runs beside the
verified ROS2/vendor code on the robot computer.

## Run

The bundled offline runtime includes Pydantic 2.13.4. Set it once in the shell:

```sh
export PYTHON=/Users/dxm/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
```

Run a reproducible scenario:

```sh
$PYTHON -m roboscientist.cli --scenario pose_offset
$PYTHON -m roboscientist.cli --mode simulation
```

Start the local API and interactive Mock page:

```sh
$PYTHON -m roboscientist.web.server --port 8001
```

Open `http://127.0.0.1:8001`. Select `simulation`, `mock`, or `real_arm` in the
page. The page and API always show the returned `data_source` and runtime status.
`simulation` currently reports `simulation_runtime_unverified` until Gazebo and
MoveIt2 are verified in the official virtual-machine environment.

The minimal API is:

- `POST /api/tasks` with `{"task_text": "把红色方块放到目标区域", "mode": "simulation", "scenario": "pose_offset"}`
- `POST /api/campaigns` with the same payload and `"max_rounds": 2` for one bounded P0 -> candidate P1 loop
- `GET /api/experiments/<experiment_id>`
- `POST /api/experiments/<experiment_id>/iterate` with `{"scenario": "success"}`
- `GET /api/skills`

Run all tests:

```sh
$PYTHON -m unittest discover -s tests -v
```

Each command writes a new, never-overwritten experiment package under
`data/experiments/<experiment_id>/` with `plan.json`, `result.json`, and, when
there is a failure, `analysis.json`.
Candidate Skills are separately written to `data/skills/`.

See [仿真适配器接入说明](docs/仿真适配器接入说明.md) for the verified/unverified
boundary and the required Gazebo/MoveIt2 handoff.

## Hardware boundary

`RealArmAdapter` is available only with an approved local profile, an enabled
robot-side bridge, and the `ROBO_ALLOW_REAL_ARM=1` process gate. The primary
robot backend is `armpi_final_wrapper_backend.py`; it calls the verified
`~/armpi_tasks/run_final_dynamic_pick_place.sh` red-cuboid baseline on the
Ubuntu robot environment. The older `armpi_backend.py` remains available only
for installations that still use the separately documented `~/my_armpi`
function modules.

The final-wrapper backend parses wrapper safety statuses and joint-state CSV
evidence, but it does not equate `pick_place_sequence_completed` with physical
success. `ARMPI_RESULT_EVALUATOR` is required to verify grasp, lift, transport,
and placement. Candidate Skill execution uses the optional
`armpi_parameterized_runner.py`; it requires a hardware wrapper to acknowledge
the exact experiment parameter SHA256 before P1 can run.
The business and core modules never import vendor SDKs or encode hardware
topics, servo IDs, pulses, or motion durations. See
[实机闭环部署与测试](docs/实机闭环部署与测试.md).

The complete robot-side pull, configuration, and P0/P1 acceptance procedure is
in [硬件组 GitHub 拉取与实机闭环验收](docs/2026-08-20_硬件组GitHub拉取与实机闭环验收.md).

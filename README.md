# RoboScientist S0/S1

This repository contains the first runnable software loop for the fixed-workbench
color-block pick-and-place task. The primary future demo is `simulation`
(Gazebo + MoveIt2); `mock` remains a local development fallback. No code sends
ROS2 commands in this environment.

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

`ArmPiAdapterStub` is intentionally non-operational until hardware A/B provide
verified high-level motion, vision, calibration, safety, and stop semantics.
The business and core modules know only the `DeviceAdapter` protocol. They do
not import vendor SDKs or encode hardware topics, servo IDs, pulses, or motion
durations.

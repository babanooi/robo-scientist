# RoboScientist · 软件虚拟闭环

This repository contains a runnable upper-layer feedback loop for the
fixed-workbench color-block pick-and-place task. The current public route is a
pure Python virtual workcell and does not require a physical robot or on-site
operation.
`mock` is a development fallback; the real-arm path remains an explicitly
gated extension for a later hardware handoff.

The current route decision and claim boundary are documented in
[当前路线决议：纯软件虚拟实验](docs/当前路线决议_纯软件虚拟实验.md).

## Acceptance modes and current boundary

There are two campaign planning modes (the execution mode is selected
separately):

- `use_qwen=false` runs the reproducible, offline candidate package used by the
  current software-only route. It demonstrates the virtual experiment,
  structured failure analysis, single-family Skill adjustment, and same-condition
  P0/P1 validation.
- `use_qwen=true` adds two live structured Qwen calls (scientific planning and
  feedback interpretation). It requires a valid local credential and network
  access; a missing key, expired key, or invalid response returns a structured
  error and never silently falls back to deterministic planning.

The HTTP campaign API keeps `use_qwen=true` as its safety-conscious default so a
caller must choose the offline mode explicitly. The browser preview starts with
the Qwen checkbox unchecked so the no-network virtual demo is immediately
reproducible; the user can opt in after verifying the credential. Mock and
unverified simulation results are never real-arm validation.

## Run

From a fresh checkout, create an environment and install the small runtime
dependency set. The repository does not commit `.venv`:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export PYTHON=python
```

Run a reproducible scenario:

```sh
$PYTHON -m roboscientist.cli --scenario pose_offset
$PYTHON -m roboscientist.cli --mode simulation
```

Start the local API and interactive virtual-workcell page:

```sh
$PYTHON -m roboscientist.web.server --port 8001
```

Open `http://127.0.0.1:8001`. For the current no-physical-robot route, select
`simulation` (the default) and leave `real_arm` unused. The page and API always
show the returned `data_source` and runtime status.
The public Web `simulation` mode uses the self-contained deterministic
`roboscientist.virtual_workcell` and reports `simulation_runtime_verified`.
This is a software virtual workcell, not Gazebo/MoveIt2 or a physical robot;
the legacy `SimulationAdapter` remains intentionally `simulation_runtime_unverified`.

Generate a clean, self-contained virtual submission candidate (campaign,
10+10 validation, manifests, checksums, test log and limitations):

```sh
$PYTHON scripts/generate_virtual_submission.py
```

The generator never copies `.env`, API keys, or hardware archives.

To show a historical ArmPi run in the action panel, start the same server with
an extracted run directory, a directory containing multiple runs, or a
`.tar`/`.tar.gz`/`.tgz` archive:

```sh
$PYTHON -m roboscientist.web.server \
  --port 8001 \
  --replay-source /absolute/path/to/grasp_run_20260513_123541.tar.gz
```

To show the read-only hardware baseline and validate a hardware delivery
package, provide its path when starting the server.  The path is fixed at
startup; the browser cannot submit an arbitrary local path to the validator:

```sh
$PYTHON -m roboscientist.web.server \
  --port 8001 \
  --hardware-evidence-source /absolute/path/to/hardware_evidence.tar.gz
```

The corresponding inspection endpoints are:

```sh
curl http://127.0.0.1:8001/api/hardware/baselines
curl http://127.0.0.1:8001/api/hardware/baselines/fixed_a_to_b_p0
curl http://127.0.0.1:8001/api/hardware/evidence
```

`/api/hardware/baselines` exposes the two documented reference snapshots
(`fixed_a_to_b_p0` and `multishape_factory`).  They are read-only and
`current_hardware_verified=false`; loading a parameter snapshot never enables
the real-arm adapter.  `/api/hardware/evidence` validates a directory, zip, or
tar archive and returns `passed`, `incomplete`, or `invalid`.  A source/code
archive or a manual that only says “success” is expected to be `incomplete`
until the same run's manifest, evaluation JSON, stage images, bridge/wrapper
log, and checksum evidence are present and internally consistent.

The replay source is read when the server handles a request; the archive is
read in memory and is never extracted or modified. After the server starts,
the available run IDs can be listed and one run can be inspected with:

```sh
curl http://127.0.0.1:8001/api/replay
curl http://127.0.0.1:8001/api/replay/<run_id>
```

`GET /api/replay` returns run metadata (`runs`, `run_count`), while
`GET /api/replay/<run_id>` returns the bounded joint trajectory, parsed vision
targets/events, evidence-file information, and limitations. Every replay
response includes `data_source: "historical_real_arm"`, `read_only: true`, and
`motion_requested: false`.

Replay is an evidence viewer, not a robot connection: it does not select an
execution adapter, call the hardware bridge, send a motion or stop command, or
enable `ROBO_ALLOW_REAL_ARM`. A recorded trajectory is not a live stream or a
TCP-optimal-path claim. If the source has no validated outcome labels, the API
must report that success/failure and success-rate claims are unavailable; the
replay page must not infer them from the trajectory alone.

The campaign API contract is:

- `GET /api/config` for the non-secret runtime and feature configuration
- `POST /api/campaigns` with `{"task_text": "把红色方块放到目标区域", "scenario": "pose_offset", "mode": "simulation", "use_qwen": false, "auto_run_p1": true}` for the reproducible no-network route
- `GET /api/campaigns/<campaign_id>` for the append-only campaign and Qwen evidence
- `GET /api/experiments/<experiment_id>` for an individual P0 or P1 package
- `POST /api/stop` for the optional future real-arm safety contract; body may be `{}` and defaults to `mode=real_arm` (other modes are rejected; it is not the Simulation stop control)

For the current software-only route, the `simulation`/`use_qwen=false` example
above is the minimal reproducible call. The API fields are also kept stable for
a future hardware handoff; a future real campaign would require a pinned commit,
bridge preflight and human safety monitor. `auto_run_p1` never disables a
physical stop procedure.

The interactive page keeps a visible `停止 / 接管` control. A failed stop response is
an immediate instruction to use the robot's physical emergency stop; it never resumes
the experiment automatically.

For low-level/local checks, the following routes remain available:

- `POST /api/tasks` with the same task fields for one P0
- `GET /api/experiments/<experiment_id>`
- `POST /api/experiments/<experiment_id>/iterate` for an explicitly supervised candidate run
- `GET /api/skills`

### Qwen / Bailian configuration

Set these in the process environment on the machine that runs the upper
application. For local development, the ignored project-root `.env` file may
contain the same four fields; process environment values take precedence. Never
put the API key in Git, JSON evidence, screenshots, or any `.env` file that is
uploaded with the experiment package:

Use [`.env.example`](.env.example) as the redacted local template; do not commit
the populated `.env`.

```sh
export DASHSCOPE_API_KEY='REDACTED_AT_HANDOFF'
export DASHSCOPE_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1'
export QWEN_MODEL='qwen3.7-plus'
export QWEN_TIMEOUT_S='90'
```

The account must be checked for access to the selected model before a real
campaign. `/api/config` may report whether Qwen is configured and the selected
model, but must never return the key. Save the model, endpoint, request ID,
prompt/request/response SHA256 values, schema validation result, and structured
decision in the campaign evidence.

Run all tests:

```sh
$PYTHON -m unittest discover -s tests -v
```

Each command writes a new, never-overwritten experiment package under
`data/experiments/<experiment_id>/` with `plan.json`, `result.json`, and, when
there is a failure, `analysis.json`.
Candidate Skills are separately written to `data/skills/`.

See [API 文档](docs/API.md)、[虚拟工作台运行说明](docs/虚拟工作台运行说明.md)
and [仿真适配器接入说明](docs/仿真适配器接入说明.md) for the API, software
virtual runtime and legacy Gazebo/MoveIt2 boundary.

## Optional hardware extension (not required for the current route)

The following sections document a later ArmPi integration only. They are not
needed to run the software virtual workcell, and no Raspberry Pi, ROS, vendor
SDK or physical-arm operation is required for the current submission candidate.

### Hardware preflight and campaign launch

On the ArmPi host, first create an append-only, no-motion capability bundle:

```sh
python3 scripts/collect_armpi_capabilities.py --strict
```

Return the printed `.tar.gz`, `capability_report.json`, and terminal output to
the software team. A non-zero `--strict` result is a review stop, not permission
to guess a wrapper interface. Then run the bridge health and no-motion preflight
described in [硬件组 GitHub 拉取与实机闭环验收](docs/2026-08-20_硬件组GitHub拉取与实机闭环验收.md).

The real campaign is eligible for an autonomous P1 only when all of the
following are true: `mode=real_arm`; P0 and P1 use the same scene, target,
destination, calibration, evaluator version, and acceptance conditions; both
rounds are evaluated by `evaluator_type=vision`, include a pinned evaluator
version, an experiment-scoped evaluation file, and image/video evidence; P1 changes exactly one allowed
parameter family; and the parameterized wrapper acknowledges the identical
canonical `skill_parameters.json` SHA256 during both `check` and `pick-place`.
`operator` or `hybrid` evidence may support supervised hardware debugging, but
must produce `manual_review_required` rather than an autonomous P1.

The first P1 is only a candidate observation. Do not claim a success-rate
improvement or Skill promotion until the frozen same-condition repeat protocol
(project recommendation: at least 10 independent P0 and 10 P1 runs) is complete.

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

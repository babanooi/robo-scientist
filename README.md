# RoboScientist S0/S1

This repository contains a runnable upper-layer feedback loop for the
fixed-workbench color-block pick-and-place task. `mock` is a development
fallback; the real-arm path uses a safety-gated HTTP bridge that runs beside the
verified ROS2/vendor code on the robot computer.

## Acceptance modes and current boundary

The official demonstration path is a bounded scientific campaign with a real
Qwen call, deterministic safety checks, a vision result, and a same-condition
P0/P1 comparison. `use_qwen=true` is the default for the campaign API. If the
Qwen key is missing or the structured response is invalid, the campaign must
return a structured error; it must not silently become a deterministic campaign.

`use_qwen=false` is an explicit `deterministic_only` mode for local contract,
hardware, and safety checks. It can demonstrate the software pipeline, but it
does not provide the Qwen evidence required by the competition submission.
Mock and unverified simulation results are never real-arm validation.

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

The campaign API contract is:

- `GET /api/config` for the non-secret runtime and feature configuration
- `POST /api/campaigns` with `{"task_text": "把红色方块放到目标区域", "scenario": "pose_offset", "mode": "real_arm", "use_qwen": true, "auto_run_p1": true}`
- `GET /api/campaigns/<campaign_id>` for the append-only campaign and Qwen evidence
- `GET /api/experiments/<experiment_id>` for an individual P0 or P1 package

The API fields above are frozen for the hardware handoff, but are not accepted
as verified until the target commit passes the server smoke test. Hardware must
record the exact commit and the complete JSON response from `/api/config` before
running a real campaign. A real campaign still requires a human safety monitor;
`auto_run_p1` never disables the physical stop procedure.

For low-level/local checks, the following routes remain available:

- `POST /api/tasks` with the same task fields for one P0
- `GET /api/experiments/<experiment_id>`
- `POST /api/experiments/<experiment_id>/iterate` for an explicitly supervised candidate run
- `GET /api/skills`

### Qwen / Bailian configuration

Set these only in the process environment on the machine that runs the upper
application. Never put the API key in Git, JSON evidence, screenshots, or a
`.env` file that is uploaded with the experiment package:

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

See [仿真适配器接入说明](docs/仿真适配器接入说明.md) for the verified/unverified
boundary and the required Gazebo/MoveIt2 handoff.

## Hardware preflight and campaign launch

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
rounds are evaluated by `evaluator_type=vision`; P1 changes exactly one allowed
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

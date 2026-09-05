# RoboScientist 本地 Web API

版本：`v1`
适用实现：`roboscientist.web.server`
当前演示路线：纯 Python 确定性虚拟工作单元，不连接 ArmPi，不启动 Gazebo/MoveIt2。

## 1. API 边界

服务是一个本地、无认证的 HTTP JSON API，默认监听 `127.0.0.1:8001`。前端和
API 共用同一个进程，正常情况下不需要 CORS、反向代理或机器人端口。

公共 `mode=simulation` 使用 `VirtualSimulationAdapter`，每轮结果应明确包含：

```text
data_source=simulation
hardware_status=simulation_runtime_verified
simulation.runtime_name=roboscientist.virtual_workcell
simulation.runtime_version=1.0.0
simulation.engine=python_deterministic
simulation.synthetic=true
simulation.physical_robot_connected=false
```

这表示“软件虚拟工作单元已执行”，不是物理动力学仿真、Gazebo/MoveIt2 或实机
证据。`mode=mock` 只验证规划、归因和版本演进；`mode=real_arm` 是保留的安全
边界接口，在当前无实机路线中不应使用，缺少批准的 profile 时会被桩适配器拒绝。

API 不提供 WebSocket 实时视频或机械臂控制流。`replay` 是只读历史数据查看器，
不会发送运动命令。

## 2. 启动

在仓库根目录执行（首次运行先创建本地虚拟环境）：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
.venv/bin/python -m roboscientist.web.server \
  --host 127.0.0.1 \
  --port 8001 \
  --data-root data/preview
```

浏览器入口：<http://127.0.0.1:8001/>。

`--data-root` 是服务写入实验证据的根目录；目录会按实验 ID 追加写入，不覆盖
已有记录。若需要只读查看历史硬件包，可以额外传入
`--replay-source /path/to/run-or-archive`；这不会把历史记录变成在线设备。

## 3. 路由总览

| 方法 | 路径 | 作用 | 当前路线是否会产生运动 |
|---|---|---|---|
| GET | `/api/health` | 服务和物理设备边界探针 | 否 |
| GET | `/api/config` | 非敏感配置、运行时和 Qwen 状态 | 否 |
| GET | `/api/runtime?mode=simulation` | 查询所选适配器状态 | 否 |
| GET | `/api/skills` | 已归档 Skill 版本 | 否 |
| GET | `/api/experiments/{experiment_id}` | 读取一轮完整实验包 | 否 |
| GET | `/api/campaigns/{campaign_id}` | 读取 P0/P1 活动及证据 | 否 |
| GET | `/api/campaigns/{campaign_id}/validation` | 读取重复验证结果 | 否 |
| GET | `/api/replay` | 列出只读回放运行 | 否 |
| GET | `/api/replay/{run_id}` | 读取一条只读回放 | 否 |
| GET | `/api/hardware/baselines` | 读取文档化硬件基线快照 | 否 |
| GET | `/api/hardware/baselines/{baseline_id}` | 读取一个硬件基线 | 否 |
| GET | `/api/hardware/evidence` | 校验启动时指定的硬件证据包 | 否 |
| POST | `/api/tasks` | 执行一轮 P0 | 虚拟模式只改变本地状态 |
| POST | `/api/campaigns` | 执行有界 P0→反馈→P1 活动 | 虚拟模式只改变本地状态 |
| POST | `/api/validation` | 执行 P0/P1 重复验证 | 虚拟模式只改变本地状态 |
| POST | `/api/experiments/{experiment_id}/iterate` | 对已有失败 P0 运行候选 Skill | 虚拟模式只改变本地状态 |
| POST | `/api/stop` | 保留的实机停止合同 | 当前只接受 `mode=real_arm` |

所有请求和响应均为 UTF-8 JSON。成功状态通常为 `200`（查询/停止）或 `201`
（创建实验/活动）；参数错误为 `400`，不存在资源为 `404`，Qwen 阶段失败为
`502`，未预期服务错误为 `500`。

## 4. 运行时探针

### `GET /api/health`

示例：

```sh
curl -fsS http://127.0.0.1:8001/api/health
```

典型响应：

```json
{
  "status": "ok",
  "service": "roboscientist",
  "api_version": "v1",
  "virtual_runtime": "ready",
  "physical_robot": "disabled",
  "claims": "software_and_virtual_experiment_only"
}
```

### `GET /api/config`

返回 `supported_modes`、重复实验内部门槛、Qwen 是否配置以及虚拟运行时信息。
API Key 永远不会出现在响应中。关键字段如下：

```json
{
  "qwen": {
    "enabled": false,
    "configured": false,
    "provider": "aliyun_model_studio",
    "model": null,
    "error": null
  },
  "project_policy": {
    "minimum_runs_per_version": 10,
    "minimum_runs_is_internal_policy": true
  },
  "supported_modes": ["mock", "simulation", "real_arm"],
  "virtual_runtime": {
    "enabled": true,
    "runtime_name": "roboscientist.virtual_workcell",
    "runtime_version": "1.0.0",
    "engine": "python_deterministic",
    "data_source": "simulation",
    "hardware_status": "simulation_runtime_verified",
    "physical_robot_connected": false,
    "gazebo_moveit2": "not_used"
  }
}
```

### `GET /api/runtime?mode=simulation`

这是无动作状态查询。生产演示应显式传 `mode=simulation`；省略参数时服务端
默认查询 `mock`，不要把该默认值解释为虚拟工作台状态。

典型虚拟响应字段：`adapter=simulation`、`motion_state=idle`、`scene_id`、
`scene_version`、`joint_names`、`joint_positions_rad`、`synthetic=true` 和
`physical_robot_connected=false`。

## 5. 任务和活动

### 5.1 任务文本与场景

当前解析器支持颜色 `red/blue/yellow`（或中文“红/蓝/黄”）、物体
`cube/block/cuboid/方块`，并要求文本包含放置/目标语义。纯 Python 虚拟场景
只有一个红色方块和右侧目标区，因此非红色目标会在虚拟预检阶段返回
`TARGET_NOT_FOUND`。

虚拟场景值：

| `scenario` | 目的 | P0/P1 典型结果 |
|---|---|---|
| `pose_offset` | 注入固定 X 位姿残差；推荐主演示 | P0 `POSE_OFFSET`，P1 补偿后成功 |
| `path_risk` | 低中转高度触发障碍/安全风险 | P0 `PATH_BLOCKED`，P1 提高高度 |
| `path_blocked` | 同上，强调路径拒绝 | P0 拒绝，P1 候选可验证 |
| `grasp_failed` | 提起阶段夹持失败 | P0 `GRASP_FAILED`，P1 提高抓取 Z |
| `timeout` | 模拟超时 | 不生成自动候选，停止人工处理 |
| `collision` | 显式碰撞风险场景 | P0 `PATH_BLOCKED` 并记录碰撞事件 |
| `success` | 无扰动成功基线 | 通常不需要 P1 |

`mode=mock` 使用其中与 Mock 适配器对应的 `success`、`pose_offset`、
`grasp_failed`、`timeout` 子集；它不产生虚拟 TCP/障碍物证据。

### 5.2 `POST /api/tasks`：单轮 P0

请求：

```sh
curl -fsS -X POST http://127.0.0.1:8001/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "task_text": "把红色方块放到右侧目标区域",
    "scenario": "pose_offset",
    "mode": "simulation"
  }'
```

字段：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `task_text` | string | 必填 | 自然语言任务 |
| `scenario` | string | `success` | 见场景表 |
| `mode` | string | `simulation` | `mock`/`simulation`/`real_arm` |

响应是一个实验记录对象：

```json
{
  "plan": {"experiment_id": "exp-...", "task": {}, "skill": {}, "scene_id": "simulation-virtual-workcell-v0", "adapter": "simulation", "expected_data_source": "simulation"},
  "result": {
    "experiment_id": "exp-...",
    "data_source": "simulation",
    "hardware_status": "simulation_runtime_verified",
    "skill_version": "p0",
    "status": "failed",
    "safety_check": {"allowed": true, "errors": [], "messages": []},
    "outcome": {"object_grasped": true, "object_lifted": false, "object_placed": false, "position_error_m": 0.02},
    "failure": {"code": "POSE_OFFSET", "stage": "grasp", "message": "..."},
    "metrics": {"position_error_m": 0.02, "path_length_m": 0.562418, "collision_detected": 0.0},
    "simulation": {"runtime_name": "roboscientist.virtual_workcell", "engine": "python_deterministic", "synthetic": true, "physical_robot_connected": false, "tcp_trajectory": [], "evaluation": {}}
  },
  "analysis": {},
  "candidate_skill": {},
  "execution_mode": "simulation",
  "mode_notice": "纯 Python 软件虚拟工作单元；不等同于 Gazebo/MoveIt2 或真实机械臂。"
}
```

上例省略了完整轨迹和动作数组；实际响应中的 `result.simulation.joint_trajectory`、
`tcp_trajectory`、`scene_objects`、`evaluation` 和 `replay` 是可审计数据。

### 5.3 `POST /api/campaigns`：P0→反馈→P1

请求：

```sh
curl -fsS -X POST http://127.0.0.1:8001/api/campaigns \
  -H 'Content-Type: application/json' \
  -d '{
    "task_text": "把红色方块放到右侧目标区域",
    "scenario": "pose_offset",
    "mode": "simulation",
    "max_rounds": 2,
    "use_qwen": false,
    "auto_run_p1": true
  }'
```

字段：

| 字段 | 类型/范围 | 默认 | 说明 |
|---|---|---|---|
| `task_text` | string | 必填 | 同 `/api/tasks` |
| `scenario` | string | `success` | P0/P1 必须继承同一场景 |
| `mode` | enum | `simulation` | 当前推荐 `simulation` |
| `max_rounds` | `1` 或 `2` | `2` | 有界活动，不支持无限迭代 |
| `use_qwen` | boolean | `true` | `false` 必须明确表示 deterministic-only |
| `auto_run_p1` | boolean | `true` | 是否自动执行候选 P1 |

响应重点字段：

```json
{
  "campaign_id": "campaign-...",
  "status": "completed",
  "classification": "deterministic_only",
  "planning_mode": "deterministic_only",
  "use_qwen": false,
  "records": [{"plan": {}, "result": {}, "analysis": null, "candidate_skill": null}, {"plan": {}, "result": {}, "analysis": null, "candidate_skill": null}],
  "scientific_plan": {},
  "feedback_adjustment": {
    "strategy": "signed_residual_compensation",
    "recommended_parameter_family": "grasp_offset"
  },
  "decision": {"status": "p1_executed", "strategy_matches": true},
  "same_condition": {"verified": true, "differences": []},
  "rounds_completed": 2,
  "promotion": "candidate_only"
}
```

`candidate_only` 是有意保守的版本状态：一次 P1 通过不能直接声称 Skill 已晋升，
应使用 `/api/validation` 完成重复对照。

### 5.4 `GET /api/campaigns/{campaign_id}`

返回已保存的活动，并附加 `qwen_evidence`（若存在）、`validation`（若已运行）
以及每轮完整实验对象。活动文件位于：

```text
<data-root>/campaigns/<campaign_id>/campaign.json
<data-root>/campaigns/<campaign_id>/validation.json   # 可选
<data-root>/campaigns/<campaign_id>/qwen/             # 可选、已脱敏
```

### 5.5 `POST /api/experiments/{experiment_id}/iterate`

请求体可为空对象 `{}`。它读取已有 P0 的候选 Skill、任务、模式和场景，不能通过
请求改换条件：

```sh
curl -fsS -X POST \
  http://127.0.0.1:8001/api/experiments/exp-xxxxxxxxxxxx/iterate \
  -H 'Content-Type: application/json' -d '{}'
```

如果 P0 没有可归因失败、超时或安全证据不足，可能返回无候选或错误；服务不会
自动尝试多个参数族。

## 6. 重复验证

### `POST /api/validation`

该接口先生成一条有界 P0→P1 活动，再在冻结适配器、任务和条件下各运行
`repeats` 次。`repeats` 必须是 `1..100` 的整数；`mode=real_arm` 在当前构建中
明确禁用。默认值是 `scenario=pose_offset`、`mode=simulation`、`repeats=10`、
`use_qwen=false`。

```sh
curl -fsS -X POST http://127.0.0.1:8001/api/validation \
  -H 'Content-Type: application/json' \
  -d '{
    "task_text": "把红色方块放到右侧目标区域",
    "scenario": "pose_offset",
    "mode": "simulation",
    "repeats": 10,
    "use_qwen": false
  }'
```

响应包含 `validation`：

```json
{
  "format_version": "validation-v1",
  "repeats_per_version": 10,
  "same_adapter": "simulation",
  "data_source": "simulation",
  "records": {"p0": [{"plan": {}, "result": {}}], "p1": [{"plan": {}, "result": {}}]},
  "summary": {
    "p0": {"sample_count": 10, "success_count": 0, "task_success_rate": 0.0, "position_error_mean_m": 0.02, "collision_rate": 0.0},
    "p1": {"sample_count": 10, "success_count": 10, "task_success_rate": 1.0, "position_error_mean_m": 0.0, "collision_rate": 0.0}
  },
  "comparison": {
    "decision": "promote_candidate_for_review",
    "minimum_samples_per_version": 10,
    "samples_sufficient": true,
    "improvement_supported": true,
    "deltas": {"task_success_rate": 1.0, "position_error_mean_m": -0.02}
  }
}
```

决策含义：

- `promote_candidate_for_review`：样本数足够且主要指标改善，仍需人工复核；
- `candidate_only_insufficient_samples`：每版本样本少于 10；
- `reject_candidate`：没有满足安全/改善约束。

通过 `GET /api/campaigns/{campaign_id}/validation` 可单独读取保存的
`validation.json`。

## 7. 实验结果字段

每一轮 `result` 对应 `ExperimentResult`，稳定字段如下：

| 字段 | 含义 |
|---|---|
| `experiment_id` / `task_id` | 实验和任务唯一 ID |
| `adapter` / `data_source` | 适配器名和数据来源 |
| `hardware_status` | 运行时状态，不等同于真实硬件成功 |
| `skill_version` | 本轮使用的 Skill 版本 |
| `scene_id` | 场景版本标识 |
| `status` | `succeeded`/`failed`/`rejected`/`timed_out` |
| `safety_check` | 是否通过确定性安全门及错误列表 |
| `actions` | 预检、接近、夹爪、提起、转运、放置等阶段事件 |
| `outcome` | `object_grasped/lifted/placed` 与位置误差 |
| `failure` | 失败码、阶段和消息；成功时为 `null` |
| `metrics` | 执行时间、路径长度、规划耗时、安全事件、碰撞等数值 |
| `artifacts` | 本地证据文件路径或虚拟 URI |
| `failure_analysis` | 失败证据和推荐参数族 |
| `candidate_skill_version` | 可归因失败生成的单参数候选；可能为 `null` |
| `simulation` | 虚拟/仿真专属轨迹、场景、评价和来源字段 |
| `replay` | 前端只读轨迹回放载荷 |

`simulation.evaluation` 的 `evaluator_type` 当前为
`virtual_deterministic`，`evaluator_version` 当前为 `virtual-evaluator-v1`。
评价由程序生成，Qwen 不能直接改写成功判定。

## 8. 只读数据接口

### Skill

`GET /api/skills` 返回 `{"skills": [...]}`。候选版本会记录
`parent_version`、`changed_parameter_family`、`change_reason` 和
`source_experiment_id`，用于审计“只改一个参数族”。

### 回放

`GET /api/replay` 返回 `runs` 和 `run_count`；详情返回轨迹、事件、指标和限制
字段，并固定标记：

```text
data_source=historical_real_arm
read_only=true
motion_requested=false
```

这是历史资料查看，不是实时视频、在线设备状态或路径最优性证明。

### 硬件资料

`/api/hardware/baselines` 和 `/api/hardware/evidence` 只读取文档化快照/启动时
指定的证据包，响应包含 `read_only=true`、`motion_requested=false`。基线对象还
包含 `current_hardware_verified=false`（当前无实机路线）；证据接口则返回校验器的
`status`、`missing` 和 `warnings`，不把资料包当作当前硬件状态。浏览器不能提交
任意路径给证据校验器。

## 9. 停止接口边界

`POST /api/stop` 为未来实机桥接保留的合同，只接受 `mode=real_arm`；空请求体
允许省略字段：

```sh
curl -fsS -X POST http://127.0.0.1:8001/api/stop -d '{}'
```

当前纯软件路线没有正在运行的物理设备，因此不要把它当作虚拟实验停止按钮，
也不要据此宣称存在实机急停实现。任何真实设备操作必须由经过审计的独立安全
链路负责。

## 10. Qwen 可选配置

Qwen 只负责科学计划和反馈策略的结构化文本，不产生舵机、ROS 或绕过安全门的
命令。配置在启动服务的进程环境中：

```sh
export DASHSCOPE_API_KEY='仅在本机进程环境中设置'
export DASHSCOPE_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1'
export QWEN_MODEL='qwen3.7-plus'
export QWEN_TIMEOUT_S='90'
```

`use_qwen=true` 时，缺少 key、网络失败或 Schema 校验失败会返回 `502` 的结构化
错误，并在活动目录保存脱敏失败记录；不会悄悄退回确定性模式。离线可复验演示
应显式使用 `use_qwen=false`，活动中的 `planning_mode` 会标为
`deterministic_only`。不要把 `.env`、API key 或完整授权头复制到提交包。

## 11. 错误格式

参数/资源错误示例：

```json
{"error": {"message": "task_text is required"}}
```

Qwen 阶段错误示例：

```json
{
  "error": {
    "code": "QWEN_PLANNING_FAILED",
    "stage": "planning",
    "message": "Qwen planning failed",
    "campaign_id": "campaign-..."
  }
}
```

未预期错误只返回通用 `INTERNAL_SERVER_ERROR` 和 `request_id`，不会回显异常文本
或密钥。

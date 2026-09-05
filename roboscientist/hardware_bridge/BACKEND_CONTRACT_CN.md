# ArmPi Ultra Bridge 中文接口合同（硬件交付验收版）

版本：v1.1
日期：2026-09-04
适用对象：ArmPi Ultra 机器人主机、RoboScientist 上层应用

## 1. 合同目的

本合同定义“上层电脑如何安全地请求机械臂实验”。它只规定高层实验计划、健康状态、预检、执行和停止，不规定厂商 ROS2 Topic、Service、舵机 ID、脉宽或内部脚本实现。

硬件侧负责把以下内部链路封装在机器人主机上：

```text
RoboScientist 上层应用
        │ 仅发送高层 JSON 计划
        ▼
ArmPi HTTP Bridge
        │
        ├─ ROS2 / 厂商 SDK
        ├─ 视觉定位与标定
        ├─ P0/P1 wrapper
        └─ 评价器与实验归档
```

当前合同采用**同步执行**：`POST /execute_pick_place` 在 wrapper 和结果评价完成后返回结果。项目上层已经有实验归档接口，因此 `/result/{experiment_id}` 不是当前必需接口；如以后改成异步任务，必须另行版本化，不能悄悄替换本合同。

### 1.0 接口总览

| 接口 | 是否运动 | 硬件侧职责 | 上层用途 |
|---|---:|---|---|
| `GET /health` | 否 | live probe bridge/ROS/相机/标定/Stop/profile | 显示连接和安全状态 |
| `GET /state` | 否 | 返回当前 experiment 与状态能力 | 前端轮询动作状态 |
| `POST /preflight` | 否 | 校验本机安全档案、定位、IK 和参数白名单 | 执行前安全门 |
| `POST /execute_pick_place` | 是 | 执行 wrapper、评价结果、归档工件 | 发起一次 P0/P1 实验 |
| `POST /stop` | 是（停止） | 立即停止并使授权失效 | 人工/上层紧急停止 |

### 1.1 当前实现状态

本文件是硬件侧必须实现并通过验收的合同，不代表仓库中的参考 backend 已经全部满足。当前 `roboscientist.hardware_bridge.server` 已具备五个 HTTP 路由、统一响应封装、执行前二次 preflight 和互斥运动锁；但 `armpi_final_wrapper_backend.py` 仍未完整实现以下合同项：

| 待补齐项 | 当前风险 | 验收要求 |
|---|---|---|
| 本机只读安全档案 | 目前主要使用请求里的约束，客户端可能误放宽边界 | bridge 加载本机 profile；客户端只能收紧，不能放宽 |
| 实时健康探测 | 当前主要检查文件/模块存在，不能证明 ROS、相机和控制器在线 | `/health` 分项 live probe，并带探测时间与状态 |
| 实验 ID 强制校验 | HTTP 层尚未统一拒绝空 ID | preflight/execute 必须校验并回显；stop 允许空 ID 作为全局安全停止 |
| 运动状态和 Stop 失效 | 当前状态近似固定 `idle`，Stop 后旧批准可能仍存在 | 跟踪 active experiment；Stop 后失效批准并禁止续跑 |
| 输入字段白名单 | 当前只要求 JSON object | 拒绝未知低层控制字段、命令、Topic 和任意路径输入 |

因此，在上述项、动作中 Stop 和实机评价器验收完成前，`real_motion` 仍视为 **blocked**；只能进行 dry-run、无动作联调和历史数据回放。

## 2. 网络和部署边界

### 2.1 两台电脑的地址规则

- bridge 运行在 ArmPi 主机；
- 上层应用运行在开发电脑或比赛电脑；
- `127.0.0.1` 只表示“上层和 bridge 在同一台电脑”；
- 两台电脑时，上层 `bridge_url` 必须使用 ArmPi 的局域网 IP，或使用受控 SSH 隧道地址；
- 如监听 `0.0.0.0:8060`，必须限制在比赛内网并配置防火墙，禁止暴露到公网；
- 当前标准库 bridge 没有内置 TLS 或 Token 认证，不得把它直接暴露到互联网；
- 交付包必须记录实际 bind 地址、advertised URL、端口和测试电脑 IP 段。

默认端口：`8060`。

### 2.2 启动模式

安全 dry-run（不驱动真实机械臂）：

```sh
python3 -m roboscientist.hardware_bridge.server
```

真实运动模式只能在现场安全确认后启动：

```sh
python3 -m roboscientist.hardware_bridge.server \
  --host 0.0.0.0 \
  --port 8060 \
  --allow-real-motion \
  --backend armpi_final_wrapper_backend:create_backend
```

`--allow-real-motion` 必须同时有明确的 backend；只设置环境变量或只启动 rosbridge 不构成真实运动授权。

## 3. 安全放行条件

真实 P0 执行必须同时满足：

1. 工作区已清场，有现场安全监护人和物理急停；
2. 上层 profile：`motion_enabled=true`；
3. 上层安全约束：`allow_real_robot=true`；
4. 上层进程：`ROBO_ALLOW_REAL_ARM=1`；
5. bridge：`--allow-real-motion` 且加载正确 backend；
6. `/health` 报告 bridge、wrapper、相机、标定和 Stop 状态可用；
7. `/preflight` 返回 `approved=true`；
8. bridge 在执行前再次运行 preflight；
9. P1 另需参数化 runner、视觉评价器和同条件检查全部通过。

Qwen 或其他模型不得直接输出舵机命令、ROS 动作、shell 命令或绕过安全条件的字段。

### 3.1 本机安全档案优先级

- bridge 必须在 ArmPi 主机上加载版本化、只读或受控写入的 `safety_profile.json`；
- 本机档案是工作空间、置信度、速度、超时、偏移、A/B 点和参数族的最终上限；
- 请求中的 `safety_constraints` 只允许比本机档案更严格。例如请求可以降低最大速度，但不能提高；可以缩小工作空间，但不能扩大；
- 请求与本机档案冲突时必须 fail closed，并返回差异原因；
- `/health`、`/preflight` 和执行结果都必须回显 `safety_profile_version`、`safety_profile_sha256`；上层应用自身配置如需回显，应使用不同字段 `application_profile_version`，不得混用；
- B 点必须只有一个现场确认的真源。`destination_pose`、本机 profile 和 wrapper 环境变量必须在容差内一致；参考 wrapper 的默认值 `(0.220, -0.080, 0.030) m` 不能代替现场实测和签字确认。

### 3.2 单位、坐标系和时间

- 位姿、偏移、路径长度和位置误差统一使用米 `m`；速度使用 `m/s`；持续时间使用秒 `s`；
- 角度字段必须在字段名中写明 `_deg` 或 `_rad`，不得裸写未说明单位的角度；
- 高层位姿与 `grasp_offset_m` 默认使用机器人 `base` frame；硬件侧必须提供轴向正负说明；
- 绝对时间使用带时区的 ISO-8601，持续时间使用单调时钟测量，不能混用系统时间差计算 Stop 延迟。

## 4. 通用 HTTP 响应封装

### 4.1 传输成功响应

所有成功响应使用：

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {}
}
```

`mode` 取值：

- `dry_run`：合成响应，不代表实机；
- `real_motion`：加载了真实 backend，但仍须看 `data` 中的健康、预检和评价结果。

`ok=true` 只表示 bridge 成功处理并返回了业务结果，不等于抓取成功；物理结果以 `data.status`、`data.outcome` 和评价证据为准。

### 4.2 错误响应

```json
{
  "ok": false,
  "mode": "real_motion",
  "error": {
    "code": "PREFLIGHT_REJECTED",
    "message": "target is outside the configured workspace"
  },
  "data": {
    "reasons": ["..."]
  }
}
```

### 4.3 HTTP 状态码

| 状态码 | 含义 |
|---:|---|
| 200 | 请求处理完成；不代表物理抓取成功，需检查 `data.status` 和评价结果 |
| 400 | JSON、字段或 Content-Length 无效 |
| 409 | 预检拒绝或机器人忙（`PREFLIGHT_REJECTED` / `ROBOT_BUSY`） |
| 413 | 请求体超过 64 KiB |
| 503 | backend、wrapper、评价器或 Stop 服务不可用 |

## 5. 接口一：`GET /health`

### 5.1 作用

读取 bridge 和机器人运行条件，不触发运动。

### 5.2 最小返回字段

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {
    "available": true,
    "backend": "armpi_final_red_cuboid_wrapper",
    "hardware_status": "verified_baseline_ready",
    "mode": "real_motion",
    "motion_enabled": true,
    "motion_busy": false,
    "stop_available": true,
    "stop_verified": true,
    "result_evaluator_configured": true,
    "parameterized_skill_configured": true,
    "safety_profile_version": "safety-v1",
    "safety_profile_sha256": "...",
    "source_commit": "...",
    "probe_at": "2026-09-04T10:30:00+08:00",
    "components": {
      "ros": "ready",
      "camera": "ready",
      "calibration": "ready",
      "motion_controller": "ready",
      "software_stop": "ready",
      "physical_estop": "operator_verified"
    }
  }
}
```

### 5.3 字段语义

- `available=true` 在 `dry_run` 模式只表示合成 backend 可响应，不表示真实机器人可用；
- 真实执行至少要求 `mode=real_motion`、`motion_enabled=true`、`available=true`，并且没有配置缺口；任一关键组件为 `unknown`、`unverified` 或断连时不得返回 ready；
- `available=true` 必须来自当次或足够新鲜的 live probe，不能只检查文件、环境变量或 Python import；
- `stop_available=true` 只表示 Stop 入口已配置，不能替代动作中 Stop 实测；
- `stop_verified=true` 只有在有真实测试证据时才能返回；
- `result_evaluator_configured` 是 P0 物理成功判定的必要条件；
- `parameterized_skill_configured` 是 P1 的必要条件；H-02 阶段可以为 `false`，但必须明确标为 `p1_blocked`，不能因此宣称 P1 已可执行；
- 没有连续关节状态时，必须明确返回 `state_capability="unknown_or_snapshot_only"`，不能伪装成实时状态；
- 如需前端显示相机画面，可返回 `stream_url` 或 `camera.stream_url`。该地址必须是上层电脑实际可访问且经安全白名单确认的 HTTP(S) 地址。

## 6. 接口二：`GET /state`

### 6.1 作用

读取当前运行状态，不触发新动作。

### 6.2 最小返回字段

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {
    "state": "idle",
    "active_experiment_id": null,
    "updated_at": "2026-09-04T10:30:00+08:00",
    "state_capability": "snapshot_only",
    "last_error": null
  }
}
```

允许的 `state`：`idle`、`running`、`stopping`、`stopped`、`fault`、`unknown`。

状态至少要在 `running → stopping/stopped` 或 `running → fault` 间可追踪，并携带 `active_experiment_id`。Stop 或故障后不得自动回到 `idle` 继续执行；必须由人工确认/新的 preflight 才能恢复。

如果当前 wrapper 不能提供连续状态，返回 `idle` 只能表示“没有已知活动任务”，同时必须附带限制说明，不能宣称已获得连续实时关节状态；此时 `state_capability` 应为 `snapshot_only` 或 `unknown`。

## 7. 接口三：`POST /preflight`

### 7.1 作用

在不移动机械臂的情况下，检查任务计划、目标位姿、标定、安全约束、wrapper 和参数白名单。

### 7.2 请求体

请求体使用完整的高层 `ExperimentPlan` JSON。最小示例：

```json
{
  "experiment_id": "exp-p0-001",
  "task": {
    "task_id": "task-001",
    "source_text": "把红色方块放到右侧目标区域",
    "target_color": "red",
    "target_object": "cube",
    "target_zone": "right"
  },
  "skill": {
    "skill_id": "pick_place_color_block",
    "version": "p0",
    "status": "stable",
    "parameters": {
      "grasp_offset_m": [0.0, 0.0, 0.0],
      "approach_height_m": 0.03,
      "transit_height_m": 0.12,
      "speed_m_s": 0.10
    }
  },
  "scene_id": "fixed-color-cube-workcell-v0",
  "adapter": "real_arm",
  "expected_data_source": "real_arm",
  "target_pose": {
    "x": 0.22,
    "y": 0.05,
    "z": 0.03,
    "frame_id": "base",
    "object_id": "red-cube-001",
    "confidence": 0.96,
    "calibration_version": "calib-v1"
  },
  "destination_pose": {
    "x": 0.22,
    "y": -0.08,
    "z": 0.03,
    "frame_id": "base"
  },
  "timeout_s": 8.0,
  "safety_constraints": {
    "profile_version": "safety-v1",
    "allow_real_robot": true,
    "minimum_confidence": 0.8,
    "workspace_min_m": [0.10, -0.20, 0.0],
    "workspace_max_m": [0.40, 0.20, 0.30],
    "max_speed_m_s": 0.15,
    "max_timeout_s": 10.0,
    "max_offset_m": 0.05
  }
}
```

注意：为兼容当前上层 `SafetyConstraints` 模型，请求体中的字段名保持为 `safety_constraints.profile_version`；bridge 对外回显本机最终裁决时使用 `safety_profile_version`/`safety_profile_sha256`。两者含义不同，不能把客户端请求版本当作本机已加载版本。

### 7.3 通过响应

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {
    "experiment_id": "exp-p0-001",
    "approved": true,
    "checks": [
      "wrapper_check",
      "numeric_safety",
      "ik_preflight",
      "fixed_destination"
    ],
    "safety_profile_version": "safety-v1",
    "safety_profile_sha256": "...",
    "artifacts": {
      "wrapper_check_log": "/path/to/wrapper_check.log"
    }
  }
}
```

### 7.4 拒绝规则

以下情况必须返回 HTTP 409 和 `PREFLIGHT_REJECTED`（这是硬件 bridge 必须实现的安全门；当前参考 server 尚未全部强制）：

- `experiment_id` 缺失、为空、重复使用或不是安全的字符串（仅适用于 `preflight` 和 `execute_pick_place`；`stop` 可不带 ID，以支持未知任务/失联场景的全局安全停止）；
- `target_pose`/`destination_pose` 缺失、不是 `base` frame、坐标越界或标定无效；
- 目标置信度低于安全阈值；
- 安全档案、Stop、wrapper 或评价器配置不完整；
- P1 修改了多个参数族，或使用了未批准字段；
- 对 P1/非基线 Skill，参数 SHA256 无法和 wrapper 预检回显对应；P0 基线不要求参数 ACK，但必须使用冻结基线参数；
- 请求包含 `servo_id`、`pulse`、`ros_topic`、`shell`、任意命令、任意本地路径或其他未在本合同列出的低层字段。

未知字段默认拒绝（allow-list）；输入中的 artifact 路径只能作为受控标识，不能让客户端指定 bridge 任意读写路径。

单独一次 preflight 的批准不能永久授权；`/execute_pick_place` 必须在执行前重新检查。

## 8. 接口四：`POST /execute_pick_place`

### 8.1 作用

按高层计划执行一次抓取—运输—放置，并返回动作链、物理评价和实验工件。

### 8.2 执行约束

- 请求体与 `/preflight` 使用同一份完整计划；
- bridge 在真正执行前再次 preflight；
- 同时只允许一个运动操作；并发请求返回 HTTP 409 `ROBOT_BUSY`；
- 不接受舵机 ID、脉宽、ROS Topic、shell 命令或任意路径作为动作指令；
- 结果中的 `experiment_id` 必须回显请求 ID；`execution_id` 如存在，必须与之区分；
- 当前合同为同步返回，超时必须停止并返回明确失败状态；物理执行失败仍可用 HTTP 200 返回。被 Stop 中断时使用 `data.status="failed"` 且 `failure.code="STOPPED"`；超时时使用 `data.status="timed_out"` 且 `failure.code="TIMEOUT"`；其他失败保留 `GRASP_FAILED`、`POSE_OFFSET`、`PATH_BLOCKED` 或 `EXECUTION_FAILED`。`/state` 才使用 `stopped` 表示当前机器人状态。

### 8.3 成功结果格式

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {
    "experiment_id": "exp-p0-001",
    "execution_id": "run-001",
    "status": "succeeded",
    "hardware_status": "real_arm_wrapper_execution_evaluated",
    "evaluator_type": "vision",
    "evaluator_version": "camera-evaluator-v1",
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
    "metrics": {
      "path_length_m": 0.42,
      "planning_time_ms": 180.0,
      "execution_time_s": 4.91
    },
    "artifacts": {
      "evaluation_file": "/path/to/evaluation.json",
      "evaluation_evidence_1": "/path/to/after.jpg",
      "joint_states": "/path/to/joint_states_session.csv",
      "wrapper_pick_place_log": "/path/to/wrapper.log",
      "safety_profile_sha256": "..."
    }
  }
}
```

### 8.4 物理成功判定

只有同时满足以下条件，才允许 `status="succeeded"`：

- `evaluator_type="vision"`；
- 有非空 `evaluator_version`；
- 有与该实验关联的实际图片或视频；
- `object_grasped=true`、`object_lifted=true`、`object_placed=true`；
- `experiment_id`、参数、标定和评价文件一致。

`manual` 或 `hybrid` 评价只能标为 `supervised/manual_review`，不能作为自动 P0→P1 或比赛成功证据。`path_length_m` 只有在存在实测 TCP/TF 轨迹时才返回；没有实测数据时应省略或写 `unknown`，不得填估算值。

仅有 `pick_place_sequence_completed`、wrapper 退出码为 0 或人工口头确认时，必须返回失败/未验证状态，例如 `hardware_status="motion_completed_result_unverified"`，不得宣称物理成功。

### 8.5 失败结果格式

失败、拒绝和超时必须带 `failure`：

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {
    "experiment_id": "exp-p0-002",
    "status": "failed",
    "hardware_status": "real_arm_wrapper_execution_failed",
    "evaluator_type": "vision",
    "evaluator_version": "camera-evaluator-v1",
    "actions": [
      {
        "action": "grasp",
        "status": "failed",
        "error_code": "GRASP_FAILED",
        "message": "object released during lift",
        "duration_s": 2.1
      }
    ],
    "outcome": {
      "object_grasped": false,
      "object_lifted": false,
      "object_placed": false
    },
    "failure": {
      "code": "GRASP_FAILED",
      "stage": "grasp",
      "message": "object released during lift"
    },
    "artifacts": {
      "evaluation_file": "/path/to/evaluation.json",
      "failure_evidence": "/path/to/grasp_lift.jpg"
    }
  }
}
```

## 9. 接口五：`POST /stop`

### 9.1 作用

请求机器人执行最快的安全软件停止。该接口不应被运动锁阻塞，必须能在 `/execute_pick_place` 执行期间调用。

### 9.2 请求体

```json
{
  "experiment_id": "exp-p0-001",
  "reason": "operator_request"
}
```

`experiment_id` 在 Stop 请求中可选：带 ID 时用于关联当前任务；不带 ID 时必须执行全局安全停止并返回 `experiment_id: null`。Stop 不得因为任务 ID 未知而拒绝，这一点优先于普通实验归档关联。

### 9.3 返回格式

```json
{
  "ok": true,
  "mode": "real_motion",
  "data": {
    "experiment_id": "exp-p0-001",
    "stopped": true,
    "verified": true,
    "status": "stopped",
    "latency_ms": 85,
    "state_before": "running",
    "state_after": "stopped",
    "evidence_file": "/path/to/real_stop_result.json"
  }
}
```

### 9.4 重要边界

- 软件 Stop 不等于物理急停；现场必须保留物理急停；
- `stopped=true` 的空闲调用只能证明接口响应，不能证明动作中停止能力；
- 交付时必须提供动作中 Stop 的前后状态、响应时间、动作中断日志和无续动证据；
- Stop 成功后必须清除当前 preflight 批准、将 active experiment 标为 `stopped` 或 `fault`，并禁止同一批准自动续跑；恢复动作必须重新 preflight；
- Stop 失败时返回 `stopped=false`，上层必须提示人工立即使用物理急停，不能自动恢复动作。

## 10. 错误码

业务层沿用项目错误码：

```text
INVALID_TASK
LOW_CONFIDENCE
OUT_OF_WORKSPACE
INVALID_PARAMETERS
CALIBRATION_MISSING
REAL_ROBOT_NOT_ALLOWED
HARDWARE_UNVERIFIED
POSE_OFFSET
GRASP_FAILED
PATH_BLOCKED
TARGET_NOT_FOUND
BRIDGE_UNAVAILABLE
EXECUTION_FAILED
TIMEOUT
STOPPED
```

bridge 层额外使用：

```text
INVALID_JSON
PAYLOAD_TOO_LARGE
BACKEND_UNAVAILABLE
PREFLIGHT_FAILED
PREFLIGHT_REJECTED
ROBOT_BUSY
STOP_FAILED
NOT_FOUND
```

## 11. P1 参数化合同

### 11.1 允许的 canonical 参数

当前 runner 要求参数集合恰好为：

```json
{
  "grasp_offset_m": [0.0, 0.0, 0.0],
  "approach_height_m": 0.03,
  "transit_height_m": 0.12,
  "speed_m_s": 0.10
}
```

参考范围：

- `grasp_offset_m` 每轴绝对值不超过 `0.02 m`；
- `approach_height_m`：`0.01–0.10 m`；
- `transit_height_m`：`0.05–0.20 m`；
- `speed_m_s`：`0.02–0.15 m/s`。

实际限制以现场安全档案为准，不能因为范围在合同内就绕过现场 profile。

`grasp_offset_m` 是相对于 `base` frame 的 `[x, y, z]` 米制偏移，硬件侧必须在文档和 ACK 中说明三个轴的正方向。反馈补偿必须使用“观测误差的反向修正”并经过 preflight，不能把未经验证的模型输出直接当作偏移。

### 11.2 SHA256 回显

硬件参数化 wrapper 在 `check` 和 `pick-place` 两次都必须输出：

```text
STATUS: skill_parameters_applied sha256=<64位小写SHA256>
```

该摘要必须与上层 canonical JSON（排序键、无多余空格、UTF-8）完全一致。没有一致的 ACK，P1 preflight 和执行都必须拒绝。

### 11.3 单参数族规则

一次 P1 只能修改一个批准参数族，例如：

- `grasp_offset`；或
- `path_profile`（当前实现具体只允许改变 `transit_height_m`）。

不能同时改变目标位姿、抓取偏移、速度、夹爪和路径。P1 必须保留 `p0_experiment_id`，并在 `same_scene_check.json` 中证明条件一致。任务 1–10 源码中只修改 `target_xyz` 的旧版候选格式与本合同不兼容，必须先适配为上述四键 schema，不能两套格式并列。

## 12. 实验工件与路径合同

每次执行至少要能归档：

```text
experiment.json
plan_applied.json
preflight.json
bridge_result.json
evaluation.json
before.jpg / grasp_lift.jpg / after.jpg
joint_states_session.csv
wrapper.log
calibration + SHA256
runtime_manifest.json
SHA256SUMS.txt
```

### 12.1 绝对路径与归档路径

运行时评价器可以使用现存的绝对图片/视频路径；归档时必须同时复制媒体到实验包内，并在 manifest 中记录 `packaged_path` 与原始路径映射。不能只交一个解包后失效的绝对路径。

### 12.2 `bridge_result.json` 规范化

保存 HTTP 响应时，建议同时保存：

- `raw_response.json`：完整 `{ok, mode, data}` 原始响应；
- `bridge_result.json`：规范化结果，至少在顶层包含 `experiment_id`、`status`、`outcome` 和 `artifacts`。

这样可以让校验器和人工审查直接核对实验 ID，而不依赖读取嵌套的 `data` 字段。

### 12.3 校验规则

- SHA256 必须覆盖所有声明的运行工件，不能只覆盖源码；
- 压缩包不得包含路径穿越、软链接或特殊文件；
- `evaluation.json` 的三项物理结果必须是显式布尔值；
- 文档声明、截图或源码字符串不能替代运行工件。

## 13. 硬件侧实现步骤

1. 实现并启动 dry-run bridge，先通过合同测试；
2. 接入真实 wrapper，但保持 `--allow-real-motion` 关闭；
3. 完成 `/health`、`/state`、`/preflight` 和空闲 `/stop`；
4. 完成动作中 Stop 实测；
5. 接入视觉定位和标定，导出三条非空样例；
6. 接入评价器，导出成功 P0；
7. 在相同条件下导出失败 P0；
8. 接入参数化 runner，核对两次 SHA256 ACK；
9. 执行 P1 并导出同条件比较；
10. 打包、脱敏、生成 SHA256 并交付软件侧。

## 14. 交付验收签字条件

硬件侧提交后，软件侧按以下顺序验收：

```text
包安全性
  → SHA256
  → bridge 合同
  → 无动作预检
  → Stop 实测
  → 视觉定位/标定
  → 成功 P0
  → 失败 P0
  → 单参数 P1
  → 同条件比较
```

只有最后一项通过，才可以在比赛材料中表述为“真实反馈迭代闭环”。在此之前，前端必须明确显示 Mock、仿真、历史实机和当前实机的区别。

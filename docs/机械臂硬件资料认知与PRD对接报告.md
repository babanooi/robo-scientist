# ArmPi Ultra 机械臂硬件资料认知与 PRD 对接报告

> 版本：v0.1（资料初读版）  
> 日期：2026-07-19  
> 目的：基于已上传的教程、硬件资料和 ROS2 源码，整理机械臂硬件认知、可用接口、关键限制以及对“机械臂 AI Scientist” PRD 和开发的影响。

## 1. 阅读范围与证据等级

本轮优先读取了以下高价值资料：

- `docs/1 教程资料/1.ArmPi Ultra使用入门/ArmPi Ultra使用入门手册.pdf`
- `docs/1 教程资料/3.机械臂基础控制/机械臂基础控制手册.pdf`
- `docs/1 教程资料/4.机械臂高级控制/机械臂高级控制手册 .pdf`
- `docs/4.硬件资料/1 ROS主控资料/树莓派及扩展板原理图/树莓派5官方手册.pdf`
- `docs/4.硬件资料/2 STM32控制器资料/SCH_STM32_Core Module.pdf`
- `docs/4.硬件资料/2 STM32控制器资料/SCH_xArm Open Controller V2.0.pdf`
- `docs/4.硬件资料/4 相机资料/光鉴深度相机/` 下的 ROS2 教程、规格书、用户手册和结构资料
- `docs/3.源码资料/ROS2/Armpi_ultra-main.zip` 中的 ROS2 驱动、消息、服务、运动学、舵机控制和 RGB-D 示例目录
- 当前项目中的 PRD、进度计划和异地交接 Markdown 文档

证据等级定义：

| 等级 | 含义 |
|---|---|
| A：源码/接口证据 | 在 ROS2 消息、服务、节点或源码中直接看到 |
| B：厂商文档 | 教程、手册、规格书中的描述 |
| C：项目交接事实 | 交接文档中记录的已跑通结果，但本轮未连接实机复测 |
| D：待实机验证 | 只能作为开发假设，不能在答辩中写成已完成 |

四个资料目录总计约 242 个文件、17.68 GiB。本报告是关键资料初读版，不等价于对所有安装包、虚拟机镜像、压缩包和重复课程逐文件审计。STM32 工程 RAR 和大部分二进制安装包暂未展开分析。

## 2. 对 ArmPi Ultra 的总体认知

ArmPi Ultra 是一套面向 ROS/AI 教学的桌面级视觉机械臂平台。其核心硬件链路可以抽象为：

```text
树莓派 5 / Ubuntu / ROS2
        |
        +-- USB/扩展接口 --> Aurora 930 RGB-D 深度相机
        |
        +-- 串口 /dev/rrc --> STM32 控制器
                                |
                                +-- 总线舵机 1-6
```

源码中的 `ros_robot_controller_sdk.py` 默认使用 `/dev/rrc`、1,000,000 波特率、5 秒串口超时；这是源码默认值，仍需在实际设备上确认。

机械臂主体是 5 个运动关节加 1 个夹爪舵机，共 6 个总线舵机。运动学模块只对主体的 5 个关节求解，夹爪作为独立动作控制。

## 3. 舵机与关节映射

源码配置 `servo_controller.yaml` 给出了比“舵机 ID 1-6”更具体的映射：

| 逻辑部件 | 舵机 ID | 初始脉宽 | 源码中的位置范围 | 说明 |
|---|---:|---:|---:|---|
| 主体 joint1 | 6 | 500 | 0-1000 | 底座旋转方向 |
| 主体 joint2 | 5 | 500 | 0-1000 | 大臂/俯仰相关 |
| 主体 joint3 | 4 | 500 | 1000-500 | 配置中为反向范围，必须实机确认 |
| 主体 joint4 | 3 | 500 | 0-1000 | 小臂/俯仰相关 |
| 主体 joint5 | 2 | 500 | 0-1000 | 末端方向/腕部相关 |
| 夹爪 r_joint | 1 | 700 | 0-1000 | 夹爪开合 |

厂商基础控制手册说明舵机位置脉宽通常使用 0-1000。项目交接文档记录过夹爪 `400` 打开、`600` 闭合的测试值；源码示例还出现过 `210` 和 `600`。因此，夹爪参数不能直接写死为通用事实，应该写入设备配置并通过当前机械臂实测确定。

舵机协议和源码具备以下能力：

- 设置位置和运动持续时间；
- 读取 ID、当前位置、偏差、温度、位置限制、电压限制和温度限制；
- 设置/保存偏差；
- 设置位置、电压和温度限制；
- 开关扭矩；
- 对指定舵机发送停止指令。

源码中 `BusServoState` 已有电压字段，但 ROS 节点的电压读取分支目前被注释掉；因此“接口结构支持电压”不等于“当前运行链路一定能返回电压”。

## 4. ROS2 控制接口

### 4.1 低层总线舵机接口

低层控制节点由 `ros_robot_controller` 提供：

| 类型 | 名称 | 消息/服务 |
|---|---|---|
| Topic | `/ros_robot_controller/bus_servo/set_position` | `ros_robot_controller_msgs/msg/ServosPosition` |
| Service | `/ros_robot_controller/bus_servo/get_state` | `ros_robot_controller_msgs/srv/GetBusServoState` |
| Topic | `/ros_robot_controller/bus_servo/set_state` | `ros_robot_controller_msgs/msg/SetBusServoState` |
| Service | `/ros_robot_controller/init_finish` | `std_srvs/srv/Trigger` |

低层位置消息为：

```text
ServosPosition
  float64 duration
  ServoPosition[] position

ServoPosition
  uint16 id
  uint16 position
```

`duration` 在源码中按秒传入，底层 SDK 再转换为毫秒；舵机位置是脉宽/编码器位置语义，通常在 0-1000 范围内。

状态服务请求可以按舵机 ID 选择读取项目，返回内容包括：

- 当前存在的舵机 ID；
- 当前位置；
- 偏差；
- 温度；
- 位置限制；
- 电压限制；
- 最大温度限制；
- 扭矩状态。

### 4.2 上层舵机控制接口

`servo_controller` 包另外定义了：

```text
servo_controller_msgs/msg/ServosPosition
  float64 duration
  string position_unit
  ServoPosition[] position
```

并提供 `/servo_controller` 话题。这个消息比低层消息多了 `position_unit` 字段，示例中设置为 `pulse`。

这意味着项目不能简单地把所有 Topic 当作同一个接口使用。PRD 中应明确：

1. 上层业务只调用项目自己的 `ArmAdapter`；
2. `ArmAdapter` 内部再选择 `servo_controller` 或 `ros_robot_controller`；
3. 消息类型、单位、脉宽方向和异常返回由适配器隔离；
4. Mock 不应直接暴露厂商消息类型。

### 4.3 启动链路

厂商 `armpi_ultra.launch.py` 会组合启动：

- `ros_robot_controller`：串口和底层板卡通信；
- `servo_controller`：主体关节和夹爪控制抽象；
- `kinematics`：运动学求解服务；
- 可选底盘控制器：取决于 `CHASSIS_TYPE`。

源码还依赖环境变量 `need_compile` 和 `CHASSIS_TYPE`，因此部署文档必须把环境变量和工作空间是否已编译纳入设备自检。

## 5. 运动学认知

### 5.1 运动学模型

源码 `kinematics/transform.py` 给出 Modified DH 模型和长度参数：

| 参数 | 源码值 |
|---|---:|
| 普通底座高度 `base_link` | 0.094605 m |
| 滑轨配置底座高度 | 0.162 m |
| `link1` | 0.10048 m |
| `link2` | 0.100 m |
| `link3` | 0.055 m |
| `tool_link` | 0.115 m |

主体关节角度限制在源码中约为：

```text
joint1: -120° 到 120°
joint2: -180° 到 0°
joint3: -120° 到 120°
joint4: -200° 到 20°
joint5: -120° 到 120°
```

这些是模型/碰撞约束层面的初始范围，不应直接视为当前装配状态下的安全运动范围。PRD 应要求实际设备对关节范围、桌面边界和碰撞风险重新确认。

### 5.2 正运动学与逆运动学的边界

源码和高级控制手册共同表明：

- `/kinematics/set_joint_value_target` 根据 5 个主体关节的脉宽计算末端位姿；
- `/kinematics/set_pose_target` 根据目标位置、俯仰角和角度搜索范围计算脉宽解；
- 这两个服务本身主要负责求解和返回结果；
- 示例程序拿到 `pulse` 后，仍需要发布 `ServosPosition` 消息才会让机械臂实际移动。

因此，项目此前提出的“不要把 `/kinematics/*` 直接当作真实执行接口”是正确的。真实执行链路应拆为：

```text
目标位姿
 -> 安全检查
 -> 运动学求解
 -> 解的有效性检查
 -> 舵机脉宽映射
 -> 执行 Topic
 -> 读取状态
 -> 结果判定
```

## 6. Aurora 930 深度相机

Aurora 930 使用散斑结构光，输出 RGB、红外和深度数据，并支持三图对齐。规格资料中给出的典型能力包括：

- 模组尺寸约 61 mm × 18 mm × 14.3 mm；
- Baseline 约 40 mm；
- USB 2.0 Wafer 接口；
- 深度图、彩色图和红外图最高约 640 × 400；
- Raw16 深度、NV12 彩色、Raw8 红外；
- 深度精度标称约 3 mm@0.5 m、7 mm@1 m；
- 工作距离存在 15 cm 或 30 cm 近端版本，远端约 300 cm；
- 供电约 5 V ±10%，1.5-1.6 A；
- Class 1 激光安全；
- 支持 Linux/ARMv8/ROS/ROS2/Windows/Android。

资料之间存在 12 fps 与 15 fps 的规格差异，用户手册也说明近距离有 15 cm/30 cm 两种版本。因此 PRD 应写“以当前设备实际枚举和实测规格为准”，不能直接固化一个理论值。

### 6.1 ROS2 视觉话题

源码使用以下话题：

| 话题 | 消息 | 用途 |
|---|---|---|
| `/depth_cam/rgb/image_raw` | `sensor_msgs/msg/Image` | RGB 图像 |
| `/depth_cam/depth/image_raw` | `sensor_msgs/msg/Image` | 深度图 |
| `/depth_cam/depth/camera_info` | `sensor_msgs/msg/CameraInfo` | 内参和图像尺寸 |

源码用近似时间同步把 RGB、深度和内参放入同一处理回调。深度数据按 `uint16` 读取，并按毫米换算为米后通过针孔模型得到相机坐标：

```text
x = (px - cx) * z / fx
y = (py - cy) * z / fy
z = depth
```

### 6.2 视觉到机械臂坐标

示例代码的处理链路是：

```text
像素坐标 + 深度
 -> 相机内参
 -> 相机坐标
 -> hand2cam 变换
 -> 当前机械臂末端/基座位姿
 -> 目标世界/基座坐标
 -> 逆运动学
 -> 舵机脉宽
```

源码和配置中存在 `hand2cam`、`extristric`、`white_area_pose_cam`、`white_area_pose_world` 以及深度/像素/运动学补偿参数。这些参数说明厂商示例确实包含标定和残差补偿机制，但其中一部分是示例场景的硬编码值，不能直接当成当前设备的标定结果。

项目应将以下内容版本化：

- 相机内参；
- 相机坐标系定义；
- 相机到末端或基座的外参；
- 标定板/AprilTag ID 和尺寸；
- 深度偏移与缩放；
- 像素到基座的残差补偿；
- 标定采集时间和适用设备。

## 7. 厂商示例与项目创新的边界

源码中已经包含颜色识别、RGB-D、点云、形状识别、目标跟踪、抓取动作和语音控制示例。这些能力应被视为厂商提供的实验底座，而不是项目原创成果。

项目真正需要建立的是：

- 用结构化任务描述调用这些能力；
- 在执行前经过统一安全层；
- 自动记录每次实验的输入、图像、位姿、动作和结果；
- 根据失败阶段和数值证据修改明确参数；
- 形成新的 Skill/参数版本；
- 用重复实验和留出场景决定晋升、拒绝或回滚。

## 8. 对 `ArmAdapter` 的建议

上层业务不应直接依赖厂商 ROS2 消息。建议项目自己的硬件适配器至少提供：

```python
reset_home() -> RobotActionResult
get_robot_state() -> RobotState
move_joints(joint_pulses, duration) -> RobotActionResult
solve_pose(pose, pitch_range) -> KinematicsResult
move_to_pose(pose, speed, safety_context) -> RobotActionResult
open_gripper(config) -> RobotActionResult
close_gripper(config) -> RobotActionResult
stop(reason) -> RobotActionResult
capture_scene() -> SceneObservation
get_object_pose(object_id) -> ObjectPose
transform_camera_to_base(pose, calibration_version) -> ObjectPose
```

每个动作至少返回：

- `success`；
- `error_code`；
- `message`；
- 开始和结束时间；
- 目标参数；
- 最终舵机状态；
- 最终位姿；
- 是否触发安全拦截；
- 原始 ROS2 日志或数据引用。

安全层至少检查：

- 舵机 ID 是否允许；
- 脉宽是否在设备配置范围内；
- 关节角/末端位姿是否超出模型和工作空间；
- 夹爪参数是否经过当前设备标定；
- 深度和目标置信度是否达到阈值；
- 控制器是否初始化完成；
- 执行是否超时；
- 急停或停止后是否回到安全状态。

## 9. 对当前 PRD 的直接修改建议

当前 PRD 可以立即补充以下硬件事实：

1. 明确 5 个主体运动关节 + 1 个夹爪舵机的结构。
2. 把 ROS2 控制拆成“求解接口”和“执行接口”，不再把运动学服务当成执行器。
3. 在 `ArmAdapter` 契约中加入状态读取、停止、错误码和最终状态。
4. 明确低层总线舵机消息与上层 `servo_controller` 消息的隔离关系。
5. 把 Aurora 930 的 RGB、深度、内参和近似时间同步加入视觉接口。
6. 把标定版本、坐标系、深度单位和补偿参数加入 `ExperimentResult`。
7. 把夹爪打开/闭合值改为设备配置，不写死为 400/600 或 210/600。
8. 把“厂商已有抓取示例”和“团队反馈迭代闭环”作为明确的创新边界。

## 10. 当前仍不能直接宣称的内容

以下内容还需要实际设备、日志或重复实验确认：

- 当前机械臂的真实固件、ROS2 工作空间和环境变量；
- 源码默认串口 `/dev/rrc` 是否就是当前设备实际端口；
- 6 个舵机是否全部在线且 ID 映射与配置一致；
- 真实安全关节范围、工作空间和桌面碰撞边界；
- 夹爪开合值、连续稳定性和负载能力；
- 电压、温度、扭矩状态是否能通过当前运行节点完整返回；
- Aurora 930 当前设备版本的实际帧率、近端盲区和深度误差；
- 当前设备的手眼标定参数和重复定位误差；
- 抓取成功、提起成功和放置成功的独立判定方法；
- 从另一台电脑向树莓派同步代码的最终部署方式。

## 11. 是否可以完善 PRD 并开始开发

结论：**可以进行 PRD 初步完善，也可以立即开始软件侧开发；但真实机械臂闭环仍必须经过实机验证。**

现在可以直接开发：

- `ExperimentPlan`、`ExperimentResult`、`RobotActionResult`、`ObjectPose`、`SkillVersion`；
- Mock 执行器和四类可复现失败注入；
- `ArmAdapter` 抽象和 Mock 实现；
- 任务解析、实验编排、日志和报告；
- 安全校验器；
- 版本比较、晋升和回滚；
- 基于定位偏差或抓取点偏移的首个优化变量；
- RGB-D 数据接口和离线视觉处理骨架。

暂时不能只依据资料宣称已经完成：

- 完整真实抓取；
- 实机安全运动；
- 真实三维定位精度；
- 抓取/提起/放置成功率；
- 真实反馈迭代效果。

建议的开发顺序是：

```text
冻结项目 Schema
 -> 完成 Mock 闭环
 -> 实现 ArmAdapter ROS2 适配器
 -> 在树莓派上做最小舵机/状态读取测试
 -> 接入 RGB-D 和标定
 -> 做一次可追溯真实单轮实验
 -> 再进行重复优化和版本晋升/回滚
```

本报告后续应继续补充：STM32 工程源码、当前设备实机日志、实际 `ros2 topic/service` 清单、标定文件和硬件连续运行测试结果。

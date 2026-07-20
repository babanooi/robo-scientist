# ArmPi Ultra 机械臂资料整理

> 来源：Hiwonder官方文档 https://docs.hiwonder.com/projects/ArmPi-Ultra/en/latest/index.html
> 说明：文档未公开具体IP/账号等私有部署信息，以下内容为该机械臂**通用的、公开可查的接口规范**，标记【需自行核实】的字段需要你在自己的实机上核实并补全。

---

## 1. 设备与部署资料

| 项目 | 内容 |
|---|---|
| 机械臂型号 | ArmPi Ultra（6自由度，Hiwonder） |
| 主控 | Raspberry Pi 5（负责ROS2/AI/相机），STM32F103RBT6（负责舵机底层控制，通过Type-C下载程序，串口与树莓派通信） |
| 相机型号 | 标准/高级版：Deptrum® Aurora 930 深度相机（3D结构光）；入门版：USB单目相机（无深度信息） |
| 树莓派系统 | 官方定制镜像（基于树莓派OS），需用 DiskGenius + Win32DiskImager 烧录到TF卡，镜像从官方Google Drive下载 |
| ROS2/SDK版本 | ROS2（Docker容器内运行），具体distro【需自行核实，可用 `printenv ROS_DISTRO` 查看】 |
| 网络模式 | 默认AP直连模式：开机后出现"HW"开头热点，密码 `hiwonder`，树莓派固定IP **192.168.149.1**；也支持STA/LAN模式接入你自己的WiFi，IP由路由器分配（可在APP中长按机器人图标查看） |
| SSH/远程登录 | 支持。用户名 `pi`，密码 `raspberrypi`（VNC和MobaXterm的SSH都用这组账号密码；注意用户名必须小写）|
| 是否能SSH登录 | 可以，MobaXterm走SSH命令行；VNC走图形桌面 |
| 代码怎么传到树莓派 | 通过VNC图形界面直接编辑，或SSH命令行操作；也可用共享目录 `shared`（Docker与系统之间共享空间） |
| 项目放哪个目录 | 主目录下：`~/ros2_ws`（核心工作空间）、`~/software`（各类PC工具，如动作编辑软件、舵机调试工具、标定工具）、`~/large_models_sdk`（大模型示例）、`~/third_party_ros2` |
| ros2_ws下结构 | `build`（编译缓存）、`install`（编译产物）、`log`（日志）、`src`（源码，含 `app`应用层、`interfaces`自定义接口、`driver`底层驱动、`example`示例程序、`bringup`开机自启动launch、`peripherals`外设、`large_models`等）|
| 怎么启动 | 停止自启动服务：`~/.stop_ros.sh`；启动SDK底层：`ros2 launch sdk armpi_ultra.launch.py`；启动某个功能包：`ros2 launch example <文件名>.launch.py` |
| 怎么停止 | 终端内 `Ctrl+C`（可能需多按几次才能彻底退出）|
| 怎么看日志 | launch时加 `output='screen'`会直接在终端打印；ROS2工作空间下有 `log`目录存历史日志 |
| 开机自启动服务 | `ros2 launch bringup bringup.launch.py`（每次改完自定义功能测试后，必须重新启动这个服务，否则APP端功能失效；成功后机械臂回到初始位姿并蜂鸣一声）|

---

## 2. 控制接口资料

ArmPi Ultra 提供**两层控制接口**：底层舵机Topic（直接控制单个舵机脉宽）+ 上层运动学Service（给坐标/姿态，自动解算成舵机角度）。两者是分离的，运动学Service**默认只做解算，不会让机械臂动**，除非显式发布到舵机话题或用对应的launch文件。

### 2.1 基础外设接口（舵机/LED/蜂鸣器）

| 接口 | 类型 | 话题/说明 | 输入 | 输出/反馈 | 备注 |
|---|---|---|---|---|---|
| 舵机控制 | Topic (Publisher) | `/ros_robot_controller/bus_servo/set_position`，消息类型 `ros_robot_controller_msgs/msg/ServosPosition` | `position: [{id: 1~6, position: 0~1000}]`，可选 `duration`（运动时长，秒） | 无返回值，终端会打印发布的log | 舵机ID范围1-6；position(角度)范围0-1000，对应0-240°物理转动范围 |
| 初始化完成信号 | Service (Trigger) | `/ros_robot_controller/init_finish` | 无 | Trigger成功/失败 | 各控制节点都会先 `wait_for_service`等待此服务，确保底层已就绪后再发指令 |
| LED控制 | Topic (Publisher) | `/ros_robot_controller/set_led`，消息类型 `ros_robot_controller_msgs/msg/LedState` | `id`(2=板载LED)，`on_time`(秒)，`off_time`(秒)，`repeat`(次数) | 无 | 示例：`on_time:0.1, off_time:0.2, repeat:10` |
| 蜂鸣器控制 | Topic (Publisher) | `/ros_robot_controller/set_buzzer`，消息类型 `ros_robot_controller_msgs/msg/BuzzerState` | `freq`(2~4kHz)，`on_time`，`off_time`，`repeat` | 无 | 频率越高音调越高 |
| 统一舵机发布口(运动学层用) | Topic (Publisher) | `/servo_controller`，消息类型 `servo_controller_msgs/msg/ServosPosition` | `position:[{id, position}]`，`position_unit: "pulse"`，`duration` | 无 | fk/ik示例程序实际驱动舵机走的是这个topic，和2.1中的 `bus_servo/set_position`是不同命名空间，需注意区分（可能是SDK层再封装了一层） |

**启动方式**：先执行 `~/.stop_ros.sh` 停自启动 → `ros2 launch sdk armpi_ultra.launch.py` 启动SDK底层节点 → 新开终端 `ros2 topic list` 查看当前话题 → 用 `ros2 topic pub` 命令行发布，或运行python示例文件（如 `~/ros2_ws/src/example/example/simple/include/bus_servo_node.py`）。**用完必须重新** `ros2 launch bringup bringup.launch.py` 恢复自启动，否则APP端功能会失效。

### 2.2 运动学（正逆解）接口——`/kinematics/*`

**关键结论：`/kinematics/*` 下的 Service 只做数学解算（IK/FK求解），调用后终端只打印位姿或角度信息，机械臂本体并不会真实运动。** 要让机械臂真的动，必须再把解算结果通过上面的舵机Topic（`/servo_controller` 或 `bus_servo/set_position`）发布出去，或者直接运行封装好的 `ros2 launch example fk.launch.py` / `ik.launch.py`（这两个launch文件内部会自动调用解算+发布舵机话题两步)。

完整服务列表（均为 `kinematics_msgs`下的自定义Service）：

| Service | 功能 |
|---|---|
| `/kinematics/get_current_pose` | 获取机械臂当前末端位姿 |
| `/kinematics/get_joint_range` | 获取各关节运动范围 |
| `/kinematics/set_joint_range` | 设置各关节运动范围 |
| `/kinematics/get_link` | 获取连杆信息 |
| `/kinematics/set_link` | 设置连杆信息 |
| `/kinematics/set_joint_value_target` | **正解**：输入5个舵机角度值，输出末端位姿（坐标+四元数）|
| `/kinematics/set_pose_target` | **逆解**：输入目标XYZ坐标+俯仰角，输出可行的舵机角度解 |
| `/kinematics/set_pose_target_smooth` | 逆解的平滑版本（用于连续轨迹）|
| `/kinematics/init_finish` | 初始化完成信号（Trigger类型，其他节点等它启动）|
| `/kinematics/describe_parameters` `/get_parameters` `/set_parameters` `/set_parameters_atomically` `/list_parameters` `/get_parameter_types` | ROS2标准参数服务，非机械臂专用 |

**正解调用示例（只解算，不会动）：**
```bash
ros2 service call /kinematics/set_joint_value_target kinematics_msgs/srv/SetJointValue "{joint_value: [500.0, 400.0, 300.0, 400.0, 500.0]}"
```
输入：5个舵机的position值(0-1000脉宽制)。
输出：末端XYZ坐标 + 四元数姿态。

**逆解调用示例（只解算，不会动）：**
```bash
ros2 service call /kinematics/set_pose_target kinematics_msgs/srv/SetRobotPose "{position: [0.3, 0.0, 0.2], pitch_range: [-180,180], pitch: 10, resolution: 1}"
```
输入参数：
- `position`: 目标XYZ坐标（单位**米**，浮点数）
- `pitch`: 目标俯仰角（-180~180）
- `pitch_range`: 允许的俯仰角搜索范围
- `resolution`: 角度调整步进，一般固定给1.0

输出：若有解，返回可行舵机角度组合；**若无解，终端不返回舵机信息**（无显式错误码，需自行判断返回是否为空）。

**要真正驱动机械臂运动**，用封装好的launch文件（内部包含"解算+发布舵机话题"完整闭环）：
```bash
ros2 launch example fk.launch.py   # 正解并真实运动
ros2 launch example ik.launch.py   # 逆解并真实运动
```

**错误码/超时**：官方文档未给出显式错误码表，无解时仅日志提示"没有逆运动学解，请检查末端坐标"，需要在业务代码里自行加超时和空值判断。这部分需自行在实机测试补全。

### 2.3 回零/初始化

- 没有单独的"回零"Service，是通过启动 `ros2 launch bringup bringup.launch.py`（APP自启动服务）来让机械臂回到初始位姿（回零+蜂鸣器响一声）。
- 也可以直接发送一组预设的舵机position值（各舵机500，即中位）实现回零效果。

### 2.4 夹爪控制

夹爪本质是servo ID=1的舵机，走同一个 `/ros_robot_controller/bus_servo/set_position`（或 `/servo_controller`）话题，没有独立的夹爪接口。开合通过设置该ID的position数值控制（值越接近某一端夹爪越开/合，具体开合阈值需在PC软件里手动试出对应数值）。

### 2.5 急停

文档中没有找到独立的"急停Service/Topic"，急停方式主要是：
- 硬件层面：直接断电（滑动电源开关）
- 软件层面：终端 `Ctrl+C` 停止当前运行的控制节点
【急停后的状态恢复、是否有软件急停接口，需自行核实】

---

## 3. 安全边界资料

| 关节(θ) | 对应舵机功能 | 角度范围 |
|---|---|---|
| θ1 | 底座旋转 | -120° ~ 120° |
| θ2 | 大臂 | -180° ~ 0° |
| θ3 | 小臂 | -120° ~ 120° |
| θ4 | 腕部 | -200° ~ 20° |
| θ5 | 腕部旋转 | -120° ~ 120° |

（θ6/夹爪舵机范围文档未单独给出，需自行用 `/kinematics/get_joint_range` 服务查询或用舵机调试工具读取）

- **舵机型号与承重**：6个智能总线舵机组成——LX-15D×1(夹爪)、HX-06L×1(腕关节)、HTS-16L×2(机身)、LX-225×1(机身)、HTS-25L×1(云台)。具体扭矩/堵转电流参数官方文档未列出数值表，**需查各舵机单独的资源文档（Hiwonder提供了独立的舵机资源下载包）**。
- **速度/加速度限制**：官方文档未给出显式限速参数；`/kinematics/set_pose_target_smooth` 提供"平滑"轨迹但未注明具体限速值。
- **桌面高度/工作空间**：文档未给出具体的三维工作空间边界数值（球面/圆柱体范围），只有关节角度范围+连杆长度（DH参数里a3=0.10048m, a4=0.100m），如需要精确的可达工作空间，需要根据DH参数自行做正运动学包络计算。
- **禁入区**：无专门定义，操作说明中提示不要把机械臂放在桌边、不要堆叠机械臂、操作时人员需与机械臂保持安全距离。
- **异常恢复流程**：文档未提供统一的异常码/恢复SOP，仅有"卡关节/偏差过大"时的**舵机偏差校准流程**（分"小偏差"用软件滑块微调、"大偏差"需拆卸舵机机械复位重装）。
- **扭矩、电流、碰撞检测**：**官方文档未提及可读取扭矩/电流/碰撞状态的接口，应视为"不可用"**，若舵机支持（部分Hiwonder总线舵机可读电压/温度），需用舵机调试工具（`~/software/servo_tool/servo_tool/main.py`）单独查询，非ROS2话题形式暴露。

---

## 4. 视觉感知资料

| 项目 | 内容 |
|---|---|
| 相机型号 | Aurora 930深度相机（标准/高级版）或USB单目相机（入门版），二者切换需用系统里的"版本配置工具"选择 `aurora` 或 `usb_cam` |
| RGB/深度/点云读取 | 深度相机支持RGB、深度图、点云三种数据（文档第4、9章有对应的深度图伪彩色处理、点云换算、3D抓取示例），单目相机**只有RGB，无深度/点云能力** |
| 颜色方块检测输出字段 | 文档未给出具体消息字段定义（如msg类型名），只说明识别结果用于"色块定位与抓取"(第5章OpenCV视觉课程)。**需自行查看 `~/ros2_ws/src/example` 或 `app` 包里对应识别节点的msg定义来确认具体字段** |
| 目标位置坐标系 | 存在像素坐标(2D，用于单目)、相机坐标(3D，深度相机点云点击)两种标定/定位方式，两者都通过"标定工具" `~/software/calibration/tool.sh` 中的"像素定位"和"深度定位"分别标定 |
| 置信度计算方式 | 文档未提供 |
| 目标丢失/深度无效/低置信度返回值 | 文档未提供具体返回值定义，**需自行测试/查代码确认** |
| 颜色阈值调整 | 提供LAB TOOL工具（`~/software/...` 下，具体路径文档中为图标点击方式），可调 L(黑白)/A(绿红)/B(蓝黄) 三分量的min/max，范围均为0~255 |

---

## 5. 标定与坐标资料

| 项目 | 内容 |
|---|---|
| 坐标系定义 | 采用DH建模法，基坐标系{0}在底座，末端坐标系{5}在夹爪；地图上标注了机械臂的X轴(前后)、Y轴(左右)方向供参考 |
| camera→base转换 | 通过"手眼标定"(Hand-Eye Calibration)工具完成：打开标定软件→加载`test.npz`→依次让机械臂对准地图二维码若干个预设点位→点Update→最后点"Hand-Eye Calibration"保存转换矩阵参数 |
| 单位 | **米(m)**——如DH参数a3=0.10048、a4=0.100，逆解坐标输入示例`[0.3, 0.0, 0.2]`均为米；舵机层面用的是0-1000的"脉宽/角度值"，非物理单位 |
| 姿态表示 | 正解输出为四元数(quaternion)，可用配套的 `transform.qua2rpy()` 转成RPY欧拉角；逆解输入用pitch角度(-180~180度) |
| 标定参数存放位置 | `/home/ubuntu/software/hand2cam_tf_matrix_software/calibration/config/test.npz` |
| DH参数表 | i=1~5：`αi-1`(0,-90,0,0,-90)，`ai-1`(0,0,0.10048,0.100,0)，`di`=0，θi为变量，具体角度范围见上文"安全边界"表格 |
| 重复定位误差 | 文档未给出量化数值，只说明出厂前已校准，运输/使用中可能因震动产生偏差，需要时用PC软件做"偏差校准"(区分±30°内的小偏差可软件滑块调、超过30°的大偏差需拆卸舵机重装) |
| 标定相关工具汇总 | ① 手眼标定工具(hand2cam) ② 定位标定工具`tool.sh`(内含相机标定/运动学/像素定位/深度定位四个子功能) ③ 舵机调试工具`servo_tool` ④ LAB颜色阈值工具 ⑤ PC动作编辑软件(armpi_ultra_control) |

---

## 待你补充/核实的部分（文档未公开或依赖实机）

1. 树莓派系统具体版本号、ROS2 distro名称
2. 实际部署IP、账号（默认AP模式固定 192.168.149.1，账号pi/raspberrypi，但LAN模式下IP是动态的，需自己在APP里查看）
3. 颜色/标签检测节点的具体消息字段名（建议 `ros2 interface show <消息类型>` 直接查看，或 `ros2 topic echo /对应话题` 实测）
4. 舵机扭矩、电流、碰撞检测数据是否可读——按官方文档判断为**不可用**，如舵机本身支持，需通过舵机调试工具单独查询，不在ROS2话题体系里
5. 精确工作空间包络、速度/加速度限制的具体数值
6. 置信度算法、目标丢失/低置信度时视觉节点的具体返回值

建议获取以上信息的方式：SSH登录实机后运行 `ros2 topic list`、`ros2 topic echo`、`ros2 interface show`、`ros2 service list`、`ros2 param list` 等命令直接从系统里读取权威数据，比查文档更准确。

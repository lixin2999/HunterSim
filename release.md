# HunterSim 发布说明

> 本文件采用**追加式**版本记录：新增版本时追加新章节，不修改历史内容。
>
> **版本号与条目规则（自 V3.4.1 起执行）**
> 1. **每次改动都必须新增发布条目**：无论是功能开发还是问题修复，均不得合并进已有版本章节，也不得改写历史内容。
> 2. **功能新增或功能变更**：版本号在**第二位（minor）**递增，第三位归零（例：`3.4.1` → `3.5.0`）。
> 3. **问题修复或功能完善**：版本号在**第三位（patch）**递增（例：`3.4.0` → `3.4.1`）。
> 4. 新条目需同步更新 `pyproject.toml` 的 `version`，并在条目中写明「验证（测试/mypy/flake8）」与「分发」tag。
> 5. 每条发布必须同步更新**文档四件套**：`README.md`（当前版本 + 能力清单）、`user_manual.md`（端点数/面板行为/FAQ）、`deployment_and_operations.md`（升级章节）、`docs/单机部署指南.md`（版本头 + 排障表），缺一不算发版完成。

---

## V2.0.0 — HunterSim 架构重构版

**发布日期**：2026-09-30
**版本定位**：依据《HunterSim_AI辅助开发提示词工程》规范，从旧采集/处理架构**从零重构**为 CARLA + ROS2 + FastAPI 的 VIL/SIL/Replay 虚拟仿真平台。

### 概览

HunterSim 2.0.0 是一次彻底的重写，围绕 **HUNTER SE 自动驾驶底盘车**（820×640×310 mm / 60 kg / 轴距 0.46 m / 后驱阿克曼 / 最大 4.8 m/s）建立三层 C/S 架构：接口层（FastAPI）、服务层（业务编排，可独立单测）、引擎层（CARLA 原子能力封装）。旧架构模块（acquisition / processing / evaluation-legacy / container 等）已整体移除。

### 新增能力

- **三种运行模式**：VIL（车辆在环）、SIL（软件在环 / 场景驱动）、REPLAY（数据回放）。
- **引擎层 `engine/`**：`map_manager`（内置 8 幅 Town 地图 + OpenDRIVE 自定义地图 + 地面高度查询）、`weather_manager`（8 种天气预设与渐变过渡）、`hunter_se_vehicle`（VIL `set_transform` / SIL `apply_control` 双模式控制器）、`vehicle_blueprint_generator`（HUNTER SE 蓝图与传感器安装位）、`coordinate_converter`（CARLA↔ROS↔车辆坐标）、`opendrive_parser`、`carla_server_config`（Windows 服务端启动参数与命令生成）、`check_environment`（GPU/CUDA/驱动/Python 环境校验）。
- **场景运行 `scene_runner/`**：`scene_config`（场景 JSON 契约）、`scene_validator`、`scene_converter`、`scene_state_manager`（状态机）、`event_detector`、`scenario_adapter`、`custom_scenario`。
- **虚实映射 `vil_mapper/`**：`telemetry_buffer`（环形缓冲）、`pose_extrapolator`（位姿外推）、`data_health_monitor`（延迟与健康监测）、`calibration_manager`（初始标定）、`state_visualizer`、`vil_data_consumer`、`vil_sync_service`（同步主循环）。
- **传感器仿真 `sensor_sim/`**：`sensor_factory`、`sensor_mount`、`data_converters`、`topic_mapper`（ROS2 话题映射）、`carla_ros_bridge`、`data_recorder`。
- **交通流 `traffic_sim/`**：`traffic_flow_manager`、`actor_behaviors`、`behavior_tree`、`walker_controller`、`trigger_conditions`。
- **回放与评估 `replay_service/` `eval_service/`**：数据加载、回放引擎、数字孪生；轨迹/舒适性/安全性/覆盖率四维评估与 S/A/B/C/D 评级。
- **资源编排 `resource_manager/`**：`instance_manager`（实例生命周期状态机）、`gpu_resource_pool`（按画质限制并发：low≤8 / medium≤4 / epic≤2）、`health_monitor`。

### 接口层（REST + WebSocket）

统一前缀 `/api/v1/sim`，响应体 `{code, message, data}`，HS256 JWT 鉴权（密钥 `API_JWT_SECRET`）：

- **健康/指标**：`/health`、`/health/live`、`/health/ready`、`/metrics`（Prometheus 文本格式）。
- **实例**：创建/列出/查询/启动/停止/暂停/恢复/销毁。
- **场景**：加载/状态/启动/停止/暂停/恢复/天气/标定/截图。
- **资源**：地图/车辆/天气预设/GPU 状态/用户配额。
- **实时推送**：`WS /ws/sim/{instance_id}/status`（订阅实例状态与遥测）。

### 配置

- `common/models.py` 聚合 pydantic `BaseSettings`，按前缀分组：`HUNTER_SIM_` / `CARLA_` / `KAFKA_` / `VIL_` / `API_` / `RESOURCE_`；优先级 环境变量 > `.env` > 默认值。
- `configs/` 提供 `weather_profiles.json`（8 预设）、`sensor_configs.json`（7 类传感器）、`scene_templates.json`（7 场景模板）、`traffic_config.json`（默认 + 5 档位）。

### 部署与可观测性

- `docker/`：多阶段 `Dockerfile`（setuptools builder + uvicorn/uvloop 运行时）、`docker-compose.yml`（Kafka + 仿真服务 + Prometheus + Grafana）、`prometheus.yml` 抓取 `/api/v1/sim/metrics`、Grafana 数据源自动配置。
- `k8s/`：`namespace` / `configmap` / `secret` / `deployment`（含 liveness/readiness 探针）/ `service`。
- 新增 `requirements.txt` 与修正 `pyproject.toml` 构建后端（`setuptools.build_meta` + `packages.find where=["src"]`）。

### 质量与工程规范

- **测试**：457 项单元 + 集成测试全绿；行覆盖率 **82.56%**（≥80% 门禁），分支覆盖率达标；CARLA/ROS2 依赖通过 `sys.modules` 桩注入，无引擎环境可运行。
- **工具链**：Ruff（lint）、mypy（类型）、pytest + coverage（`fail_under=80`）。
- 移除一次性脚手架脚本、空目录与调试产物；强化 `.gitignore`（Python/打包/虚拟环境/工具缓存/临时产物/仿真大文件）。

### 破坏性变更

- **完全替换 1.x 数据管道架构**：接口、配置模型、模块结构均不向后兼容。
- 依赖要求升级至 **Python 3.12 / CARLA 0.9.16 / ROS2 Humble / Kafka 3.6**。
- 评估/回放 API 语义随新架构重新定义，旧客户端需按 `user_manual.md` 迁移。

### 已知限制

- CARLA 服务端仅原生运行于 Windows；跨平台部署采用「Windows CARLA 主机 + 服务主机」双机方案。
- ROS2 / CARLA 相关引擎路径在无对应运行库时以桩覆盖，端到端联调需在真实环境验证。
- K8s 部署需集群已安装 NVIDIA GPU Operator；`k8s/secret.yaml` 中 `jwt_secret` 为占位值，上线前**必须替换**。

---

## V2.1.0 — 设计文档 V4.0 全面对齐版

**发布日期**：2026-09-30
**版本定位**：依据《Carla 仿真系统详细设计文档》V4.0 第 3–15 章逐条比对落地，补齐标准接口面（§12）、安全设计（§14）、性能目标与监控告警（§13/§15.4）、容器化 CARLA 引擎与 K8s GPU 编排（§15.1/§15.3），并按附录 B 统一地图上传与自定义地图引用契约。

### 接口层（对齐 §12 标准接口）

- **实例子资源标准化**：`GET /instances/{id}/status`（实时状态）、`POST /instances/{id}/scene`（场景下发 §12.4）、`POST /instances/{id}/weather`、`POST /instances/{id}/vil/calibrate`（odom↔map 标定 §12.3，请求体 `map_x/map_y/map_heading` + `odom_*`）、`GET /instances/{id}/screenshot`、`GET /instances/{id}/stream`（WebRTC 视频流地址，`API_STREAM_BASE_URL`）。
- **创建实例响应对齐 §12.2**：返回 `sim_instance_id`、`status`、`carla_server{host, rpc_port, stream_port}`、`stream_url`；请求体支持 `map`（兼容 `map_id`）、`vehicle_model`、大小写不敏感 `quality`、`replay_config` 回放窗口。
- **批量场景测试（§9.5/§11.2）**：新增 `POST /scenarios/batch` 注册多场景任务（后台执行器可注入 `app.state.scenario_batch_executor`），`GET /scenarios/{task_id}/report` 输出通过率与逐场景评估结果。
- 旧 `/scenes/*` 路由保留为兼容入口，内部实现与实例子资源共享。

### 安全设计（§14，全部默认启用）

- **权限分级**：创建/销毁实例要求 JWT 载荷 `role=admin`（缺失默认 `guest`），角色不足返回 `403`（code 1015）。
- **并发配额（§14.2）**：单用户默认 5 个实例（`RESOURCE_MAX_INSTANCES_PER_USER`）、全局 50 个；超限创建返回 `503`，销毁自动释放配额名额。
- **车辆模型白名单（§14.3）**：`vehicle_model` 必须在平台审核白名单（`SUPPORTED_VEHICLES` ∪ `hunter.se`）内，否则 `422`。
- **审计日志**：实例创建/启动/停止/销毁写入 `hunter_sim.audit` 结构化日志（action / user_id / target / detail）。
- **实例守护（§14.1）**：健康监控周期化——探针失败自动重启（≤ `RESOURCE_MAX_RETRY_COUNT` 默认 3 次，耗尽标记不可恢复），超时实例（默认 2h）自动回收；恢复健康后重试计数归零。
- **场景安全审核**：`scene_validator` 新增硬限制——自车速度 > 2× 物理极限（9.6 m/s）或交通参与者 > 200 判定为 error 拒绝加载。
- **地图上传安全**：`map_id` 白名单字符校验（防路径穿越）、文件大小 ≤ 50 MB、入库前 OpenDriveParser 解析审核，不合格即回滚删除。

### 资源仓库与自定义地图（§10.4 / 附录 B）

- `POST /maps/upload` 主形式改为 **multipart/form-data**（`file` + 可选 `map_id`，缺省从文件名推导），保留 JSON `{map_id, content}` 兼容形式。
- 自定义地图统一以 **`custom/{map_name}`** 引用：`GET /maps` 列表、上传响应 `reference` 字段、`MapManager.load_map` 前缀自动从 `resources/maps/custom` 导入并动态加载。
- 新增 `docker/Dockerfile.carla`（§15.3 CARLA 引擎镜像）、`hunter_assets/`（引擎镜像构建上下文占位）。

### 可观测性（§13.1 / §15.4）

- 新增 `GET /health/performance-targets`：输出 §13.1 六项性能目标（FPS≥30、VIL 端到端 <300ms、场景加载 <30s 等）与 §15.4 六项告警阈值。
- `/metrics` 扩展条件输出 gauge：`huntersim_carla_rpc_latency_seconds`、`huntersim_simulation_fps`、`huntersim_vil_latency_ms`、`huntersim_disk_usage_ratio`（由服务注册 `app.state` 后输出）。
- 新增 `docker/alerts.yml`：7 条 Prometheus 告警规则（RPC>5s、GPU 利用率>95% 持续 10min、显存>90%、FPS<20、VIL>500ms、磁盘>85%、实例数异常下降），`prometheus.yml` 已挂载 `rule_files`。

### 部署编排（§15.1 / §15.3）

- `docker-compose.yml` 新增 `carla-engine` profile 服务（NVIDIA Container Toolkit、端口 2000/2001、资源卷挂载）：`--profile carla up -d` 可在 Linux GPU 主机容器化运行 CARLA；`sim-service` 挂载 `resources/` 数据卷。
- `k8s/deployment.yaml` 新增 **CARLA GPU Deployment**（`nvidia.com/gpu: "1"`、`-RenderOffScreen`）与 **ReadWriteMany PVC**（500Gi 共享资源仓库），`sim-service` 挂载同一 PVC。

### 配置新增

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `RESOURCE_MAX_INSTANCES_PER_USER` | `5` | 单用户并发实例配额（§14.2） |
| `RESOURCE_MAX_INSTANCES_TOTAL` | `50` | 全局并发实例上限 |
| `RESOURCE_MAX_RETRY_COUNT` | `3` | 实例异常自动重启上限（§14.1） |
| `RESOURCE_HEALTH_CHECK_INTERVAL_SECONDS` | `10` | 健康巡检周期 |
| `RESOURCE_RESOURCES_DIR` | `resources` | 资源仓库根目录（§10.4.1） |
| `API_STREAM_BASE_URL` | `webrtc://localhost:8080` | WebRTC 视频流基础地址（§12.2） |

### 质量

- **测试**：557 项单元 + 集成测试全绿（较 2.0.0 新增约 100 项，覆盖安全权限、配额、监控周期、multipart 上传、批量场景、性能端点）；CARLA/ROS2 依赖仍通过桩注入，无引擎环境可运行。

### 兼容性说明

- 无破坏性变更：`/scenes/*` 旧路径、`map_id` 字段别名、JSON 上传形式均保留。
- **行为收紧**（升级需注意）：创建/销毁实例需 `role=admin` Token；单用户超过 5 个并发实例创建被拒（`503`）；非白名单 `vehicle_model` 拒绝（`422`）。

---

## V2.1.1 — PR 审查阻塞问题修复版（天气闭环 / 安全加固 / 配额账本）

**发布日期**：2026-10-01
**版本定位**：针对 V2.1.0 PR Review 判定的 A–F 六项阻塞问题逐项修复：打通天气设置到 CARLA 世界的端到端链路，统一天气刻度单一换算出口，收紧上传与数据查询安全边界，并将实例配额释放收敛为单一出口。

### 天气端到端闭环（§3.4.1）

- **天气接口改调真实实现**：`POST /instances/{id}/weather`（兼容 `/scenes/{id}/weather`）改为调用 `WeatherManager` 真实接口——预设分支 `set_preset` / `start_transition`，自定义分支以当前天气为基底合并显式字段后 `set_weather`；未知预设名返回 `422`；`transition_seconds=0` 立即生效；响应新增 `transitioning` 字段。
- **渐变真正生效**：`SceneRunnerService` 装配 `WeatherManager(world)` 并暴露 `weather_manager`，运行循环每 tick 调用 `update_transition()` 推进渐变（实例需处于 `running` 状态）。
- **实际下发 CARLA 世界**：`WeatherManager._apply_profile` 改为实例方法，构造 `carla.WeatherParameters` 并调用 `world.set_weather()`；无 carla 包（`ImportError`）或 CARLA 断连（`RuntimeError`）时记录警告降级，不再静默丢弃。
- **单一换算出口**：`scene_converter` 删除手工 `/100` 归一化，场景 JSON 天气字段与 API 同为 **0–100 统一刻度**，经 `WeatherProfile.to_carla_dict()` 唯一出口换算为 CARLA 参数，杜绝双刻度漂移。

### 安全加固（§10.4 / §14.2）

- **地图上传需 admin**：`POST /maps/upload` 纳入 `role=admin` 权限分级，非管理员返回 `403`（code 1015）。
- **路径穿越双重校验**：`map_id` / 自定义地图名改用 `re.fullmatch`（封堵尾换行绕过 `$` 锚的漏洞）；`MapManager` 加载 `custom/{name}` 追加 `resolve().is_relative_to()` 目录越界检查，非法名直接拒绝。
- **回放数据查询防注入**：`data_loader` PostgreSQL 路径引入表名白名单（`vehicle_telemetry`/`scene_events`）+ `psycopg2.sql.Identifier` 参数化表名；数据库连接改为 `try/finally` 确保关闭，消除连接泄漏。

### 配额账本闭环（§14.2）

- 配额释放收敛至 `SimInstanceManager.destroy_instance()` 唯一出口（锁内移除实例、锁外释放配额，无死锁环），同时覆盖 DELETE API、超时回收（§14.1）与异常销毁全部路径，杜绝漏放与重复释放。
- 创建实例失败路径补全：GPU/配额 `ResourceError` → 释放预留配额并返回 `503`；其他异常 → 释放配额后原样抛出。

### 质量

- **测试**：593 项单元 + 集成测试全绿（较 2.1.0 新增约 36 项：`world.set_weather` 下发断言、渐变/立即生效分支、未知预设 422、表名白名单、路径穿越参数化、上传 admin 403，以及建议级 G–T 行为断言如边沿闩锁、端口递增、触发帧时间基准、步长推导、任务归属 404 等）。
- **质量门禁**：mypy strict + flake8 零违规（`setup.cfg` 统一配置），覆盖率 `fail_under=80`。
- **测试桩对齐**：天气相关测试桩方法名与真实 `WeatherManager` 接口严格一致（`set_preset`/`set_weather`/`start_transition`/`current_profile`），并通过伪 `carla` 模块注入验证天气真正下发到世界。

### 行为变更（升级注意）

- `POST /maps/upload` 需 `role=admin` Token（原任意登录角色可上传）。
- 未知天气预设名返回 `422`（不再静默接受）。
- 天气渐变仅在场景运行循环推进（实例 `running`）期间生效。
- 批量任务报告查询增加归属校验，非属主 Token 查看他人任务返回 `404`。
- CARLA 启动参数：显卡选择由 `-gpu=` 改为 `-graphicsadapter=`；`-benchmark` 与同步定步模式互斥（默认关闭）。

### PR 审查建议级问题修复（G–T）

- **仿真行为正确性**：脚本化 Actor 行为改以触发时刻为时间基准（触发后相对时间，修复 cut_in 等延迟触发行为首帧进度跳变）；角速度单位边界规范化（内部 rad/s，CARLA `set_angular_velocity` 边界换算 deg/s，原直传差 57.3 倍）。
- **事件与日志防洪泛**：事件检测器改边沿触发（条件持续成立仅上报一次）+ 有界缓冲（默认 1000 条）；可视化绘制失败日志由 DEBUG 静吞改为限流 WARNING（同一上下文 30s 一条）。
- **降级健壮性**：无 carla 包环境 VIL 同步循环惰性导入降级，不再逐 tick 抛 `ModuleNotFoundError` 累加同步错误计数。
- **并发与资源回收**：健康监控共享集合全面加锁，过期/销毁实例自动注销，后台循环异常不再静默终止线程；批量场景任务表有界（1000）+ 锁内状态写 + 任务归属隔离（非创建者查看报告 404）。
- **生产装配主线打通**：`lifespan` 集中装配 `gpu_pool / instance_manager / scene_runners / map_manager / health_monitor`（only-if-absent，不覆盖测试/外部注入），`/health`、`/metrics`、实例与资源端点不再仅测试路径可达；CARLA 连通性/活跃实例数由健康监控周期刷新。
- **CARLA 启动与端口**：显卡选择参数修正为 `-graphicsadapter=`（旧 `-gpu=` 在 0.9.16 引擎不生效）；`-benchmark` 与同步定步模式互斥改为默认关闭；多实例 CARLA RPC/流端口按空闲对递增分配，避免 load_world 互相重置。
- **API 响应性**：场景加载/停止/天气/截图等含阻塞操作的路由改走 `asyncio.to_thread`，不再阻塞事件循环。
- **评估引擎**：帧步长从数据时间戳推导（中位数，回退 50Hz），不再三处硬编码 0.02；时间字段兼容规范名 `time_stamp` 与录制名 `timestamp`。
- **部署配置**：K8s 共享 PVC 显式指定 RWX `storageClassName`；`carla-engine` 改单副本（多副本会负载均衡到错误引擎）并新增 `carla-engine-svc` 集群内入口；Prometheus 补 `alerting` 段并新增 Alertmanager 服务与最小路由配置（`docker/alertmanager.yml`）；`Dockerfile.carla` 移除冗余 fastapi/uvicorn/kafka-python 依赖。

### 迁移说明（破坏性变更，升级前必读）

以下变更随 2.1.x 逐步引入，旧客户端/旧部署配置升级时需逐项确认：

1. **Kafka 消费组切换**：`KAFKA_GROUP_ID` 默认值由 `hunter_sim_consumer` 改为 `carla-vil`（对齐设计文档 §4.2.2）。切换后旧组 offset 不再继承（默认 `latest` 不重放）；多副本共用默认组会触发分区再平衡，生产建议按部署显式设置。
2. **`POST /instances` 响应体替换**：返回体改为标准 `InstanceResponse` 结构（`{code,message,data}` 包裹，data 含 `sim_instance_id/status/gpu_id/carla_host/carla_rpc_port` 等）；旧半结构化 dict 字段不再兼容。
3. **VIL 标定请求体改名**：`CalibrationRequest` 采用 `map_x / map_y / map_heading` + `odom_x / odom_y / odom_heading`（heading 为弧度）；旧字段名（`origin_*` 类）不再接受，缺失返回 422。
4. **交通配置改名**：`ignore_lane_change_rate`（比例 0–1）改为 `allow_lane_change`（布尔，默认 `true`）；`configs/traffic_config.json` 旧字段需手工替换，未知字段会被 pydantic 校验拒绝。
5. **ROS 话题前缀统一**：仿真侧话题统一为 `/carla/*`（如 `/carla/lidar_points`、`/carla/rgb_front/color/image_raw`），经 `TopicMapper` 映射实车话题；旧订阅/发布脚本需同步改话题名或改用映射接口。
6. **CARLA 启动参数变更**：自写启动脚本中 `-gpu=` 应改 `-graphicsadapter=`；如依赖 `-benchmark`，需确认运行于异步模式（同步定步下两者互斥）。

---

## V2.1.2 — CARLA 连通性可诊断 / `.env` 前缀配置修复

**发布日期**：2026-10-21
**版本定位**：修复实机部署中“CARLA 服务已在运行，`/health` 却长期 `degraded` 且响应不含任何原因”的问题——补齐探测失败原因链，区分“SDK 未安装 / 客户端初始化失败 / 目标不可达”；同时修复 `.env` 对 `CARLA_*/KAFKA_*/VIL_*/API_*/RESOURCE_*` 前缀既可能启动失败、也可能完全不生效的配置缺陷。

### 现场（实机现象）

- 服务主机 Windows（`192.168.1.103`，源码部署于 `D:\python\HunterSim`）：`CarlaUE4.exe` 正常运行，`Test-NetConnection 127.0.0.1 -Port 2000` 可通，`carla.Client(...).get_server_version()` 可返回版本。
- 但 `GET /health` 持续返回 `{"status":"degraded","carla_connected":false}`，`/health/ready` 为 `code=503`，`/metrics` 中 `huntersim_carla_connected 0`，响应体中没有任何原因说明。
- 服务进程由 `D:\python\HunterSim\.venv\Scripts\python.exe` 启动；验证 CARLA 用的解释器则是系统 Python（`...\Python312\Lib\site-packages\carla`）。

### 根因（已定位，实机复测见待办）

1. **解释器不一致**：服务跑在项目 venv，CARLA PythonAPI 安装在系统解释器；venv 内 `import carla` 失败 → `_assemble_services` 走 `ImportError` 降级分支，`carla_client=None`。
2. **不可自愈**：CARLA 客户端仅在 lifespan 装配一次，`_probe_carla(None)` 恒返回 `False`；CARLA 随后就绪也不会恢复，必须重启服务。
3. **无诊断输出**：`_probe_carla` 吞掉异常，不记录 `host:port` 与原因，`/health` 也不暴露，只能靠翻启动日志与猜测。
4. **`.env` 配置缺陷**：`.env` 写 `CARLA_HOST` 会被 `HunterSimSettings` 的 `extra=forbid` 判为非法键抛 `ValidationError`；嵌套分组未配置 `env_file`，文档宣称的 `.env` 优先级对上述前缀不成立。

### 修复内容

- **探测可诊断**：`_probe_carla` 改为返回 `(是否连通, 失败摘要)`；新增 `_refresh_carla_state` 统一刷新 `app.state.carla_connected / carla_probe_error / carla_probe_evaluated`，仅在**首次评估与状态翻转**时记录日志（含 `host:port` 与异常摘要，持续不可达不刷屏），失败摘要截断至 200 字符。
- **`/health` 新增字段**：`carla_sdk_installed`（服务解释器是否可导入 carla SDK）、`carla_probe_error`（探测目标 + 异常摘要，连通时为 `null`）；启动期即写入“SDK 未安装 / 客户端初始化失败”结论，无需等首个巡检周期。
- **启动健壮性**：`import carla` 成功但 `carla.Client(...)` 构造失败时不再阻断服务启动，降级为可诊断状态并记录 error 日志。
- **`.env` 前缀配置**：`CarlaSettings / KafkaSettings / VILSettings / APISettings / ResourceSettings` 增加 `env_file=".env"`，与 `HunterSimSettings` 统一 `extra="ignore"`；`.env` 中其他前缀与未知键不再触发校验错误，真实环境变量仍优先于 `.env`。

### 向后兼容

- `/health` 仅新增字段，`status / carla_connected / uptime_seconds / active_instances` 语义与取值不变。
- 配置读取由“部分键直接报错 / 部分前缀静默忽略”改为“按前缀生效”，环境变量优先级不变。

### 风险

- 若部署目录 `.env` 曾残留无效的 `CARLA_*` 配置，升级后将开始生效，需复核。
- 探测日志仅在首次评估与状态翻转时输出，长期断连不会每 10s 刷屏。

### 验证

- 新增单测：探测成功清空错误、失败记录 `host:port` 与异常类型、无 SDK 时给出安装提示、`/health` 暴露 `carla_probe_error`、`.env` 嵌套前缀生效 / 环境变量优先 / 未知键不崩溃。
- 质量门禁：`pytest` 601 项全绿；`mypy --strict` 71 个源文件无问题；`flake8` 无告警。

### 实机执行

- 待办（尚未在实机验证）：把 CARLA PythonAPI 装进**服务 venv** 后重启，复测 `/health` 是否转 `healthy`。

### 分发

- 镜像 tag 提升为 `hunter-sim:2.1.2`（`k8s/deployment.yaml`、部署手册已同步）；源码部署需同步 `src/`、`tests/` 并重启 uvicorn（`--workers 1`）。

### 待办

- [ ] `D:\python\HunterSim\.venv\Scripts\python.exe -m pip install "carla==0.9.16"`，用同一解释器验证 `get_server_version()`。
- [ ] 重启服务后复查：预期一个巡检周期（默认 10s）内 `carla_connected=true`、`carla_probe_error=null`、`/health/ready` 返回 `code=0`。
- [ ] （可选）后续版本增加“运行时补装 SDK 后自动重试导入”，避免必须重启。

---

## V2.2.0 — Windows 桌面仿真客户端 / ScenarioRunner / Windows Docker 编排 / HunterCore 接入

**发布日期**：2026-10-21
**版本定位**：面向 **HUNTER SE 车辆在环（VIL）/ 软件在环（SIL）** 实机测试训练，新增 **PySide6 原生 Windows 桌面仿真客户端**（对标 CarlaSceneX，进程内直连 CARLA 与 ScenarioRunner）、**Windows 单机全容器编排**（Docker Desktop + WSL2 + NVIDIA Container Toolkit，含 ROS2 Bridge 容器）与 **HunterCore 数据采集分析平台接入**（Kafka 遥测 Topic + MinIO bag 预签名直传，`192.168.31.35`）。纯新增能力，**向后兼容**，不改动既有 REST/服务层行为。

### 新增内容

- **配置模型（`common/models.py`）**：新增 `CloudSettings`（`env_prefix="HUNTER_CORE_"`：`enabled`/`vehicle_id`/`kafka_bootstrap`/`security_protocol`/`sasl_mechanism`/`sasl_username`/`sasl_password`/`ca_cert_path`/`telemetry_interval_s`/`telemetry_topic`/`event_topic`/`health_topic`/`gateway_url`/`jwt_token`/`rosbag_bucket`/`upload_timeout_s`）与 `GuiSettings`（`env_prefix="GUI_"`：`theme`/`default_map`/`stream_fps`/`scenario_runner_path`），聚合进 `HunterSimSettings.cloud` / `.gui`（`default_factory`）。
- **HunterCore 接入 `cloud/`（新包，非界面逻辑无 Qt/无引擎依赖）**：
  - `telemetry_mapper`：`VehicleState`/`Transform` → `telemetry.schema.json` 载荷（仅 required 六段，`additionalProperties=false`，heading 归一 [-π,π]），并提供 `build_health` / `build_event`（事件等级按 EVENT_LEVEL_BY_TYPE 自动映射，未知类型抛 `ValueError`）。
  - `telemetry_producer`：kafka-python `KafkaProducer`（SASL_SSL/SCRAM-SHA-512，key=vehicle_id，topic 模板 `{vehicle_id}` 解析），发送失败入有界缓冲（maxlen=1000），`flush()` 尽力重投；未启用/依赖缺失/构造失败整体降级 no-op，不阻断仿真。
  - `bag_uploader`：`httpx` 调 data-collector `POST /api/v1/data/files/presign` → 单次 PUT / 分片 PUT（>32MiB）→ `POST /api/v1/data/files/complete`（流式 size/md5/sha256）；解统一响应体 `{code,message,data}`，`code≠0` 抛 `UploadError`；缺 JWT/未启用降级 no-op。
  - `connector`：`CloudConnector` 聚合 producer+uploader，`start/stop/publish_state/publish_event/publish_health/upload_bag`；`publish_state` 按 `telemetry_interval_s` 节流。
- **桌面客户端 `gui/`（新包，PySide6 可选依赖，惰性导入）**：
  - `scenario_launcher`（无 Qt）：`ScenarioRunnerLauncher` 构建并管理官方 `scenario_runner.py` 子进程（`--host/--port/--trafficManagerPort/--timeout/[--sync]/--openscenario|--scenario/--waitForEgo`），`start/stop(terminate→kill)/read_output/is_running`，与既有 `scene_runner.scenario_adapter` 并存。
  - `sim_presets` / `camera_bridge` / `theme`（无 Qt）：传感器蓝图与安装位预置表、CARLA BGRA→RGB 帧转换（交换 R/B 通道 + 行序翻正）、深/浅 QSS 主题。
  - `sim_worker`：`SimWorker(QThread)` 独占 `carla.Client`，命令队列 + world tick 循环，所有阻塞 CARLA 调用仅在本线程；Qt 信号回推画面/状态/日志。
  - 面板与视图：连接/地图/Actor/传感器/控制/场景面板 + CARLA LIVE STREAM 画面 + 带时间戳日志；`main_window` 装配（QSplitter 左控制右画面+日志 + 状态栏 + 键盘 Manual 控制）；`app.main()` 入口（`python -m hunter_sim.gui.app` 或 `hunter-sim-gui`），无 PySide6 时提示而非崩溃。
- **Windows Docker 编排**：新增 `docker/docker-compose.windows.yml`（`carla-server` GPU 离屏 :2000/:2001/:8000 + `ros2-bridge` + `sim-management` :8080 + `kafka`）；既有 `docker/docker-compose.yml` 增补 `ros2-bridge`（profile `ros`），默认编排不变。
- **打包**：`pyproject.toml` 版本 2.2.0，新增 extras `gui`（PySide6）/`cloud`，`[project.scripts] hunter-sim-gui`，mypy overrides 增补 `PySide6.*`/`scenario_runner.*` 与 Qt-only 界面模块 `ignore_errors`，coverage 排除 Qt 界面组件；`requirements.txt` 增补可选依赖说明。

### 向后兼容

- 全为新增：`cloud/`、`gui/` 新包，`CloudSettings`/`GuiSettings` 默认 `enabled=false`/深色，不影响无 Qt/无 CARLA/未接入环境下的 `import hunter_sim` 与既有 REST/服务层行为。
- `scenario_adapter.py` 因新增 `scenario_runner.*` mypy override，移除一处冗余 `# type: ignore[import-not-found]`（运行行为不变）。

### 风险

- CARLA 官方镜像为 Linux，Windows 全容器需 WSL2 + NVIDIA Container Toolkit GPU 直通；桌面客户端与 `sim-management` 服务各自独立连 CARLA，同一 server 建议单控制端以避免并发写冲突。
- HunterCore 接入按契约命名 `hunter.{vehicle_id}.*` 与 data-collector presign/complete，不新增 Topic/字段；实机连通（Kafka SASL/MinIO/JWT）依赖 HunterCore 侧账号与 Topic 已开通。

### 验证

- 新增单测：`test_cloud_telemetry_mapper`（required 字段/无多余键/heading 归一/事件等级）、`test_cloud_producer`（topic 解析/key/缓冲重投/no-op）、`test_bag_uploader`（presign→PUT→complete/分片/`code≠0`/缺 JWT 降级）、`test_cloud_connector`（聚合/节流/no-op）、`test_scenario_launcher`（命令行构建/生命周期打桩）、`test_gui_logic`（预置位姿/分辨率→蓝图映射/BGRA→RGB/主题），扩展 `test_settings_env`（`HUNTER_CORE_`/`GUI_` 前缀生效 + 未知键忽略）。
- 质量门禁：`pytest` **635 项全绿**，覆盖率 **80.70%**（≥80 门禁）；`mypy src` 无问题（95 源文件）；`flake8 src tests` 无告警。

### 实机执行（待办，尚未实机验证）

- [ ] `pip install -e ".[gui]"` 并安装 CARLA 0.9.16 同版本 wheel，`hunter-sim-gui` 启动冒烟：连接本机/局域网 CARLA，加载 Town03、生成自车、挂载 RGB 相机出画面、Spawn/Manual(WASD)/Autopilot、启动 ScenarioRunner `.xosc`。
- [ ] `docker compose -f docker/docker-compose.windows.yml up -d`（Docker Desktop WSL2 + NVIDIA Container Toolkit）复测 `carla-server` GPU 直通与 `ros2-bridge` 话题桥接。
- [ ] 配置 `HUNTER_CORE_*` 凭据，验证 Kafka `SASL_SSL/SCRAM` 连通、遥测三 Topic 落数、bag presign→complete 成功。

### 分发

- 镜像 tag 提升 `hunter-sim:2.2.0`；桌面客户端随源码分发，`pip install -e ".[gui]"` 后 `hunter-sim-gui` 启动；`docker/docker-compose.windows.yml` 为 Windows 单机验证拓扑（非生产多并发编排）。

---

## V2.2.1 — 代码审查修复专题（桌面客户端实时画面 / 同步与线程生命周期 / HunterCore 遥测与上传健壮性）

**发布日期**：2026-10-21
**版本定位**：针对 V2.2.0 代码审查（CodeReview）判定的阻塞合入项（A-1/B-1/B-2/B-3/B-4/C-1/C-2/C-3）逐项修复，聚焦**桌面客户端实时画面真机可用性**、**同步模式与线程生命周期健壮性**、**HunterCore 遥测/上传链路的非阻塞与失败回捞**。纯缺陷修复，不改 V2.2.0 对外契约与 REST/服务层行为，**向后兼容**（`upload_bag` 返回类型变更见行为变更）。其中部分项（raw_data 取数、画面方向）需 CARLA 0.9.16 真机冒烟才能最终确认，已列入实机待办。

### 实时画面链路（`gui/camera_bridge` / `gui/sim_worker`）

- **A-1 `image_to_rgb_array` 取数属性名**：改为 `getattr(carla_image, "raw_data", None)` 优先取官方 `carla.Image.raw_data`，缺失时回退 `.data`（兼容旧版/桩）；真机不再因属性名不存在而 `AttributeError`（原被 `_on_frame` 的 `try/except` 吞掉，表现为永不出图 + 日志风暴）。
- **B-1 相机回调传 sensor id**：`carla.Image` 不携带 actor id，原 `sensor.listen(self._on_frame)` + `image.actor_id` 真机必抛 `AttributeError`。改为闭包绑定 `sensor.listen(lambda image, sid=sensor.id: self._on_frame(sid, image))`，`_on_frame` 增 `sensor_id` 形参并经 `frameReady(int, object)` 正确发射（与 `StreamView.on_frame` 签名对齐）。

### 同步模式与线程生命周期（`gui/sim_worker` / `gui/main_window`）

- **B-2 关闭同步模式**：`_do_set_sync` 中 `WorldSettings.fixed_delta_seconds` 由 `None` 改为 `0.0`——CARLA pybind `float` 字段赋 `None` 会抛 `TypeError` 致 `apply_settings` 不执行、世界冻结；`0.0` 为官方“变步长”语义。
- **B-3 同步模式单一事实源**：抽出 `_apply_sync_settings()`，在 `_do_connect`/`_do_load_map`/`_do_import_xodr`/`_do_set_sync` 统一按 `_sync_mode` 标志下发 `synchronous_mode`+`fixed_delta_seconds`——修复 `load_world`/`generate_opendrive_world` 返回新 world 重置为异步后标志与 world 长期脱节。
- **B-4 `closeEvent` 线程竞态**：`_worker.wait(3000)` 超时则 `event.ignore()` 并提示稍后重试，避免在运行中的 QThread 上析构导致 `QThread: Destroyed while thread is still running` / 回调向已销毁 QObject 发信号崩溃。

### HunterCore 遥测/上传健壮性（`cloud/connector` / `cloud/telemetry_producer`）

- **C-1 异常收口**：`CloudConnector.publish_event` 统一 `try/except`，未知 `event_type` 映射失败时记 WARN 返回 `False`；`upload_bag` 异常在上传线程内由 `_perform_upload` 收口为 WARN，绝不冒泡阻断仿真主循环。
- **C-2 `upload_bag` 非阻塞化**：大文件的哈希+presign+PUT 移入后台守护线程（队列 + 惰性幂等启动，`stop()` 发终止哨兵并 `join`）；`upload_bag` 改为 fire-and-forget，调用线程（GUI/仿真）不再被长时 IO 卡住。**重启安全**：`stop()` 置空 `_upload_thread/_upload_q/_upload_stop`，`_ensure_upload_worker()` 重启时分配**全新 Event**，`stop→start` 后新 worker 不会因旧 Event 已置位而立即退出。**并发串行化**：`_upload_exec_lock` 保证任意时刻至多一次在途 `uploader.upload`，即便 `join(3.0)` 超时导致旧收尾线程与新线程短暂并存也杜绝并发双上传。
- **C-3 Kafka 异步失败回捞**：`_send` 捕获 `send()` 返回的 Future 并挂 `add_errback`，broker 不可达/SASL 失败/投递超时等异步错误经回调重入有界缓冲（不再静默丢失）；KafkaProducer 配置增 `max_block_ms=1000`/`delivery_timeout_ms=5000` 限定 send 对调用线程最大阻塞。桩生产者无 `add_errback` 时自动跳过，兼容既有单测。

### 行为变更（升级注意）

- `CloudConnector.upload_bag` 返回类型由 `Optional[Any]` 改为 `None`（fire-and-forget，上传结果仅记日志，不再同步返回）；尚无生产调用点，不影响现有代码。

### 质量

- **测试**：`pytest` **640 项全绿**（较 V2.2.0 新增 5 项：`test_publish_event_swallows_exception`/`test_upload_bag_swallows_exception`（异常收口）、`test_upload_bag_runs_in_background`（后台执行）、`test_upload_worker_restarts_after_stop`（重启安全）、`test_async_send_error_rebuffers`（errback 回捞））。
- **质量门禁**：覆盖率 **81%**（≥80 门禁，较 V2.2.0 的 80.70% 因新增连接器/生产者后台路径用例而上）；`mypy src` 无问题（95 源文件）；`flake8 src tests` 无告警。

### 实机执行（待办，尚未实机验证）

- [ ] 挂 RGB 相机一次真实冒烟：确认 `raw_data` 取数与画面方向/通道正确，据此决定 `camera_bridge` 行序纵向翻转（A-3）**去留**（本次未盲改）。
- [ ] 云端回传链路尚未接入 `SimWorker` 主循环（当前 `publish_state` 需 `VehicleState`，worker 侧为裸 dict，需新增装配）。
- [ ] 本地 `docker-compose.windows.yml` 的 Kafka 为 PLAINTEXT 而 `CloudSettings` 默认 `SASL_SSL`，默认组合下会静默降级 no-op；需 `.env`/compose 显式对齐（`HUNTER_CORE_SECURITY_PROTOCOL=PLAINTEXT`、`HUNTER_CORE_KAFKA_BOOTSTRAP=kafka:9092`）。

### 分发

- `pyproject.toml` 版本提升 `2.2.1`，镜像 tag `hunter-sim:2.2.1`。仅含代码修复，无新增能力/依赖；从 V2.2.0 升级可直接替换。

---

## V2.2.2 — 桌面客户端启动崩溃修复（`map_panel` QComboBox API 误用）+ 应用版本动态化

**发布日期**：2026-10-21
**版本定位**：修复自 V2.2.0 起桌面客户端启动即崩溃的 GUI 缺陷。因 `gui/*` 被排除在单测/mypy/coverage 之外（`pyproject.toml` 中 Qt 界面组件 `ignore_errors` + coverage omit），该错误仅在真机 `python -m hunter_sim.gui.app` 才暴露。纯缺陷修复，**向后兼容**。

### 桌面客户端启动崩溃（`gui/panels/map_panel`）

- **现象**：`MainWindow._build_panels` 构造 `MapPanel` 时抛 `AttributeError: 'PySide6.QtWidgets.QComboBox' object has no attribute 'addSortedItems'`，客户端启动即退出。
- **根因**：`QComboBox` 从不存在 `addSortedItems` 方法（Qt/PySide6 未提供此 API）；待插入列表已由 `sorted(BUILTIN_MAPS)` 排好序，属臆造方法名。
- **修复**：`map_panel.py` 改用 `self._combo.addItems(sorted(BUILTIN_MAPS))`。已逐一复核其余全部 GUI 组件（`connection/map/actor/sensor/control/scenario` 面板、`stream_view`、`log_console`、`main_window`）的 Qt API 调用均为合法方法，无同类臆造问题。

### 应用版本动态化（`api/main`）

- FastAPI `app` 的 `version` 原硬编码 `"2.1.2"`，与 `pyproject.toml` 长期漂移；改为经 `importlib.metadata.version("hunter-sim")` 动态读取，包未安装时降级 `"0.0.0+unknown"`。`/health`、OpenAPI 文档版本随包元数据保持一致，后续升版无需再手改此处。

### Docker 编排可用性修正（`docker/docker-compose*.yml`，同日并入）

> 仅变更 docker 编排配置与文档，**未动 Python 源码**；故 `pyproject.toml` 与 `hunter-sim` 镜像 tag 保持 `2.2.2` 不变，不另起包版本。

- **Kafka 镜像失效**：`bitnami/kafka:3.6` 因 Bitnami 将 `docker.io/bitnami/*` 旧 tag 从 Docker Hub 免费目录下架（Nov 2024 迁移）而 `not found`；两份 compose 均改用内容一致的 `bitnamilegacy/kafka:3.6`（`KAFKA_CFG_*` 环境变量与 `kafka-topics.sh` healthcheck 无需改）。
- **移除 obsolete `version: "3.9"`**：Compose v2 已忽略该字段并告警；两份 compose 删除该行（文件早已使用 `profiles`/healthcheck `condition` 等 v2-only 特性，本就仅适用 v2）。
- **windows 栈本地镜像未构建提示**：`docker-compose.windows.yml` 头注与部署手册§4.1 明确 `carla-server`/`sim-management` 为本地构建镜像，首次需 `up -d --build`（并先构建 `hunter-carla`）。
- **新增故障排查条目**（§8.2）：docker.sock 权限（`usermod -aG docker`）、registry mirror 拒服/缺库（换稳定 mirror 或直连 Docker Hub）、`bitnami/kafka not found`、windows 栈本地镜像 `not found`。

### 质量

- **测试**：`pytest` **640 项全绿**（GUI 界面组件不在单测覆盖内；本项修复由用户真机 traceback 定位、Qt API 语义确证，非盲改）。`mypy src` 无问题（95 源文件）；`flake8 src tests` 无告警；覆盖率 **81%**。
- **编排配置**：两份 compose 经 PyYAML 解析验证通过（`version` 字段已移除、Kafka 镜像为 `bitnamilegacy/kafka:3.6`）；镜像实际可拉性依赖用户环境的 registry mirror/凭证（未在本机跑 docker 验证）。

### 实机执行（待办，尚未实机验证）

- [ ] 在装有 PySide6 + CARLA 0.9.16 的机器完整启动客户端，走完 Connect→Load Map→Spawn→Attach→Start，确认修复后无后续 GUI 步骤崩溃（本次仅确证到原崩溃点之前的组件构造，崩溃点之后的交互需真机走一遍）。
- [ ] 在实际部署主机验证 `docker compose -f docker/docker-compose.yml up -d` 能成功拉起 `bitnamilegacy/kafka:3.6`（本修改基于用户日志中已拉取成功的事实，未在本机 docker 环境验证）；windows 栈需 `up -d --build` + 预先 `docker build -f docker/Dockerfile.carla -t hunter-carla:0.9.16 .`。

### 分发

- `pyproject.toml` 版本提升 `2.2.2`，镜像 tag `hunter-sim:2.2.2`；桌面客户端随源码分发，`pip install -e ".[gui]"` 后 `hunter-sim-gui` 启动。纯修复，向后兼容，从 V2.2.1 可直接替换。

---

## V2.3.0 — 阶段一「可用性止血」：REST 场景闭环 + 安全基线

**发布日期**：2026-10-22
**版本定位**：落地系统评审报告阶段一（P0）。目标是让 `docker compose up -d` 之后，**仅通过 REST 就能完整拉起并跑完一个场景**：获取 Token → 创建实例 → 下发场景（真实加载地图、生成自车）→ 启动 → 查询进度 → 停止/销毁；同时补齐鉴权、密钥闸门、日志级别与指标等「可用性/安全速赢」。向后兼容（行为变更见下）。

### 控制面与执行面闭环（`api/runtime` / `scene_runner`）

- **新增 `api/runtime.py`**：实例首次下发场景/启动时，按需装配 `SceneRunnerService`——`load_map` → 切换同步模式（50Hz）→ 创建运行器 → 登记到 `app.state.scene_runners`；实例状态自动 `CREATED→LOADING→READY`。此前 `scene_runners` 永远为空，所有 `/scenes/*` 与 `/instances/{id}/scene` 请求均报 404。
- **新增 `scene_runner/builtin_scenario.py`**：把 `SceneConfig` 真正落到 CARLA —— 按 `ego_vehicle` 生成自车（蓝图名自动补全 `vehicle.` 前缀、按地图车道推算高度、`autopilot` 交由 Traffic Manager）、生成交通参与者（车辆非 static 行为启用 autopilot，行人/骑行者先静态生成）、按 `scene_events` 挂载碰撞/车道偏离传感器并回调 `EventDetector`。单个参与者失败不阻断整场景；同一运行器重复下发会先回收旧 Actor。
- **自车蓝图回退**：标准 CARLA 0.9.16 不含 `vehicle.hunter_se` 资产，且 `BlueprintLibrary.find` 对不存在的 ID 抛 `IndexError` 而非返回 `None`。现统一捕获并按 `vehicle.micro.mpc.gen_01` → `vehicle.tesla.model3` → `vehicle.*` 顺序回退（WARNING 日志），保证「未部署 HUNTER SE 蓝图」时默认示例仍能生成自车。
- **`SceneRunnerService` 增强**：默认使用内置场景；下发时应用场景天气（预设名或自定义参数）；`start()` 重入保护（重复启动返回 409，防双线程 tick 同一 world）；运行时长达到 `duration_seconds` 正常转 `COMPLETED`（超时仅作兜底 `FAILED`）；`_cleanup` 幂等，`stop/reload` 不再残留 Actor。
- **实例路由打通**：`create` 响应新增 `next_step`；`start/stop/pause/resume/status` 与实际运行器联动（start 未下发场景且 CARLA 就绪时返回 409；stop/destroy 先释放运行器再流转状态；status 附带场景进度）；`GET /instances/{id}/stream` 返回 CARLA 连接信息。
- **资源回收**：健康巡检的超时回收、服务关停都会释放运行器（停 tick 线程 + 销毁 Actor）；CARLA 未就绪时 `start` 降级为纯状态流转（响应 `scene_runner=unavailable`），服务本身仍可用。
- **单引擎单场景约束**：一个 CARLA server 同一时刻只允许一个实例持有运行器，跨地图或重复占用返回 503 并给出处置指引，避免 `load_world` 重置他人世界。

### 安全基线

- **新增 `POST /api/v1/sim/auth/token`**：用 `API_ADMIN_USERNAME` / `API_ADMIN_PASSWORD` 换取 JWT（HS256、`role=admin`、常量时间比较、非 ASCII 凭据不报 500）。此前该路径只在白名单与数据模型里存在，没有实现，接入方拿不到 Token 形成死循环。
- **生产启动闸门**：`HUNTER_SIM_ENV=prod` 时，默认/过短 JWT 密钥、CORS `*`、默认管理员口令 `admin` 任一命中直接拒绝启动。
- **WebSocket 握手鉴权**：`/ws/sim/{id}/status` 支持 `?token=<JWT>` 或 `Authorization: Bearer`，校验实例归属；缺失/非法令牌以 4401 关闭，越权以 4403 关闭。
- **实例级越权（IDOR）修复**：实例详情/启动/停止/暂停/恢复/状态/视频流/场景子资源均校验归属，非本人实例仅 `admin` 可操作（403）。
- **日志级别生效**：`HUNTER_SIM_LOG_LEVEL` 通过 `set_default_log_level` 同步已创建的 `hunter_sim.*` logger，不再被模块内 `setLevel(INFO)` 覆盖。
- **指标落地**：巡检周期写入 `carla_rpc_latency_seconds`、`disk_usage_ratio`，由 `/metrics` 条件输出；`/health` 的 `carla_probe_error` 可直接用于诊断 503。

### 部署闭环（Docker）

- **镜像内置 CARLA SDK**：`docker/Dockerfile` 默认安装 `carla==0.9.16`（`ARG CARLA_VERSION` / `INSTALL_CARLA` 可覆盖），容器内 sim-service 可直接连接宿主机 CARLA 并生成 Actor；无 manylinux wheel 的平台可置 `INSTALL_CARLA=false`（服务可启动，下发场景返回 503）。
- **`docker-compose.yml`**：`sim-service` 支持 `.env` 变量插值（密钥/管理员口令/CORS/CARLA 地址/Kafka），新增 `hunter-sim:2.3.0` 镜像名与 build args；Kafka 健康检查增加 `start_period` 与重试次数，缓解首次启动误判。
- **K8s 清单对齐 prod 闸门**：`deployment.yaml` 注入 `API_ADMIN_USERNAME` / `API_ADMIN_PASSWORD`（`hunter-sim-secrets`）与 `API_CORS_ALLOW_ORIGINS`（`hunter-sim-config`），避免 `HUNTER_SIM_ENV=prod` 因默认口令/CORS 被闸门拒绝启动。
- **新增根目录 `.env.example`**：开发默认值可直接使用，生产必须替换（闸门会拦截）。
- **rosbag 录制**：`DataRecorder` 子进程输出改为 `DEVNULL`，避免长时间录制写满管道缓冲区阻塞 `ros2 bag record`。

### 行为变更（升级注意）

- `POST /instances/{id}/start`：CARLA 就绪但**未下发场景**时返回 409（此前会直接置 RUNNING 而没有场景）；CARLA 未就绪仍返回 200 并标 `scene_runner=unavailable`。
- `POST /instances/{id}/scene`：CARLA 不可用时返回 503（附诊断原因与处置指引），地图与实例不一致返回 422。
- 同一 CARLA 引擎同一时刻只能有一个实例持有场景运行器（单引擎单场景）；第二个实例下发场景返回 503。
- WebSocket 订阅必须携带 JWT；旧的无令牌客户端会被 4401 拒绝。
- `stop` 后实例进入终态 `COMPLETED`，如需再次运行请新建实例（与状态机语义一致）。
- `HUNTER_SIM_ENV=prod` 的启动条件变严（见上）。

### 质量

- `pytest` **675 项全绿**（较 V2.2.2 新增 33 项）：`test_builtin_scenario`（自车/参与者/事件传感器/幂等清理/时长完成/蓝图缺失回退）、`test_runtime`（按需装配/复用/地图冲突/引擎占用/释放/归属校验）、`test_auth_router`（Token 获取与使用、错误口令、非 ASCII、四类生产闸门）、以及 WebSocket 鉴权与场景下发归属用例。
- 无真实 CARLA 环境亦可全量回归（`tests/mocks/carla_mocks.py` 桩注入）。

### 已知限制（后续阶段）

- 单 CARLA 引擎仅支持单场景实例；多实例并发需多引擎/多容器编排（阶段二）。
- 实例与场景状态仍在进程内存，服务重启丢失（阶段二持久化）。
- 行人/骑行者 AI 控制器、OpenSCENARIO `.xosc` 执行仍属 P1；当前内置场景为配置驱动的最小闭环。
- 真机验收待办：`docker compose up -d` + 宿主机 CARLA 0.9.16，按 `docs/单机部署指南.md` 阶段 5 的五步冒烟（token → create → scene → start → status → stop/delete）走通并记录 FPS。

### 分发

- `pyproject.toml` / `/health` 应用版本提升 `2.3.0`，镜像 tag `hunter-sim:2.3.0`，`k8s/deployment.yaml` 同步。升级步骤：`git pull` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；已有 `.env` 需补充 `API_ADMIN_USERNAME` / `API_ADMIN_PASSWORD`（生产）。

---

## V2.4.0 — 阶段二「执行面分离与状态持久化」：多引擎并行 + 重启恢复

**发布日期**：2026-10-22
**版本定位**：落地系统评审报告阶段二（P1）。把 CARLA 世界/地图加载/运行器生命周期从 API 进程逻辑中剥离为**执行平面**，以端点库存 + 独占租约支撑**多引擎并行**；实例与场景绑定落 **SQLite**，服务重启后实例可查询、可继续启动，场景定义自动恢复。向后兼容单引擎部署（行为变更见下）。

### 执行面分离（新增 `execution/` 包）

- **`engine_pool.py`**：`EngineEndpoint(host, rpc_port, stream_port, tm_port)`、`EngineLease`（实例/端点/地图/获取时间）、`EnginePool` 独占租约（同一端点同时只允许一个实例；同一实例不可运行时换引擎；释放后可复用）、`parse_engine_endpoints` 解析 `EXECUTION_ENGINE_ENDPOINTS=host:rpc[:stream[:tm]],...`（端口范围/重复/数量校验，非法配置抛 `ConfigurationError`）。
- **`plane.py`**：`ExecutionPlane` 是执行面唯一入口——租约 → `MapManager.load_map` → world 切 50Hz 同步模式 → 创建 `SceneRunnerService`；失败自动归还租约并抛 `ExecutionUnavailable`；另提供 `release/release_all/get_runner/snapshot`。
- **`api/runtime.py` 重写为控制面适配层**：只做权限/状态机/HTTP 错误映射；装配互斥锁与执行面共用 `RLock`（可重入，避免死锁）；按端点解析 `MapManager`（注入表 → 共享实例 → 惰性 `carla.Client`）。
- **多引擎语义**：未声明端点库存时所有实例共享单引擎（同一时刻仅一个场景，与 2.3.0 一致）；声明后每个实例独占一个端点并行运行，端点耗尽时创建实例返回 **503** 并提示扩引擎或销毁空闲实例。
- **移除端口递增猜测**：V2.1.1 的「RPC/流端口按空闲对递增」会让第二个实例指向不存在的引擎（2002），现改为端点库存分配 + 租约；修复单引擎下实例 2 必然连不上 CARLA 的可用性缺陷。
- **新增 `GET /api/v1/sim/execution`（admin）**：返回引擎清单及 holder/map_id、活跃租约、运行器与所在引擎、持久化开关/路径/恢复实例数，用于容量核对与排障。

### 状态持久化（新增 `persistence/` 包）

- **`StateStore`（SQLite，WAL）**：`instances` 表存实例完整记录，`scene_bindings` 表存场景 ID/地图/完整 `SceneConfig` JSON 与最近快照（status/progress/elapsed/error）；`check_same_thread=False` + `RLock`，父目录自动创建，损坏记录跳过不阻断启动。
- **落盘时机**：实例创建、每次状态流转（含失败原因）、销毁（级联删除场景绑定）；场景下发成功后写入绑定，运行中状态变化回调更新快照。
- **重启恢复**：`SimInstanceManager.restore()` 重建实例表——`LOADING→CREATED`、`RUNNING/PAUSED→READY`、`gpu_id=-1`（GPU 池不持久化），非终态实例回填用户配额（防重启绕过并发限制）；`normalize_scene_statuses()` 把 loading/running/paused 归一为 ready。
- **场景定义恢复**：重启后首次装配运行器时，若运行器尚无场景且存在绑定，自动 `load_scene(SceneConfig(**config))`（`EXECUTION_RESTORE_SCENE_ON_ASSEMBLE` 可关闭）；恢复失败仅告警，等待重新下发。
- **查询不再断崖**：`GET /instances/{id}/status` 与 `GET /scenes/{id}/status` 在运行器未装配时回落持久化快照（`scene.detached=true`），只有既无运行器也无记录才 404。
- **`/health` 新增**：`version`（动态包版本）、`state_persistence`、`restored_instances`。

### 配置新增

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `EXECUTION_ENGINE_ENDPOINTS` | 空 | CARLA 引擎清单 `host:rpc[:stream[:tm]],...`；空 = 单引擎（`CARLA_HOST:CARLA_RPC_PORT`） |
| `EXECUTION_MAX_ENGINES` | `8` | 端点清单最大解析数量（1-64） |
| `EXECUTION_RESTORE_SCENE_ON_ASSEMBLE` | `true` | 装配运行器时自动恢复已持久化场景 |
| `STATE_ENABLED` | `false`（compose/k8s 默认 `true`） | 是否启用状态持久化 |
| `STATE_DB_PATH` | `data/state/hunter_sim.db` | SQLite 路径（compose 挂载 `data/` 卷） |
| `STATE_RECOVER_INSTANCES` | `true` | 启动时恢复未终态实例 |
| `STATE_RESTORE_SCENES` | `true` | 启动后按需恢复场景定义 |
| `STATE_BUSY_TIMEOUT_SECONDS` | `5.0` | SQLite 忙等待超时 |

### 部署编排

- **`docker-compose.yml`**：镜像 tag `hunter-sim:2.4.0`；新增 `carla-engine-2`（profile `carla`，宿主 2100/2101/8100）用于多引擎并行；`sim-service` 注入 `EXECUTION_*`/`STATE_*` 并挂载 `../data:/home/hunter/app/data`。
- **`docker-compose.windows.yml`**：镜像 tag 与 `EXECUTION_*`/`STATE_*`、`data` 卷同步。
- **`k8s/`**：`configmap` 增加 `execution_engine_endpoints/execution_max_engines/state_enabled/state_db_path`；`deployment` 注入对应环境变量，新增 **RWO `hunter-sim-state-pvc`（10Gi）** 挂载 `/home/hunter/app/data/state`（SQLite WAL 不适用 NFS/RWX）；CARLA 扩容注释改为 StatefulSet + Headless Service + 端点清单。
- **`.env.example`**：版本头与 `EXECUTION_*`/`STATE_*` 示例（部署默认开启持久化）。

### 行为变更（升级注意）

- 未声明 `EXECUTION_ENGINE_ENDPOINTS` 时，新实例不再获得 `2002/2003` 递增端口，而是与首个实例共享 `CARLA_HOST:CARLA_RPC_PORT`；同一时刻仍只允许一个场景运行（第二个返回 503）。
- 声明端点库存后，实例创建即绑定端点：库存耗尽返回 503（此前不限制）；销毁实例后端点立即可复用。
- 服务重启后实例状态归一（RUNNING→READY、LOADING→CREATED），场景保留定义但不延续运行进度；需要重新 `start`。
- `STATE_ENABLED` 默认 false（源码直跑不产生本地文件）；compose/.env.example/k8s 部署默认开启。

### 质量

- `pytest` **749 项全绿**（较 2.3.0 新增 74 项）：`test_state_store`（实例/场景读写、快照更新、损坏跳过、级联删除、重启归一）、`test_engine_pool`（端点解析非法分支/独占租约/释放复用/快照）、`test_execution_plane`（装配/回调/占用/失败回滚/多引擎并行/释放）、`test_execution_router`（admin 200 / user 403 / 未带令牌 401、两次 `create_app` 重启恢复与销毁不恢复）、`test_runtime` 场景持久化/恢复/`describe_execution`、`test_resource_manager` 端点库存分配与实例恢复配额。
- `mypy src`（strict）无问题；YAML 清单经 `safe_load_all` 解析校验。

### 已知限制

- SQLite 为单写者：`sim-service` 保持 `replicas=1`，共享卷（NFS/RWX）上 WAL 锁不可靠，K8s 使用 RWO 卷。
- 重启只恢复「实例 + 场景定义」，不恢复运行中的 tick 线程与 Actor；恢复后的场景需重新 `POST /start`。
- 多引擎要求各端点端口/`load_world` 互不干扰；同一引擎仍禁止两个实例并行（租约强制）。
- 引擎端点库存为静态配置，暂不支持运行时热扩缩容（扩容需改环境变量并重启）。

### 分发

- `pyproject.toml` 版本提升 `2.4.0`，镜像 tag `hunter-sim:2.4.0`（compose/k8s 已同步）。升级步骤：`git pull` → 合并 `.env.example` 新增的 `EXECUTION_*`/`STATE_*` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；已有 `.env` 若保留 `STATE_ENABLED` 未设，compose 默认注入 `true`。

---

## V2.5.0 — 阶段三「平台化」：多租户 / 服务账号 / 场景库 / 审计

**发布日期**：2026-10-22
**版本定位**：落地系统评审报告阶段三（P2）。V2.4.0 已把控制面、执行面与状态面拆开；本版在此基础上把「单机单用户服务」补齐为可运营平台：请求主体携带租户身份并按租户隔离资源，服务间调用改用服务账号 API Key，场景可入库版本化后按 ID 下发，批量任务与审计事件落 SQLite 可跨重启查询，并新增平台概览与审计查询端点。向后兼容（行为变更见下）。

### 多租户与 RBAC（`common/models.py` / `api/deps.py`）

- **新增 `PlatformSettings`**（`env_prefix="PLATFORM_"`）：`default_tenant`（`default`）、`max_instances_per_tenant`（`20`）、`api_keys_enabled`（`true`）、`audit_enabled`（`true`），聚合为 `HunterSimSettings.platform`。
- **统一请求主体 `Principal`**：`user_id / role / tenant / auth_type / api_key_id`，并提供 `is_admin` 与 `has_role()`；角色层级固定为 **guest(0) < viewer(1) < operator(2) < admin(3)**。`get_principal()` 优先使用中间件写入的 `request.state.principal`，其次 Token 载荷，最后从 Bearer 头解析；`require_operator()` 返回 `Principal`。
- **JWT 增加 `tenant` claim**：`POST /auth/token` 签发的 Token 携带平台默认租户；旧 Token（无该 claim）与未显式声明租户的请求统一回落 `PLATFORM_DEFAULT_TENANT`，升级后既有客户端无需改 Token。
- **实例归属租户**：`SimInstance.tenant` 落库/回显；旧状态库记录无 `tenant` 时恢复为平台默认租户。
- **租户配额**：`create_instance` 在分配 GPU 前校验 `PLATFORM_MAX_INSTANCES_PER_TENANT`（统计非终态实例），与「单用户 5 / 全局 50」叠加生效；超限返回 `503`。
- **隔离语义**：列表/详情/场景子资源/WebSocket 订阅全链路校验租户；非 admin **按 ID 读取跨租户实例/场景库条目/任务报告返回 `404`**（不泄露资源是否存在），显式以 `tenant` 指定他租户的列表/创建返回 `403`，WebSocket 跨租户订阅以 `4403` 关闭；同租户非属主仍为 `403`；admin 可跨租户查看与运维（显式设计，便于平台运维），`GET /instances`、`GET /scenes/library` 等支持 `tenant` 过滤。
- **权限下沉**：`POST /instances`、`POST /scenes/library`、`DELETE /scenes/library/{id}`、`POST /scenarios/batch` 改为 `operator` 及以上（admin 天然满足）；`DELETE /instances/{id}`、`POST /maps/upload`、`GET /execution`、`GET /audit`、`GET /platform/overview` 仍需 `admin`。

### 服务账号 API Key（`api/middleware/auth.py` / `api/routers/auth.py`）

- **签发 `POST /auth/api-keys`（admin）**：请求体 `name / role(viewer|operator|admin) / tenant / expires_in_days`；返回 **仅一次** 的明文 `hsk_<key_id>_<secret>` 与 `warning`，库中只保存 `salt` 与 `sha256("salt:secret")`。
- **列表 `GET /auth/api-keys`（admin）**：返回元数据（`key_id/name/role/tenant/created_at/expires_at/last_used_at/revoked`），不含密钥；默认本租户，admin 可指定 `tenant`。
- **吊销 `DELETE /auth/api-keys/{key_id}`（admin）**：置 `revoked_at`，立即生效；已吊销/不存在返回 `404`（重复调用不改变服务端状态）。
- **认证方式**：`X-API-Key: hsk_...` 或 `Authorization: ApiKey hsk_...`；校验摘要用 `hmac.compare_digest` 常量时间比较，已吊销/已过期/摘要不匹配均拒绝；`last_used_at` 按 **60 秒节流** 回写，避免每请求写库。
- **主体合成**：API Key 请求的 `sub` 为 `svc:<name>`，并携带 Key 绑定的 `role/tenant`；审计 `auth_type=api_key`、`api_key_id=<key_id>`。
- **开关与依赖**：`PLATFORM_API_KEYS_ENABLED=false` 时中间件不再接受 API Key（回退 JWT），签发接口返回 `403`；API Key 必须持久化，未启用状态库（`STATE_ENABLED=false`）时签发/列表/吊销返回 `503`。
- **审计事件**：`auth.api_key.create` / `auth.api_key.revoke`。

### 场景库（`/scenes/library`，新增 `api/routers/scene_library.py`）

- **版本化保存 `POST /scenes/library`**：请求体 `name / description / config / scene_id`，先经 `SceneConfig` 校验；同租户同名再次保存自动 **版本自增** 并把旧版本归档（`archived=true`），返回 `library_scene_id / name / version / scene_id / map_id`（不回传完整配置）。
- **查询**：`GET /scenes/library` 列出租户条目（默认不含历史版本，`include_archived=true` 可含）；`GET /scenes/library/{library_scene_id}` 返回含完整 `config` 的条目，可直接用于下发。
- **归档**：`DELETE /scenes/library/{library_scene_id}` 为软删除，已下发到实例的场景不受影响。
- **按库下发**：`POST /instances/{id}/scene` 请求体现在支持 `library_scene_id`，与 `scene_config` **二选一**（同时提供或都不提供返回 422）；非 admin 仅能引用本租户条目（否则 404），admin 可跨租户引用；状态库未启用返回 `503`。
- **审计事件**：`scene.library.save` / `scene.library.delete`。

### 批量任务持久化与任务中心（`api/routers/scenarios.py` / `persistence/state_store.py`）

- **双写状态库**：新增 `batch_tasks` 表；`POST /scenarios/batch` 注册任务时同时写内存热缓存与 SQLite，执行结束（或生成报告时）回写 `status/results/report`；写库失败仅告警，任务仍按内存态执行。
- **重启归一**：服务启动时 `normalize_batch_tasks()` 把残留的 `running/pending` 任务标记 `failed`（`error_message=interrupted by service restart`），不再出现永远 pending 的僵尸任务。
- **任务中心 `GET /scenarios`**：合并状态库与内存缓存（新→旧，最多 200 条）；非 admin 仅返回本人任务，admin 可查看本租户全部。
- **报告不丢失**：`GET /scenarios/{task_id}/report` 内存未命中时从状态库读回（read-through），重启后历史报告仍可查询。
- **维护**：状态库提供 `prune_batch_tasks(keep=1000)` 供运维脚本裁剪历史任务（不在请求路径自动触发）。

### 审计落库与查询（`api/middleware/logging.py` / `api/routers/audit.py`）

- **落库 sink**：`audit_log(action, user_id, target, detail, tenant, auth_type)` 在保持 stdout 结构化输出的同时，经 `set_audit_sink(store.append_audit)` 写入 `audit_logs` 表；落库失败仅告警，不影响主流程。
- **覆盖面**：`instance.create/start/stop/destroy`、`scenario.batch.create`、`scene.library.save/delete`、`auth.api_key.create/revoke`；记录租户与认证方式（`jwt`/`api_key`）。
- **查询 `GET /audit`（admin）**：支持 `tenant`（`*` 为全部租户）、`action`、`user_id` 过滤与 `limit/offset` 分页，返回 `{items,total,limit,offset,tenant}`；`PLATFORM_AUDIT_ENABLED=false` 返回 `403`，状态库未启用返回 `503`。

### 平台概览（`GET /platform/overview`，新增 `api/routers/platform.py`）

管理员汇总视图，`tenant` 缺省取调用方租户、`tenant=*` 为全局统计，返回：`tenant/default_tenant`、`limits`（租户/用户/全局配额）、`instances`（总数/按状态/按租户）、`quota`、`scene_library`、`batch_tasks`（总数/按状态）、`api_keys`（总数/生效数）、`features`（API Key/审计开关）、`persistence`（开关/路径/恢复实例数）、`execution`（引擎租约快照，复用 `/execution` 数据）。

### 配置新增

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `PLATFORM_DEFAULT_TENANT` | `default` | 默认租户（旧 Token 与未声明租户的归属） |
| `PLATFORM_MAX_INSTANCES_PER_TENANT` | `20` | 单租户最大并发实例数（与用户级/全局配额叠加） |
| `PLATFORM_API_KEYS_ENABLED` | `true` | 是否接受服务账号 API Key 认证 |
| `PLATFORM_AUDIT_ENABLED` | `true` | 是否将审计事件落库并允许 `GET /audit` 查询 |

### 部署编排

- **`docker/docker-compose.yml` / `docker-compose.windows.yml`**：镜像 tag `hunter-sim:2.5.0`；`sim-service` / `sim-management` 注入 `PLATFORM_DEFAULT_TENANT` / `PLATFORM_MAX_INSTANCES_PER_TENANT` / `PLATFORM_API_KEYS_ENABLED` / `PLATFORM_AUDIT_ENABLED`（均可经 `.env` 覆盖）。
- **`k8s/`**：`configmap.yaml` 增加 `platform_default_tenant/platform_max_instances_per_tenant/platform_api_keys_enabled/platform_audit_enabled`；`deployment.yaml` 注入对应环境变量并同步镜像 tag。
- **`uv.lock`**：`hunter-sim` 版本同步为 `2.5.0`（此前 lock 中的版本号长期落后于 `pyproject.toml`）。

### 行为变更（升级注意）

- `POST /instances` 所需角色由 `admin` 下沉为 **`operator`**；`guest/viewer` 返回 `403`（`/auth/token` 签发的内置管理员 Token 仍为 admin，不受影响）。`DELETE /instances/{id}` 仍限 admin。
- 跨租户访问实例/场景库：非 admin 按 ID 读取返回 **404**（不泄露存在性），显式指定他租户的列表/创建返回 **403**；WebSocket 跨租户订阅关闭码 **4403**（同租户非属主仍为 403）。
- `POST /instances` 新增可选 `tenant`（跨租户仅 admin），创建响应新增 `tenant` 字段。
- 未启用状态库时，`/auth/api-keys`、`/scenes/library`、`/audit` 返回 `503`；批量任务仍可执行但不持久化。
- 服务重启后未完成的批量任务由 `pending/running` 变为 `failed`（此前重启即从内存消失）。
- 审计 stdout 记录新增 `tenant` 与 `auth_type` 字段。
- WebSocket 握手仍只接受 **JWT**（`?token=` 或 `Authorization: Bearer`），不支持 API Key。

### 质量

- `pytest` **776 项全绿**（较 V2.4.0 的 749 项新增 27 项）：新增 `tests/test_platform.py` 覆盖平台配置与 `Principal` 角色层级、API Key 签发/校验/吊销与节流、场景库版本自增与租户隔离、批量任务持久化与重启归一、审计落库/查询、租户配额、跨租户 `404`/admin 运维访问、`/platform/overview` 聚合字段。
- `mypy src`（strict）无问题（107 个源文件）；`flake8 src tests` 无告警；覆盖率门禁配置未变（`fail_under=80`）。

### 已知限制

- 租户隔离为应用层约束（同一 JWT 密钥、共享 SQLite）；尚无租户级签名密钥与数据库行级安全。
- admin 跨租户可读可操作是刻意的运维能力，审计按 `tenant` 记录实际目标；若需强隔离的多运营方场景，应拆分独立部署。
- API Key 仅用于 REST；WebSocket 订阅需 JWT。
- SQLite 单写者约束不变：`sim-service` 保持 `replicas=1`、`--workers 1`。

### 分发

- `pyproject.toml` / `uv.lock` 版本提升 `2.5.0`，镜像 tag `hunter-sim:2.5.0`（compose / k8s 已同步）。升级步骤：`git pull` → 按需在 `.env` 增加 `PLATFORM_*` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；已有状态库无需迁移（新表在启动时自动创建），旧 Token / 旧实例记录自动回落默认租户。

---

## V2.6.0 — 阶段四「并发安全与稳定性加固」（P0）

**发布日期**：2026-10-22
**版本定位**：落地系统评审报告 P0 五项改进（线程安全 / 安全治理 / 数据链路背压 / 可观测性），并顺带修复两处真实缺陷。本版不改接口契约与状态库结构，升级即得；默认参数对正常客户端零感知。

### 现场（评审发现的问题）

1. 运行器注册表 `app.state.scene_runners` 与全局装配锁均在请求路径内**惰性创建**（`getattr`→`setattr` 非原子），首个并发请求可能各自建锁，装配互斥形同虚设。
2. 多引擎并行部署下，`ensure_runner` 用**一把全局锁**串行化所有引擎的 `load_world`（单引擎仍受单场景租约约束）：第二个引擎装配最长被阻塞 30s+，V2.4.0 的多引擎并行能力被锁层级抵消。
3. `API_RATE_LIMIT_PER_MINUTE` 配置**声明了但没有任何消费方**：全部接口（含 `/auth/token`）无限流与口令爆破防护。
4. cloud bag 上传队列为**无界 `queue.Queue`**：MinIO/网络长故障时上传任务无限堆积，内存持续增长。
5. `/metrics` **手工拼接文本**：仅输出少量 Gauge，无 HTTP 延迟/错误、无安全与链路计数；请求级指标完全缺失，告警只能依赖瞬时 Gauge。

### 根因

- 锁与注册表的生命周期没有与 lifespan 对齐，「谁先请求谁建锁」留下竞态窗口；
- 装配互斥粒度错误地按「进程」而非「CARLA 引擎端点」划分；
- 配置模型与中间件装配脱节（声明未接线）；
- 后台队列无背压与丢弃策略设计；
- 指标未走 `prometheus_client` 统一注册与导出。

### 修复内容

- **P0-1 eager 初始化（`api/runtime.py` / `api/main.py`）**：新增 `init_runtime_state(app)`，`create_app` 阶段即建立唯一的 `scene_runtime_lock` 与 `scene_runners` 注册表；模块级 `_BOOT_LOCK` 双检兜底保留（兼容测试/外部注入路径），并发初始化只会得到同一把锁。
- **P0-2 引擎级装配锁（`execution/plane.py`）**：全局装配锁拆分为 `_engine_locks: dict["host:rpc_port", RLock]`（`dict.setdefault` 原子懒建）；控制面注册表锁只保护租约与登记（短临界区），`load_world`/建运行器等慢操作在**对应引擎锁**内执行——不同引擎并行装配，同一引擎严格串行；`release` 同样按引擎锁串行销毁。
- **P0-3 限流与登录退避（新增 `api/middleware/rate_limit.py`）**：
  - `TokenBucket` 按客户端 IP 令牌桶（线程安全、空闲桶 600s 回收、键数软上限 4096），`RateLimitMiddleware` 仅对 `/api/v1/sim/**` 生效，豁免 `/health*`、`/metrics`、`/ws/`，超限返回 **429**（统一响应体）+ `Retry-After`；`API_RATE_LIMIT_PER_MINUTE` / `API_RATE_LIMIT_ENABLED` 自此真正生效。
  - `LoginFailureGuard`：`/auth/token` 同 IP 连续失败达 `API_LOGIN_MAX_FAILED_ATTEMPTS`（默认 5）后指数退避 `base * 2^min(failures-max, 16)`（默认 2s 起、封顶 60s），退避期直接 429 + `Retry-After`（不进入凭据比较），成功登录即清零。
- **P0-4 上传队列有界化（`cloud/connector.py`）**：队列容量 `HUNTER_CORE_UPLOAD_QUEUE_MAX_SIZE`（默认 32，1~1024）；队满丢**最旧**为新任务腾位，腾位仍失败则丢新任务；丢弃计入 `huntersim_cloud_upload_dropped_total` 并记 WARN；新增 `upload_stats`（success/failed/dropped 三重计数）。
- **P0-5 标准化指标（新增 `common/metrics.py`，`/metrics` 改 `generate_latest()` 导出）**：`huntersim_http_request_duration_seconds`（method/path/status_class，路径归一化 `{id}` 防高基数）、`rate_limited_rejections_total`、`login_rejections_total{reason=credential|backoff}`、`instance_lifecycle_total{event}`、`carla_rpc_errors_total`、`kafka_consume_errors_total`、`cloud_upload_total{result}`、`cloud_upload_dropped_total`；app.state 派生 Gauge（uptime/连接状态/RPC 延迟/FPS/VIL 延迟/磁盘）改由**抓取时刻刷新**，缺值写 NaN（不误告警）。Grafana 预置盘 `docker/grafana/provisioning/dashboards/huntersim-overview.json`（uid `huntersim-overview`，file provisioning 自动加载）。
- **缺陷修复 1（`api/error_handlers.py`）**：`http_exception_handler` 此前**丢弃 `exc.headers`**——401 的 `WWW-Authenticate: Bearer` 与 429 的 `Retry-After` 均到不了客户端。`_err_response` 增加 `headers` 参数并在各分支透传。
- **缺陷修复 2（`common/metrics.py`）**：`_GAUGE_SPECS` 解包顺序写反导致刷新 Gauge 时 `KeyError`（开发期暴露、已修）；测试辅助 `metric_exists` 改用公开 API `REGISTRY.collect()`（不再依赖 `_names` 私有属性）。

### 向后兼容

- 接口契约、状态库、JWT/API Key 语义均不变；限流默认 1000 req/min·IP、退避默认 5 次起 2s，正常客户端零感知；`API_RATE_LIMIT_ENABLED=false` 可整体关闭。
- 多引擎部署下装配从全局串行变为引擎级并行（行为增强，无需改配置）；同一引擎并发语义与 V2.4.0/V2.5.0 一致。
- `/metrics` 改标准 prometheus_client 导出：浮点按最小精度序列化（如 `420.0` 而非 `420.0000`）——若对导出文本做过固定格式抓取/解析需自查；`huntersim_*` 指标族名不变，`docker/alerts.yml` 规则兼容。

### 风险

- 限流按**直连源 IP** 计数：经无反向代理透传 `X-Forwarded-For` 时所有请求可能计入代理 IP（容量规划时按聚合值调 `API_RATE_LIMIT_PER_MINUTE`）。
- 限流/退避为进程内计数，与 `--workers 1` 单副本前提一致；多副本部署需网关层全局限流。
- 上传队列「丢最旧」策略意味着 HunterCore 接入故障时长录制可能丢 bag——有审计级留存要求需自行落盘归档（丢弃均有 WARN 日志与指标可告警）。

### 验证

- `mypy src`（strict）通过：**109 个源文件无问题**。
- `pytest` 全量：**822 项通过**（较 V2.5.0 的 776 项新增 46 项，58 项告警为既有第三方告警）。
- 覆盖率门禁：`pytest --cov` **Total coverage 81.11% ≥ 80%**（`fail_under=80`）。
- 新增/扩展测试：`test_rate_limit.py`（令牌桶/中间件豁免与 429+Retry-After/退避守卫/`/auth/token` 集成退避）、`test_metrics.py`（路径归一化/直方图与计数观测/Gauge 刷新/REGISTRY 判断/`/metrics` 端点族名）、`test_execution_plane.py::TestEngineLocks`（不同端点并行装配 Barrier 验证、外部持锁串行、同实例去重、释放销毁串行）、`test_runtime.py::TestInitRuntimeState`（预初始化幂等、并发单锁、惰性兜底）、`test_cloud_connector.py`（丢最旧/丢新/get 竞态落 worker、端到端背压计数）。
- CARLA/ROS2 依赖仍经 `tests/mocks/carla_mocks.py` 桩注入，无引擎环境全量回归。

### 实机执行（待办）

- [ ] 双引擎真机：两个实例并行下发场景，日志确认 `load_world` 交叠执行（此前被全局锁串行），单引擎仍严格串行。
- [ ] 对 `/auth/token` 连续错口令 ≥5 次，抓包确认 429 携带 `Retry-After` 头（V2.6.0 前的错误处理器会丢掉）。
- [ ] 压测脚本超 `API_RATE_LIMIT_PER_MINUTE` 触发 429，Prometheus 侧 `huntersim_rate_limited_rejections_total` 增长。
- [ ] Grafana 打开预置盘 **HunterSim 平台总览**（`docker/grafana/provisioning/dashboards/huntersim-overview.json`）确认面板取数。
- [ ] 接入 HunterCore 拔网线制造上传故障，确认队列深度封顶 32、`huntersim_cloud_upload_dropped_total` 增长且内存不漂移。

### 分发

- `pyproject.toml` 版本提升 `2.6.0`，镜像 tag `hunter-sim:2.6.0`（compose / compose.windows / Dockerfile 注释 / k8s deployment 已同步）；k8s `configmap.yaml` 新增 `api_rate_limit_* / api_login_*` 键并由 deployment 注入；compose 两栈注入 `API_RATE_LIMIT_* / API_LOGIN_* / HUNTER_CORE_UPLOAD_QUEUE_MAX_SIZE`（默认值同代码）。升级步骤：`git pull` → 按需在 `.env` 增加上述变量（可全省略，走默认）→ `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；无状态库迁移。

---

## V2.7.0 — 阶段五「稳定性与数据链路加固」（P1）

**发布日期**：2026-10-22
**版本定位**：落地系统评审报告 P1 七项改进（认证生命周期 / 租约心跳 / 消费自愈 / VIL 链路强化 / 持久化写节流 / 日志统一 / 端到端集成测试）。接口契约与状态库既有表结构不变，升级即得；默认参数对正常客户端零感知。本版顺带修复一处**真实安全缺陷**：JWT 吊销校验在中间件中永远取不到状态库（见「缺陷修复」）。

### 现场（评审发现的问题）

1. JWT 签发后**无法提前撤销**：无 `jti`、无吊销名单，Token 一旦泄漏在有效期内（默认 24h）永远可用；也无续期端点，长时批量任务只能重新登录拿新 Token。
2. 引擎租约只在创建时写入、销毁时删除，**没有存活证据**：运行器意外死亡（如 tick 线程崩溃未被感知）时租约永久占用端点，新实例持续 503，只能重启服务解堵。
3. Kafka 消费侧遇到 broker 抖动/会话失效后 `poll` 持续报错就**永久空转**，不重建不降级；无法区分“消费健康”与“消费卡死”；坏消息（非法 JSON）只丢掉计数，无事后排查入口。
4. VIL 链路无**单调性/去重闸门**：重发、乱序的遥测帧直接入环，位姿外推会被旧帧拉回；环深（默认 10 帧）与外推窗口/频率无联动，10Hz×150ms 够用但 20Hz 或长外推窗口下覆盖不足；健康报告只有均值，无 P95/P99 分位数，告警对拖尾不敏感。
5. 运行循环每 tick 状态回调都**无条件写 SQLite**：50Hz 下单实例每天约 430 万次写，与实例创建/销毁写争抢单写者锁。
6. 日志无统一装配：各模块 `get_logger` 自挂 handler，格式不可控；无 request_id 贯穿，排障无法串联一次请求跨多模块的日志；无文件输出与轮转。
7. 既有测试均为组件级单测/局部集成，**没有一条从 REST 入口跑到引擎交互再回到持久化的端到端用例**，重启恢复、租约归还等跨层链路靠人肉验证。

### 根因

- 认证只做了“签发 + 过期”，未设计撤销通道与续期通道（jti 从未进入 payload）；
- 租约是静态映射而非心跳型资源，缺少 `last_renew_at` 与僵判定；
- 消费循环把“错误处理”与“会话恢复”混在一起，没有重建策略与积压指标；
- 链路假设上游严格有序，缺少入口闸门与配置自适应；观测只有均值；
- 回调与落库 1:1 直连，未区分“心跳型重复写”与“跃迁型必写”；
- 日志装配分散在每个模块的 import 路径里，contextvar 没有任何载体；
- 测试基建缺一个能同时提供 FakeCarla + 真 REST + 真 SQLite 的装配层。

### 修复内容

- **P1-6 JWT 吊销与滑动续期（`api/deps.py` / `api/routers/auth.py` / `api/middleware/auth.py` / `persistence/state_store.py`）**：`mint_access_token` 统一签发携带 `jti`+`exp` 的 Token；状态库新增 `token_revocations` 表（`revoke_token` 幂等、`is_token_revoked`、`prune_revoked_tokens` 只清自然过期记录）；中间件逐请求校验吊销名单（命中 401 + `WWW-Authenticate`）。新增 `POST /auth/refresh`（有效 Token 换发新 Token，旧 `jti` 就地吊销——轮换；需 `API_JWT_REFRESH_ENABLED` 与状态库）与 `POST /auth/revoke`（立即吊销当前 Token，幂等；旧版无 `jti` 的 Token 返回 `revoked=false`）。审计事件 `auth.token.refresh` / `auth.token.revoke`；指标 `huntersim_auth_token_events_total{event=refresh|revoke|revoked_hit}`。
- **P1-7 租约心跳与僵尸回收（`execution/engine_pool.py` / `execution/plane.py` / `api/runtime.py` / `api/routers/execution.py`）**：`EngineLease` 增加 `last_renew_at` 与 `renew()/is_stale(ttl)`；`EXECUTION_LEASE_HEARTBEAT_ENABLED=true` 时租约池按 `EXECUTION_LEASE_TTL_SECONDS`（默认 120s，下限 10s）启用僵判定（禁用时 TTL=0 永不判僵，与 V2.6.0 行为一致）；健康巡检周期对全部存活运行器批量续租（`renew_live_leases`，仅对 `is_running` 为真的运行器续）；`GET /execution` 快照暴露每个租约/端点的 `stale` 标志与 `stale_leases` 计数；新增 **`DELETE /execution/leases/{instance_id}`（admin）**强制回收僵尸租约（释放运行器 + 归还端点，计入 `huntersim_execution_lease_reclaims_total`；未知实例 404，operator 403）。
- **P1-8 Kafka 消费自愈与 DLQ（`vil_mapper/vil_data_consumer.py`）**：连续 `poll` 错误达 `KAFKA_REBUILD_AFTER_ERRORS`（默认 10）自动关闭旧消费者并重建（`KAFKA_ENABLE_SELF_HEAL=false` 可禁用，保持旧行为）；新指标 `huntersim_kafka_consumer_rebuilds_total` / `huntersim_kafka_consumed_total` / `huntersim_kafka_consumer_lag`（end offset − position，best-effort）；坏消息（JSON 解析失败）转发 `KAFKA_DLQ_TOPIC`（留空仅计数），计入 `huntersim_cloud_dlq_published_total`；DLQ producer 惰性创建、`stop()` 时 flush+close。
- **P1-9 VIL 链路强化（`vil_mapper/vil_data_consumer.py` / `telemetry_buffer.py` / `data_health_monitor.py`）**：入口单调性闸门 `_accept_in_order` 按 `seq`（缺省回退 `timestamp`）丢弃重复/回退消息，计入 `huntersim_vil_message_dropped_total`（`VIL_ORDERING_GUARD_ENABLED` 默认开，关则透传）；`compute_auto_buffer_depth(extrapolation_ms, telemetry_frequency_hz)` = `ceil(窗口×频率)+5` 帧余量、钳制 `[10,200]`，`VIL_AUTO_BUFFER_DEPTH=true`（默认）时 `TelemetryBuffer` 按 `VILSettings` 自动推导环深覆盖手工值；`push()` 单点观测收帧延迟→ `huntersim_vil_latency_seconds` 直方图（P50/P95/P99 用 `histogram_quantile`）；`DataHealthReport` 新增 `p50/p95/p99_latency_ms`（最近秩法）。
- **P1-10 快照写节流（`api/runtime.py`）**：`STATE_SNAPSHOT_WRITE_INTERVAL_SECONDS`（默认 1.0，0 不节流）——同一实例同状态间隔内只落库一次，**状态跃迁（running→paused→completed 等）不受限必写**，恢复语义不丢关键节点；守卫挂 `app.state`（双检锁惰性创建，每实例 `(last_status, last_ts)`）；settings 缺失/无 `state` 配置回退不节流（兼容测试桩）。
- **P1-11 日志统一（`common/utils.py` / `api/main.py` / `api/middleware/logging.py`）**：新增 `configure_logging(log_level, json_logs, log_file, log_max_bytes, log_backup_count)`，lifespan 启动时统一装配（幂等：清重复 handler）；文本模式格式 `[时间][级别][模块][request_id] 消息`，JSON 模式 structlog `ProcessorFormatter + JSONRenderer`；request_id 经 structlog contextvars 贯穿——中间件优先复用客户端 `X-Request-ID`（截断 64 字符）便于全链路追踪，`finally` 必清理；`log_file` 非空时挂 `RotatingFileHandler`（默认 10 MiB × 5 份）；`get_logger` 未装配时保持旧行为，存量脚本/单测不受影响。配置：`HUNTER_SIM_LOG_JSON` / `HUNTER_SIM_LOG_FILE` / `HUNTER_SIM_LOG_MAX_BYTES` / `HUNTER_SIM_LOG_BACKUP_COUNT`。
- **P1-12 端到端集成测试（新增 `tests/integration/test_e2e_rest_flow.py`）**：FakeCarla（`sys.modules` 注入）+ 真 FastAPI 路由链 + 真 SQLite，同库双 app 验证重启恢复。7 条链路：登录→创建→scenes/load→start（真实 tick 循环）→租约查询→stop→销毁→租约归还；快照跃迁落库；服务重启后实例恢复 + detached 快照 + 再装配；refresh 轮换并吊销旧 Token；revoke 幂等；admin 强制回收/operator 403/未知实例 404。新增 `tests/test_p1_vil_chain.py`（19 项）、`tests/test_p1_ops_chain.py`（25 项）覆盖 P1-6〜P1-11 单元行为。
- **缺陷修复（`api/middleware/auth.py`，集成测试暴露的真 bug）**：吊销校验 `_is_revoked` 此前经 `self.app`（`BaseHTTPMiddleware` 包装的内层 ASGI 应用，无 `.state`）取状态库，**永远取不到 → 吊销名单形同虚设**；改为 `request.app.state`。该 bug 由 `test_e2e_rest_flow` 首跑失败定位，修复后吊销链路由 REST 入口真实验证。

### 向后兼容

- 接口仅新增（`/auth/refresh`、`/auth/revoke`、`DELETE /execution/leases/{id}`），既有端点请求/响应结构不变；旧版无 `jti` 的 Token 仍可正常使用（只是无可吊销）。
- 状态库仅新增 `token_revocations` 表，启动时自动创建，既有表无需迁移；`STATE_ENABLED=false` 时吊销/续期降级（refresh/revoke 返回 503，中间件不校验名单）。
- VIL 组件为库层，新参数全部带默认值：不传 `vil_settings` 时 `TelemetryBuffer` 行为与 V2.6.0 完全一致；`ordering_guard_enabled/auto_buffer_depth` 默认开，若上游确实需要重复帧回放可显式关闭。
- 节流默认 1s 仅合并“同状态”重复写，跃迁必写，`GET /scenes/{id}/status` 语义不变；`0` 回到旧版逐 tick 全写。
- `HUNTER_SIM_LOG_JSON=false`（默认）保持可读文本格式；仅新增 `[request_id]` 字段——若有按旧格式正则采集的脚本需适配（建议改 JSON 模式）。
- 心跳禁用（`EXECUTION_LEASE_HEARTBEAT_ENABLED=false`）时租约永不判僵，与 V2.6.0 行为一致。

### 风险

- `/auth/refresh` 是**轮换式**：旧 Token 即时失效，客户端必须拿到新 Token 后立即替换；刷新请求失败而旧 Token 已吊销时需重新登录（best-effort：吊销写入失败不阻断新 Token 签发）。
- 吊销名单为进程内 SQLite：多副本部署下吊销存在复制间隙（与限流/退避同一前提，`--workers 1`）。
- 心跳依赖健康巡检周期（默认 10s）续租：TTL 必须显著大于巡检周期（默认 120s ≥ 12×周期，调低 TTL 需同步调密巡检）。
- Kafka 自愈重建后按 `auto_offset_reset` 语义继续，可能少量重复——由 P1-9 去重闸门兜底（两者配合才完整）。
- 环深自动推导钳制 `[10,200]`：超低频（<1Hz）或超长外推窗口场景需显式关闭 `VIL_AUTO_BUFFER_DEPTH` 并手设 `max_frames`。
- 写节流后极端情况下快照最多滞后一个落库间隔（默认 1s）；对“秒内崩溃”的恢复粒度有要求时调小或置 0。

### 验证

- `mypy src`（strict）通过：**109 个源文件无问题**。
- `pytest` 全量：**867 项通过**（较 V2.6.0 的 822 项新增 45 项，含 7 项端到端集成链路）；覆盖率 **83.31% ≥ 80%**（`fail_under=80`）。
- 端到端：FakeCarla 下完整 REST 生命周期（含真实 tick 循环与租约获取/归还）、重启恢复后 `detached` 快照可查、refresh 后旧 Token 请求 401、admin 强收后 operator 强收 403。
- 编排配置：两份 compose 与 k8s 五份清单经 PyYAML 解析验证；deployment 引用的全部 configmap 键逐一核对存在。
- CARLA/ROS2/Kafka 依赖仍经 `sys.modules` 桩注入，无引擎/无 broker 环境可全量回归。

### 实机执行（待办，尚未实机验证）

- [ ] 真机杀掉运行中实例的 tick 线程（或断 CARLA），确认心跳停止后 TTL 到期 `GET /execution` 显示 `stale=true`，`DELETE /execution/leases/{id}` 后新实例立即可装配。
- [ ] 停/启 Kafka broker：确认消费者自动重建（`huntersim_kafka_consumer_rebuilds_total` 增长）且恢复后计数归零；向 telemetry topic 灌非法 JSON 确认落 `KAFKA_DLQ_TOPIC`。
- [ ] VIL 真机：`histogram_quantile(0.95, huntersim_vil_latency_seconds)` 与 `DataHealthReport.p95_latency_ms` 交叉比对；乱序重发实测闸门丢弃。
- [ ] 长时运行对 SQLite 的写入频率采样，确认节流后快照写入量下降而状态跃迁零丢失。
- [ ] 生产侧开启 `HUNTER_SIM_LOG_JSON=true`，验证采集链路（Loki/ELK）解析与 request_id 串联查询。

### 分发

- `pyproject.toml` / `uv.lock` 版本提升 `2.7.0`，镜像 tag `hunter-sim:2.7.0`（compose / compose.windows / k8s deployment 已同步）；k8s `configmap.yaml` 新增 `execution_lease_* / state_snapshot_write_interval_seconds / kafka_enable_self_heal / kafka_rebuild_after_errors / kafka_dlq_topic / vil_ordering_guard_enabled / vil_auto_buffer_depth / api_jwt_revocation_enabled / api_jwt_refresh_enabled / log_json` 并由 deployment 注入；compose 两栈与 `.env.example` 注入同名 `EXECUTION_LEASE_* / STATE_SNAPSHOT_WRITE_INTERVAL_SECONDS / KAFKA_* / VIL_* / API_JWT_* / HUNTER_SIM_LOG_*`（默认值同代码）。升级步骤：`git pull` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；无状态库迁移（新表启动时自动创建）；依赖新增 structlog（已在 requirements/pyproject，无则 `uv sync`）。

### 待办

- [ ] 吊销名单定期清理：`prune_revoked_tokens()` 目前作为维护方法提供（参照 `prune_batch_tasks` 模式），尚未接入健康巡检周期自动触发；名单膨胀仅影响查询效率不影响正确性。
- [ ] WebSocket 握手 Token 的吊销校验目前仅在建连时一次；长连接期间 Token 被吊销不会主动断开（后续可加周期复核）。

---

## V2.8.0 — 阶段六「多用户 / 调度智能化 / 引擎自愈 / 部署体验」（P2）

**发布日期**：2026-10-22
**版本定位**：落地系统评审报告 P2 五项改进（多用户体系 / 地图感知调度 / CARLA 进程自愈 / 部署预检体验 / 无状态化边界）。状态库仅新增 `users` 表（启动自动建表，无迁移），接口只增不改；未启用新开关时行为与 V2.7.0 一致。本版顺带修复两处真实缺陷：口令策略实现与自身文档语义相反、控制面同批实例被调度到同一引擎端点（见「缺陷修复」）。

### 现场（评审发现的问题）

1. 平台只有**一个内置管理员账号**（`API_ADMIN_USERNAME` / `API_ADMIN_PASSWORD` 与配置明文比对）：无法区分操作人，审计里所有变更都记在 `admin` 一个主体上；口令不落库、不能改、不能禁用，人员变动只能改环境变量并重启服务；多租户（V2.5.0）在“人”这一维度形同虚设。
2. 地图冲突判定是**进程级全局**的：V2.4.0 的 `scene_world_map_id` 一旦加载 A 地图，任何请求 B 地图的实例都会被 `SceneRuntimeUnavailable` 拒绝——即使执行面还挂着多个空闲引擎端点。多引擎并行部署被「单引擎单地图」这一局部约束误伤为「全局限一地图」。
3. **CARLA 服务端进程无人监管**：Windows 单机部署下 `CarlaUE4.exe` 崩溃 / OOM / 显卡复位后不会自动拉起，只能人工干预；期间新实例持续 503、运行中场景丢失，且没有任何「引擎死过几次、何时恢复」的留痕。
4. 部署全靠按文档逐条手工检查（Python 版本、JWT 密钥是否仍是占位值、8080 是否被占、磁盘余量、GPU/CUDA、CARLA SDK、状态库路径可写）：新环境首启失败排查成本高，没有一条命令能回答「这台机器能不能上线」。
5. 编排层监控栈**无 healthcheck、无依赖条件**：Grafana 早于 Prometheus 就绪会反复重试数据源；`sim-service` 早于 Kafka 就绪首连报错。同时「单副本 + 重启恢复」这一受支持形态从未在文档中成为明确契约，容易被误用为可水平扩展服务。

### 根因

- 认证模型停留在「一个引导账号」：没有用户实体、没有摘要存储、没有角色/租户与人的绑定关系；
- 地图约束按「一个进程 = 一个世界」建模，未随 V2.4.0 引擎端点抽象下沉到端点粒度，也没有「端点当前加载了什么地图」的记忆；
- 引擎被当成外部黑盒依赖，控制面不拥有其生命周期，缺少进程托管与退避重启预算；
- 环境校验逻辑只活在 `engine/check_environment.py`（面向 GPU/CUDA 的库函数）与文档步骤里，缺一个面向运维、聚合全部前置条件的入口；
- compose 只声明「依赖关系」而未声明「健康状态」；容量边界只在实现细节中，未沉淀为用户契约。

### 修复内容

- **P2-13 多用户体系（`persistence/state_store.py` / `api/routers/users.py` / `api/routers/auth.py` / `api/deps.py`）**：状态库新增 `users` 表（`username` 主键 + `tenant/role/pass_hash/salt/iterations/created_by/created_at/updated_at/last_login_at/disabled_at`，附 tenant 索引，启动自动建表）。口令以 **PBKDF2-HMAC-SHA256** 摘要 + 每用户随机 salt 入库，迭代次数逐条随记录保存（`PLATFORM_PASSWORD_HASH_ITERATIONS` 默认 100000，调高后旧记录仍可按自身参数校验），全程 stdlib 实现不引入新依赖；`verify_password` 用 `hmac.compare_digest` 恒时比对，非法格式/算法不符一律判失败。**用户不存在时也跑一次同迭代次数的 PBKDF2**（虚假 salt），使「无此用户」与「口令错」响应时间近似相等，堵住用户名枚举的时序侧信道。新增 admin 限定路由 `POST/GET /auth/users`、`GET/PATCH/DELETE /auth/users/{username}` 与自助改密 `POST /auth/password`；对外视图裁剪掉摘要/salt/迭代次数。登录链 `_authenticate` **先查用户表再回退内置管理员**（校验经 `run_in_threadpool` 下沉线程池，100k 迭代不阻塞事件循环），角色与租户写入 JWT 声明，审计主体与 `created_by` 由此落到具体人。口令策略：长度 ≥ `PLATFORM_MIN_PASSWORD_LENGTH`（默认 10）、须同时含字母与数字、不得等于或包含登录名（不分大小写），违反返回 422。防护：内置管理员不可经接口修改（409）、**最后一名可用 admin** 不得降级/禁用/删除（409 锁死保护）、禁用用户登录 401、`PLATFORM_USERS_ENABLED=false` 管理接口整体 403、`STATE_ENABLED=false` 返回 503。指标 `huntersim_user_events_total{event=create|update|delete|password_change|password_change_denied|login|login_denied}`；审计 `auth.user.create/update/delete/password_change` 与 `auth.login`。
- **P2-14 地图感知调度（`execution/engine_pool.py` / `execution/plane.py` / `resource_manager/instance_manager.py` / `api/runtime.py`）**：`EnginePool` 新增端点地图记忆 `_endpoint_maps`（`note_loaded_map` / `loaded_map_of`，空地图不覆盖事实）——`acquire` 记「申请地图」，`ensure_runner` 在 `load_world` 成功后以 `_world_map_id(world)` 读回的**实际地图**覆盖（Mock/受限 world 无 `get_map` 时回退申请值）。新增 `pick_free_endpoint(candidates, prefer_map_id)`：三档优先级「已加载目标地图的空闲端点 → 从未加载过地图的空闲端点 → 声明顺序首个空闲」，全占用返回 `None`；配套 `is_endpoint_free` / `lease_of`。控制面创建实例改为按地图亲和调度，决策打点 `huntersim_scheduling_decisions_total{kind=affinity_reuse|fresh_endpoint|fallback}`。冲突检查从进程级下沉为端点级：`plane.map_conflict(endpoint_key, holder, target_map)` 仅当**该端点被其他实例活跃占用且已加载地图不同**才拒绝，`api/runtime.py` 据此替换原全局 `scene_world_map_id` 判定（单引擎形态 `active_engine_count <= 1` 仍保留该全局字段，`/health` 与旧客户端依赖不变）——多引擎下 A 地图运行中不再误伤 B 地图新实例，同地图多实例则复用已加载引擎省一次 `load_world`。`GET /execution` 空闲端点也展示最后已知地图并置 `map_affinity_enabled=true`。
- **P2-15 引擎自愈（新增 `engine/carla_supervisor.py`，`api/main.py` 装配）**：`CarlaProcessSupervisor` 看门狗线程托管**本地回环端点**（`127.*` / `localhost` / `::1`；远程端点自动忽略，由其自身编排器负责），进程退出即按 `CARLA_SUPERVISOR_RESTART_BACKOFF_SECONDS` 退避重启，单 300s 窗口内最多 `CARLA_SUPERVISOR_MAX_RESTARTS`（默认 3）次，超预算转 `exhausted` 并停止抢管（避免崩溃循环）；拉起后探测 RPC 端口就绪（`CARLA_SUPERVISOR_READY_TIMEOUT_SECONDS`，默认 60s，超时记 `ready_timeout` 并回收进程重试）。状态机 `idle/starting/running/crashed/backoff/exhausted/disabled/unconfigured`，事件 `start/exit/restart/ready_timeout/exhausted/stop/giveup` 同时进指标 `huntersim_carla_supervisor_events_total{event}`、内存环形 `recent_events`（上限 50）、审计 `engine.carla.<event>`（主体 `carla-supervisor`，`auth_type=system`，便于区分人操与自愈）。仅 `CARLA_SUPERVISOR_ENABLED=true` 时装配；`CARLA_SUPERVISOR_BINARY_PATH` 为空则**只观测不自愈**（`unconfigured`）。lifespan 关停只终止本服务亲自拉起的进程（人工启动的不接管）。新增 admin 路由 **`POST /execution/engines/restart?endpoint=127.0.0.1:2000`**（手动重启不计入自动重启预算；未启用 409、仅观测 409、端点不受管 404，审计 `engine.carla.manual_restart`）；`GET /execution` 新增 `carla_supervisor` 快照。
- **P2-16 部署体验（新增 `cli/main.py`、`Makefile`，compose 编排）**：`huntersim doctor` 一次跑完单机部署指南的手工检查项——`config / python / jwt_secret / admin_password / users / disk_space / state_store / gpu_env / carla_sdk / carla_reachable / api_port`；`ok|warn|fail|skip` 四级，依赖缺失（无 nvidia-smi、未装 SDK）记 warn/skip，只有会让服务起不来或跑不稳的问题才 fail（默认密钥 `CHANGE_ME_IN_PRODUCTION` 等在 prod 直接 fail、dev 记 warn；生产密钥长度下限 32 与启动闸门同源；磁盘 <20GB warn、<5GB fail；API 端口可 bind→ok、被占可连→warn、被占不可连→fail）。`--json` 输出 `{version, env, checks, counts}` 供脚本消费，`--strict` 把 warn 提为 fail 作上线门禁，`--path` 指定磁盘/状态库基准路径；退出码 0/1/2（成功/失败/参数错）。入口注册为 `pyproject` 的 `huntersim` console script。新增 `Makefile` 统一日常回路（`install/sync/dev-install/doctor/serve/test/coverage/typecheck/lint/image/up/up-carla/down/logs/clean`，Windows 与 Linux/macOS 通用，变量 `PYTHON`/`COMPOSE_FILE`/`IMAGE_TAG` 可覆盖）。compose：Kafka/Prometheus/Alertmanager/Grafana 加 healthcheck，`sim-service` 改 `depends_on.kafka.condition: service_healthy`、Grafana 等 Prometheus 健康后再起（P2-16 就绪探针用镜像内置 busybox `wget --spider`，不额外装包）。
- **P2-17 无状态化边界（文档化）**：本版明确**受支持形态 = 单副本 + 重启恢复**（`--workers 1`；限流/登录退避/JWT 吊销名单/引擎租约/进程托管均为进程内状态，SQLite 为单写者）。多副本水平扩展需外置会话与共享状态库（PostgreSQL/Redis + 引擎注册中心），列为长期项，不在本版范围。文档落点：`deployment_and_operations.md` §受支持的部署形态与边界、`docs/单机部署指南.md` §容量边界、`README.md` §运行形态；`k8s/deployment.yaml` 头注同步声明（`replicas: 1` + RWO 卷，进程自愈在 K8s 形态由 Pod liveness 承担）。
- **缺陷修复 1（`api/routers/users.py`，单测暴露）**：`_check_password_policy` 原写 `password in username`（口令是用户名的**子串**才拒），与自身文档「不得等于/包含用户名」相反——`admin` 账号配 `grace-2026-pass` 这类口令可正常创建。改为「等于或包含（不分大小写）即拒」，`detail` 与文档同步。新增大小写不敏感用例守护。
- **缺陷修复 2（`resource_manager/instance_manager.py`，单测暴露的真实隐患）**：`_assign_endpoint` 走 engine_pool 分支时**未排除「控制面已分配给活跃实例、但租约尚未产生」的端点**（租约在场景装配时才建立，create→assemble 存在时间窗），同批多个实例会被调度到同一引擎，直到装配期才以 `EngineUnavailable` 失败。改为先按活跃实例（非 `completed/failed/destroyed`）已分配地址算 `reserved` 集合、过滤后再交给 `pick_free_endpoint`；新增模块级 `_TERMINAL_STATUSES` 与 `_endpoint_addr()` 统一地址投影。

### 向后兼容

- 接口只新增（`/auth/users*`、`/auth/password`、`POST /execution/engines/restart`），既有端点请求/响应结构不变；`GET /execution` 新增 `carla_supervisor`（未启用为 `null`）与端点 `map_id`/`map_affinity_enabled` 字段，属增量字段，旧客户端忽略即可。
- 登录语义**只增不改**：用户表未命中（含空表、`STATE_ENABLED=false`、`PLATFORM_USERS_ENABLED=false`）时完整回退 V2.3.0 的内置管理员明文比对，既有脚本与凭据无需修改。
- 状态库仅新增 `users` 表，启动自动创建，既有表无迁移。
- 调度改造对单引擎部署零感知（`active_engine_count <= 1` 时保留原全局地图字段）；多引擎部署从「跨地图互斥」放宽为「同端点互斥」，属行为增强。
- `CARLA_SUPERVISOR_ENABLED=false`（默认，且 compose/K8s 显式置 false）时不装配 supervisor、不托管任何进程，`/execution/engines/restart` 返回 409。
- 口令策略只作用于**新建/改密**，不影响存量摘要校验；迭代次数入库逐条记录，后续调高 `PLATFORM_PASSWORD_HASH_ITERATIONS` 不需重建用户（存量摘要仍按入库时的次数校验，需提升强度则重置其口令）。

### 风险

- 内置管理员口令仍来自 `API_ADMIN_PASSWORD` 明文配置（用户表不存它）：本版引入 bcrypt/argon2 与 OIDC 对接仍列为待办，生产建议尽快建用户表并收敛内置账号使用面（改密走环境变量 + 重启）。
- 删除用户为硬删除，已签发 JWT 靠自然过期失效（默认 24h）；需即时失效请配合 `POST /auth/revoke` 或等吊销名单接入用户删除链路（待办）。
- PBKDF2 迭代 100k 时单次登录约几十毫秒 CPU 开销（已下沉线程池不阻塞事件循环）；高并发登录场景请按机器规格调 `PLATFORM_PASSWORD_HASH_ITERATIONS`，过低削弱抗爆破能力、过高放大 CPU 争抢。
- 进程自愈仅在**服务与 CARLA 同机、且端点为回环地址**时可用；托管进程会重置世界，该端点上的场景需重新装配——`restart` 前有活跃实例时应先销毁或迁移实例。远程/K8s 形态请依赖容器 restart 与 liveness，不要开启 supervisor（跨层抢管进程）。**单机指南的容器拓扑（sim-service 在 Linux 容器、CarlaUE4 在 Windows 宿主）同样不适用自愈**：容器无法启动 Windows 可执行文件，且 `host.docker.internal` 非回环端点会被自动忽略（`carla_supervisor` 恒为 `null`，手动 restart 返回 409/404），已在单机部署指南新增「提示 F」与附录条目 27 明记边界。
- 地图亲和依赖「端点最后加载地图」内存记忆：服务重启后记忆清空，首批调度按「从未加载过地图的空闲端点」优先级选择，正确性不受影响但可能多一次 `load_world`。
- `doctor` 为只读检查，不覆盖真实渲染链路（无 CARLA 二进制时 GPU/CUDA 项按 warn/skip）：warn 不阻断上线，`--strict` 才提为 fail。`Makefile` 配方本身不依赖 sh 专有语法（只调 `python` / `docker`），但 `make` 本体为 Unix 工具，**Windows 不自带**（需 GNU make 或 Git Bash / WSL）：文档已注明无 `make` 时直接执行等价命令。

### 验证

- `mypy src`（strict）通过：**113 个源文件无问题**（较 V2.7.0 增 4 个：`cli/main.py`、`engine/carla_supervisor.py` 及新路由/工具模块）。
- `pytest` 全量：**1008 项通过**（较 V2.7.0 的 867 项新增 141 项），无失败、无跨用例污染；告警 74 条均为既有第三方告警（starlette/httpx 弃用提示）。
- 覆盖率门禁：`pytest --cov` 全量 **Total coverage 84.3% ≥ 80%**（`fail_under=80`，分支覆盖开启；小数点后第二位因分支部分覆盖计数而跳动）；新增模块自身覆盖率：`cli/main.py` 92%、`engine/carla_supervisor.py` 93%、`api/routers/users.py` 91%、`execution/engine_pool.py` 99%。
- 新增测试文件：`test_users_router.py`（31 项：对外视图不含摘要字段、重复 409、`created_by` 落到调用主体、tenant/disabled 过滤、口令策略三类 422 + 含登录名（含大小写不敏感）、内置 admin 409、最后一名 admin 降级/禁用/删除 409、自助改密全链（401/403/409/轮换后旧口令失效）、用户表登录 JWT 携带 role+tenant、禁用用户 401、`last_login_at` 回写、`PLATFORM_USERS_ENABLED=false` 403、无状态库 503）、`test_carla_supervisor.py`（26 项：仅回环托管、启动成功/就绪超时/启动失败/未配二进制四种状态迁移、崩溃自动重启与计数、窗口内达上限转 exhausted 且不再拉起、过期记录 prune 恢复预算、开关关闭 disabled、手动 restart 不耗预算、stop 幂等与事件落 sink、sink 抛异常不影响自愈、snapshot 字段、recent_events 上限）、`test_doctor_cli.py`（29 项：11 个检查项三态与 hint 文案、多端点逐个探测、配置加载失败短路、`--strict` 提级、默认密钥 dev-warn/prod-fail、磁盘三档与 OSError skip、state_store 路径不可写 fail、api_port ok/warn/fail、gpu_env 折叠/展开/异常、`--json` 载荷键、退出码 0/1/2）、`test_map_scheduling.py`（10 项：同图亲和复用、异图优先干净端点、**双重占用防护**、销毁后端点回收、全占用 `ResourceError`、fallback/fresh 指标、无池回退 V2.4.0 语义）。扩展 `test_engine_pool.py::TestMapAffinity`（12 项）、`test_execution_plane.py`（实际地图覆盖、`endpoint_key_of`、`map_conflict` 四态）、`test_state_store.py`（摘要格式/恒时比对/非法摘要参数化 6 项、用户表 CRUD、**时序对齐**以 `_password_fingerprint` 探针断言恰为一次虚假 salt 调用）、`test_execution_router.py`（supervisor 未装配/仅观测/非托管端点/成功重启/非 admin 403）。
- 编排与清单：两份 compose + k8s 五份清单经 PyYAML 解析验证；deployment 引用的全部 configmap 键逐一核对存在；`Makefile` 各配方仅调用 `python`/`docker`，不依赖 sh 专有语法。
- CARLA/ROS2/Kafka/Qt 依赖仍经 `sys.modules` 桩注入，无引擎/无 broker/无 GPU 环境可全量回归。

### 实机执行（待办，尚未实机验证）

- [ ] Windows 单机：置 `CARLA_SUPERVISOR_ENABLED=true` + `CARLA_SUPERVISOR_BINARY_PATH`，任务管理器强杀 CarlaUE4，确认看门狗自动拉起、`GET /execution` 的 `carla_supervisor.endpoints[].total_restarts` 增长、审计出现 `engine.carla.exit/restart`。
- [ ] 连续强杀至窗口内达上限，确认转 `exhausted` 后不再抢管且有 `engine.carla.exhausted` 审计（防崩溃循环），人工 `POST /execution/engines/restart` 后恢复托管且不计入自动预算。
- [ ] 双引擎真机：实例 1 占 Town03 运行中，创建 Town05 实例，确认落到另一引擎且装配成功（V2.7.0 此场景被全局单地图误伤）；再创建 Town03 实例确认复用同图引擎、日志无多余 `load_world`。
- [ ] 建 3 个不同租户/角色用户，分别登录并创建/销毁实例，`GET /audit` 按 `actor` 核对操作可归属到人（不再全是 `admin`）。
- [ ] 全新机器按 `make doctor` → `make up` 走完首启；`huntersim doctor --json --strict` 接入 CI 作为上线门禁。
- [ ] `docker compose up -d` 观察启动顺序：确认 `sim-service` 等 Kafka healthy、Grafana 等 Prometheus healthy，Grafana 面板不再出现数据源首连失败重试。
- [ ] 重建镜像后验证容器内预检可用：`docker compose -f docker/docker-compose.yml exec sim-service huntersim doctor --strict`（本版已补 `ENV PATH` 指向 `--user` 脚本目录，**未在本机 docker 环境实机验证**；旧镜像可等价改用 `python -m hunter_sim.cli.main doctor`）。

### 分发

- `pyproject.toml` / `uv.lock` 版本提升 `2.8.0`，镜像 tag `hunter-sim:2.8.0`（compose / compose.windows / k8s deployment / `Dockerfile` 头注已同步）；新增 console script `huntersim`（Windows 编辑安装后为 `.venv\Scripts\huntersim.exe`）。
- **镜像内 console script 修复**：`docker/Dockerfile` 的 runtime 阶段依赖全部 `pip install --user`，入口脚本落在 `/home/hunter/.local/bin` 而该目录不在默认 `PATH`，导致 `docker compose exec sim-service huntersim doctor` 会 `command not found`；本版在 wheels 安装后补 `ENV PATH="/home/hunter/.local/bin:${PATH}"`（仅影响 PATH，不改 ENTRYPOINT/HEALTHCHECK 与依赖层次）。
- k8s：`configmap.yaml` 新增 `platform_users_enabled / platform_min_password_length / platform_password_hash_iterations`，deployment 注入上述三项 + `CARLA_SUPERVISOR_ENABLED="false"` / `CARLA_SUPERVISOR_BINARY_PATH=""`（集群形态显式关闭进程托管）。
- compose 两栈注入 `PLATFORM_USERS_ENABLED / PLATFORM_MIN_PASSWORD_LENGTH / PLATFORM_PASSWORD_HASH_ITERATIONS / CARLA_SUPERVISOR_*`，`.env.example` 同步（默认值同代码）。
- 升级步骤：`git pull` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`（或 `make up`）；无状态库迁移（`users` 表启动自动创建）；无新增第三方依赖（PBKDF2 走 stdlib `hashlib`）。上线前建议先跑 `huntersim doctor --strict`；建第一个平台用户：`POST /auth/users`（admin Token）。

### 待办

- [ ] 口令哈希升级为 bcrypt/argon2（需引入依赖）与 OIDC/LDAP 对接：本版为 stdlib PBKDF2，抗 GPU 破解强度低于专用 KDF。
- [ ] 多副本无状态化：限流/退避/吊销名单/租约/进程托管外置共享，SQLite 换 PostgreSQL——本版仅在文档中明确「单副本 + 重启恢复」为受支持形态。
- [ ] 用户删除时联动吊销其已签发 Token（当前靠自然过期）。
- [ ] supervisor 与引擎租约联动：自动重启前主动回收该端点上的租约并标记实例需重装配。
- [ ] `huntersim doctor` 增加 `login`（真实凭据试登录）与 `scenario`（端到端下发一个小场景）两个可选写操作检查（需显式开关，保持默认只读）。

---

## V2.9.0 — 阶段七「代码审查缺陷修复」（Blocker + High）

**发布日期**：2026-10-23
**版本定位**：针对系统代码审查报告中的 **1 个 Blocker + 4 个 High** 逐项修复，全部是既有代码的**正确性缺陷**（多引擎会把地图打到错误端点、禁用账号仍可续期、强收会挖断活场景、重启会阻塞整个服务、装配期销毁泄漏运行器）。不新增功能、不引入新依赖、不改库表结构；接口仅 `DELETE /execution/leases/{instance_id}` 语义收紧与回收响应新增 `instance_failed` 字段。

### 审查结论（V2.8.0 遗留）

1. **Blocker｜多引擎共用一个 MapManager/Client**：执行面装配时按端点解析管理器，但共享 `map_manager` 分支对**所有端点无条件命中** —— 多引擎部署下所有实例的 `load_world` 都打在 base 引擎上（A 实例加载地图会顶掉 B 端点正在跑的世界），且不同端点锁下并发复用同一个 `carla.Client`（CARLA 官方明确非线程安全）。
2. **High｜`/auth/refresh` 不回查用户表**：续期只验签名与吊销名单，账号被禁用/删除后旧 Token 在过期前可反复续期，角色被降级后仍按 Token 里的旧 claim 签发高权限。
3. **High｜强收不看租约死活、不处置实例**：`DELETE /execution/leases/{id}` 对**活跃租约**也直接回收（等于把正在跑的场景的引擎挖走）；回收后又只释放租约，实例仍停在 `running`，`_assign_endpoint` 继续把该端点算作已分配 —— 端点实际空闲却调度不到。
4. **High｜引擎重启阻塞事件循环**：`POST /execution/engines/restart` 在协程内同步调用 `supervisor.restart()`（拉起进程 + 就绪轮询，最长 `CARLA_SUPERVISOR_READY_TIMEOUT_SECONDS` 默认 60s），期间整个服务的 REST/WS 全部停摆。
5. **High｜装配-销毁竞态泄漏**：`ensure_runner` 的慢装配在注册表锁外执行（`load_world` 10~30s），窗口内 `release()` 查不到运行器与租约（尚未登记）而返回“未释放”，装配随后成功登记 —— **孤儿运行器继续 tick 且端点被永久占用**。

### 修复内容

- **Blocker 1 端点级 MapManager/Client 隔离（`api/runtime.py`）**：新增 `_resolve_map_manager(app, endpoint)` 与 `_is_base_endpoint(settings, endpoint)`，`ExecutionPlane` 的装配回调由“固定返回共享管理器”改为按端点解析。优先级：① `app.state.map_managers` 显式注入（键依次试 `endpoint.key` / `host` / `host:rpc`，注入即权威、未声明端点返回 `None`）→ ② 共享 `app.state.map_manager` **仅当端点确为 base 端点**（`host`+`rpc_port` 与 `CARLA_HOST`/`CARLA_RPC_PORT` 一致）时复用 → ③ 其余端点惰性新建 `carla.Client(host, rpc_port)` 并以 `CARLA_TIMEOUT_SECONDS` 设超时、包成独立 `MapManager`，按 `endpoint.key` 缓存到 `app.state.execution_map_managers`（端点不可达时告警并返回 `None`，由上层转 503）。无 `settings` 上下文（测试/库用法）时保持旧行为。
- **High 2 续期回查用户表（`api/routers/auth.py`）**：新增 `_resolve_refresh_claims(principal)`，签新 Token 前先 `StateStore.get_user()`：记录存在且禁用 → `401`（detail 含 `disabled`）；记录存在且启用 → 角色与租户**取自库内当前值**（降级即时生效）；记录不存在（已删除）→ 仅内置管理员（`principal.user_id == API_ADMIN_USERNAME`）放行，其余 `401`（detail 含 `not stored`）。新增 `_record_disabled(record)` 统一禁用口径：`StateStore.get_user()` 返回的是**原始行**（禁用标记为 `disabled_at` 时间戳），`list_users()` 才派生布尔 `disabled` —— 两种口径都认，字段类型异常按“已禁用”处理（fail-closed）。轮换式吊销（旧 `jti` 即时失效）语义不变。
- **High 3 回收仅限僵尸租约 + 联动实例终态（`api/routers/execution.py` / `api/runtime.py`）**：回收前先查 `plane.lease_of(instance_id)`，若租约仍在且 `not plane.is_stale_lease(instance_id)` → `409`（提示先 `stop` 或 `DELETE` 实例，不要挖断活场景）；强收成功后调用新增 `runtime.mark_instance_failed(app, id, reason)` 经实例状态机置 `FAILED` 终态（状态机拒绝时返回 `False` 只记 debug 日志，不影响回收结果），响应体新增 `instance_failed` 字段供调用方判断实例是否已同步失效。`EnginePool.is_stale()` / `ExecutionPlane.is_stale_lease()` 复用既有 `EngineLease.is_stale(ttl)` 判定，`EXECUTION_LEASE_HEARTBEAT_ENABLED=false`（TTL=0）时永不判僵，也就无从强收。
- **High 4 重启下沉线程 + 关停可中断（`api/routers/execution.py` / `api/main.py` / `engine/carla_supervisor.py`）**：路由改为 `await asyncio.to_thread(supervisor.restart, endpoint)`，重启期间事件循环继续服务其他请求；`_wait_ready()` 在轮询中就绪等待中检查 `self._stop.is_set()`，服务关停或手动重启时**立即返回**，不再白等 `ready_timeout` 并遗留孤儿进程；启动路径移除同步 `ensure_started()`（原实现会阻塞启动最长 `ready_timeout × 端点数`，且与看门狗首轮检查存在**双拉起窗口**），改为 `supervisor.start()` 后由看门狗首轮立即检查，进程就绪前 `/health` 保持 `degraded`（语义与 V2.8.0 观测一致）。
- **High 5 装配取消闸门（`execution/plane.py`）**：`ExecutionPlane` 新增 `_assembling: dict[str, int]`（在途装配计数）与 `_cancelled: set[str]`（取消标记）。装配进入慢操作前在注册表锁内登记在途；`release()` 在**同一把注册表锁**内发现该实例有在途装配即打取消标记（即使运行器/租约尚未登记也能传达销毁意图）；装配成功后在同一临界区内递减计数并查取消标记 —— 命中则**归还租约、销毁刚建好的运行器**并抛 `ExecutionUnavailable("Scene assembly cancelled: ...")`，正常路径才登记 `_runners`/`_runner_engines`。异常路径（装配失败/取消）统一清空计数与标记，避免状态残留。

### 向后兼容

- 接口仅一处语义收紧：`DELETE /execution/leases/{instance_id}` 对**活跃租约**由 200 改为 `409`（僵尸租约仍 200），响应 `data` 新增 `instance_failed`（增量字段）；其余端点请求/响应结构不变。
- 单引擎部署（默认形态）不受 Blocker 修复影响：唯一端点是 base 端点，仍复用启动期 `map_manager`，`/health` 与 `GET /execution` 字段不变。
- 续期回查只增加一次主键读：`STATE_ENABLED=false` 时 refresh 本就返回 503；内置管理员不受用户表约束（行为同 V2.7.0）。
- 回收后实例进入 `failed` 终态属**预期行为变更**：此前“回收但实例仍 running”是缺陷，依赖该错误状态的脚本应改为“回收后重建实例”。
- 装配取消仅发生在“装配窗口内主动销毁实例”这一竞态下：正常串行调用（装配完成后再销毁）行为不变；取消时返回 `503`（`ExecutionUnavailable`）而非 200，客户端销毁接口本身仍是幂等成功。
- 进程托管语义不变（默认 `CARLA_SUPERVISOR_ENABLED=false`）；仅启动不再同步拉起进程，`/health` 在首轮就绪前短暂 `degraded`。

### 风险

- `mark_instance_failed` 依赖实例状态机允许 `READY/LOADING/RUNNING/PAUSED → FAILED`；若实例已 `destroyed/completed` 则返回 `false`，此时端点释放已完成但实例状态不变（本就终态，无占用）。
- 端点级 `MapManager` 缓存（`execution_map_managers`）**无上限、无端口关闭**：端点数由 `EXECUTION_ENGINE_ENDPOINTS` 固定，长跑影响可忽略；引擎下线/换址需重启服务清缓存（与租约池的地图记忆同生命周期）。
- 续期回查在用户表不可读（状态库异常）时会抛错转为 500/503，属 fail-closed 方向：宁可拒绝续期也不签发无法追溯的 Token。
- 重启下沉线程后，同一端点的并发重启请求不再被事件循环天然串行化；supervisor 内部状态机（`backoff/exhausted`）与进程句柄仍会互斥，但建议运维不要并发触发同一端点重启。
- 取消闸门用“实例 ID”做键：同一 ID 被释放后在装配完成前**重新创建**（当前控制面不允许同 ID 重建，仅测试可构造）会被误取消。

### 验证

- `mypy src`（strict）通过：**113 个源文件无问题**。
- `pytest` 全量：**1030 项通过**（较 V2.8.0 的 1008 项新增 22 项，并改写 1 项端到端用例），无失败、无跨用例污染。
- 覆盖率门禁：`pytest --cov` 全量 **Total coverage 84.7%（实测 84.71%）≥ 80%**（`fail_under=80`）；重点模块 `api/routers/execution.py` 98%、`execution/engine_pool.py` 99%、`execution/plane.py` 94%。
- 新增用例：`test_execution_plane.py::TestAssemblyCancellation`（3 项：装配阻塞期间 `release()` → 抛 `ExecutionUnavailable` 且文案含 `cancelled`、已建运行器被 `destroy`、端点归还且计数/取消标记清空；成功装配无残留；无在途装配时 `release()` 不打标记）与 `TestLeaseStaleness`（1 项）；`test_engine_pool.py::TestLeaseStaleness`（3 项：TTL=0 永不判僵、超 TTL 判僵且 `renew` 可复活、未知实例 `False`）；`test_runtime.py::TestMapManagerResolution`（3 项：base 端点复用共享管理器、非 base 端点新建**独立** `Client`/`MapManager` 并按 key 缓存、注入的 `map_managers` 优先且未声明端点返回 `None`）与 `TestMarkInstanceFailed`（3 项：`READY→FAILED`、无实例管理器、状态机抛异常）；`test_execution_router.py::TestLeaseReclaim`（4 项：活跃租约 409 且租约仍在、僵尸租约 200 + `instance_failed=true` + 实例 `failed` + 端点空闲、未知租约 404、非 admin 403）与 `TestRestartNonBlocking`（1 项：`restart` 阻塞 0.2s 期间事件循环仍推进 ≥5 个 tick）；`test_users_router.py::TestRefreshRevalidatesUser`（4 项：禁用账号 401、operator 降级 viewer 后续期 JWT `role=viewer`、删除账号 401、内置管理员放行）。
- 端到端改写：`tests/integration/test_e2e_rest_flow.py::TestLeaseReclaim::test_admin_reclaims_only_stale_lease` —— operator 403 → 活跃租约 409 → 把 `lease.renew(now=time.time()-10000)` 伪造成僵尸 → 200 且 `instance_failed=true` → `GET /execution` 租约消失 → `GET /instances/{id}` 为 `failed`。
- **测试过程中抓到两个真实缺陷**（均已修复）：① 初版续期回查只读 `record.get("disabled")`，而 `StateStore.get_user()` 返回的行只有 `disabled_at`，禁用账号仍能续期 —— 新增 `_record_disabled()` 双口径后修复；② e2e 旧用例按“活跃租约可强收”断言 200，与新语义冲突，按 409→伪造 stale→200 的完整链路改写。

### 实机执行（待办，尚未实机验证）

- [ ] 双引擎真机：实例 A 占 Town03、实例 B 请求 Town05，确认 B 的 `load_world` 落在**自己的端点**（V2.8.0 会全部落在 base）且 A 世界不受影响；两实例并发加载地图确认无 `Client` 线程安全异常。
- [ ] 建普通账号登录 → admin 禁用该账号 → 用旧 Token 调 `POST /auth/refresh` 确认 `401`；恢复启用后再次续期应为 200 且 `role` 为库内当前值；降级后再验一次。
- [ ] 运行中实例执行强收确认 `409` 文案；杀 tick 线程/断 CARLA 等 TTL 过期后强收确认 200 + `instance_failed=true`、`GET /instances/{id}` 为 `failed`，随后创建新实例立即装配成功。
- [ ] 真实 CARLA 下 `POST /execution/engines/restart`，期间并发 `GET /health` 与 WebSocket 订阅，确认无请求堆积/超时（V2.8.0 期间整服务停摆）。
- [ ] 真实 `load_world`（≥10s）期间 `DELETE /instances/{id}`，确认日志出现 assembly cancelled、`GET /execution` 端点空闲、无孤儿运行器 tick。

### 分发

- `pyproject.toml` / `uv.lock` 版本提升 `2.9.0`，镜像 tag `hunter-sim:2.9.0`（`docker/docker-compose.yml` / `docker-compose.windows.yml` / `k8s/deployment.yaml` / `Makefile` 的 `IMAGE_TAG` / `docker/Dockerfile` 头注示例已同步）；文档（README、运维手册、用户手册、单机部署指南）同步 V2.9.0。
- 无新增第三方依赖；无库表结构变化与迁移（`users`/`token_revocations` 等表与 V2.8.0 一致，启动自动建表）。
- k8s / compose 环境变量无新增：本版只改代码路径，`EXECUTION_LEASE_*`、`CARLA_SUPERVISOR_*` 语义沿用 V2.7.0/V2.8.0。
- 升级步骤：`git pull` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`（或 `make up`）；单机指南拓扑需把 `build -t hunter-sim:2.9.0` 的 tag 与本步对齐。

### 待办

- [ ] `_assign_endpoint` 仍以控制面实例状态为准，回收与调度的一致性靠 `mark_instance_failed` 保证；后续可让调度**直接以租约为唯一真源**，彻底消除“状态与租约两份真相”。
- [ ] 端点级 `MapManager` 缓存与 `carla.Client` 无关闭路径：多引擎动态上下线场景需引入端点生命周期管理（当前端点数静态）。
- [ ] 续期回查为每次一次状态库读：外置 PostgreSQL 后可加短 TTL 缓存，避免高频 refresh 放大数据库压力。
- [ ] 并发重启同一端点的互斥仍靠 supervisor 内部状态机，可考虑在路由层加端点级异步锁以给出明确的 409/排队语义。

---

## V2.10.0 — 阶段八「代码审查缺陷修复」（Medium + Low）

**发布日期**：2026-10-23
**版本定位**：在 V2.9.0（Blocker + High）基础上继续修复代码审查报告中的 **Medium + Low** 问题，集中在**事件循环阻塞、进程内状态无界增长、安全校验缺口与接口契约一致性**四类。不新增功能、不引入新依赖、不改库表结构；仅 `GET /execution` 回退快照补字段、`StateStore.get_user()` 返回值补 `disabled` 键，以及关闭用户体系后的续期语义收紧。

### 审查结论（V2.9.0 遗留）

1. **Medium｜认证链路在事件循环内同步访问状态库**：用户管理路由（含 PBKDF2 100k 次日均计算）、`/auth/refresh` 回查用户表、JWT 中间件逐请求查吊销名单、API Key 摘要校验、WS 吊销查询全部同步执行，高并发登录/鉴权时整个事件循环被阻塞。
2. **Medium｜进程内状态无界增长**：`LoginFailureGuard._states` 按 IP 累积且无回收；快照写节流守卫 `_snapshot_write_guard["last"]` 按实例累积，销毁后不清理；`StateStore.prune_revoked_tokens()` 已实现但无任何调用点，`revoked_tokens` 表随时间单调增长。
3. **Medium｜关闭用户体系后旧 Token 仍可续期**：`/auth/refresh` 未检查 `PLATFORM_USERS_ENABLED`，用户表账号在用户体系关闭后仍能反复换取新 Token（登录路径已按开关跳过用户表，两条链路语义不一致）。
4. **Medium｜用户表两条读路径结构不一致**：`StateStore.get_user()` 返回原始行（禁用标记为 `disabled_at` 时间戳），`list_users()` 才派生布尔 `disabled` —— 这正是 V2.9.0“禁用账号仍可续期”缺陷的根因温床。
5. **Medium｜执行面回退快照缺字段**：`describe_execution()` 在执行平面未初始化时返回的 payload 缺少 `stale_leases` / `lease_ttl_seconds` / `map_affinity_enabled`，与 `ExecutionPlane.snapshot()` 的契约不一致，客户端/看板需做条件分支。
6. **Medium｜WebSocket 广播被慢客户端拖住**：`broadcast()` 串行 `await send_text()`，同一实例一个慢连接会阻塞其他所有订阅者；且握手与长连接均不校验吊销名单，已退出登录的 Token 仍可新建/保持订阅。

### 修复内容

- **认证链路下沉线程池（`api/routers/users.py` / `api/routers/auth.py` / `api/middleware/auth.py`）**：用户 CRUD、改密、续期、吊销、API Key 签发/列表/吊销中的全部 `StateStore` 调用改为 `run_in_threadpool`（含 PBKDF2 计算与 SQLite 读写）；`_guard_last_admin` 改为 async；JWT 中间件 `_is_revoked()` 与 API Key `verify_api_key()` 同样离开事件循环。登录路径原本已用线程池，本版把其余认证入口对齐。
- **限流守卫空闲回收（`api/middleware/rate_limit.py`）**：`LoginFailureGuard._states` 值扩展为 `(failures, blocked_until, last_seen)`；`record_failure` 在新键且达 `4096` 软上限时回收空闲超过 3600s 的记录，`retry_after` 查询到过期记录时顺带删除；`reset`/退避判定语义不变。
- **快照守卫与吊销名单清理（`api/runtime.py` / `api/main.py`）**：新增 `_forget_snapshot_guard()` / `_clear_snapshot_guards()`，在 `release_scene_runner`、`force_release_execution_lease`、`release_all_scene_runners` 中清理；守卫条目达 `4096` 时按活跃运行器集合兜底清理残留键。健康巡检新增 `_prune_revoked_tokens()`，按 3600s 节流调用 `StateStore.prune_revoked_tokens()`，避免每个巡检周期扫表。
- **续期遵守用户体系开关（`api/routers/auth.py`）**：`PLATFORM_USERS_ENABLED=false` 时仅内置管理员（`API_ADMIN_USERNAME`）可续期，用户表账号返回 `401` 并提示重新用内置账号登录；开关打开时保持 V2.9.0 的回查语义（禁用 401、删除后非内置 401、降级按库内新角色签发）。
- **用户表读路径对齐（`persistence/state_store.py`）**：`get_user()` 与 `list_users()` 一样派生布尔 `disabled`（字段异常按已禁用 fail-closed），原始 `disabled_at` 保留；`verify_user()` 仍直接用时间戳，行为不变。
- **执行面回退契约补齐（`api/runtime.py`）**：无执行平面时按 `EXECUTION_LEASE_HEARTBEAT_ENABLED` / `lease_ttl_seconds` 计算并返回 `stale_leases=0`、`lease_ttl_seconds`、`map_affinity_enabled=true`，与平面快照字段对齐。
- **WebSocket 广播隔离与吊销复核（`api/routers/websocket.py`）**：`broadcast()` 改为 `asyncio.gather` 并发发送，单条 `send_text` 设 5s 超时，失败/超时连接统一清理；新增 `_is_token_revoked()`（经 `asyncio.to_thread` 查库，读库失败按未吊销处理），握手阶段即拒绝已吊销 Token，长连接每 60s 心跳前复核一次，命中则 `4401` 关闭。

### 低危项同步修复

- `_resolve_map_manager()` 复用 `_is_base_endpoint()`，删除重复的 base 端点判定；`settings` 缺省时新建 `carla.Client` 仍设 10s 超时。
- `ExecutionPlane.heartbeat()` 将 `is_running` 读取移入注册表锁内，消除与释放/替换的毫秒级竞态。
- `ExecutionPlane.stale_instance_ids()` 修正 docstring，与“返回全部心跳超时租约”的实现一致。
- `release_scene_runner()` / `force_release_execution_lease()` 的 `active_engine_count` 判断与 `scene_world_map_id` 写入移入控制面锁，与装配路径的写入口径一致。
- `RequestLogMiddleware` 对 query 中的 `token` / `access_token` / `api_key` / `password` 值脱敏为 `***` 后再入日志。
- `EnginePool.snapshot()` 的 `stale_leases` 直接复用租约字典里已计算的 `stale` 标记，去掉一次重复遍历。

### 向后兼容

- `GET /execution` 仅在“执行平面未初始化”的回退分支新增 3 个字段，正常分支字段不变；客户端可无条件读取 `stale_leases` / `lease_ttl_seconds` / `map_affinity_enabled`。
- `StateStore.get_user()` 返回值新增 `disabled` 布尔键（增量）；`disabled_at`、`pass_hash` 等原有字段与 `verify_user()` 行为不变。
- 认证结果语义不变，仅执行位置从事件循环移到线程池；登录/改密/API Key 校验的响应码与文案保持原样。
- `PLATFORM_USERS_ENABLED=false` 时用户表账号续期由“可续期”改为 `401`（预期语义收紧，与登录路径对齐）；内置管理员不受影响。
- WebSocket 握手与长连接新增吊销校验：已吊销 Token 的新连接会被 `4401` 拒绝，存量连接在下一个 60s 心跳周期内断开（安全行为增强）。
- 日志中敏感 query 值显示为 `***`：排障时不能再从日志读取 Token/密钥（安全优先）。
- 无新增环境变量、无新增第三方依赖、无库表结构变化或迁移。

### 风险

- 线程池化后高并发认证会在默认线程池排队，极端压力下延迟上升，但不再阻塞其他 REST/WS 请求；后续可对登录单独限流。
- WebSocket 每连接每分钟一次吊销名单查询：大量订阅时读库次数 = 连接数/分钟，主键索引查询成本低，仍建议按实例连接数规划 SQLite 负载。
- `get_user()` 新增 `disabled` 键会改变“严格比较返回字典”的外部脚本行为（内部调用方已适配）。
- 快照守卫的软上限清理只删除“不在 `scene_runners` 中”的键：若实例被绕过释放路径直接删除，残留键要等到下一次软上限触发才清理。
- 日志 query 脱敏不可逆：需要原始参数复现问题时请使用请求体/客户端抓包。

### 验证

- `mypy src`（strict）通过：**113 个源文件无问题**。
- `pytest` 全量：**1047 项通过**（较 V2.9.0 的 1030 项新增 17 项），无失败、无跨用例污染。
- 覆盖率门禁：`pytest --cov=src/hunter_sim --cov-fail-under=80` 全量 **Total coverage 84.86% ≥ 80%**；重点模块 `api/middleware/rate_limit.py` 100%、`execution/engine_pool.py` 99%、`execution/plane.py` 94%、`api/routers/users.py` 91%。
- 新增/调整用例：`test_rate_limit.py`（失败状态软上限回收、查询时回收）；`test_state_store.py`（`get_user` 派生 `disabled`）；`test_runtime.py`（回退快照 3 字段契约）；`test_p1_ops_chain.py`（守卫 `_forget_snapshot_guard`、软上限兜底、吊销名单按小时节流与无存储跳过）；`test_users_router.py`（关闭用户体系后用户表账号续期 401、内置管理员仍可续期）；`test_websocket_router.py`（慢客户端不阻塞广播、吊销命中/缺 jti/无存储/读库失败）；新增 `test_request_logging.py`（query 脱敏 4 项）。

### 实机执行（待办，尚未实机验证）

- [ ] 并发压测 `/auth/token` 与 `/auth/users`（PBKDF2 100k 次），确认登录期间 `GET /health` 与 WebSocket 推送延迟无明显抖动。
- [ ] 建立 WS 订阅后调用 `POST /auth/revoke`，确认连接在下一个心跳周期内收到 `4401` 关闭；用已吊销 Token 新建连接应立即被拒。
- [ ] 置 `PLATFORM_USERS_ENABLED=false` 后用普通用户旧 Token 调 `/auth/refresh` 确认 `401`，内置管理员仍能续期。
- [ ] 长时间运行后检查 `revoked_tokens` 行数受小时裁剪控制、`LoginFailureGuard._states` 与快照守卫不再随请求/实例数单调增长。
- [ ] 多客户端订阅同一实例，注入一个不读消息的慢连接，确认其他客户端推送不受影响且慢连接在 5s 后被清理。

### 分发

- `pyproject.toml` / `uv.lock` 版本提升 `2.10.0`，镜像 tag `hunter-sim:2.10.0`（`docker/docker-compose.yml` / `docker-compose.windows.yml` / `k8s/deployment.yaml` / `Makefile` 的 `IMAGE_TAG` / `docker/Dockerfile` 头注示例已同步）；文档（README、运维手册、用户手册、单机部署指南）同步 V2.10.0。
- 无新增第三方依赖；无库表结构变化与迁移（`revoked_tokens`、`users` 等表与 V2.9.0 一致，启动自动建表）。
- k8s / compose 环境变量无新增：本版只改代码路径与巡检清理逻辑。
- 升级步骤：`git pull` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`（或 `make up`）；单机指南拓扑需把 `build -t hunter-sim:2.10.0` 的 tag 与本步对齐。

### 待办

- [ ] WebSocket 只在心跳周期复核**吊销名单**，账号禁用/删除仍不即时断开（需在心跳中增加用户表查询或引入 token 版本号）。
- [ ] 改密/删除用户仍不批量吊销其已签发 Token：需按 `user_id` 记录签发批次或引入“用户级 token 版本”。
- [ ] `audit_log()` 的 SQLite sink 仍在事件循环内同步写（单条 INSERT，影响小）；后续可改为有界队列 + 后台 writer。
- [ ] 端点级 `MapManager` / `carla.Client` 仍无关闭路径，端点动态上下线需引入生命周期管理。
- [ ] 并发重启同一端点仍靠 supervisor 内部状态机，路由层端点级互斥锁未实现。

---

## V2.10.1 — Docker 构建 pip 超时修复（网络环境）

### 现场

Windows + Docker Desktop 实机执行 `docker compose -f docker/docker-compose.yml build sim-service`：`requirements.txt` 安装阶段耗时异常（1100s+），随后在 `pip install carla==0.9.16` 下载 34MB wheel 时速度仅约 45 kB/s，最终报 `pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org')`，构建失败（exit code 2）。

### 根因

非代码缺陷：容器内 pip 直连 PyPI CDN 速度过慢。Docker/WSL2 容器不继承 Windows 系统代理，宿主机挂 VPN 对容器内下载无效；原 Dockerfile 未设 `--timeout/--retries`，也无镜像源切换入口。

### 修复内容

- `docker/Dockerfile`：新增 `ARG PIP_INDEX_URL` / `ARG PIP_TRUSTED_HOST`（默认官方源，不改变既有构建行为），runtime 阶段 `requirements.txt` 与 CARLA SDK 安装均接入镜像源参数，并统一加 `--retries 5 --timeout 120`；builder 阶段同步加重试/超时。
- `docker/docker-compose.yml`：`sim-service.build.args` 透传 `PIP_INDEX_URL` / `PIP_TRUSTED_HOST`（默认为官方源，可用环境变量覆盖）。
- `docs/单机部署指南.md`：§3.1 构建要点新增“慢速网络构建”说明（含阿里云镜像源示例与代理不继承提示）；§3.4 故障排查表新增 `ReadTimeoutError ... files.pythonhosted.org` 条目。

### 向后兼容

默认参数仍为 `https://pypi.org/simple` + `--retries 5 --timeout 120`，不传 build-arg 时构建行为与 patch1 前一致（仅抗超时能力增强）；无代码/接口/依赖变化。

### 风险

- 镜像源与 PyPI 存在同步延迟（小时级），选版本严格 pin 的包（carla==0.9.16）影响可忽略；使用第三方镜像源需自行信任其内容。

### 验证

- Dockerfile / compose 改动后语法校验无误（多阶段 ARG 作用域已在 base/runtime 两段分别声明）；本次未涉及 Python 代码，不影响 mypy/pytest 门禁。
- [ ] 实机验证待确认：在 D:\python\HunterSim 用镜像源参数重跑 build 并确认 artifacts 生成、容器 healthy（见待办）。

### 实机执行（待办）

```powershell
# PowerShell 下重跑构建（镜像源）：
$env:PIP_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/"
$env:PIP_TRUSTED_HOST="mirrors.aliyun.com"
docker compose -f docker/docker-compose.yml build sim-service
docker compose -f docker/docker-compose.yml up -d sim-service
```

- [ ] 确认 build 成功且 `docker compose ps` 中 sim-service 转 healthy，`/health` 显示 `carla_sdk_installed=true`。
- [ ] 如仍超时，备选：Docker Desktop → Settings → Resources → Proxies 配置宿主机代理（需代理允许容器网络访问）。

### 分发

- 仅改 `docker/Dockerfile`、`docker/docker-compose.yml`、`docs/单机部署指南.md`；镜像版本/包版本不变，无需重新分发 hunter-sim 包。

---

## V2.10.2 — 桌面客户端出生点与传感器 Parent 易用性改进

### 现场

Windows 实机使用桌面客户端（HUNTER SE — CARLA 仿真客户端）：连接 Town03 后在 Add Actor 以默认坐标 (0,0,0.5) 点 Spawn → 日志“生成失败（位置非法/被占）”；随后在 Add Sensor 以默认 Parent=0 点 Attach → “ERROR: 父 Actor 不存在：0”；右侧画面一直 `Waiting for Signal...`。

### 根因

非连接/崩溃缺陷，为 GUI 易用性问题：

- 世界原点 (0,0) 在多数地图（含 Town03）不是合法出生点（空地/水面/路网外），`try_spawn_actor` 返回 None；
- Actor 未生成→ `_actors` 为空，而 Parent 输入框默认 0（CARLA 真实 actor id 自分配、非 0），挂载传感器必然“父 Actor 不存在”；无相机→无帧。

### 修复内容

- **A、出生点回退**：`sim_presets.py` 新增无 Qt 纯逻辑 `choose_spawn_point_index` / `pick_spawn_point`（count≤0 返回 None，preferred 越界回落随机，可注入 rng）；`sim_worker.py` 的 `_do_spawn` 新增 `use_spawn_point` 参数，勾选或坐标仍为默认 (0,0) 时从 `world.get_map().get_spawn_points()` 取合法出生点；失败日志附实际坐标与“可勾选使用地图出生点”提示，自车生成成功时回显 id 引导挂载。`actor_panel.py` 新增“使用地图出生点”复选框，`spawnRequested` 信号扩展为 `(str,float,float,float,bool)`。
- **B、Parent 自动回填**：`sensor_panel.py` 将 Parent 由 `QSpinBox` 改为可编辑 `QComboBox`，`set_parents()` 用已生成 actor id 回填下拉（保留当前输入），新增 `_parse_actor_id` 解析（非数字回退 0，由 worker 统一报错）；`main_window.py` 无需改动（`actorsChanged → set_parents` 已接线）。

### 向后兼容

- `request_spawn` 新增参带默认值 `False`，旧调用方不变；`spawnRequested` 信号多一个 bool，与新 `request_spawn` 签名对齐（同一仓库内接线）。
- Parent 从数字框改为可编辑下拉，仍可手动输入未列出的 id；交互语义不变。
- 无新增环境变量/第三方依赖，无库表变化。

### 风险

- 随机出生点可能在连续多次 Spawn 时选到相邻/相同位置导致互相占用（`try_spawn_actor` 返回 None）；日志会提示改用其他坐标或勾选出生点，用户可重试。
- 坐标恰为 (0,0) 的刻意生成会被回退逻辑拦截（(0,0) 在常见地图几乎总是非法，误伤概率极低）。

### 验证

- **flake8**（项目 setup.cfg 配置，v6.1.0）：改动的 5 个文件零报错。
- **mypy strict**：`sim_presets.py`（未排除，strict）与 `sim_worker.py`/panels（按 pyproject `ignore_errors` 排除）均无问题。
- **pytest**：全量 **1052 项通过**（较 patch1 前新增 5 项）；新增 `test_gui_logic.py` 对 `choose_spawn_point_index`（空/越界/优先/随机可复现）与 `pick_spawn_point` 的直接覆盖。

### 实机执行（待办，尚未实机验证）

- [ ] 桌面客户端连 Town03：不填坐标直接勾选“使用地图出生点” Spawn，确认自车落在路面且日志回显 id；Parent 下拉自动含该 id。
- [ ] 选 Parent 后 Attach RGB Camera + Start，确认右侧 SOURCE 出图（不再 `Waiting for Signal`）。

### 分发

- 仅改 `src/hunter_sim/gui/`（sim_presets / sim_worker / panels/actor_panel / panels/sensor_panel）与 `tests/test_gui_logic.py`、`user_manual.md`、`release.md`；需重新构建含 GUI extras 的桌面客户端环境（`pip install -e ".[gui]"`）后重启客户端生效。

---

## V2.11.0 — 数据采集分析系统接入全量重构（2026-10-23）

### 概述

本版本完成 HunterSim 与 数据采集分析系统的 Kafka 契约对齐：以 `carla.{sim_id}.sensor_data`（6 类消息）取代旧 `hunter.{vehicle_id}.telemetry/event/health` 发布链路，以 `carla.{sim_id}.vil_state` 取代旧 `telemetry_clean` 消费链路。所有实例按 `sim_id` 隔离 Topic 命名空间，支持多实例并行接入平台。

### 核心变更

| 模块 | 变更 |
|------|------|
| **消息契约层** | 新增 `cloud/sim_data_mapper.py`（6 类构造函数 + `SimStatusPayload` dataclass）；新增 `cloud/vil_state_parser.py`（解析 vil_state → TelemetryFrame） |
| **生产者** | 新增 `cloud/sim_data_producer.py`（`SimDataProducer`：key=sim_id、acks=all、有界缓冲 2000、DLQ 转发）；`CloudConnector` 聚合入口全面替换为 SimDataProducer |
| **配置模型** | `CloudSettings` 删除旧 `vehicle_id/telemetry_topic/event_topic/health_topic`，新增 `sim_id_prefix/sensor_data_topic_template/sensor_data_dlq_template/sensor_data_acks/sensor_data_max_inflight/status_report_interval_s/telemetry_proxy_interval_s/sim_id`；`KafkaSettings.telemetry_topic` 改为 `carla.{sim_id}.vil_state` 模板，新增 `sim_id/legacy_telemetry_topic` |
| **VIL 消费** | `VILDataConsumer` 支持 `{sim_id}` 模板渲染、sim_id 一致性校验、新/旧格式自动检测、`vil_state_parser` 集成 |
| **场景运行器** | `SceneRunnerService` 注入 `SimDataProducer`：1Hz `sim_status`、10Hz `sim_telemetry`、逐事件 `sim_event`；`DetectedEvent` 新增 `event_level/sim_time` |
| **评估回传** | `EvaluationEngine.publish_results()`（@staticmethod）发布 `sim_metrics` + `artifact`；`EvaluationReport.to_artifact_info()` 辅助；`CloudConnector.upload_bag` 成功后自动发布 `sensor_file` |
| **API / 实例** | `CreateInstanceRequest` 新增 `sim_id/calibration/kafka_topics`；新增 `VilCalibration/KafkaTopicConfig/BatchScenarioRequest` 模型；`SimInstance` 扩展 `sim_id/calibration/vil_state_topic/sensor_data_topic`；新增 `POST /instances/{id}/vil/calibrate` 与 `POST /instances/batch` |
| **监控指标** | 8 个新 Prometheus 指标：`SIM_STATUS_PUBLISHED_TOTAL`、`SIM_EVENT_PUBLISHED_TOTAL`、`SIM_METRICS_PUBLISHED_TOTAL`、`SENSOR_FILE_PUBLISHED_TOTAL`、`SIM_DATA_PRODUCER_ERRORS_TOTAL`(labeled)、`SIM_DATA_BUFFER_SIZE`(labeled)、`VIL_STATE_CONSUMED_TOTAL`、`VIL_E2E_LAG_MS`(Histogram) |
| **循环导入修复** | `vil_mapper/__init__.py` 改 PEP 562 `__getattr__` 延迟导入 `parse_vil_state`，消除 `cloud ↔ vil_mapper` 循环依赖 |
| **向后兼容** | `TelemetryProducer` 标注 @deprecated 保留；`legacy_telemetry_topic` 兼容旧 telemetry_clean 单机调试；`KafkaSettings.telemetry_topic` 渲染前保留原始模板 |

### 新增/删除文件

| 操作 | 文件 |
|------|------|
| 新增 | `cloud/sim_data_producer.py`、`cloud/sim_data_mapper.py`、`cloud/vil_state_parser.py` |
| 新增 | `tests/test_sim_data_producer.py`、`tests/test_sim_data_mapper.py`、`tests/test_vil_state_parser.py`、`tests/test_cloud_connector_sim.py`、`tests/test_vil_data_consumer.py`、`tests/test_scene_runner_service.py` |
| 修改 | `common/models.py`、`common/metrics.py`、`cloud/connector.py`、`cloud/__init__.py`、`cloud/bag_uploader.py`、`cloud/telemetry_producer.py`、`vil_mapper/vil_data_consumer.py`、`vil_mapper/__init__.py`、`scene_runner/scene_runner_service.py`、`scene_runner/event_detector.py`、`eval_service/evaluation_engine.py`、`eval_service/evaluation_report.py`、`api/runtime.py`、`api/models/__init__.py`、`api/routers/instances.py`、`resource_manager/instance_manager.py`、`.env.example` |

### Topic 命名规范

```
上行（发布 → 平台）：carla.{sim_id}.sensor_data        — 6 种 type
死信队列：           carla.{sim_id}.sensor_data.dlq    — Schema 校验失败/broker 不可达
下行（平台 → VIL）： carla.{sim_id}.vil_state          — 平台转发的实车状态（10Hz）
兼容旧格式（可选）： legacy_telemetry_topic            — 单机调试用
```

### 统一消息信封

```json
{
  "schema_version": "1.0.0",
  "sim_id": "sim-20261023-0001",
  "vehicle_id": "HUNTER-001",
  "scene_id": "uuid",
  "timestamp": 1724035200.123,
  "seq": 42,
  "type": "sim_status|sim_telemetry|sim_event|sim_metrics|sensor_file|artifact",
  "payload": { ... }
}
```

### 环境变量迁移

| 旧（V2.10.0） | 新（V2.11.0） | 说明 |
|---|---|---|
| `HUNTER_CORE_VEHICLE_ID` | 已删除 | sim_id 由实例创建时传入或自动 |
| `HUNTER_CORE_TELEMETRY_TOPIC` | `HUNTER_CORE_SENSOR_DATA_TOPIC_TEMPLATE` | `carla.{sim_id}.sensor_data` |
| `HUNTER_CORE_EVENT_TOPIC` | 已删除 | 统一走 sensor_data（type=sim_event） |
| `HUNTER_CORE_HEALTH_TOPIC` | 已删除 | 统一走 sensor_data（type=sim_status） |
| `KAFKA_TELEMETRY_TOPIC` | `KAFKA_TELEMETRY_TOPIC` | 默认值改为 `carla.{sim_id}.vil_state` |
| — | `KAFKA_SIM_ID` | 新增：当前实例 sim_id |
| — | `KAFKA_LEGACY_TELEMETRY_TOPIC` | 新增：兼容旧单机模式 |
| — | `HUNTER_CORE_SIM_ID_PREFIX` | 新增：GUI 单实例模式 sim_id 前缀 |
| — | `HUNTER_CORE_SIM_ID` | 新增：手动指定当前 sim_id |
| — | `HUNTER_CORE_STATUS_REPORT_INTERVAL_S` | 新增：sim_status 周期（1s） |
| — | `HUNTER_CORE_TELEMETRY_PROXY_INTERVAL_S` | 新增：sim_telemetry 周期（100ms） |

### 向后兼容声明

- `TelemetryProducer` 保留但不再被 `CloudConnector` 使用，供独立调试脚本继续使用。
- 未配置 `KAFKA_LEGACY_TELEMETRY_TOPIC`（默认空）时，VIL 仅订阅新格式 `carla.{sim_id}.vil_state`。
- API 端点完全兼容旧调用（新增字段均可选，不传时行为不变）。
- `CreateInstanceRequest.sim_id` 为空时由本服务自动生成（`sim-{YYYYMMDD}-{random4}`）。

### 测试验证

- 260 项 V2.11.0 相关单元测试 + 集成回归全部通过（0 failure）。
- 新增 6 个测试文件 + 更新 10 个既有测试文件。
- 循环导入已修复（`cloud.__init__` → `vil_state_parser` ← `vil_mapper.__init__`）。
- `publish_results` 改为 `@staticmethod`（无 self 引用）。

### 实机部署验证步骤

> 前提：已按单机部署指南完成源码安装 / Docker Compose 部署，CARLA 0.9.16 已启动，Kafka Broker 可达。

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 服务健康 | `GET /api/v1/sim/health` | `status=ok`，`carla_connected=true`，`kafka_connected=true`（平台启用时） |
| 2 | Kafka 连通 | `SimDataProducer.connect()` 或 `kafka-producer-perf-test` | 返回 True / 发送成功 |
| 3 | Topic 模板渲染 | 启动日志或 `GET /instances/{id}` 查看 `sensor_data_topic` / `vil_state_topic` | 形如 `carla.sim-20261023-0001.sensor_data`，含正确 sim_id |
| 4 | 创建实例（含 sim_id） | `POST /instances` body 指定 `sim_id`、`vehicle_id`、`calibration` | 响应 200，`data.sim_id` 与传入一致 |
| 5 | 下发场景 + 启动 | `POST /instances/{id}/scene` → `POST /instances/{id}/start` | 状态机转 `running`，无 503 |
| 6 | 上行消息验证 | `kafka-console-consumer --topic carla.{sim_id}.sensor_data --from-beginning --max-messages 10` | 收到 `type=sim_status`（1Hz）和 `type=sim_telemetry`（10Hz）消息，信封字段完整 |
| 7 | sim_event 触发 | 在场景中制造碰撞/急刹事件 | consumer 中出现 `type=sim_event` 且 `payload.event_level` 非空 |
| 8 | VIL 消费链路 | 向 `carla.{sim_id}.vil_state` 写入测试帧 → 查看实例日志 | VILDataConsumer 输出 “consumed vil_state” 且映射为 TelemetryFrame |
| 9 | 评估回传 | 场景结束后触发评测 → 观察 sensor_data Topic | 出现 `type=sim_metrics`（含 scores）和 `type=artifact`（含 download_url） |
| 10 | Bag 上传 → sensor_file | 实例停止后配置 `bag_upload_enabled=true` → 观察 Topic | `upload_bag` 回调后自动发布 `type=sensor_file` |
| 11 | DLQ 转发 | 手动投入格式错误消息到 producer | `carla.{sim_id}.sensor_data.dlq` 可消费到失败消息 |
| 12 | Prometheus 指标 | `GET /metrics` | 包含 `huntersim_sim_status_published_total`、`huntersim_vil_state_consumed_total`、`huntersim_vil_e2e_lag_ms` 等新指标 |
| 13 | 回归确认 | `pytest tests/ -x -q` | 260+ 用例全部通过，无 failure |

### 待办 / 已知边界

- 生产环境多副本需外置 Kafka 凭证与 Redis 缓冲（当前为进程内有界 deque）。
- DLQ 消费侧告警与自动重试属平台侧职责，本服务仅负责写入。
- `sensor_file` 消息中 `start_time/end_time/frame_count` 当前传入 0（需与 bag 录制元数据打通，后续版本补充）。

---

## V2.12.0 — 接入方案代码审查缺陷修复（2026-10-24）

### 概述

对照《CARLA 仿真系统接入数据采集与分析系统方案》（V1.0，1961 行）对 V2.11.0 落地实现做逐条审查，覆盖 14 个生产文件，修复**契约不符、发布链路断点、连接/内存泄漏、并发竞态**四类缺陷。审查结论：方案主体链路已打通，但存在 1 处契约严重偏离（vil_state 扁平格式）、3 处死代码未接线、2 处确定泄漏/死锁风险。

### 核心变更

| 类别 | 模块 | 修复内容 |
|------|------|----------|
| 契约对齐 | `cloud/vil_state_parser.py` | 重写对齐方案 §5.3：支持 `twist.linear[]`/`angular[]` 数组格式、`chassis.velocity`/`steering_angle`、`perception.objects[].object_id` 字符串（CRC32 稳定映射为非负 int）、`calibration` 段、`source_seq`/`source_timestamp`/`forwarded_at` 透传；`pose` 缺失改为抛 `KeyError` 走 DLQ，不再静默补 0 |
| 契约对齐 | `cloud/sim_data_mapper.py` | `sim_event` 增 `event_id`（幂等键，方案 §7.3.1，缺省 `uuid4().hex`）；`sim_status` 增 `events_triggered`（§5.4）；`sim_telemetry` 并发 `steering_angle`（规范名）与 `steering`（兼容别名）；`sensor_file` 增 `md5`/`format`/`topics`；`build_sim_status` 支持 `vehicle_id` |
| 发布链路 | `scene_runner/scene_runner_service.py` | `sim_status` 改用 `next_seq()` 递增（原恒 0，导致平台 `(sim_id, seq)` 幂等键失效）；状态跃迁即时上报 + PAUSED 期间按周期上报（原仅 1Hz 轮询且 PAUSED 完全静默）；终态补发幂等（`_terminal_status_published`）；`_handle_event` 接线使 `publish_event_to_platform()` 真正生效（原为死代码）；上报周期可配（`status_interval_s`/`telemetry_interval_s` 取自 `CloudSettings`，含下限钳制） |
| 死锁规避 | `scene_runner/scene_runner_service.py` | `_handle_state_change` 改用回调入参 `snapshot`，不再回查 `get_state_snapshot()` —— `SceneStateManager._lock` 为**不可重入** `Lock` 且持锁调用回调，回查会自死锁 |
| 资源回收 | `execution/plane.py` | 云端 producer 生命周期闭环：`release()` 与取消分支关闭 producer 并移出字典（原永不关闭 → Kafka 连接与内存泄漏）；新增 `_producer_lock` 保护 `_cloud_producers`/`_instance_sim_ids`，消除装配期无锁写竞态 |
| 实例校验 | `resource_manager/instance_manager.py` | `sim_id` 正则校验 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`；模式必填校验（VIL 需 `vehicle_id`、REPLAY 需 `replay_config`、非 VIL 不得携带 `calibration`）；同一 `vehicle_id` 仅允许一个活动 VIL 会话（方案 §6.4）；`sim_id` 重复直接拒绝；`SimInstance` 持久化并恢复 `sim_id`/`calibration`/`vil_state_topic`/`sensor_data_topic`（原重启后丢失平台绑定） |
| HTTP 语义 | `common/exceptions.py`、`api/error_handlers.py`、`api/routers/instances.py` | `ResourceError` 新增可选 `http_status` 提示：入参非法 → **400**、`sim_id` 重复/同车 VIL 互斥 → **409**（原先一律 503，平台侧会把调用方参数错误当作服务故障重试并告警）；GPU/引擎/配额耗尽不加 hint，仍返回 503 |
| API | `api/routers/instances.py` | 实例响应回填 `sim_id`/`vil_state_topic`/`sensor_data_topic` |
| 健壮性 | `cloud/sim_data_producer.py` | 新增 `SIM_DATA_DROPPED_TOTAL` 指标；本地缓冲溢出计数 + 告警（原静默丢最旧）；`flush()` 重投注册 errback（原投递失败被静默吞掉） |
| 健壮性 | `vil_mapper/vil_data_consumer.py` | command_result 回读改为就地更新最新帧，不再重复入环（原污染 `total_received` 与延迟直方图，且与消费线程存在写竞态）；`sim_id` 为空时不订阅字面量模板（原会静默卡在永远无数据的 Topic）；Topic 模板未知占位符降级跳过而非 `KeyError` 中断订阅；`sim_id` 不匹配计入丢弃指标 |
| 并发/性能 | `vil_mapper/vil_sync_service.py` | 断流（`SimTimeoutError`）日志 5s 限频；`stats`/`_latency_samples`/`_tick_times` 写入加锁 |
| 并发/性能 | `vil_mapper/vil_data_consumer.py` | `_update_lag` 由逐分区 `position(tp)` 改为一次 `position(assignments)`，减少 RPC 次数 |
| 类型门禁 | `api/models/__init__.py`、`cloud/connector.py`、`scene_runner/scene_runner_service.py` | 修复 3 处 mypy strict 错误（`dict`→`dict[str, Any]`、`Callable`→`Callable[..., Any]`、移除失效 `type: ignore`），`mypy src/hunter_sim` 恢复 0 错误 |

### 新增/变更指标

| 指标 | 类型 | 标签 | 含义 |
|------|------|------|------|
| `huntersim_sim_data_dropped_total` | Counter | `sim_id`、`reason` | 上行消息被丢弃数（当前 `reason=buffer_overflow`：本地有界缓冲满时丢最旧） |
| `huntersim_vil_message_dropped_total` | Counter | `reason` | 新增 `reason=sim_id_mismatch` 取值（原有 `out_of_order`/`duplicate_seq`） |

### 测试验证

- 全量回归 `pytest -q`：**1193 passed**，0 failure。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 116 source files**。
- 新增回归用例（共 54 例）：
  - `tests/test_vil_state_parser.py`：§5.3 契约 9 例（数组 twist / `chassis.velocity` / CRC32 `object_id` / `calibration` / 缺失 `pose` 走 DLQ）
  - `tests/test_sim_data_mapper.py`：V2.12 契约 8 例（`event_id` / `events_triggered` / `steering_angle` / `md5`+`format`+`topics`）
  - `tests/test_scene_runner_service.py`：发布链 5 例（跃迁即时发布 / seq 单调 / `vehicle_id`+`events_triggered` / 终态仅一次 / 事件上报）
  - `tests/test_resource_manager.py`：校验与持久化 10 例（模式必填 / `sim_id` 正则 / 重复拒绝 / 同车互斥 / 持久化往返 / 400、409 状态码 hint）
  - `tests/test_execution_plane.py`：producer 生命周期 3 例（释放即关闭 / 复用不重建 / 取消分支关闭）
  - `tests/test_sim_data_producer.py`：溢出计数与 errback 4 例
  - `tests/test_vil_data_consumer.py`：V2.12 加固 6 例（命令回读不重复入环 / Topic 模板降级 / 空 `sim_id` / `sim_id_mismatch` 指标）
  - `tests/test_instances_router.py`：HTTP 语义 3 例（VIL 缺 `vehicle_id` → 400 / 非法 `sim_id` → 400 / 重复 `sim_id` → 409）
  - `tests/test_vil_sync_service.py`（新增文件）：并发与日志治理 6 例（stats 快照副本 / 平均延迟推导 / 断流告警 5s 限频但逐帧计数 / 健康周期统计）

### 待办 / 待确诊

以下缺口经本次审查**确认存在但未修复**，需后续版本处理（不得按已闭环理解）：

- **`EvaluationEngine.publish_results()` 在生产链路无调用方**：全仓检索仅见定义（`eval_service/evaluation_engine.py`）与自身异常日志，`EvaluationEngine` 亦无实例化点。因此 `type=sim_metrics` 与 `type=artifact` 消息**当前不会被发出**；V2.11.0 中「评估回传」条目应据此现状理解，闭环需补齐「场景终止 → 评估 → 发布」的装配与 API 触发点。
- **`VILSyncService` 未被实例化**：仅 `vil_mapper/__init__.py` 导出，无生产装配点，VIL 同步循环实际未运行。
- **VIL 断流未上报 `sim_status(PAUSED)`**：方案 §8.2.3 要求实车数据中断时主动上报暂停语义，当前仅本地日志限频告警。
- **`VILSyncService` / `VILDataConsumer` 与 `SimDataProducer` 尚未打通**：VIL 侧未发布任何上行消息。
- `sensor_file` 的 `start_time`/`end_time`/`frame_count` 仍传 0（沿用 V2.11.0 待办）。

---

## V2.13.0 — HunterCore CARLA 管理面闭环（2026-10-28）

### 概述

新增独立包 `hunter_sim.platform_adapter`，以 `huntersim carla-manager` 进程提供 **CARLA 管理面控制平面**（默认 `127.0.0.1:8090`，仅内网、无鉴权头、不暴露 Ingress/NodePort）。HunterCore `scene-service` 通过 `CARLA_MANAGEMENT_ENDPOINT` 调用 8 个端点，完成实例创建、场景下发、进度/结果轮询与暂停/恢复/停止全链路。

适配器复用既有执行面（`ExecutionPlane` / `SceneRunnerService`）、数据面（`SimDataProducer` / `sim_data_mapper` / `VILDataConsumer` / `VILSyncService`）与评估/产物能力，不复制场景执行逻辑。V2.12.0 遗留的 `sim_metrics`/`artifact` 发布、`VILSyncService` 装配、VIL 断流 `PAUSED` 三项已在管理面链路闭环，并修正 `sim_telemetry` 的 `pose.yaw`、数值型 `velocity`/`acceleration` 等契约偏差。

### 核心变更

| 类别 | 模块 | 内容 |
|------|------|------|
| 控制平面 | `platform_adapter/app.py` | 新增 8 个平台端点与 `GET /health`；生命周期内完成注册表连接、重启恢复、执行器启停；实例视图同时返回 `sim_instance_id`/`instance_id`/`id`，状态使用平台词汇并附 `carla_status` |
| 串行调度 | `platform_adapter/executor.py` | 单 CARLA 世界串行队列：同一时刻只运行一个场景；`create`/`submit` 立即返回，地图加载（10~30s）在后台完成；相邻 revision 合并，只执行最新场景 |
| 幂等与合并 | `platform_adapter/service.py` | `sim_id` 命中非终态实例直接返回；同 `scene_id` 已有活动实例返回已有实例；`create` 后紧随的 `submit` 若配置等价（忽略 `quality`）不重启世界，配置真正变化才提高 revision 并重跑 |
| 重启恢复 | `platform_adapter/store.py` | 独立 SQLite 注册表（实例/场景/sim_id/进度/结果/看门狗截止时间），启动时把所有非终态实例标记为 `failed(message=adapter restarted)`，避免平台永久轮询 |
| 看门狗 | `platform_adapter/executor.py` | 超时阈值 `duration × 3 + 120s`（`ADAPTER_WATCHDOG_FACTOR` / `ADAPTER_WATCHDOG_GRACE_SECONDS`），超时停止运行器并置 `failed(message=watchdog_timeout)`；运行期间 0.5s 级落库进度 |
| 场景映射 | `platform_adapter/scene_mapper.py` | 只读平台已合并的 `scene_config`，**不重复应用 `param_overrides`、不改 `map`**；不支持的参与者行为/仿真模式显式失败（`unsupported_behavior:<value>` / `unsupported_mode:<mode>`），不做静默降级；`replay` 明确不支持 |
| VIL 数据面 | `platform_adapter/vil.py`、`vil_mapper/vil_sync_service.py` | VIL 运行时消费 `carla.{sim_id}.vil_state`（默认消费组 `carla-vil`），latest-wins + odom→map 标定 + `extrapolation_ms` 外推；`apply_once()` 挂到场景唯一 tick，禁止第二个 `world.tick()`；断流按 3.0 m/s² 减速至停车并上报 `PAUSED(message=vil_data_interrupted)`；会话/场景切换清空帧缓存与乱序闸门 |
| 回传面 | `platform_adapter/executor.py`、`platform_adapter/artifacts.py` | 场景结束后用 `EvaluationEngine` 生成评估结果，发布 `sim_metrics` 与 `artifact`；产物 `eval.json`/`events.jsonl`/`run.log` 先落本地，再尽力预签名上传 `hunter-scene-assets`，失败不阻断结果；`object_key` 形如 `scenarios/simulations/{sim_id}/{scene_id}/{name}` |
| 遥测契约 | `cloud/sim_data_mapper.py`、`scene_runner/scene_runner_service.py` | `sim_telemetry.pose` 统一提供 `yaw`（保留 `heading` 别名）与真实 `z`（`roll`/`pitch` 字段保留、缺省 0）；`velocity`/`acceleration` 改为契约要求的 number；`twist.vx/vy` 由速度与航向分解；`twist` 保留向量语义；支持可选 `sim_time`；`sim_status.updated_at` 改为 Unix epoch 秒（平台对外视图仍为 ISO8601 UTC） |
| 运行器扩展点 | `execution/plane.py`、`scene_runner/scene_runner_service.py` | 新增 `runner_hooks`（`pose_driver` / `sample_sink` / `status_provider`）与 `producer_for_sim_id()` / `producer_for_instance()`；`SceneRunnerService` 暴露 `world`/`ego_vehicle`/`get_ego_state()`，供 VIL 绑定与进度视图读取 |
| CLI | `cli/main.py` | 新增 `huntersim carla-manager`，支持 `--host` / `--port` / `--db-path` / `--log-level`，默认取 `ADAPTER_*` 环境变量 |

### 控制平面端点（默认 8090，仅内网）

| 方法与路径 | 语义 |
|------------|------|
| `POST /api/v1/instances` | 创建实例并立即入队；请求含 `scene_id`/`scene_name`/`scene_config`/`sim_id`/`mode`/`vehicle_id`/`vil`/`quality`；响应回显平台 `sim_id` 与三种实例 ID 键名 |
| `GET /api/v1/instances?scene_id=` | 按场景查询进行中/历史实例；无匹配返回 **200** + `{"items": []}`；不带参数时返回全部（创建时间倒序） |
| `POST /api/v1/instances/{id}/scenario` | 下发/重发场景配置；`scene_config` 已由平台合并 `param_overrides`；不存在返回 **404** |
| `GET /api/v1/instances/{id}` | 进度快照（状态/进度/碰撞/车道偏离/自车状态/时间戳）；不存在返回 **404** |
| `GET /api/v1/instances/{id}/result` | 结果视图；**非终态也返回 200 + 当前状态**（平台据此映射 3003），终态附 `success`/`evaluation`/`success_criteria_result`/`artifacts`/`failure_reason` |
| `POST /api/v1/instances/{id}/pause` | 幂等暂停；`status=running` 且 `carla_status=PAUSED`、`message=paused` |
| `POST /api/v1/instances/{id}/resume` | 幂等恢复；终态实例原样返回 |
| `POST /api/v1/instances/{id}/stop` | 幂等停止并回收 CARLA 资源；终态为 `canceled` |
| `GET /health` | 容器 liveness/readiness 探针 |

响应中的平台状态映射如下（未知原始状态**按失败处理**，不猜测）：

| CARLA/内部状态 | 平台状态 | 说明 |
|----------------|----------|------|
| `CREATED` / `LOADING` / `READY` / `PENDING` | `pending` | 世界加载中 |
| `RUNNING` | `running` | 正常运行 |
| `PAUSED` | `running` | 通过 `message=paused` 表达暂停 |
| `COMPLETED` / `SUCCEEDED` | `succeeded` | 场景正常结束 |
| `FAILED` | `failed` | 失败原因写入 `message` |
| `CANCELED` / `CANCELLED` | `canceled` | 平台停止或回收 |
| 其他未知值 | `failed` | fail-closed |

### 执行与恢复语义

- **单世界串行**：适配器只维护一个执行队列；实例按创建顺序执行，同一时刻只有一个场景运行，符合 CARLA 单世界约束。多实例请求排队而非并发加载。
- **立即返回**：`create` 只做幂等检查与入队，`submit` 只更新配置/revision；两者都不等待 `load_world`。平台应按 `pending → running → 终态` 轮询。
- **场景合并**：平台 `create` 时已携带完整 `scene_config`，随后的 `submit` 通常是同一份配置；适配器用规范化 JSON 比较（忽略 `quality` 元数据），配置等价时不重启，避免重复 `load_world`。
- **重启恢复**：进程重启不会续跑旧世界；所有 `pending`/`running` 实例统一置 `failed`，`message=adapter restarted`，由平台决定是否重新创建。已进入终态的实例保留结果可查。
- **看门狗**：从场景开始加载起计时，阈值 `duration × 3 + grace`；超时后停止运行器、释放端点并置 `failed(message=watchdog_timeout)`，防止单场景占满世界。
- **失败可见**：场景映射错误直接写实例 `message`（如 `unsupported_mode:replay`、`unsupported_behavior:xxx`、`invalid_scene_config:map`），不会以“成功但空跑”掩盖；执行环境不可用写 `execution_unavailable:<detail>`，内部异常写 `adapter_internal_error:<type>`。
- **平台已合并配置**：适配器不重新应用 `param_overrides`，也不修改 `scene_config.map`。平台需要覆盖参数时应在下发前完成合并。

### VIL 数据面

- **订阅**：`carla.{sim_id}.vil_state`，默认消费组 `carla-vil`（可由 `KAFKA_GROUP_ID` 覆盖）；`sim_id` 取平台 create 时原样回显的值，空值不订阅字面量模板。
- **标定**：平台 `vil.calibration` 的 `map_x/map_y/map_heading/odom_x/odom_y/odom_heading` 转为 `CalibrationParams`：
  - `yaw0 = -map_heading - odom_heading`
  - `x0 = map_x - (odom_x*cos(yaw0) - odom_y*sin(yaw0))`
  - `y0 = map_y + (odom_x*sin(yaw0) + odom_y*cos(yaw0))`
- **同步**：`TelemetryBuffer` latest-wins；`PoseExtrapolator` 在 `extrapolation_ms` 内做延迟补偿；每 tick 在 `world.tick()` 之前应用一次位姿，避免与场景运行器并发 tick。
- **断流**：超过 `max_data_latency_ms` 或尚无首帧时，按 3.0 m/s² 逐步减速至停车（不冻结在最后一帧）；健康状态判定为中断后，`sim_status` 上报 `PAUSED`、`message=vil_data_interrupted`；恢复收帧后回到 `RUNNING`。
- **会话切换**：自车 Actor 替换/场景重载时清空帧缓存并重置 `VILDataConsumer` 的 seq/timestamp 闸门，避免新会话首帧被误判为乱序丢弃。
- **配置覆盖**：`vil.extrapolation_ms` 与 `vil.data_timeout_ms` 可按实例覆盖（分别钳制 0~1000ms、100~5000ms）。

### 回传面与产物

- `sim_metrics`：场景结束后发布评估等级、`passed`、安全/效率/舒适分项、任务完成率、成功判据结果、接管标记。
- `artifact`：每个产物发布 `name`/`bucket`/`object_key`/`size_bytes`/`sha256`/`media_type`，Bucket 默认 `hunter-scene-assets`。
- 产物生成：`eval.json`（评估结果）、`events.jsonl`（检测事件）、`run.log`（运行摘要）。
- 上传：配置 `HUNTER_CORE_ENABLED=true` 且 `HUNTER_CORE_JWT_TOKEN` 有效时调用 data-collector `/api/v1/data/files/presign` 与 `/complete`；上传失败只告警，本地副本与结果登记仍保留（`object_key` 使用约定键）。
- 无 producer（未启用平台接入）时，指标/产物发布安全跳过，不影响场景结果与 `GET .../result`。

### 新增配置（`ADAPTER_*`）

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ADAPTER_HOST` | `127.0.0.1` | 监听地址；仅内网/本机，不要设为 `0.0.0.0` 后暴露公网 |
| `ADAPTER_PORT` | `8090` | 控制平面端口（平台 `CARLA_MANAGEMENT_ENDPOINT` 对应） |
| `ADAPTER_DB_PATH` | `data/adapter/carla_instances.db` | 实例注册表 SQLite 路径（重启恢复/幂等） |
| `ADAPTER_ARTIFACTS_DIR` | `data/adapter/artifacts` | 产物本地暂存目录 |
| `ADAPTER_ARTIFACT_BUCKET` | `hunter-scene-assets` | 产物对象存储 Bucket |
| `ADAPTER_BUSY_TIMEOUT_SECONDS` | `5.0` | SQLite 忙等待超时 |
| `ADAPTER_WATCHDOG_FACTOR` | `3.0` | 看门狗时长倍数 |
| `ADAPTER_WATCHDOG_GRACE_SECONDS` | `120` | 看门狗基础宽限（秒） |
| `ADAPTER_PROGRESS_INTERVAL_SECONDS` | `0.5` | 进度落库间隔（秒） |
| `ADAPTER_DEFAULT_DURATION_SECONDS` | `60` | 场景未提供 duration 时的默认时长 |
| `ADAPTER_MAX_SCENE_SECONDS` | `7200` | 平台下发时长上限；超限映射失败 |

平台侧对应配置：`CARLA_MANAGEMENT_ENDPOINT=http://127.0.0.1:8090`（跨机部署填内网地址）；创建实例、下发场景为**非幂等**调用，平台不自动重试，暂停/恢复/停止为幂等调用。

### CLI

```bash
huntersim carla-manager                    # 默认读取 ADAPTER_*，监听 127.0.0.1:8090
huntersim carla-manager --host 0.0.0.0 --port 8090
huntersim carla-manager --db-path data/adapter/carla_instances.db --log-level debug
```

### V2.12.0 待办闭环情况

| V2.12.0 待办 | V2.13.0 状态 | 说明 |
|--------------|--------------|------|
| `EvaluationEngine.publish_results()` 无生产调用方 | **管理面链路已闭环** | 执行器直接实例化 `EvaluationEngine`，场景结束后发布 `sim_metrics` 与 `artifact`；`publish_results()` 本身仍未被调用 |
| `VILSyncService` 未被实例化 | **管理面链路已闭环** | `VILRuntime` 按实例创建并绑定 `VILSyncService`，挂到场景唯一 tick 循环 |
| VIL 断流未上报 `sim_status(PAUSED)` | **已闭环** | `status_provider` 在断流时覆盖为 `PAUSED(message=vil_data_interrupted)` |
| `VILSyncService`/`VILDataConsumer` 与 `SimDataProducer` 未打通 | **管理面链路已闭环** | VIL 实例经 `SceneRunnerService` 发布 `sim_telemetry`/`sim_status`/`sim_event`；适配器另外发布 `sim_metrics`/`artifact` |
| `sensor_file.start_time`/`end_time`/`frame_count` 仍为 0 | **未修复** | 沿用 V2.11.0 待办，bag 上传链路未改动 |

> 上述闭环仅适用于 `carla-manager` 管理面装配路径；`/api/v1/sim` 旧 REST 入口的 VIL 装配不在本次改动范围。

### 测试验证

- 全量回归 `pytest -q`：**1289 passed**，0 failure。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 126 source files**。
- 覆盖率：全仓 **84.77%**；`platform_adapter` 包 **88.76%**。
- 新增适配器测试 **95 例**（7 个文件）：
  - `test_platform_adapter_app.py`（20）：8 端点契约、空查询、404、ISO 时间戳、状态映射、生命周期恢复
  - `test_platform_adapter_executor.py`（23）：串行队列、revision 合并、看门狗、暂停/恢复/取消、评估/发布、重启清理
  - `test_platform_adapter_vil.py`（12）：标定公式、外推、断流减速、PAUSED 覆盖、会话切换重置
  - `test_platform_adapter_mapper.py`（12）：场景/参与者/事件/天气映射、`param_overrides` 不重复应用、不支持行为/模式显式失败
  - `test_platform_adapter_service.py`（11）：`sim_id`/`scene_id` 幂等、create/submit 合并、控制语义
  - `test_platform_adapter_artifacts.py`（10）：产物落盘、object_key、预签名上传成功/失败降级
  - `test_platform_adapter_store.py`（7）：注册表 CRUD、幂等查询、重启标记
- 既有文件补充：`test_vil_data_consumer.py` 增加乱序闸门重置 1 例；`test_sim_data_mapper.py` 同步 `sim_telemetry`/`updated_at` 契约断言。

### 待办 / 已知边界

- `sensor_file` 的 `start_time`/`end_time`/`frame_count` 仍传 0（V2.11.0 遗留）。
- `sim_telemetry.sim_time` 已支持但当前场景运行器未传，消息中不出现该字段；平台侧应按可选字段处理。
- 管理面单实例串行，不支持同一进程并行跑多个 CARLA 世界；需要并行请部署多个管理面进程/端口并在平台侧分片。
- 适配器无鉴权，安全边界依赖网络隔离；禁止把 8090 暴露到公网或经 Ingress/NodePort 转发。
- 产物预签名上传失败只保留本地文件与约定 `object_key`；需要可下载时需修复 JWT/MinIO 连通后重跑或手工补传。

---

## V3.0.0 — 仿真端收敛与 VIL / 传感器链路补齐

**发布日期**：2026-11-02
**版本定位**：仿真端只做「执行」（跑 CARLA、用实车数据驱动虚拟自车、回传状态/事件/指标/传感器文件/画面），治理能力全部归云端；补齐 VIL 车辆状态同步与传感器录制–上传–通知全链。

### 概览

目标拓扑收敛为两个进程：

```
huntersim carla-manager  :8090   唯一业务入口（内网、无鉴权）
huntersim probes         :8080   只读探针面（/health + /metrics）
```

数据面契约不变：消费 `carla.{sim_id}.vil_state`，发布 `carla.{sim_id}.sensor_data`（6 类 type）+ `.dlq`。

### 破坏性变更

| 项 | 变更 |
|---|---|
| 业务面 | `/api/v1/sim` 全部端点删除；8080 只保留 `/health`（+`/health/live`、`/health/ready`）与 `/metrics` |
| 鉴权 | JWT / API Key / 用户 / 角色 / 限流 / 登录退避全部移除（安全边界改为网络隔离） |
| 模块 | 删除 `gui/`、`replay_service/`、`api/`、`persistence/`、`resource_manager/` |
| 配置前缀 | 删除 `API_*` / `STATE_*` / `PLATFORM_*` / `GUI_*`；新增 `PROBE_*`；`KAFKA_COMMAND_TOPIC` 删除 |
| 依赖 | 删除 `python-jose[cryptography]` / `websockets` / `python-multipart` / `aiofiles`；新增 `mcap` / `mcap-ros2-support`；删除 `[gui]` extra |
| CLI | `huntersim serve` → `huntersim probes`；`doctor` 删除 `state_store`/`users`/`jwt`，新增 `adapter_db`/`recordings_dir`/`mcap` |
| 版本 | `2.13.0` → `3.0.0`（同时修正 README 与包版本漂移） |

### 一、模块裁剪与替代

- 新增 `probes/`：`create_probes_app()` 仅注册 `GET /health`、`/health/live`、`/health/ready`、`GET /metrics`。`/health` 通过 `PROBE_ADAPTER_URL` 拉取控制面快照并透传 `carla_connected` / `carla_probe_error` / `active_instance` / `vil_state` / `sensor` / `cloud` / `version`；上游不可达仍返回 **200 + `status=degraded`**，失败原因写 `upstream.error`（避免容器 liveness 反复杀进程）。
- `common/models.py` 精简：删除 `APISettings` / `PlatformSettings` / `StateSettings` / `GuiSettings`；新增 `ProbeSettings`（`PROBE_` 前缀）；`ResourceSettings` 仅留 `instance_max_lifetime_seconds` / `health_check_interval_seconds`；`HunterSimSettings` 新增 `adapter` 字段（与 8090 共用同一份配置对象）。
- 编排收敛：`docker/Dockerfile` 去 `--workers`，`ENTRYPOINT huntersim` + `CMD carla-manager`，`EXPOSE 8090 8080`；`docker-compose.yml` 为 `sim-service` + `probes` + `kafka` + `prometheus` + `alertmanager` + `grafana`（`carla-engine` 收进 `--profile carla`）；`docker-compose.windows.yml` 为 `carla-server` + `sim-service` + `kafka`；`k8s/deployment.yaml` 改为单 Pod 双容器（`sim-service` + `probes`）、`replicas: 1`；`Makefile` 为 `manager` / `probes` / `test` / `coverage` / `typecheck` / `lint`。

### 二、VIL Bridge 补齐

- 新增 `vil_mapper/frame_aligner.py`：把 10–50Hz 实车帧规约到固定 10Hz 栅格。`grid_index = floor(now × target_hz)` 同栅格去重（降采样）；跨 `now` 的左右帧线性插值（航向最短弧）→ `interpolated`；仅左帧且在 `max_extrapolation_ms` 内 → `extrapolated`；其余 → `nearest` 并记录 `skew_ms`。
- `vil_mapper/vil_sync_service.py`：`apply_once()` 改走 `FrameAligner.next(now)`；新增舵角写入（`set_wheel_steer_angle` 前左/前右）、灯光同步（`StateVisualizer.apply_vehicle_lights`）、感知叠加（`PerceptionOverlay`，每 3 个栅格绘制一次）。CARLA 调用失败降级为 debug 日志，不影响 tick。
- `cloud/vil_state_parser.py`：新增 `lights` 段解析（`left_turn`/`right_turn`/`low_beam`/`hazard`，顶层或 `chassis.lights`），缺省时不写入任何键（不改动已开灯光）；`chassis.steering_angle` 内部统一**弧度**，报文标注 `steering_unit="deg"` 时解析阶段换算。`VehicleState` 增加 `lights`。
- `platform_adapter/vil.py`：装配 `FrameAligner` / `StateVisualizer` / `PerceptionOverlay`；`vil_config` 支持 `target_hz` / `overlay_enabled` / `lights_enabled` 覆盖；`status_override()` 在连续 `nearest` 且 `skew > 2×max_data_latency_ms` 时同样上报 `PAUSED(vil_data_interrupted)`。

### 三、Sensor Emulator 落地

- 新增 `sensor_sim/profiles.py`：`helios_16p`（16 线/150m/10Hz/FOV ±15°/σ=0.02m/131072 pts·s⁻¹）、`d435`（RGB 1280×720 HFOV 69° 30fps + Depth 1280×720 HFOV 87° 0.2–10m）、`ch10x`（IMU 100Hz/accel σ=0.01/gyro σ=0.001）、默认 `hunter_se_full`（三件套 + 三类事件传感器）。`configs/sensor_configs.json` 增 `profiles` 段，与代码双向对应（单测逐字段断言）。
- `scene_runner/scene_config.py` 增 `SensorSpec{profile, enabled, ros_enabled, record}` 与 `SceneConfig.sensors`；`platform_adapter/scene_mapper.py` 增 `sensors` 映射（平台缺省 → 默认 `hunter_se_full`）。
- 新增 `sensor_sim/frame_hub.py`：线程安全，`push/latest/stats`，stream ∈ `{rgb, depth, lidar, imu}`，只保留最新帧并维护 fps。
- `scene_runner/builtin_scenario.py`：`_attach_sim_sensors()` 按剖面创建 bundle 并挂到自车（安装位取 `HunterSESpec` 偏移与 `sensor_configs.json.mount`），回调统一走 `data_converters` → `FrameHub.push` → 录制器，`ros_enabled` 时额外投递 ROS2；`cleanup()` 幂等销毁；blueprint 缺失只 warning 不阻断。
- 新增 `sensor_sim/mcap_recorder.py`：纯 Python（`mcap` + `mcap_ros2.writer`），主题 `/carla/lidar_points`（PointCloud2）、`/carla/camera/color/image_raw`、`/carla/camera/depth/image_rect_raw`（Image）、`/carla/imu/data`（Imu）；`open/push_stream/close → RecordStats(start_time, end_time, frame_count, topics, size_bytes, path)`；依赖缺失或写失败降级 no-op + warning。`sensor_sim/data_recorder.py`（`ros2 bag record` 子进程包装）删除。
- ROS2 出口：`CarlaRosBridge` 由 `SimulationExecutor._ros_bridge_for(scene)` 按 `sensors[].ros_enabled` 惰性装配（`rclpy` 或构造失败 → None + warning），修复“导出但从未实例化”的死代码问题。

### 四、Monitor & Recorder

- 新增 `GET /api/v1/instances/{id}/screenshot`（`image/jpeg`，无相机/无帧 → `409 camera_not_attached`，实例不存在 → `404`）与 `GET /api/v1/instances/{id}/stream.mjpg`（`multipart/x-mixed-replace; boundary=frame`，客户端断开即结束）。
- `AdapterSettings` 新增 `stream_fps`(10.0) / `stream_max_width`(640) / `screenshot_quality`(75) / `recordings_dir` / `recording_retention_hours`(24)。
- 录制→上传→通知全链：`SimulationExecutor` 按 `sensors[].record` 打开 `McapRecorder`（`recordings_dir/{instance_id}/sensors_r{revision}.mcap`）→ 运行期逐帧写入 → 终态（成功/失败/取消）close 得 `RecordStats` → `cloud.enabled` 时上传 → 成功后发布 `sensor_file`。
- `sensor_file` 的 `start_time` / `end_time` / `frame_count` / `topics` / `size_bytes` 改为 `RecordStats` 真值，`file_format="mcap"`。
- `InstanceRecord` 增 `recording_path` / `recording_bytes`，`GET /instances/{id}` 与 `/result` 可见；录制文件按 `ADAPTER_RECORDING_RETENTION_HOURS` 在 `_watchdog_loop` 周期清理。

### 五、四条断链修复（对照）

| 断链 | 状态 | 落地方式 |
|---|---|---|
| `sensor_file` 时间/帧数恒为 0 | **已修复** | `RecordStats` 真值回填 |
| `CloudConnector.upload_bag()` 无调用点 | **已修复** | 场景结束链路上传并发布 `sensor_file` |
| 无画面回传端点 | **已修复** | `/screenshot` + `/stream.mjpg`（MJPEG） |
| `StateVisualizer` / `CarlaRosBridge` 导出未接线 | **已修复** | VIL 灯光同步 + 执行器 ROS2 桥接装配 |

### 六、质量门禁（实测）

- `pytest tests -q --cov=src/hunter_sim`：**1007 passed**，全仓行覆盖 **83.79%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 80 source files**。
- `flake8 src tests`：**0 告警**。
- 新增“无死代码”门禁 `tests/test_no_orphan_exports.py`：AST 扫描 `__init__.py` 的 `__all__`，断言本期接入符号（`StateVisualizer`/`FrameHub`/`McapRecorder`/`FrameAligner`/`CarlaRosBridge`）在生产代码有引用；历史欠账以只减不增的 allowlist 固定。
- 新增控制面 VIL 全链集成测试 `tests/integration/test_vil_e2e_control_plane.py`（CARLA / Kafka / MinIO / VILRuntime 全替身）：`create → 断流 PAUSED → 恢复 → completed + sim_metrics + sensor_file`。
- 补回执行面测试：`test_engine_pool_lease.py`(25) + `test_execution_plane_lifecycle.py`(35)，覆盖租约/心跳/地图亲和/装配取消/生产者生命周期。

### 七、兼容性说明

- **服务端**：平台只配置 `CARLA_MANAGEMENT_ENDPOINT` 即可（无变化）；新端点与 `sensors` 字段均为增量，旧报文仍可解析（`lights`/`steering_unit` 缺省时行为与旧版一致）。
- **数据面**：Topic 名与 6 类 type 不变；唯一语义变化是 `sensor_file` 从“恒 0”变为真值，严格 Schema 校验方需放行。
- **部署**：镜像 tag 改 `3.0.0`，`CMD` 为 `carla-manager`；compose 新增 `probes` 服务，需同步更新 Prometheus 抓取目标（业务指标从 `:8090/metrics`，探针自身从 `:8080/metrics`）。
- **运维脚本**：凡引用 `/api/v1/sim`、`huntersim serve`、`API_JWT_SECRET` 的脚本必须改写（迁移对照见 `user_manual.md` §12）。

### 八、待办 / 已知边界

- 真实 CARLA 环境下的画面观感（灯光、转向灯、感知叠加节流）与 MJPEG 在平台前端的播放效果**未实测**（本期测试全部使用 CARLA/Kafka/MinIO 替身）。
- `.mcap` 仅用 `mcap` Python reader 读回验证，尚未用 Foxglove/ROS2 工具在同一环境下复核。
- ROS2 出口（`ros_enabled=true`）在装有 `rclpy` 的环境中**未实测**。
- MinIO 实际上传与平台消费方对 `sensor_file.topics` 的解析**未联调**。
- 单进程单世界仍是硬约束；需要并行只能部署多个控制面进程/端口并在平台侧分片（不共享注册表）。

---

## V3.0.1 — V3.0.0 代码审查缺陷修复

**发布日期**：2026-10-29
**版本定位**：按代码审查报告（功能遗漏 / 边界与异常 / 并发与性能）修复 V3.0.0 的三条高危与多条中低危缺陷，不改变对外数据契约。

### 现场 / 根因

| 级别 | 缺陷 | 根因 |
|---|---|---|
| 高 | ROS2 出口静默失效 | `_ros_bridge_for()` 只构造 `CarlaRosBridge()` 不调 `initialize()`，`is_initialized` 恒 False，所有 `publish_*` 被 `_publish_ros` 跳过 |
| 高 | 传感器编码占用 CARLA tick 线程 | `sensor.listen()` 回调在 `world.tick()` 线程内同步做 BGRA→RGB、降采样、JPEG 编码与 mcap zstd 写盘，单帧 15–25 ms（帧预算 20 ms），50 Hz 被拖到 20–30 Hz |
| 高（未修，见待办） | `record` 默认 True，无录制总开关/空间闸门 | 平台未显式关闭时必录；磁盘满只会逐帧写失败 |
| 中 | Pillow 不在依赖 | `encode_jpeg` 惰性导入 PIL，缺包时恒回退 PNG，`ADAPTER_SCREENSHOT_QUALITY` 失效且带宽翻倍 |
| 中 | MJPEG 占用 anyio 线程 | 同步生成器 + `time.sleep` 被 `iterate_in_threadpool` 包裹，每个客户端独占 1 个工作线程（默认线程池 40） |
| 中 | mcap 写失败无熔断 | 磁盘满时逐帧重试 + 逐帧 warning，日志与 IO 二次放大 |
| 中 | 上传任务跨实例串台 | 上传线程执行时读 `self._producer`，单世界串行下可能已被下一个实例替换，`sensor_file` 发到错误 sim 的 Topic |
| 低 | `lights: {}` 不回退 | `isinstance({}, dict)` 为真，顶层空字典被当作“已上报”而不回退 `chassis.lights` |
| 低 | 探针吞掉上游 degraded | 聚合状态只按上游可达性给 `ok`，上游自报 degraded 被掩盖 |

### 修复内容

- **ROS2 装配（`platform_adapter/executor.py`）**：`_ros_bridge_for()` 构造后立即 `initialize()`（失败 → None + warning）；`_RunContext` 新增 `ros_bridge`，`_run` 的 `finally` 调 `_shutdown_ros_bridge()` 幂等释放节点。
- **传感器回调队列化（`scene_runner/builtin_scenario.py`）**：回调只做「拷贝为不可变载荷 `_SensorFrame` + 入有界队列（上限 32）」；新增 `hunter-sensor-encoder` 守护线程串行执行编码/落盘/ROS 发布，保帧序；队满丢帧并计入 `huntersim_sensor_frames_dropped_total{stream}`；新增 `drain_sensor_frames()`，`cleanup()` 先停收帧 → 排空队列 → 销毁传感器，收尾不丢已采集帧；线程停止后复位待处理计数，避免下一场景重放旧帧。
- **Pillow 依赖**：`pyproject.toml` / `requirements.txt` 增 `Pillow>=10.0,<12.0`（缺失时仍降级 PNG，不阻断启动）。
- **MJPEG 异步化（`platform_adapter/service.py` / `app.py` / `config.py`）**：`stream_frames` 改为异步生成器（`await asyncio.sleep`），路由改 `async def` 并用 `anext()` 预取首帧；新增 `StreamLimitExceeded` 与 `ADAPTER_STREAM_MAX_CLIENTS`（默认 8，0 = 不限），超限直接 `503 stream_limit_reached`（不再“先 200 再中途断流”）；`_mjpeg_chunks` 为异步生成器，`finally` 中 `aclose()` 上游保证名额立即释放。
- **mcap 熔断（`sensor_sim/mcap_recorder.py`）**：连续写失败 10 次即熔断（`_broken`），后续 `push` 立即返回 False、日志只在前几条与每 100 条记一次；`RecordStats` 新增 `write_errors`；`close()` 在熔断路径下仍调 `finish()` 落盘已写分块并返回真值统计。
- **上传任务捕获生产者（`cloud/connector.py`）**：`upload_bag()` 把投递时刻的 `self._producer` 随任务入队，`_perform_upload(..., producer=)` 优先使用任务自带生产者（None 才回退当前）。
- **灯光回退（`cloud/vil_state_parser.py`）**：`lights` 顶层缺失**或为空字典**时回退 `chassis.lights`。
- **探针状态聚合（`probes/app.py`）**：新增 `_derive_status()`，上游 `degraded` 原样透传；上游 `status=ok` 但 `carla_probe_error` 非空时降为 `degraded`。

### 配置与指标变更（增量，默认值向后兼容）

| 项 | 默认 | 说明 |
|---|---|---|
| `ADAPTER_STREAM_MAX_CLIENTS` | `8` | MJPEG 并发客户端上限，超限 `503`；`0` = 不限 |
| `huntersim_sensor_frames_dropped_total{stream}` | — | 新增 Counter：编码队列满导致的丢帧计数 |
| `RecordStats.write_errors` | `0` | 新增字段：mcap 写失败累计次数（熔断后仍可从 `close()` 读取） |

`.env.example`、两套 compose、`k8s/configmap.yaml` + `k8s/deployment.yaml` 已同步 `ADAPTER_STREAM_MAX_CLIENTS`。

### 质量门禁（实测）

- `pytest tests -q --cov=src/hunter_sim`：**1022 passed**，全仓行覆盖 **83.96%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 80 source files**。
- `flake8 src tests`：**0 告警**。
- 新增/改写测试：`test_adapter_stream.py`（异步推流 + 客户端上限 `503` + 名额计数）、`test_builtin_scenario_sensors.py`（回调入队 / worker 落盘 / `cleanup` 排空 / 队满丢帧计数）、`test_platform_adapter_executor.py`（`initialize()` 必调、初始化失败降级、`shutdown()` 幂等）、`test_mcap_recorder.py`（熔断阈值、熔断后不再触底 writer、已写帧仍可读回）、`test_vil_state_parser_lights.py`（空 `lights` 回退）、`test_probes_app.py`（上游 degraded 透传 / 探针错误降级）、`test_cloud_connector_sim.py`（任务携带 enqueue 时刻 producer）。

### 兼容性说明

- 对外 REST/数据面契约不变；`stream.mjpg` 新增一种**成功建立前**的错误码 `503 stream_limit_reached`。
- `RecordStats` 新增字段为向后兼容增量；`sensor_file` 报文结构未改。
- 传感器帧进入 FrameHub / 录制 / ROS 的顺序与内容不变（单 worker 串行），变化仅是执行线程与丢弃策略（队满丢帧而非阻塞 tick）。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.0.0 → 3.0.1`，镜像 tag `hunter-sim:3.0.1`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）。升级步骤：`git pull` → `uv sync`（新增 Pillow）→ `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；`.env` 可选择性新增 `ADAPTER_STREAM_MAX_CLIENTS`（缺省 8）。

### 待办 / 已知边界

- **`record` 默认值与录制总开关、运行期空间闸门未改**：属产品口径决策（默认是否录制 / 低磁盘时是否自动停止），需与平台侧确认后再落。
- **失败/取消路径是否也应上传 mcap** 未定（当前维持既有触发点）。
- 时钟域假设（CARLA sim 时间 vs 系统时间）未引入偏置估计；`depth` 未进 `FrameHub`；上传失败无自动重试、`_sweep_recordings` 与上传存在竞态窗口。
- 本次修复的 MJPEG 与 ROS2 行为仍使用替身验证，真实 CARLA + `rclpy` 环境与平台前端播放效果**未实测**。

---

## V3.0.2 — 代码审查剩余 6 项缺陷修复（录制闸门 / 上传韧性 / 时钟偏置 / depth 入 Hub）

**发布日期**：2026-10-29
**版本定位**：收口 V3.0.0 代码审查报告中 V3.0.1 未处理、原本标为“需产品口径”与低优先级的 6 项（H3 / L4 / M4 / L1 / L5 / L6）。全部采用「可配置开关 + 保守默认」，**默认行为与 V3.0.1 完全一致**，不改变对外 REST / 数据面契约。

### 现场 / 根因

| 级别 | 缺陷 | 根因 |
|---|---|---|
| 高 | `record` 无总开关与空间闸门 | 平台未显式关闭时必录；磁盘满/卷耗尽只会逐帧写失败（叠加 mcap 熔断后表现为“录制静默变空”），无运行期保护 |
| 低 | 失败/取消/看门狗超时路径不上传 mcap | 已录内容在失败场景直接落盘待保留期清理，无法用于事故复盘 |
| 中 | VIL 时钟域假设未验证 | 实车与仿真端时钟不同源时 `skew` 恒大，栅格对齐偏差被误判为链路延迟；阈值告警失真 |
| 低 | `depth` 未进 `FrameHub` | 深度只在录制/ROS 出口可见，控制面与调试工具无法取最新深度帧 |
| 中 | 上传失败无重试 | 短暂网络抖动即判定失败，`sensor_file` 永久缺失，需人工重跑场景 |
| 中 | 保留期清理与上传竞态 + 产物暂存无清理 | `_sweep_recordings` 只看目录 mtime，可能删除正在上传的 mcap 父目录；`artifacts/<sim>/<scene>/` 导出后长期堆积无上限 |

### 修复内容

- **录制三级闸门（`platform_adapter/executor.py`）**：`_start_recording()` 依次判定 `ADAPTER_RECORD_ENABLED` 总开关 → 场景 `sensors[].record` → `ADAPTER_RECORD_MIN_FREE_GB` 磁盘余量（`shutil.disk_usage` 测所在卷，目录未建时向上找已存在父目录，测量失败不拦截）；新增 `_enforce_disk_gate()` 在完成等待循环内每 5s 复查，跌破阈值置 `recording_stopped=low_disk` 并主动收尾（已录部分照常 close/上传）；生命周期事件计入 `huntersim_recording_events_total{event}`（`started` / `skipped_disabled` / `skipped_low_disk` / `stopped_low_disk`）。
- **失败路径可选上传（`platform_adapter/executor.py`）**：`ADAPTER_UPLOAD_ON_FAILURE`（默认 `false`）开启后，`canceled` / `watchdog_timeout` / `failed` 三条路径也调用 `_upload_recording()`；默认沿用 V3.0.1 触发点，避免残片污染平台 `sensor_file` 列表。
- **上传自动重试（`cloud/connector.py` / `common/models.py`）**：`_perform_upload` 改重试循环，`1 + HUNTER_CORE_UPLOAD_MAX_RETRIES` 次尝试，间隔 `HUNTER_CORE_UPLOAD_RETRY_BACKOFF_S` 且用 `_upload_stop.wait()` 退避（`stop()` 立即中断）；仅末次失败才计 `failed`。
- **上传在途登记（`cloud/connector.py`）**：新增 `_pending_paths` + `pending_upload_paths()`，入队前登记、`_perform_upload` 的 `finally` 注销（成功/失败/停止都注销）；队满丢任务时同步注销。`_sweep_recordings()` 通过 `pending_upload_paths()` 跳过在途目录，消除保留期清理与上传的竞态。
- **产物暂存保留期清理（`platform_adapter/executor.py`）**：新增 `_sweep_artifacts()`，`_watchdog_loop` 周期删除 `artifacts/<sim>/<scene>/` 下超过 `ADAPTER_ARTIFACT_RETENTION_HOURS`（默认 72）未更新的 scene 目录，随后尝试 `rmdir` 空的 sim 目录（非空自动跳过）；`_newest_mtime()` 读失败返回 None 时跳过删除。
- **VIL 时钟偏置估计（`vil_mapper/frame_aligner.py` / `platform_adapter/vil.py`）**：`FrameAligner` 新增 `clock_offset_auto`，按 `receive_time - timestamp` 的 30 样本滑动窗口取**最小值**作为偏置（样本 < 3 或 |Δ| > 3600s 不补偿），`clock_time = now - offset` 参与栅格索引计算；`AlignerStats.clock_offset_ms` 随 `stats()` 暴露，控制面可由场景 `vil.clock_offset_auto` 覆盖全局默认值。默认关闭（`VIL_CLOCK_OFFSET_AUTO=false`），保持既有对齐行为。
- **depth 进 FrameHub（`scene_runner/builtin_scenario.py`）**：深度分支把 `depth_bgra_to_meters()` 结果只算一次，同时推送 FrameHub（`encoding=32FC1` + `width`/`height`/`frame_id`）与 mcap（`build_image_message(..., "32FC1", ...)`），控制面可同源取最新深度帧。

### 配置与指标变更（增量，默认值向后兼容）

| 项 | 默认 | 说明 |
|---|---|---|
| `ADAPTER_RECORD_ENABLED` | `true` | 传感器录制总开关；`false` 时忽略场景 `sensors[].record` |
| `ADAPTER_RECORD_MIN_FREE_GB` | `0.5` | 录制所需最小空闲磁盘（GB）：启动前不足则不录，运行期每 5s 复查跌破即停录；`0` = 不检查 |
| `ADAPTER_UPLOAD_ON_FAILURE` | `false` | 失败/取消/看门狗超时路径是否也上传已录 mcap |
| `ADAPTER_ARTIFACT_RETENTION_HOURS` | `72` | 产物暂存目录保留期（小时），看门狗周期清理；`0` = 不清理 |
| `HUNTER_CORE_UPLOAD_MAX_RETRIES` | `2` | bag/mcap 上传失败后的自动重试次数；`0` = 不重试 |
| `HUNTER_CORE_UPLOAD_RETRY_BACKOFF_S` | `1.0` | 重试退避秒数（`stop()` 时立即中断） |
| `VIL_CLOCK_OFFSET_AUTO` | `false` | 是否自动估计并补偿实车与仿真端时钟偏置 |
| `huntersim_recording_events_total{event}` | — | 新增 Counter：录制生命周期事件（`started` / `skipped_disabled` / `skipped_low_disk` / `stopped_low_disk`） |
| `AlignerStats.clock_offset_ms` | `0.0` | 新增字段：当前估计的时钟偏置（毫秒，未开启补偿时恒 `0`） |

`.env.example`、两套 compose、`k8s/configmap.yaml` + `k8s/deployment.yaml` 已同步上述全部新增项（`deployment.yaml` 用 `configMapKeyRef` 逐项引用小写 key）；场景级 `vil.clock_offset_auto` 可作为会话级覆盖。

### 质量门禁（实测）

- `pytest tests -q --cov=src/hunter_sim`：**1041 passed**，全仓行覆盖 **84%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 80 source files**。
- `flake8 src tests`：**0 告警**。
- 新增/改写测试：`test_platform_adapter_executor.py`（`TestRecordingGates` 总开关/低磁盘跳过启动/余量充足启动/运行期低磁盘停录，`TestFailureUpload` 默认不改变触发点与开关开启后上传，`TestRetentionSweep` 过期录制清理/在途上传避让/过期产物清理/新产物保留）、`test_cloud_connector_sim.py`（重试成功、重试耗尽计 failed、`pending_upload_paths` 在途跟踪）、`test_frame_aligner.py`（`TestClockOffset` 六个用例：默认关闭、补偿后 skew、不补偿对照、样本不足、未标定时间戳、`reset` 清除）、`test_builtin_scenario_sensors.py`（四路进 FrameHub 与 `depth` 的 `32FC1` 载荷校验）。

### 兼容性说明

- **录制**：`record` 默认仍为 `true`，`ADAPTER_RECORD_ENABLED` 默认 `true`，既有部署行为不变；仅新增低磁盘保护（`ADAPTER_RECORD_MIN_FREE_GB=0.5`）。若目标卷基线空闲本就低于 0.5GB，表现为“不录制”（`skipped_low_disk`），按实际调整或置 `0` 关闭检查。
- **失败路径 mcap**：`ADAPTER_UPLOAD_ON_FAILURE` 默认 `false`，失败/取消场景仍不产生 `sensor_file`，与 V3.0.1 一致。
- **时钟偏置**：`VIL_CLOCK_OFFSET_AUTO` 默认 `false`，对齐行为与 V3.0.1 完全一致；开启后 `stats()` 的 `skew_ms` 扣除估计偏置，`VIL_MAX_DATA_LATENCY_MS` 告警阈值按扣除后口径判定。
- **清理语义**：录制目录清理新增“在途上传避让”，只会更保守；产物暂存新增保留期删除（默认 72h），如需永久保留置 `0`。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.0.1 → 3.0.2`，镜像 tag `hunter-sim:3.0.2`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）。升级步骤：`git pull` → `uv sync` → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`；`.env` 可选择性新增上述 7 项（均有默认值，不填即保持 V3.0.1 行为）。

### 待办 / 已知边界

- 时钟偏置估计仅在受控时钟替身下验证（窗口最小值法），**未经实车时钟不同源的联调**；开启前建议先用 `clock_offset_ms` 观察收敛值。
- `_sweep_artifacts()` 按“目录内最新文件 mtime”判定过期，长期未更新但仍在引用的产物目录会被清理；若有外部引用需求，需将 `ADAPTER_ARTIFACT_RETENTION_HOURS` 置 `0`。
- 磁盘闸门以“卷余量”为准，不区分其他进程占用；多进程共用同一卷时阈值需按总占用上调。
- 本次修复的 MJPEG 与 ROS2 行为仍使用替身验证，真实 CARLA + `rclpy` 环境与平台前端播放效果**未实测**；MinIO 实际重试链路（网络抖动）未做故障注入实测。

---

## V3.0.3 — HunterCore mTLS 客户端证书支持（9093 双向 TLS 接入）

**发布日期**：2026-10-29
**版本定位**：补齐 HunterCore 数据面接入的 mTLS 能力。仅新增可选配置项与参数透传，**未配置客户端证书时行为与 V3.0.2 完全一致**，不改变对外 REST / 数据面契约。

### 现场 / 根因

| 级别 | 缺陷 | 根因 |
|---|---|---|
| 高 | HunterCore 9093 接入面遥测 / sensor_file 全部静默丢失 | 平台侧 `infra/deploy/docker-compose.yml` 的 `KAFKA_CFG_SSL_CLIENT_AUTH` 默认 `required`（双向 TLS），而 HunterSim 的 producer 只透传 `ssl_cafile`（`sim_data_producer` / `telemetry_producer`），没有 `ssl_certfile` / `ssl_keyfile`。SCRAM 账号口令正确也过不了 TLS 握手，`connect()` 捕获异常后整体降级为 no-op（仅一条 warning），表现为“平台什么也没收到” |
| 低 | 配置错误难定位 | 证书路径写错/只配一半时没有任何前置校验，只能从 kafka-python 抛出的模糊 SSL 错误反推 |

### 修复内容

- **新增 `cloud/kafka_security.py`**：`build_ssl_kwargs(settings)` 把 `CloudSettings` 的 CA / 客户端证书 / 私钥路径翻译为 kafka-python 的 `ssl_cafile` / `ssl_certfile` / `ssl_keyfile`；仅当 `security_protocol` 以 `SSL` 结尾（`SASL_SSL` / `SSL`）时生效。客户端证书与私钥**必须成对配置**，缺一抛 `ValueError`；三类文件均做存在性校验，缺失抛 `FileNotFoundError`（提前失败，由 `connect()` 捕获并降级为 no-op，日志给出明确原因）。
- **`common/models.py`**：`CloudSettings` 新增 `client_cert_path` / `client_key_path`（环境变量 `HUNTER_CORE_CLIENT_CERT_PATH` / `HUNTER_CORE_CLIENT_KEY_PATH`），取值即接入包中的 `client-cert.pem` / `client-key.pem`。
- **`cloud/sim_data_producer.py` / `cloud/telemetry_producer.py`**：`_build_kafka_producer()` 统一改为 `kwargs.update(build_ssl_kwargs(s))`，两端 SSL 参数不再各写一份，避免配置漂移。

### 配置与指标变更（增量，默认值向后兼容）

| 项 | 默认 | 说明 |
|---|---|---|
| `HUNTER_CORE_CLIENT_CERT_PATH` | 空 | mTLS 客户端证书路径（接入包 `client-cert.pem`）；空 = 不做客户端认证，行为同 V3.0.2 |
| `HUNTER_CORE_CLIENT_KEY_PATH` | 空 | mTLS 客户端私钥路径（接入包 `client-key.pem`）；与证书必须成对 |
| `HUNTER_CORE_CA_CERT_PATH` | 空 | 既有项，本次一并纳入存在性校验，并在两套 compose 中补上透传 |

`.env.example` 与两套 compose（`docker-compose.yml` / `docker-compose.windows.yml`）已同步上述三项。容器内路径需在挂载卷内可见，推荐把接入包证书放到 `data/certs/`（`../data` 已挂载到 `/home/hunter/app/data`），即 `HUNTER_CORE_CLIENT_CERT_PATH=/home/hunter/app/data/certs/client-cert.pem`。

### 质量门禁（实测）

- `pytest tests -q --cov=src/hunter_sim`：**1049 passed**，全仓行覆盖 **84.25%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 81 source files**。
- `flake8 src tests`：**0 告警**。
- 新增测试（`tests/test_sim_data_producer.py`）：`TestSslKwargs`（非 SSL 协议不注入、仅 CA、CA+证书+私钥三件套、半配置报 `ValueError`、文件缺失报 `FileNotFoundError`）、`TestProducerMtlsWiring`（`SimDataProducer` 与 `TelemetryProducer` 均把 `ssl_cafile/ssl_certfile/ssl_keyfile` 透传给 `KafkaProducer`）；`tests/test_settings_env.py` 新增环境变量映射用例（`HUNTER_CORE_CLIENT_CERT_PATH` / `HUNTER_CORE_CLIENT_KEY_PATH` / `HUNTER_CORE_CA_CERT_PATH` → `CloudSettings`）。

### 兼容性说明

- 未设置 `HUNTER_CORE_CLIENT_CERT_PATH` / `HUNTER_CORE_CLIENT_KEY_PATH` 时 SSL 参数与 V3.0.2 一致（仅 `ssl_cafile` 可选），单向 TLS / SCRAM 环境不受影响。
- 唯一行为变化：CA / 客户端证书路径**不存在**时，从“握手期才失败”提前为初始化阶段 `warning` 明确原因后降级 no-op。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.0.2 → 3.0.3`，镜像 tag `hunter-sim:3.0.3`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）。升级步骤：`git pull` → `uv sync` → 放置接入包证书 → `.env` 配三项路径 → `docker compose -f docker/docker-compose.yml build sim-service` → `up -d`。

### 待办 / 已知边界

- **未与真实 HunterCore 9093 接入面联调验证**：本次仅以 `KafkaProducer` 参数替身断言参数透传正确，真实证书链、broker 侧 ACL 与用户名绑定仍需现场执行一次 `HUNTER_CORE_ENABLED=true` 的发布验证（观察 `huntersim_sim_status_published_total` 与平台 Topic）。
- **主机名校验未开放**：kafka-python 默认 `ssl_check_hostname=True`，若用 IP 直连且 broker 证书只含 DNS SAN，握手会失败；当前未暴露 `ssl_check_hostname` 开关，需平台侧证书补 IP SAN（或后续按需增加配置项）。
- **VIL 下行消费（`KafkaSettings`）仍无 SSL/CA/mTLS 配置**：`vil_data_consumer` 只透传 SASL 用户名口令，同样连不上 9093 双向 TLS 接入面，需另行补齐（本次未改动）。
- **k8s 形态需先建证书 Secret**：`k8s/deployment.yaml` 已把 `hunter-sim-tls` Secret 以 `optional` 方式挂到 `/home/hunter/app/certs`，并在 ConfigMap 预留三个路径键（默认空）。启用 mTLS 前需按 `k8s/secret.yaml` 注释创建 Secret 并把三个路径键指到挂载文件；Secret 缺失时只要路径留空，producer 行为与 V3.0.2 一致。

---

## V3.0.4 — `huntersim doctor` CUDA 判定口径修复（误报「CUDA 8.9」）

**发布日期**：2026-10-29
**版本定位**：只修预检项的取值口径与提示文案，**不改变任何运行时行为与对外契约**：`EnvironmentCheckResult` 新增一个仅用于展示的字段（带默认值），其余字段语义不变。

### 现场 / 根因

| 级别 | 缺陷 | 根因 |
|---|---|---|
| 中 | `huntersim doctor` 在 RTX 40 系机器上报 `cuda warn CUDA 8.9`，并提示「安装/升级 CUDA（要求 ≥11.0）」，误导运维 | `engine/check_environment.py` 用 `nvidia-smi --query-gpu=compute_cap` 取 CUDA 版本。该字段返回的是**GPU 计算能力**（Ada 架构 RTX 4070 为 `8.9`），与 CUDA 版本无关；结果被当作版本号与 `_MIN_CUDA_VERSION=(11, 0)` 比较，必然判定「过低」 |
| 低 | 同一行字串里分不清哪是 CUDA 版本、哪是算力 | 计算能力占用了 `cuda_version` 字段，没有独立的展示项 |

### 修复内容

- **`engine/check_environment.py`**：
  - 新增 `_GpuProbe`（`driver_version` / `cuda_version` / `compute_capability` / `memory_gb` / `gpu_name`）承载探测结果，`_check_nvidia_smi()` 换为 `_probe_gpu()`。
  - **CUDA 版本改从 `nvidia-smi` 表头解析**（`CUDA Version: 13.0`，`_parse_cuda_version` + `_CUDA_VERSION_PATTERN`），即驱动最高支持版本；`compute_cap` 只作为算力字段上报。
  - `--query-gpu` 增加 `compute_cap` 列；旧驱动不认该列导致整体查询失败时，回退到 `driver_version,memory.total,name` 三列，算力留空。
  - 判定逻辑：有 CUDA 版本则照旧与 ≥11.0 比较；**只有算力没有 CUDA 版本**时不再判失败（能枚举出 GPU 即说明驱动自带 CUDA 运行时）；两者都没有才记 `Could not detect CUDA version`。
  - `EnvironmentCheckResult` 新增 `gpu_compute_capability: Optional[str] = None`（尾部带默认值，位置参数构造方式不受影响）。
- **`cli/main.py`**：`cuda` 检查项改为 `CUDA 13.0（驱动支持），计算能力 8.9` 的形式（`_describe_cuda` 组装，缺字段自动省略），失败提示改为「安装/升级 NVIDIA 驱动（要求驱动支持 CUDA ≥11.0）」。

### 验证

- `pytest -q`：**1058 passed**（含新增 `tests/test_check_environment.py` 8 个用例与 `tests/test_doctor_cli.py` 1 个展示用例）。
- `mypy src/hunter_sim tests/test_check_environment.py`（strict）：**Success: no issues found in 82 source files**。
- `flake8 src/hunter_sim/engine/check_environment.py src/hunter_sim/cli/main.py tests/test_check_environment.py tests/test_doctor_cli.py`：**0 告警**。
- 新增用例覆盖：表头解析 CUDA 版本、算力不被当作 CUDA 版本、旧驱动回退查询、`nvidia-smi` 缺失降级为空探测、RTX 4070 场景（`cuda_version=13.0` + `compute_capability=8.9`）不再产生 `CUDA ... < required` 错误、仅有算力时不判失败、无 GPU 时驱动与 CUDA 同时失败。

### 兼容性说明

- `doctor` 输出变化：`cuda` 行的数值从「计算能力」改为「驱动支持的 CUDA 版本」，并在有值时附带「计算能力 8.9」；原本因算力导致的 `warn` 在本机变为 `ok`，`--strict` 门禁不再被误报阻断。
- `EnvironmentCheckResult.gpu_compute_capability` 带默认值 `None`，旧代码以关键字或位置参数构造均不受影响；仅 `doctor` 读取该字段（使用 `getattr` 容错，外部 stub 对象缺该属性也不会崩）。
- 本版未新增/删除任何环境变量，未改动端口、接口或数据契约。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.0.3 → 3.0.4`，镜像 tag `hunter-sim:3.0.4`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步），`README.md` 与 `docs/单机部署指南.md`、`deployment_and_operations.md`、`user_manual.md` 版本号同步。升级步骤：`git pull` → `uv sync` → `huntersim doctor` 复跑确认 `cuda` 项取值。

### 待办 / 已知边界

- **未在带 GPU 的机器上实机复跑 `doctor`**：本次验证基于 `nvidia-smi` 输出的打桩断言（采集自现场报告的 `617.42 / RTX 4070 / 8.9`），需现场执行一次 `huntersim doctor` 确认 `cuda` 项显示为 `CUDA <版本>（驱动支持），计算能力 8.9` 且状态为 `ok`。
- **CUDA 版本依赖 `nvidia-smi` 表头**：部分精简镜像/受限环境（如仅含 `nvidia-smi` 子集或 MIG 场景）可能不输出 `CUDA Version:`，此时该字段为空、按「仅有算力」处理（不报 warn），无法反映真实 CUDA 上限——如需严格判定，后续可改为查询 `nvcc --version` 或 CUDA runtime 库版本。

---

## V3.1.0 — 一键启动编排 + 只读可视化客户端（画面 / 传感器 / 事件 / 日志）

**发布日期**：2026-10-30
**版本定位**：把「起服务」与「看状态」两件运维动作收进同一条命令与同一个页面。新增 `huntersim up`（预检 → 两进程 → 打开可视化客户端 → Ctrl+C 统一回收）与 `/viz` 只读单页应用；**不新增 REST 业务面、不改平台 10 个端点契约、不改数据面 Topic**，可视化完全复用既有 MJPEG / 截图出口与进程内数据。

### 现场 / 需求

| 类型 | 诉求 | 现状缺口 |
|---|---|---|
| 运维 | 一条脚本拉起全套并看到各模块状态 | 需手工开两个终端起 `carla-manager` 与 `probes`，预检、端口占用、CARLA 可达性、平台连通性只能逐项自查 |
| 观测 | 页面里同时看到画面、传感器、事件、日志 | 只有 `/docs`、`/health`、`/metrics` 与裸 MJPEG 地址；事件只在产物 JSON 里，日志只在终端 |
| 排障 | 现场问题需要「日志全量 + 场景事件 + 传感器帧率」对照时间线 | 缺少带时间序号的进程内日志缓冲，`<img src=stream.mjpg>` 之外无法定位断流点 |

### 新增能力

- **一键启动 `huntersim up`（`cli/main.py` + `visualization/launcher.py`）**：
  - 流程：`doctor` 预检 → 依次拉起 `carla-manager`(:8090) 与 `probes`(:8080，可选) → 轮询 `/health` 就绪 → `webbrowser.open` 打开 `/viz` → 前台守护，`Ctrl+C` 统一回收子进程。
  - 参数：`--skip-doctor`（跳过预检）、`--strict`（warn 提为 fail）、`--force`（有 fail 项仍启动）、`--no-browser`（只打印地址）、`--no-probes`（不拉探针面）、`--path`（磁盘/状态库基准路径）、`--log-level`。
  - 幂等：目标端口已有同服务在跑时识别为「已运行」并复用，不重复拉起；子进程异常退出时按非零码返回并回收其余进程。
- **`doctor` 新增 `platform` 检查项（`cli/main.py`）**：`HUNTER_CORE_ENABLED=false` 时 `skip`；开启时解析 `HUNTER_CORE_GATEWAY_URL` 并对 api-gateway 做 TCP 探测，不可达给 `warn`（遥测/上传将降级），地址不可解析时同样 `warn` 并给出原因（Kafka 连通性由独立的 `kafka` 项检查）。
- **只读可视化客户端 `/viz`（`visualization/routes.py` + `assets/`）**：单页应用（无构建步骤），8 个端点全部 `include_in_schema=False`，不进 OpenAPI、不算第二套业务面：

  | 端点 | 作用 |
  |---|---|
  | `GET /viz` | 单页应用页面 |
  | `GET /viz/assets/{name}` | 静态资产（白名单外一律 404，防路径穿越） |
  | `GET /viz/api/status` | 顶栏状态灯 + 模块连通性（控制面/CARLA/平台/ROS2/场景） |
  | `GET /viz/api/instances/{id}/events` | 场景事件（碰撞/偏离/通过等）+ 按类型计数 |
  | `GET /viz/api/instances/{id}/sensors` | 各传感器流的帧数/帧率/最新帧摘要 + ROS2 话题状态 |
  | `GET /viz/api/instances/{id}/sensors/{stream}` | 单流预览：`rgb` 透传、`depth`→灰度 PNG、`lidar`→抽样点、`imu`→JSON |
  | `GET /viz/api/logs` | 日志快照（`level` / `contains` / `since_seq` / `limit` 过滤） |
  | `GET /viz/api/logs/stream` | 日志 SSE 实时推送（首屏回放历史，心跳 15s） |

- **画面**：直接复用既有 `GET /api/v1/instances/{id}/stream.mjpg`（MJPEG）与 `/screenshot`，同源无跨域；`<img>` 报错或 6s 无新帧时自动降级为单帧截图轮询，避免「黑屏但连接正常」。
- **传感器**：`rgb` 原字节透传（JPEG/PNG）；`depth`（32FC1，米制）经 numpy 归一化到 0~50m 映射灰度后缩放输出 PNG；`lidar`（float32 xyz）按 `VIZ_LIDAR_MAX_POINTS` 抽样后以 JSON 交给 Canvas 俯视散点；`imu` 的 `repr((accel,gyro))` 解析失败时回退原始文本。
- **事件**：读 `EventDetector` 的 `detected_events`（经 `executor`/`service` 只读方法暴露），首列显示**仿真时间** `t=<sim_time>s`（`time_stamp` 为仿真秒，而非墙钟）。
- **日志（全量）**：新增 `visualization/logbus.py`——root logger 旁路 `Handler` + 有界 `deque`（默认 2000 条）+ 订阅者投递；SSE 收到即推（断线或环境不支持 SSE 时降级为 `since_seq` 轮询 1.5s），页面最多保留 1500 行并可暂停滚动、按级别/关键字过滤。

### 修复与实现要点

- **`platform_adapter/executor.py`**：新增 `detected_events()`、`ros_status()` 只读访问器，事件与 ROS 话题状态不再需要绕过执行器直接摸内部字段。
- **`platform_adapter/service.py`**：新增 `events()` / `sensor_frame()` / `sensors()` / `ros()` / `runtime_status()` 五个只读方法；`events()` 返回注解引入模块级别名 `EventItems = list[dict[str, Any]]`（类内已有同名方法 `list`，直接写 `list[...]` 会被遮蔽，mypy strict 报 `Function "AdapterService.list" is not valid as a type`）。
- **`platform_adapter/app.py`**：lifespan 内装配 `LogBus` 并 `register_viz_routes()`；顺手收窄配置类型——`HunterSimSettings` 加载失败时显式 `raise RuntimeError`，而不是把 `None` 继续往下传（原先 mypy strict 报 3 处 `Optional` 实参错误）。
- **`visualization/sensor_view.py`**：深度图渲染把 Pillow 降为**可选**——numpy 缺失时返回 `None`（端点 503 并提示「深度渲染不可用（需 numpy）」）；Pillow 缺失时走新增的 `_encode_gray_png()` 标准库兜底（`struct` + `zlib` 手写 IHDR/IDAT/IEND），保证 requirements 未装 Pillow 的环境仍能出图。
- **打包**：`pyproject.toml` 新增 `[tool.setuptools.package-data]`，把 `assets/*.html|css|js` 纳入 wheel，容器/K8s 形态 `pip install .` 后页面不会 404。

### 配置与指标变更（增量，默认值向后兼容）

| 项 | 默认 | 说明 |
|---|---|---|
| `VIZ_ENABLED` | `true` | 是否注册 `/viz` 页面与 `/viz/api/*` 只读端点 |
| `VIZ_AUTO_OPEN_BROWSER` | `true` | `huntersim up` 就绪后是否自动开浏览器（`--no-browser` 可临时覆盖） |
| `VIZ_LOG_BUFFER_SIZE` | `2000` | 日志总线环形缓冲条数（进程内，重启即清空） |
| `VIZ_LOG_HISTORY_LIMIT` | `500` | 首屏/重连最多回放的日志条数 |
| `VIZ_EVENT_LIMIT` | `200` | 事件面板返回条数上限 |
| `VIZ_LIDAR_MAX_POINTS` | `4000` | 激光雷达俯视抽样点数上限 |
| `VIZ_DEPTH_MAX_WIDTH` | `480` | 深度图预览输出宽度上限（像素，0 = 不缩放） |

`.env.example` 新增 `VIZ_*` 整块（7 项，含逐行中文注释）；`Makefile` 新增 `up-local` / `up-local-headless` / `viz` 三个目标。

### 质量门禁（实测）

- `pytest -q`：**1126 passed**，全仓行覆盖 **84.40%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 86 source files**。
- `flake8 src tests`：**0 告警**。
- **启动冒烟（本机实测，无 CARLA）**：`huntersim up --no-browser --no-probes --skip-doctor` 正常拉起控制面；`/viz` 与 `/viz/assets/style.css` 返回 `200`（`text/html` / `text/css` 均带 `charset=utf-8`），`/viz/api/status` 返回 `200` 且 `version=3.1.0` / `carla.connected=false` / `log.records=3`（启动日志已进旁路缓冲），`/viz/api/logs` 返回带 `seq` 的记录与 `counts`；未知资产与未知实例均为 `404`（`instance not found: nope`），平台端点 `/api/v1/instances` 正常返回。引擎未启动时顶栏 CARLA 灯为未连接属预期。
- 新增测试：`tests/test_log_bus.py`（级别归一化、环形容量、`since_seq`/`level`/`contains`/`limit` 过滤、`counts`、格式化失败回退原模板、`attach`/`detach` 幂等、handler 吞异常不影响业务、订阅者异常不阻断 `publish`）、`tests/test_viz_api.py`（页面与资产白名单 404、**OpenAPI 不含 `/viz`**、状态汇总降级、事件计数、传感器流 404/409、lidar 抽样、imu 解析与原始回退、depth PNG 与 numpy 缺失 503、日志续传、SSE 首帧 history）、`tests/test_viz_launcher.py`（服务规格端口跟随配置、`0.0.0.0`→`127.0.0.1`、`/health` 就绪判定、复用已运行服务、必需未就绪返回 1、可选失败不阻断、`Ctrl+C` 回收、`terminate` 超时 `kill`、浏览器失败不致命、`up` 子命令 `--force`/`--no-probes` 透传）；`tests/test_doctor_cli.py` 新增 `TestPlatformCheck` 4 个用例。

### 兼容性说明

- **对外契约零改动**：平台 10 个 `/api/v1/*` 端点、`/health`、`/metrics`、MJPEG/截图出口与全部 Kafka Topic 均未变；`/viz*` 全部 `include_in_schema=False`，OpenAPI 与平台侧接口发现不受影响（`test_viz_is_not_in_openapi` 显式守护）。
- **既有启动方式仍可用**：`huntersim carla-manager` / `huntersim probes` 分进程手工启动完全保留，`up` 只是在其上做编排；`VIZ_ENABLED=false` 可整体关闭可视化端点。
- **Pillow 变为事实可选**：深度预览不再硬依赖 Pillow（缺失走标准库 PNG 编码），`numpy` 仍是深度渲染的必要条件（缺失时该端点返回 503 并在页面提示，不影响其余面板）。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.0.4 → 3.1.0`，镜像 tag `hunter-sim:3.1.0`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步），`README.md`、`user_manual.md`、`deployment_and_operations.md`、`docs/单机部署指南.md` 已同步。升级步骤：`git pull` → `uv sync` → `huntersim up`（或维持原分进程启动）；`.env` 可选择性新增 `VIZ_*` 7 项（均有默认值）。

### 待办 / 已知边界

- **真实 CARLA + `rclpy` 环境未联调**：画面（MJPEG 降级链）、传感器帧渲染与 ROS2 话题状态均以替身对象验证；真实场景下的首帧时延、点云抽样规模与话题计数准确性需现场确认。
- **`/viz` 在容器/K8s 形态的访问方式未实测**：两套 compose 与 `k8s/service.yaml` 只暴露 `8090` 业务端口，页面沿用同一端口因此无需新增映射；但若通过 Ingress/NodePort 暴露，按「8090 无鉴权」约定**仍只允许内网访问**，不得加公网入口。
- **日志只覆盖本进程**：`LogBus` 是控制面进程内的旁路 handler，`probes` 进程与 CARLA 引擎侧日志不在其中；跨进程日志仍需 `HUNTER_SIM_LOG_FILE` 或容器日志采集。
- **SSE 未经反向代理实测**：心跳 15s 用于规避空闲断连，但 Nginx/网关的缓冲与超时配置未验证；断线时页面会降级为轮询，功能可用但实时性下降。
- **事件列表为内存快照**：`detected_events` 随实例生命周期存在，实例终态回收后页面不再展示历史事件（历史事件需从产物 JSON / 平台侧获取）。
- **ruff 全仓风格欠账未处理**：`make lint` 门禁仍是 flake8（0 告警）；`ruff check` 对历史代码仍报大量 UP/I 类风格问题，属独立清理任务，本版未纳入。

---

## V3.1.1 — `/viz` 让失败可见 + `huntersim up --demo` 冒烟场景

**发布日期**：2026-10-30
**版本定位**：V3.1.0 交付后的首个可用性补丁。只改「人怎么知道发生了什么」：页面把实例 `message`、参与者数与场景名/ID 写出来，终态实例不再无效拉流；`up` 新增 `--demo`，就绪后自动提交一个 SIL 冒烟场景。**无新增端点、无配置项变更、无数据面/平台契约变更**。

### 现场 / 需求

现场按 V3.1.0 执行 `huntersim up` 后打开 `/viz`，看到的现象：

| 位置 | 现象 |
|---|---|
| 顶栏 | 控制面/日志正常，CARLA 灯未连接 |
| 实例下拉 | 存在一个 `failed / FAILED` 实例，场景名显示为 `????` |
| 画面 | 一直「暂无画面」，无报错文案 |
| 传感器/事件 | 全空；日志面板仅 3 条启动日志 |
| 服务端日志 | `screenshot` / `sensors/lidar` / `sensors/imu` / `stream.mjpg` 连续 `409` |

用户诉求：「页面能看出为什么没画面」与「`up` 之后能马上看到东西」。

### 根因（对照服务日志与代码）

1. **`failed` 实例是重启前遗留的旧实例**：控制面重启时会把未结束实例收敛为 `failed(message=adapter restarted)`（既有 fail-fast 语义）。`status` 非活动后，`/stream.mjpg`、`/screenshot`、`/sensors/*` 一律 `409`——这是设计行为，但页面**既没有展示 `message`，也没有在终态停止拉流**，于是表现为「静默无画面 + 409 刷屏」。
2. **本次 `up` 没有创建任何实例**：服务日志中无 `POST /api/v1/instances`，平台也没有下发场景；`up` 只负责起服务，页面自然没有活场景可看。此前缺少「不依赖平台也能立刻验证链路」的入口。
3. **场景名 `????` 与 409 无关**：中文在提交端按 GBK 拼进 JSON（Windows 控制台/PowerShell 默认编码），服务端按 UTF-8 解码失败——属提交通道编码问题，非服务端缺陷。

### 新增能力

- **`huntersim up --demo`（`cli/main.py`）**：全部服务就绪后 `POST /api/v1/instances` 创建 SIL 冒烟场景，参数为 `mode=sil` / `map=Town03` / `ego_vehicle=hunter_se` / `sensors=[hunter_se_full]`（`record=false`，不占现场磁盘）/ `duration=120s`；请求体显式 `ensure_ascii=False` + UTF-8 编码，避免中文场景名退化为 `????`；创建超时 20s。
  - 失败只提示不阻断：`[up] 就绪后附加动作失败（…），服务仍在运行`（CARLA 未起时冒烟场景会以 `failed` 结束，重跑 `--demo` 即可）。
  - 输出提示实例 ID 与 `/viz` 地址，页面下拉选中后即可看画面/传感器/事件。
- **`visualization/launcher.py` 新增就绪回调 `on_ready`**：`Launcher.run(..., on_ready=callable)`，在全部服务就绪（含复用已运行服务）后执行一次；异常只 `emit` 提示，不影响守护与 `Ctrl+C` 回收语义。

### 修复与实现要点

- **实例面板展示失败原因（`assets/app.js`）**：新增「原因」行直接渲染 `message`（`failed` 红、其余黄），状态行按 `failed`/`canceled` 高亮；场景显示为 `name（id）`；新增「参与者」数（`actors_count`）。`assets/style.css` 新增 `.kv dd.err/.warn` 与 `.kv dt.kv-label.err/.warn` 高亮规则（复用既有 `--err`/`--warn` 变量）。
- **终态不再拉流**：`refreshDetail` 检测到实例进入终态时 `stopVideo()` 并在画面区写明「实例已结束（failed）：<原因>」；`startVideo` 对终态实例直接提示而不再发起 `stream.mjpg`/截图轮询，消除 `409` 持续刷屏。
- **`up` 输出改为逐行刷新**：`Launcher` 默认 `emit` 换成 `flush=True` 的 `_print_line`，`_run_demo` 与 `_cmd_up` 进度输出同样刷新，并把 `sys.stdout` 设为 `line_buffering=True`（尽力而为）。此前 `huntersim up > up.log` 时父进程被块缓冲，现场看到的是「服务起来了但日志一片空白」。
- **文档修正（`platform` 检查项口径）**：V3.1.0 章节与手册原写作「校验 `HUNTER_CORE_KAFKA_BOOTSTRAP`」，与实现不符——`_check_platform` 读的是 `settings.cloud.gateway_url`（`HUNTER_CORE_GATEWAY_URL`）并对 api-gateway 做 TCP 探测，Kafka 由独立的 `kafka` 项检查。`release.md` V3.1.0 章节、`user_manual.md` §2.2 已改正。

### 配置与指标变更

- **无新增配置项**：`--demo` 复用 `ADAPTER_HOST` / `ADAPTER_PORT` 与 `VIZ_AUTO_OPEN_BROWSER`；冒烟时长（120s）与创建超时（20s）为 `cli/main.py` 内常量。
- 无新增/变更 Prometheus 指标。

### 质量门禁（实测）

- `pytest -q`：**1140 passed**，全仓行覆盖 **84.4%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 86 source files**。
- `flake8 src tests`：**0 告警**。
- 新增测试（`tests/test_viz_launcher.py` +14）：`on_ready` 就绪后执行一次 / 复用已运行服务时也执行 / 异常只提示不改退出码 / 必需服务未就绪时不执行；`up --demo` 仅登记回调（不在 `up` 内立即执行）、默认无回调；`_demo_payload` 字段口径；`_manager_base_url` 通配回落；`_create_demo_instance` 的 UTF-8 请求体与 `Content-Type`、`instance_id`/`sim_instance_id` 解析、时间戳场景 ID、HTTP 失败 / 响应不可解析 / 缺 `instance_id` 三类错误；`_run_demo` 输出实例 ID 与 `/viz` 地址。`node --check assets/app.js` 通过。
- **启动冒烟（本机实测，无 CARLA）**：`huntersim up --skip-doctor --no-browser --no-probes --demo` 拉起后自动创建实例 `inst-3ca0…`：`POST /api/v1/instances` 返回 `200`，`scene_name` 为 `一键冒烟（SIL · Town03）`（中文未退化为 `????`）且无平台下发；因缺 `carla` 模块，实例按预期以 `failed(message=execution_unavailable:No CARLA client available…)` 结束——这正是页面「原因」行要展示的内容，`actors_count=1`、`total_time_s=120` 符合预期；`/viz/api/status` 返回 `version=3.1.1` / `instances.total=1,active=0` / `platform.enabled=false`。

### 兼容性说明

- **对外契约零改动**：平台 10 个 `/api/v1/*` 端点、`/health`、`/metrics`、MJPEG/截图出口与 Kafka Topic 均未变；`/viz*` 仍全部 `include_in_schema=False`。
- **`up` 默认行为不变**：不加 `--demo` 时与本补丁前的编排完全一致；`on_ready` 是可选形参，既有调用方无需改动。
- **页面仅增展示**：`instance-info` 新增「原因」「参与者」行，原有键值顺序保留，不影响任何脚本解析（API 未变）。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.1.0 → 3.1.1`，镜像 tag `hunter-sim:3.1.1`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）；`README.md`、`user_manual.md`、`deployment_and_operations.md`、`docs/单机部署指南.md` 已同步。升级步骤：`git pull` → `uv sync` → `uv pip install -e .`（刷新安装元数据，否则 `/viz/api/status` 仍显示旧版本）→ `huntersim up --demo`。

### 待办 / 已知边界

- **未在带 CARLA 的现场机器复跑 `--demo` 端到端**：本机沙箱无 CARLA（`CarlaUE4.exe` 与工作区不在同一盘符），冒烟链路仅验证到「请求构造 + 响应解析 + 回调时序」；CARLA 就绪后 `--demo` 的首帧时延、`Town03` 默认出生点是否可用需现场确认。
- **`????` 需现场按 UTF-8 复验**：页面/服务端已确认不破坏中文（请求体显式 UTF-8）；PowerShell 侧仍需 `[Console]::OutputEncoding = [Text.Encoding]::UTF8` + `Invoke-RestMethod -ContentType 'application/json; charset=utf-8'`，或用 `curl.exe --data-binary @file.json` 规避编码转换。
- **旧实例的 `failed` 记录不会自动清理**：实例库保留历史实例，`/viz` 下拉仍能看到重启前的 `failed(adapter restarted)`；属审计需要，不视为缺陷。
- **`--demo` 只建一个场景**：单世界串行语义下重复执行会排队/与运行中实例冲突（返回 `409`），提示后自行决定是否重试。

---

## V3.1.2 — VIL 消费侧支持 SASL_SSL / mTLS 接入

### 现场问题

仿真端源码直跑、要与平台端打通做 VIL 全链路时，若平台 Kafka 只开放 9093 SASL_SSL 接入面（mTLS 强制 `ssl.client.auth=required`）：

- `KAFKA_USERNAME/KAFKA_PASSWORD` 配了也**不生效**，日志里看不到任何认证信息；
- 实例可以 `create` 成功、状态 `running`，但 `vil_state` 永远无数据，最终只能靠 `vil_data_interrupted` 判断“断流”；
- 部分机器上消费线程持续重建（`huntersim_kafka_consumer_rebuilds_total` 增长），日志只有 `Kafka consumer rebuild failed`。

### 根因

1. **消费端从未传 `security_protocol`**：`VILDataConsumer._create_consumer` 的 kwargs 只有 `bootstrap_servers/group_id/auto_offset_reset/enable_auto_commit/value_deserializer/consumer_timeout_ms`。kafka-python 默认 `PLAINTEXT`，即使给了 SASL 账号也不会启用 SASL；TLS/证书参数更是完全没有入口（`cloud/kafka_security.py` 当时只服务发布侧）。
2. **凭据键名非法**：旧代码写的是 `sasl_username` / `sasl_password`，而 kafka-python `KafkaConsumer` 的合法键是 `sasl_plain_username` / `sasl_plain_password`，且它对本类未识别键**直接抛** `KafkaConfigurationError`（不是忽略）——因此只要配了账号，消费者初始化就会失败。
3. **配置错误暴露得太晚**：参数在消费线程里才拼装，线程异常只写日志，实例侧看不到“认证配置错了”，表现为“跑着但没数据”。

### 修复内容

- **`KafkaSettings` 新增 5 项接入安全配置**：`security_protocol`（默认 `PLAINTEXT`）、`sasl_mechanism`（默认 `SCRAM-SHA-512`）、`ca_cert_path`、`client_cert_path`、`client_key_path`。默认值与旧行为一致，现场不配即零改动。
- **`cloud/kafka_security.py` 泛化**：
  - `normalize_protocol()`：去空白 + 大写，非法取值（如 `SASL_TLS`）直接报错并列出可选值；
  - `build_ssl_kwargs(settings, *, env_prefix="HUNTER_CORE")`：改为面向最小协议接口，发布侧（`HUNTER_CORE_*`）与消费侧（`KAFKA_*`）共用，错误信息按前缀定位（`KAFKA_CLIENT_KEY_PATH` / `HUNTER_CORE_CLIENT_KEY_PATH`）；
  - 新增 `build_consumer_security_kwargs()`：输出协议 + SASL 凭据 + TLS 证书全套，`SCRAM/PLAIN` 机制缺账号即报错，证书/私钥不成对或文件缺失即报错，`PLAINTEXT` 下配了账号只告警不打包。
- **`VILDataConsumer` 应用安全参数**：构造期解析并缓存 `_security_kwargs`（配置错误会让实例以 `execution_unavailable:*` 失败，而不是起一个空转线程）；`_create_consumer` 与坏消息 DLQ 的 `KafkaProducer` 共用同一套参数；初始化日志增加 `protocol=…, tls=on/off`。
- **`huntersim doctor` 的 `kafka` 项**：先按消费端口径解析接入参数，协议拼写错误 / SASL 缺账号 / mTLS 不成对或证书缺失直接判 **fail**（附 `KAFKA_*` 变量提示）；可连通时 detail 带出实际协议（如 `127.0.0.1:9092 可连通（SASL_SSL）`）。

### 配置变更（新增，均有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `KAFKA_SECURITY_PROTOCOL` | `PLAINTEXT` | `PLAINTEXT` / `SASL_PLAINTEXT` / `SSL` / `SASL_SSL`，大小写不敏感 |
| `KAFKA_SASL_MECHANISM` | `SCRAM-SHA-512` | 仅 `SASL_*` 协议生效；`PLAIN` / `SCRAM-SHA-256` / `SCRAM-SHA-512` 需账号 |
| `KAFKA_USERNAME` / `KAFKA_PASSWORD` | 空 | SASL 凭据；`PLAINTEXT` 下不生效（doctor 与日志会提示） |
| `KAFKA_CA_CERT_PATH` | 空 | CA 证书，留空用系统信任链 |
| `KAFKA_CLIENT_CERT_PATH` / `KAFKA_CLIENT_KEY_PATH` | 空 | mTLS 必须成对配置 |

无新增 Prometheus 指标；`KAFKA_SECURITY_PROTOCOL` 只影响连接参数，Topic 与消息契约不变。

### 质量门禁（实测）

- `pytest -q`：**1156 passed**（较 V3.1.1 +16），全仓行覆盖 **84.7%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 86 source files**。
- `flake8 src tests`：**0 告警**。
- 新增测试 16 条：`tests/test_vil_data_consumer.py::TestSecurityConfig`（13）覆盖默认 PLAINTEXT、协议大小写与非法值、`SASL_PLAINTEXT` 无证书、`SASL_SSL` + CA、纯 mTLS 三项、证书/私钥不成对、CA 文件缺失、SASL 缺账号、空账号视为未配置、PLAINTEXT 下账号被忽略、真实 `KafkaConsumer` kwargs 不含非法键 `sasl_username`、DLQ 生产者复用同一套参数；`tests/test_doctor_cli.py`（3）覆盖协议透出与两类配置错误判 fail。

### 兼容性说明

- **默认零改动**：不设 `KAFKA_SECURITY_PROTOCOL` 即为 `PLAINTEXT`，与本补丁前的实际连接参数一致（唯一差异是删除了会被 kafka-python 拒绝的 `sasl_username*` 键）。
- **对外契约不变**：`/api/v1/*`、`/health`、`/metrics`、Topic 名与消息格式均未变。
- **doctor 更严格**：SASL 缺账号、mTLS 不成对等配置错误由“可能 ok”变为 `fail`，属预期收紧（这类配置下 VIL 必然失败）。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.1.1 → 3.1.2`，镜像 tag `hunter-sim:3.1.2`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）；`README.md`、`user_manual.md`、`deployment_and_operations.md`、`docs/单机部署指南.md` 已同步。升级步骤：`git pull` → `uv sync` → `uv pip install -e .`（刷新安装元数据）→ `huntersim doctor` 确认 `kafka` 项。

### 待办 / 已知边界

- **未在真实 SASL_SSL / mTLS 集群上联调**：本机无平台 Kafka，验证止于参数构建与 kwargs 接线（`KafkaConsumer`/`KafkaProducer` 均以桩对象断言）；实际握手、证书链、ACL 授权需在现场按 `KAFKA_SECURITY_PROTOCOL=SASL_SSL` + 三项证书路径复验。
- **仅支持口令类 SASL**：`GSSAPI` / `OAUTHBEARER` 未提供凭据入口（会按机制名透传，但需要额外参数）。
- **DLQ 写入仍是 best-effort**：DLQ 生产者创建失败或发送异常只计数丢弃，不阻塞消费主循环。

---

## V3.1.3 — `/viz` 终态实例停止帧类轮询（消除持续 `409`）

**发布日期**：2026-11-02
**版本定位**：V3.1.1「终态不再拉流」的收尾补丁。V3.1.1 只停了 `stream.mjpg` / `screenshot`，传感器帧统计、事件列表、点云与 IMU 预览三类定时轮询仍在按 1.5–2s 打向已结束的实例，服务端 access log 被 `409` 刷满。**无新增端点、无配置项变更、无平台/数据面契约变更**。

### 现场

`huntersim up`（Windows 宿主源码直跑）后打开 `/viz`：

- 预检全绿（`carla_reachable ok 127.0.0.1:2000`），面板显示「实例 1 个（活动 0）」；
- 下拉选中 `inst-d408ba35…`：`failed / FAILED`，原因 `execution_unavailable:No CARLA client available for engine 'host.docker.internal:2000'`，场景名 `?????(smoke-001)`，**更新时间 2026-10-30**（早于本次启动 3 天）；
- 本次启动日志里**没有任何 `POST /api/v1/instances`**，只有 GET；
- 控制台每 1.5–2s 成对刷出 `GET /viz/api/instances/{id}/sensors/lidar … 409` 与 `…/sensors/imu … 409`，开头还有一条 `stream.mjpg` / `screenshot` 的 409。

### 根因

1. **`409` 刷屏（本版修复）**：`assets/app.js` 的 `bootstrap()` 把 `refreshSensors` / `refreshEvents` / `refreshPreviews` 直接挂到 `setInterval`，只有 `startVideo` / `pollScreenshot` 路径带 `TERMINAL` 判断。实例进入终态后已无帧，`/viz/api/instances/{id}/sensors/{stream}` 按设计返回 `409 sensor_frame_unavailable`（`visualization/routes.py`），于是页面按 `POLL_MS.preview=1500ms` 无限要 409。`/sensors`（帧统计）与 `/events` 返回 `200` 空快照，不报错但同样是无用轮询。
2. **页面上的 `failed` 是历史记录，不是本次失败**：实例库 `data/adapter/carla_instances.db` 按审计语义保留历史实例，无活动实例时下拉回落列表首项，于是三天前的失败原因被当成当前故障展示。
3. **`host.docker.internal:2000` 的来源（未改代码，只补文档）**：端点 host 取自 `CARLA_HOST`（`platform_adapter/executor.py::_plane_instance` → `execution/plane.py::ExecutionPlane._endpoint_of`），`build_execution_plane` 的 `resolve()` 在 `carla.Client(host, port)` / `MapManager` 构造失败时返回 `None`，`ensure_runner` 据此抛 `ExecutionUnavailable`。`host.docker.internal` 是 Docker Desktop 只注入给**容器**的 DNS 别名，Windows 宿主直跑 Python 时解析不到——即那次运行用的是容器形态的 `.env`。当前主机直跑配置（`127.0.0.1:2000`）已正确，无需改代码。

### 修复内容

- **`assets/app.js` 新增 `isTerminalInstance()`**：以既有 `TERMINAL = [succeeded, failed, canceled]` 判定当前实例是否已结束。
- **三类定时轮询短路**：`bootstrap()` 中 `refreshSensors` / `refreshEvents` / `refreshPreviews` 的 `setInterval` 回调改为先判 `isTerminalInstance()`；`refreshPreviews` 自身入口同样加 `|| isTerminalInstance()` 守卫（覆盖 `selectInstance` 之外的调用路径）。
- **终态收尾加载**：新增 `state.terminalSynced`（`selectInstance` 切换实例时复位），`refreshDetail` 首次观测到实例进入终态时主动调用一次 `refreshSensors()` + `refreshEvents()`，保证传感器帧数与事件列表落到最终值——只是此后不再周期性要。
- **文档**：`README.md` §4.4 补 Windows 中文 `scene_name` 的 UTF-8 提交写法；`docs/单机部署指南.md` 排障表新增「`No CARLA client available for engine 'host.docker.internal:2000'`」与「持续 `409`」两行；`user_manual.md` 可视化「终态实例」条目、`deployment_and_operations.md` §4.6 / §5.7 与 §7 升级说明同步口径。

### 配置与指标变更

无新增/删除环境变量，无默认值变更，无 Prometheus 指标变化。

### 质量门禁（实测）

- `pytest -q`：**1157 passed**（较 V3.1.2 +1），全仓行覆盖 **84.67%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 86 source files**。
- `flake8 src tests`：**0 告警**。
- `node --check src/hunter_sim/visualization/assets/app.js`：通过。
- 新增测试（`tests/test_viz_api.py::TestPageAndAssets::test_terminal_instance_stops_frame_polling`）：以源码接线方式锁定 `isTerminalInstance` 定义、`refreshPreviews` 入口守卫、三个 `setInterval` 均走短路、以及 `state.terminalSynced` 收尾加载存在（前端无 JS 测试框架，沿用本仓库既有的静态资源直出断言方式）。

### 兼容性说明

- **纯前端行为**：只改 `/viz` 只读页面内部逻辑，平台 10 个 `/api/v1/*` 端点、`/health`、`/metrics`、MJPEG/截图出口、Kafka Topic 与消息契约均未变。
- **服务端 `409` 语义不变**：`/viz/api/instances/{id}/sensors/{stream}` 对无帧实例仍返回 `409 sensor_frame_unavailable`（其它调用方依赖该状态码），本版只让页面不再去要。
- **在跑实例零感知**：守卫仅在 `status ∈ {succeeded, failed, canceled}` 时生效，`pending`/`running` 的轮询节奏与 V3.1.1 完全一致。
- **旧 `failed` 记录不清理**：审计语义保留；需干净列表时停服务后删 `data/adapter/carla_instances.db`。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.1.2 → 3.1.3`，镜像 tag `hunter-sim:3.1.3`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）；`README.md`、`user_manual.md`、`deployment_and_operations.md`、`docs/单机部署指南.md`、`.env.example` 已同步。升级步骤：`git pull` → `uv sync` → `uv pip install -e .`（刷新安装元数据）→ **Ctrl+F5 强刷 `/viz`**（前端资源无缓存版本号）。

### 待办 / 已知边界

- **未在浏览器里实测终态页面的轮询停止**：本机沙箱无 CARLA、无活实例，验证止于源码接线断言与 `node --check`。现场确认方式：选中一个 `failed` 实例后观察服务端 access log，`sensors/lidar`、`sensors/imu` 应只在切换实例时各出现一次 `409`，之后不再按 1.5s 周期刷出。
- **`????` 场景名仍未做现场 UTF-8 复验**（承接 V3.1.1 待办）：服务端链路已确认不丢字（`--demo` 请求体显式 UTF-8、SQLite TEXT 与 JSON 响应均 UTF-8、静态资源带 `charset=utf-8`，且 `tests/test_platform_adapter_app.py` / `test_platform_adapter_store.py` 以中文 `scene_name` 覆盖往返），丢字发生在提交端把中文按控制台代码页交给 `curl` / `Invoke-RestMethod` 的那一步，服务端无法还原。本版只补文档写法，未加运行期拦截或告警。
- **无活动实例时下拉仍选中历史 `failed` 首项**：`refreshInstances` 的 `running → pending → 首项` 优先级本身正确，但库里只剩历史实例时易被误读为当前故障；建议后续在页面顶部对「所选实例并非本次运行创建」给出提示。

---

## V3.2.0 — 仿真节点接入（HunterCore node 模式）

**发布日期**：2026-11-03
**版本定位**：让 HunterSim 可作为「仿真节点」注册到 HunterCore 平台。平台开启 `SIM_DISPATCH_MODE=node` 后，场景库下发不再依赖固定 IP：平台从入站请求头**自学习**仿真机地址，再按该地址直推本机 CARLA 管理 API（`:8090`）。**默认关闭**，不开启时与本补丁前完全一致（不发起任何平台请求）。

### 背景

平台原有 node 模式契约（`PUT /api/v1/scene/sim-nodes/{node_id}` 注册、`POST /api/v1/scene/sim-nodes/{node_id}/heartbeat` 心跳）已就绪，但 HunterSim 侧缺少注册/心跳客户端，仿真机无法进入平台的可调度节点列表。本版按 HunterCore `docs/HunterSim接入清单.md` 补齐仿真端实现。

### 新增配置（前缀 `HUNTER_CORE_SIM_NODE_`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ENABLED` | `false` | 总开关；关闭时不注册、不心跳、无后台线程 |
| `ID` | 空 | 节点永久标识（`^[a-z0-9][a-z0-9._-]{2,63}$`，禁止 IP 形式）；启用时必填 |
| `SERVICE_TOKEN` | 空 | 平台签发的服务令牌（`service:{client_id}`）；启用时必填 |
| `BASE_URL` | 空 | 平台基础地址；空表示复用 `HUNTER_CORE_GATEWAY_URL` |
| `HEARTBEAT_INTERVAL_SECONDS` | `20` | 心跳周期，范围 5–30s（平台租约默认 90s） |
| `REQUEST_TIMEOUT_SECONDS` | `5` | 注册/心跳单次 HTTP 超时 |
| `CAPACITY` | `1` | 可并行实例上限（1–32，参与平台选节点排序） |
| `MAPS` | 空 | 可用地图清单（逗号分隔，上限 64；空 = 不做地图过滤） |
| `CARLA_VERSION` | 空 | 登记展示用；空时尝试从已安装 `carla` SDK 元数据推导 |
| `PROTOCOL` | `http` | 平台回访管理 API 的协议（`http`/`https`） |
| `MANAGER_PORT` | 空 | 管理 API 端口；空表示复用 `ADAPTER_PORT`（默认 8090） |
| `LABELS` | `{}` | 自定义标签（JSON 对象，仅展示） |
| `RUNTIME_HINT` | 空 | 节点自述地址（仅展示，绝不参与寻址） |

### 运行行为

- **启动即注册，之后周期心跳**：`platform_adapter` 启动时构建 `SimNodeClient`，后台线程（`HunterSimNodeHeartbeat`）先 `PUT` 注册，随后按周期 `POST` 心跳；心跳携带当前 `pending`/`running` 实例 ID（上限 32）。
- **地址只由平台自学习**：注册体仅包含 `capacity`/`protocol`/`manager_port`/`maps`/`carla_version`/`labels`/`runtime_hint`（平台 `extra="forbid"`，多字段即 422）；节点 IP 由平台从 `X-Real-IP`/`X-Forwarded-For`/TCP 对端学习。
- **租约过期自愈**：心跳返回 `404` 或 `code=3001` 时记录 `sim_node_lease_expired`，下一轮自动回到注册分支；`401/403` 只告警并继续重试（提示检查令牌/角色）；网络与超时异常不杀死线程，也不影响本机场景执行。
- **地址漂移可见**：平台回显的 `endpoint_ip` 变化时打印 `sim_node_endpoint_refreshed`（旧/新地址 + 来源）；与本机网卡不一致时打印 `sim_node_endpoint_mismatch`（代理/NAT 场景属预期）。
- **健康与控制台**：`GET /health` 新增 `sim_node` 快照（`enabled`/`node_id`/`base_url`/`online`/`endpoint_ip`/`endpoint_source`/`manager_port`/`lease_seconds`/`running_count`/`last_error`/`last_ok_at`/`heartbeat_interval_seconds`）；`huntersim up` 启动计划新增「仿真节点」行。
- **`huntersim doctor` 新增 `sim_node` 检查**：启用但缺 `node_id`/服务令牌 → `fail`；`ADAPTER_HOST` 为回环地址 → `warn`（平台无法回访）；平台地址直连网关 `8080` → `warn`（平台可能学到网关容器地址）；平台不可达 → `warn`（注册/心跳会重试）。

### 质量门禁（实测）

- `pytest -q`：**1174 passed**（较 V3.1.3 +17），全仓行覆盖 **84.38%**（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 87 source files**。
- `flake8 src tests`：**0 告警**。
- 新增测试 17 条（`tests/test_sim_node_client.py`）：覆盖注册请求体只含声明字段与 Bearer 鉴权头、心跳上报活动实例（过滤空值/截断 32 项/提供者异常降级为空）、`404`/`3001` 后下一轮自动重注册、配置不完整时不启动、`HUNTER_CORE_SIM_NODE_ID` 环境变量按文档生效、应用 lifespan 启停与 `/health` 快照、doctor 的 fail/warn/ok 判定；网络全部由 `httpx.MockTransport` 打桩。

### 兼容性说明

- **默认零改动**：`HUNTER_CORE_SIM_NODE_ENABLED=false` 时不创建客户端、不启动线程、不产生任何出向请求，既有行为与 V3.1.3 一致。
- **对外契约只增不改**：10 个 `/api/v1/*` 实例端点、`/metrics`、VIL 数据面与 Kafka 消息格式均未变；`/health` 仅新增可选 `sim_node` 字段。
- **注册体不含地址**：这是平台契约要求（地址由平台自学习），不是遗漏；手工把 IP 写进注册体属于错误用法。
- **`node_id` 校验更严**：除平台正则外额外禁止 IP 形式，符合接入清单「标识必须与地址解耦」；已用 IP 作 `node_id` 的现场需改用固定名字（如 `sim-node-01`）。
- **版本**：`pyproject.toml` / `/health` 应用版本 `3.1.3 → 3.2.0`，镜像 tag `hunter-sim:3.2.0`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）；`README.md`（§4.6 接入说明与配置表）、`user_manual.md`（§2.4 / §9.1）、`deployment_and_operations.md`（§3.8 / §5.8 / §7）、`docs/单机部署指南.md`、`.env.example`、`k8s/configmap.yaml`、`k8s/secret.yaml` 已同步。升级步骤：`git pull` → `uv sync` → `uv pip install -e .`（刷新安装元数据）→ 按需配置 `HUNTER_CORE_SIM_NODE_*` → `huntersim doctor` 确认 `sim_node` 项。

### 待办 / 已知边界

- **未与真实平台联调**：本机无 HunterCore 平台，验证止于 `httpx.MockTransport` 打桩与配置/生命周期断言；注册成功后的真实下发链路（平台 → 本机 `:8090`）需在现场按接入清单复验。
- **直连网关 `8080` 仍是常见坑**：此时平台学到的是网关容器地址，会登记出「在线但永远打不通」的节点；应走 web-portal 80/443，或确认网关透传 `X-Real-IP` 并在平台配置 `SIM_NODE_ALLOWED_NETWORKS`。doctor 只能提示，无法代替现场网络验证。
- **Windows 防火墙/AP 隔离**：平台主机需能入向访问本机 `:8090`，需放行平台地址；WiFi AP 隔离会表现为节点在线但下发超时，属环境问题而非客户端缺陷。
- **无本地指标**：本版未新增 Prometheus 指标，节点状态以 `/health` 快照与结构化日志（`sim_node_registered` / `sim_node_endpoint_refreshed` / `sim_node_*_failed`）呈现。

---

## V3.2.1 — 留空环境变量不再中断配置加载（`.env` 模板首启失败修复）

**发布日期**：2026-11-04
**版本定位**：修复 V3.2.0 引入 `HUNTER_CORE_SIM_NODE_MANAGER_PORT` 后暴露的**首启阻塞**问题：按文档 `copy .env.example .env` 后，模板里的留空项会让整份配置加载以 `ValidationError` 失败，`huntersim up` / `huntersim doctor` 直接起不来。同时把 `.env.example` 的**平台接入开关与注释/文档口径对齐**（保持预置开启，口径统一为「默认开启，需补凭据」）。纯缺陷修复 + 配置模板口径修正，不改接口契约与任何字段默认值。

### 现场

- Windows 源码部署（`D:\python\HunterSim`，`huntersim version` = 3.2.0）执行 `copy .env.example .env` 后：

  ```text
  huntersim up
  配置加载失败：ValidationError: 1 validation error for SimNodeSettings
  manager_port
    Input should be a valid integer, unable to parse string as an integer
    [type=int_parsing, input_value='', input_type=str]
  ```

- `huntersim doctor` 同样失败，只在 `config` 项记一条 `fail`，其余检查项全部未执行（`Summary: 0 ok, 0 warn, 1 fail, 0 skip`）。
- 未改动任何配置值，用的是仓库自带的 `.env.example`——即**每台按文档起步的新机器都会命中**。

### 根因

- `.env.example` 中 `HUNTER_CORE_SIM_NODE_MANAGER_PORT=` 留空，注释与 README/user_manual 都写明「空 = 复用 `ADAPTER_PORT`」，即留空是**受支持的写法**。
- `SimNodeSettings.manager_port` 声明为 `Optional[int]`（默认 `None`）。pydantic-settings 对标量字段会把环境值原样送入校验，空串 `""` 不是合法整数 → `int_parsing`；`Optional[int]` 只允许 `int | None`，空串**不会**被当作 `None`，于是异常冒泡到 CLI 的配置加载入口。
- 同一缺陷面在 k8s 清单上同样存在：`k8s/configmap.yaml` 的 `hunter_core_sim_node_manager_port: ""` 会注入空值环境变量，Pod 启动即崩。
- 同类隐患：留空的凭据/证书字段（`KAFKA_USERNAME=`、`HUNTER_CORE_JWT_TOKEN=`、`KAFKA_CA_CERT_PATH=` 等）虽不报错，但会解析成**空口令/空路径**对象而非「未配置」，与字段文档语义（`None` = 用系统信任链）不一致。

### 修复内容（`common/models.py`）

- 新增 `_blank_is_unset()` 前置校验器与三个可选字段类型别名 `OptionalInt` / `OptionalStr` / `OptionalSecret`：**空白值（`""` 或纯空格）归一为 `None`**，回落字段默认值；非空值完全按原有类型与范围校验。
- 应用到全部声明为 Optional 的配置字段：`SimNodeSettings.manager_port` / `.service_token`；`CloudSettings.sasl_username` / `.sasl_password` / `.ca_cert_path` / `.client_cert_path` / `.client_key_path` / `.jwt_token`；`KafkaSettings.username` / `.password` / `.ca_cert_path` / `.client_cert_path` / `.client_key_path`。必填标量（如 `ADAPTER_PORT`）留空仍报错——那是真实的配置错误，不属本次归一范围。
- 消费方无需改动：读取这些字段的代码（`platform_adapter/sim_node.py`、`cli/main.py`、`cloud/kafka_security.py`、`cloud/telemetry_producer.py`、`cloud/sim_data_producer.py`、`platform_adapter/artifacts.py`、`cloud/bag_uploader.py`）本来就以 `is None` / 真值判断分支，`None` 与空串走同一降级路径。
- **预检提示给出可查路径**（`cli/main.py`）：`config` 失败时的 hint 补上「可选字段留空不算错误」，并把原本指向不存在的《单机部署指南》§2.3 改为实际章节（`deployment_and_operations.md` §5.9 与本指南 §2）。

### 模板口径统一（`.env.example` 与四份文档）

现场确认：模板把 `HUNTER_CORE_ENABLED` / `HUNTER_CORE_SIM_NODE_ENABLED` 预置为 `true`（面向平台联调机），而同一行注释与 README/user_manual 写的是「默认关闭」——两者必须挑一个口径。本版**保留 `true`**（不影响已接入节点的现场默认），把注释与文档改为「本模板默认开启，需补凭据」并突出首启必填项：

- `.env.example`：标题行升 V3.2.1；文件头新增「首启必补清单」（`CARLA_HOST` / 节点服务令牌 / 平台地址 / `ADAPTER_HOST` / 数据面凭据，逐项写明漏填后果）；两个接入分节标题改为「本模板默认开启，需补凭据」并补上「暂不接入请置 `false`」的退路；`_SERVICE_TOKEN` / `_NODE_ID` 标为「开启接入时必填」；在 `HUNTER_CORE_GATEWAY_URL` 旁注明「节点注册不要直连网关 8080」。
- **取值一个字没改**：两个开关仍为 `true`，与旧版模板行为一致；变的只有注释与文档。
- `README.md`（§4.3 启动提醒、§4 节点接入要点、§5 配置表注）与 `user_manual.md`（§2.1 首启必填表、§2.4、FAQ）、`deployment_and_operations.md`（§2.1 首启必填表与「模板取值 vs 代码默认」区分、§3.1 表注、§3.8、§5.8、§7）、`docs/单机部署指南.md`（版本演进句、§2 两个开关说明、必填表改为★首启必填并新增三项、§9 速查表新增四行）同步。
- **预检提示补退路**（`cli/main.py`）：`sim_node` 因缺服务令牌报 fail 时，hint 除了「按接入清单配置」，同时给出「暂不接入置 `HUNTER_CORE_SIM_NODE_ENABLED=false`」。
- **附带类型修正**（`visualization/sensor_view.py`）：开发 venv 装上 Pillow 后 mypy 开始解析真实类型存根，`Image.NEAREST` 报 `attr-defined`；改用 `Image.Resampling.NEAREST`（依赖下界 `Pillow>=10.0` 支持）。该分支为防御性下采样兜底，当前代码路径不触发（行为无变）。

### 向后兼容

- **只放宽不收紧**：原先能加载的配置继续按原值解析；原先因留空而崩溃的配置现在能加载。
- **模板取值不变**：`HUNTER_CORE_ENABLED` / `HUNTER_CORE_SIM_NODE_ENABLED` 仍为 `true`，已有现场升级后行为与 V3.2.0 一致（变的只有注释与文档，以及可选字段留空的容错）。
- 默认值、字段范围校验、接口契约、日志与指标均无变化；留空凭据由「空对象」变为 `None`，所有消费点判定结果相同（已逐一复核）。
- **版本**：`pyproject.toml` / `huntersim version` / `/health` 应用版本 `3.2.0 → 3.2.1`，镜像 tag `hunter-sim:3.2.1`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）；`README.md`、`user_manual.md`、`deployment_and_operations.md`（§2.1 / §3.1 / §3.8 / §5.8 / §5.9 / §7）、`docs/单机部署指南.md`（§2 留空语义与首启必填 + §9 故障速查新增四行：`int_parsing`、`config` 单项 fail 短路、`sim_node` 参数缺失、`version` 旧值）、`.env.example` 已同步。

### 风险

- 归一后「留空 = 用默认」会**掩盖把必填凭据写成空值的意图**（例如本想禁用某功能却忘填口令）；此类情况仍由 `huntersim doctor` 的 `kafka` / `sim_node` 项与启动日志的降级原因暴露。
- 模板保持预置开启意味着**新机器首检必带一条 `sim_node` fail**（不填令牌就过不了 `up` 预检）。这是选定口径的代价，已在四处给出退路：`.env.example` 分节注释、文件头首启清单、`docs/单机部署指南.md` §2 与 `huntersim doctor` 的 hint。

### 验证（实测）

- `pytest -q`：**1182 passed**（较 V3.2.0 +8），全仓行覆盖 84.30%（门禁 `fail_under=80`）。
- `mypy src/hunter_sim`（strict）：**Success: no issues found in 87 source files**；`flake8 src tests`：**0 告警**（门禁四件全绿；`ruff` 不在门禁内，存量告警未动）。
- 新增 8 条用例：`tests/test_settings_env.py::TestBlankOptionalValues` 5 条（留空端口解析为 `None`、留空凭据/证书路径解析为 `None`、非空值照常按类型解析、端口越界仍被拒（归一不得绕过范围校验）、**直接加载仓库 `.env.example` 必须成功**并断言两个接入开关为 `True`）+ 1 条**模板口径守护**（预置 `true` 必须与「本模板默认开启，需补凭据」「开启接入时必填」注释同口径，两处分节标题逐一断言）；`tests/test_doctor_cli.py` 新增 `TestSimNodeCheck` 2 条（缺令牌 fail 的 detail 与「置 false」退路、未启用为 skip）并给 `config` 失败补两条 hint 断言。
- 手工复现验证：把 `.env.example` 拷为 `.env` 后加载 `HunterSimSettings` 不再抛错，`sim_node.manager_port` 为 `None`（由 `SimNodeClient` 回落 `ADAPTER_PORT=8090`），`node_id` 仍读出 `sim-node-01`。
- 本地同路径实测（拷 `.env.example` 为 `.env` 后执行）：`huntersim version` → `3.2.1`；`huntersim doctor` 首项转为 `config ok 配置加载成功（env=dev）`，17 项检查全部跑完（`Summary: 9 ok, 6 warn, 2 fail, 0 skip`），不再止步于 `int_parsing`；`sim_node` 转为带退路说明的明确 fail（原文已核对：「模板默认开启接入：……暂不接入平台则置 HUNTER_CORE_SIM_NODE_ENABLED=false」）。本机的 2 项 `fail` 是环境事实（模板服务令牌留空、CARLA 未启动），不是配置加载问题；验证后已删除临时 `.env`。

### 实机执行（现场待办）

- 现场需 `git pull` → `uv pip install -e .`（刷新安装元数据）→ `huntersim version` 应为 `3.2.1` → `huntersim doctor`。
- 修复后 `doctor`/`up` 不再报 `int_parsing`；因模板有意保留预置开启，服务令牌留空时仍会转为**明确的** `sim_node` fail：「节点注册参数缺失：HUNTER_CORE_SIM_NODE_SERVICE_TOKEN」。接入平台前需填 `node_id`/服务令牌/平台地址（并把 `ADAPTER_HOST` 改为非回环地址），暂不接入则置 `HUNTER_CORE_SIM_NODE_ENABLED=false`——本项属配置动作，非代码缺陷。
- 本次模板改动只涉及注释与文档，**不需重建镜像**（`.env.example` 不在 wheel 内）；K8s 侧若沿用 `configmap.yaml` 里的显式取值，行为也不变。

### 待办 / 已知边界

- **模板开关口径（本版已选定）**：曾发现 `.env.example` 预置 `true` 与注释/文档「默认关闭」矛盾，上一轮只作为待办如实登记；经确认现场意图后，本版选定「**保留预置开启 + 注释与文档统一为默认开启、需补凭据**」（未动取值，以免已接入节点的部署发生默认漂移）。
- **`HUNTER_CORE_GATEWAY_URL` 示例仍为网关 `8080`（未改）**：节点注册地址留空时会复用本行，因而命中 `doctor` 的 `sim_node` warn（平台可能学到网关容器地址）。本版只在模板与文档里加提醒，没改示例值——该地址同时被数据面使用，改动会影响已在用它的现场；待确认后决定是否拆分为两个示例。
- **留空语义暂不得扩展至必填项**：`ADAPTER_PORT=`、`CARLA_HOST=` 等留空仍会以 `ValidationError` 中断加载（属真实配置错误）；若后续需要「留空即默认」的更一致口径，应连同报错文案一起重构，而非在各字段上逐个打补丁。
- **`sensor_view.py` 的 `Image.Resampling.NEAREST` 分支无测试覆盖**：该 `resize` 守卫在当前代码路径下永不触发（上游已用 numpy 步长下采样），本轮只修类型，未新增用例；后续若确认冗余可整块删除。
- **`Image.fromarray(..., mode=...)` 的 Pillow 13 前瞻问题（未改）**：装上 Pillow 11 后测试日志开始出现 `'mode' parameter is deprecated`（`sensor_view.py` 与 `image_codec.py` 共两处）。当前依赖上限 `Pillow<12.0` 不会被真的移除，且不产生功能差异，所以本轮不动；升级 Pillow 前需去掉 `mode` 参数。

---

## V3.2.2 — 平台接入默认打开 + V2.0 节点级回传通道 + 极简环境模板

**发布日期**：2026-11-04
**版本定位**：按 HunterCore《仿真端接入平台系统方案》§2.5 与《HunterSim 接入清单》§1.5 打通仿真端与平台的通信及数据，落地 V2.0「一键开通/三步接入」的仿真端侧能力，并按现场诉求**简化运行环境配置**：接入开关默认全开、无凭据即静默跳过、`.env.example` 收敛为极简模板。

### 概览

仿真端在 V3.2.0/V3.2.1 的 node 模式接入（注册/心跳 + 实例级 Kafka 回传）基础上，本版补齐 V2.0 的两处关键链路——**节点级结果回传通道**与**下发通道 mTLS**，并把「每次升级要改一大堆配置项」的痛点从根上解决：默认值内聚到代码、模板只留首启必改项。全程延续「依赖惰性导入 + 无凭据安全 no-op」的韧性纪律，**绝不阻断本机仿真**。

### 新增能力

- **节点级 Kafka 回传通道（方案 §2.5.4 / §8.1）**：新增 `cloud/node_result_producer.py`——`NodeResultProducer` 复用平台数据面凭据（`CloudSettings` 的 SASL/TLS），把**实例终态结果摘要**按节点聚合回传到 `simulation.{node_id}.result`（配套派生死信 `.dlq` 模板），由平台 `data-collector-simulation` 消费组消费，与实例级 `carla.{sim_id}.sensor_data` 并存、与真实车队 Topic 物理隔离。`build_node_result` 构造对齐方案 §5.4 结果视图的消息（`type=simulation_result`、key=sim_id、acks=all）。`common/metrics.py` 注册并导出 `huntersim_node_result_published_total{node_id}`。
- **执行器终态挂载**：`platform_adapter/executor.py` 在四个终态路径（`_complete_instance` / `_fail_instance`（含看门狗超时与场景失败）/ `cancel`）挂载 `_publish_node_result`，惰性连接、全程 `try/except`，回传失败仅告警、不影响结果落库；非终态与配置不足时安全跳过。
- **接入配置字段（`common/models.py`）**：`SimNodeSettings` 新增 `result_enabled`（默认开）、`result_topic_template`、`result_dlq_template` 与 `render_result_topic()` / `render_result_dlq_topic()`（`node_id` 为空返回空串，表示不可用）。
- **下发通道 mTLS（接入清单 §1.5 step3）**：`platform_adapter/config.py` 的 `AdapterSettings` 新增 `ssl_certfile` / `ssl_keyfile` / `ssl_ca_certfile` / `ssl_client_auth_required` / `ssl_allowed_client_cn` 与 `tls_enabled` 属性（空白证书路径归一为 `None`）。`huntersim carla-manager` 在成对配置服务端证书/私钥时以 TLS 提供服务，配置 CA 即信任平台 CA 校验回访客户端证书，`ssl_client_auth_required=true` 时强制双向 TLS（`ssl_cert_reqs=required`，否则 `optional`）。
- **三步接入 step1→配置 —— 安装管理员转交的接入包**：`platform_adapter/sim_node.py` 提供 `SimNodeClient.install_bundle()`（本地操作，不访问平台）。因 `GET /sim-nodes/{node_id}/bundle` 挂 `scene:execute`（admin/operator）且包内含节点 Kafka 客户端**私钥**（敏感物），节点服务令牌（`data-uploader`）**无权下载**（缺身份头→1001、角色不足→1002），故接入包由**管理员下载并安全转交**仿真机，仿真端只做本地解压与配置。新增 `huntersim connect --bundle <路径>` 子命令安装转交包（校验合法 ZIP → 解压到本地工作目录），并打印 step2（填一次性 SCRAM 口令）与 step3（启用 TLS/mTLS、注册体转 `https`）的后续指引。文件缺失/非法 ZIP/解压出错仅记日志返回 `None`（与心跳一样不阻断本机）。

### 配置简化（现场核心诉求）

- **接入开关默认全开**：`CloudSettings.enabled` 与 `SimNodeSettings.enabled` 的代码默认由 `False` 翻转为 `True`；模板不再逐条写 `HUNTER_CORE_ENABLED=true` / `HUNTER_CORE_SIM_NODE_ENABLED=true`，升级改默认值时无需再同步改模板。
- **无凭据即静默跳过**：未配 Kafka 凭据/服务令牌/`node_id` 时，遥测、上传、注册心跳、节点级结果回传一律安全降级为 no-op，本机场景照常执行。
- **doctor 缺凭据 fail → warn**：`_check_sim_node` 在启用但缺 `node_id`/服务令牌时改记 `warn`（原为 `fail`），不再拦住 `huntersim up`；彻底关闭仍走 `HUNTER_CORE_SIM_NODE_ENABLED=false`（转 `skip`）。
- **`.env.example` 收敛为极简模板**：从 251 行精简到约 64 行，只保留「本机首启可能要改」（`CARLA_HOST`/`CARLA_VERSION`/`HUNTER_SIM_LOG_LEVEL`）与「接入平台需补」两节；绝大多数调优项（看门狗、录制、MJPEG、VIL 对齐、上传重试、探针/可视化等）改由代码默认值承载，需要覆盖时按 `变量名=值` 追加即可（键名见 `common/models.py` 与 `user_manual.md`）。本机零配置可整份不改直接 `huntersim up`。

### 兼容与迁移

- 旧「服务令牌 + 自注册」路径（接入清单 §2–§4）保留可用；V2.0 一键开通（`POST /sim-nodes`、`/bundle`、`rotate-scram`/`retry-provision`/`deprovision`、下发 mTLS、`SIM_DISPATCH_MTLS_ENABLED`）在平台侧为 `contract_defined`，**后端与前端向导待实现**——仿真端能力已就绪，平台落地即可对接。
- 纯本机联调无需任何凭据即可起（开关默认开但静默跳过）；已在用「显式关闭」的部署可继续显式置 `false`，行为不变。

### 质量门禁

- `mypy src/hunter_sim`（strict）：88 文件无问题；`flake8 src tests`：改动文件全绿；`pytest`：**1197 passed**（新增 `tests/test_sim_node_v2_onboarding.py` 覆盖结果消息/Topic 渲染/回传门控与降级/TLS 归一/bundle 下载/执行器终态挂载/doctor 降级；更新 `tests/test_settings_env.py`、`tests/test_doctor_cli.py`、`tests/test_sim_node_client.py` 以匹配「默认打开 + 缺凭据 warn + 极简模板」新口径）。

### 版本与镜像

- **版本**：`pyproject.toml` / `huntersim version` `3.2.1 → 3.2.2`，镜像 tag `hunter-sim:3.2.2`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）；`README.md`、`.env.example` 已同步。

### 实机执行（现场待办）

- `git pull` → `uv pip install -e .`（刷新安装元数据）→ `huntersim version` 应为 `3.2.2` → `huntersim doctor`：`sim_node` 缺凭据转为 `warn`（不再是 `fail`），本机 `huntersim up` 不再被拦住。
- 接入平台（V2.0）：管理员一键开通后，由**管理员下载接入包并连同一次性 SCRAM 口令安全转交**仿真机，仿真端执行 `huntersim connect --bundle <路径>`（step1 本地解压/配置）→ 把一次性 SCRAM 口令填入包内 `kafka.properties`（step2）→ 用包内证书配 `ADAPTER_SSL_*` 并重启 `carla-manager` 启用 TLS/mTLS、把 `HUNTER_CORE_SIM_NODE_PROTOCOL` 改为 `https`（step3）。
- `.env.example` 属仓库根目录、不在 wheel 内，**模板改动无需重建镜像**；mTLS/`connect` 属代码改动，容器部署需 `--force-recreate` 生效。

### 待办 / 已知边界

- **平台侧一键开通/`/bundle`/mTLS 下发尚未落地**：均为 `contract_defined`；后端实现前，`huntersim connect` 会因平台无该端点而返回失败并降级为日志告警，旧 §2–§4 路径仍可用。
- **`ssl_allowed_client_cn` 仅登记不强制**：uvicorn/SSL 以 CA 链信任校验客户端证书，不做 CN 白名单过滤；因每个节点的 `sim-platform-{node_id}` 证书 CN 唯一，CA 信任已等效保证「只有平台签发证书可接入」，本版只把预期 CN 打印提示，未加自定义 ASGI 中间件做强 CN 校验。
- **回传通道无凭据即 no-op**：`HUNTER_CORE_KAFKA_BOOTSTRAP`/SASL/证书留空时，实例级与节点级回传均降级为 no-op（本机场景与结果落库不受影响），符合「默认开、无凭据静默跳过」的选定口径。

---

## V3.2.3 — 修复 mTLS 启动 ssl_cert_reqs 参数类型错误

**发布日期**：2026-11-05

### 现场

实机执行 `huntersim up` 启用下发通道 mTLS（`ADAPTER_SSL_CLIENT_AUTH_REQUIRED=true`）后，carla-manager 启动崩溃：
```
ValueError: 'required' is not a valid VerifyMode
```

### 根因

`cli/main.py` 向 uvicorn 传递 `ssl_cert_reqs="required"`（字符串），而 uvicorn 0.29.0 的 `Config.__init__` 声明该参数为 `int`（默认 `ssl.CERT_NONE`=0），内部直接 `ssl.VerifyMode(cert_reqs)` 构造枚举——字符串无法被 `VerifyMode` 识别，抛 `ValueError`。

### 修复内容

- `src/hunter_sim/cli/main.py`：
  - 顶部新增 `import ssl`；
  - `ssl_cert_reqs` 赋值由 `"required"` → `ssl.CERT_REQUIRED`（整型 2）、`"optional"` → `ssl.CERT_OPTIONAL`（整型 1）。

### 向后兼容

纯修复，无接口/配置变更；未启用 mTLS 的部署完全不受影响。

### 风险

无——仅修正已有缺陷，不引入新逻辑分支。

### 验证

- `pytest tests/test_platform_adapter_app.py tests/test_settings_env.py tests/test_doctor_cli.py`：全部通过。
- 实机验证：`huntersim up` 服务正常以 mTLS 启动，平台已检测到节点在线。

### 分发

镜像 tag `hunter-sim:3.2.3`。

---

## V3.2.4 — 修复 mTLS 就绪探测失败与可视化页面不可达

**发布日期**：2026-11-05

### 现场

V3.2.3 修复后 carla-manager 已正常以 mTLS 启动，平台也检测到 sim-node-01 在线，但：
1. `huntersim up` 报 “未在 60s 内就绪”并停止子进程；
2. 浏览器自动打开 `http://127.0.0.1:8090/viz` 显示 `ERR_EMPTY_RESPONSE`。

### 根因

1. **就绪探测失败**：`ServiceSpec.url` 始终用 `http://` scheme，而 mTLS 服务监听在 HTTPS 端口。`http_ok()` 对 HTTPS 端口发纯 HTTP 请求必然失败；即使改为 HTTPS，mTLS 强制客户端证书也会拒绝无证书的健康检查。
2. **可视化页面不可达**：`viz_url()` 同样硬编码 `http://`，浏览器对 HTTPS 端口发纯 HTTP 请求得到空响应；且 mTLS 要求客户端证书，浏览器无法直接访问。

### 修复内容

- `src/hunter_sim/visualization/launcher.py`：
  - `ServiceSpec` 新增 `tls: bool = False` 字段，`url` 属性根据 `tls` 切换 `http`/`https` scheme；
  - 新增 `probe_host` 属性（通配地址回落 127.0.0.1）；
  - 新增 `tcp_port_ok(host, port, timeout)` 函数，用 TCP 端口连接判断服务是否已绑定 socket；
  - `Launcher._probe_spec()` 方法：TLS 场景用 TCP 端口探测，非 TLS 保持原有 HTTP 探测；
  - `build_service_specs()` 传入 `tls=adapter.tls_enabled`。
- `src/hunter_sim/visualization/routes.py`：
  - `viz_url()` 根据 `adapter.tls_enabled` 返回正确 scheme。
- `src/hunter_sim/cli/main.py`：
  - `_cmd_up()` 检测 mTLS 强制客户端证书时跳过自动打开浏览器，并提示用户通过平台 Web 端访问或导入证书后手动访问。

### 向后兼容

纯修复，无接口/配置变更；未启用 mTLS 的部署完全不受影响（`tls` 默认 `False`，走原有 HTTP 探测 + 自动打开浏览器路径）。

### 风险

无——仅修正已有缺陷，不引入新逻辑分支。

### 验证

- `pytest tests/` 全量：**1200 passed**。

### 实机执行

`git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.2.4` → 配好 `ADAPTER_SSL_*` 后 `huntersim up` 应正常以 mTLS 起 carla-manager 并检测就绪，不再报超时。

### 分发

镜像 tag `hunter-sim:3.2.4`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）。

### 待办 / 已知边界

- **mTLS 下本机 viz 浏览器访问**：需将客户端证书导入 Windows 证书存储后浏览器才能完成 mTLS 握手；或通过平台 Web 端访问可视化页面。后续可考虑在探针面（:8080 HTTP）代理 viz 内容以方便本机访问。
- 沿用 3.2.2 待办项。

---

## V3.2.5 — viz 改由探针面 HTTP 反向代理访问（免除浏览器导入证书）

**发布日期**：2026-11-05

### 现场

V3.2.4 后 `huntersim up` 已能正常以 mTLS 起 carla-manager 并通过就绪探测，但本机访问可视化页面仍有障碍：管理面 `:8090` 全程 HTTPS+mTLS，浏览器需将 `client-cert.pem` + `client-key.pem` 导入 Windows 证书存储（`certutil -importpfx`）才能完成 mTLS 握手，操作繁琐；V3.2.4 临时以“跳过自动打开浏览器 + 提示导入证书”缓解。

### 根因

mTLS 是 TLS 层全有或全无，无法按路径豁免：`:8090` 一旦启用客户端证书校验，浏览器不导入证书就无法直接访问任何端点（含 `/viz`）。

### 修复内容（功能完善：探针面反向代理）

架构：`:8090` 保持 HTTPS+mTLS（平台下发通道安全契约不变）；`:8080` 探针面（HTTP）新增 viz 反向代理，浏览器直接访问 `http://<IP>:8080/viz`，无需任何客户端证书；探针进程用同机同一份 `ADAPTER_SSL_CERTFILE`/`KEYFILE` 作为客户端证书连接管理面，在本机回环完成 mTLS 握手。

- 新增 `src/hunter_sim/probes/viz_proxy.py`：反向代理核心模块。
  - `resolve_upstream_base()`：TLS 时计算 `https://<host>:<adapter_port>`（通配地址回落 127.0.0.1）；
  - `build_ssl_kwargs()`：按 adapter TLS 配置组装 httpx 客户端 `cert`/`verify` 参数；
  - `proxy_request()`：透明转发 method/path/query/headers/body，过滤 hop-by-hop 头；对流式响应（MJPEG `multipart/x-mixed-replace`、SSE `text/event-stream`）用 `StreamingResponse` 逐块转发，普通 JSON/图片缓冲返回；上游不可达 502、超时 504；
  - `register_viz_proxy()`：在探针应用注册 `/viz`、`/viz/assets/{name}`（直读静态文件）、`/viz/api/*`、`/api/v1/*`、`/docs`、`/openapi.json`（代理到管理面）；client 由 lifespan 写入 `app.state`，路由内部延迟取值。
- `src/hunter_sim/probes/app.py`：`create_probes_app()` 在 `adapter.tls_enabled` 时以 lifespan 管理全局 `httpx.AsyncClient`（证书缺失时降级、不阻断启动）并调用 `register_viz_proxy()`；`_fetch_upstream()` 新增 `ssl_kwargs` 透传，`_health_body()` TLS 时用 `resolve_upstream_base` 计算上游地址。
- `src/hunter_sim/visualization/routes.py`：`viz_url()` 在 TLS 时返回 `http://<probe_host>:<probe_port>/viz`，非 TLS 保持 `http://<adapter_host>:<adapter_port>/viz`。
- `src/hunter_sim/cli/main.py`：`_cmd_up()` 恢复无条件自动打开浏览器（URL 已自动指向 8080），移除 V3.2.4 临时加的“mTLS 跳过浏览器 + 导入证书提示”逻辑。

### 向后兼容

无平台侧接口/配置变更；未启用 mTLS 的部署完全不受影响（探针面不注册代理路由，`viz_url` 仍指向 8090，走原有 HTTP 探测 + 自动打开浏览器路径）。平台 Web 端仍通过 `https://<IP>:8090` 访问管理 API（mTLS 契约不变）。

### 风险

低。新增探针面 HTTP 代理仅在本机回环转发 viz 只读页面与 API；探针与管理面同机，代理使用与 server 端同一份证书完成 mTLS，不扩大攻击面。要求探针面与管理面同机部署。

### 验证

- `pytest tests/` 全量：**1217 passed**（新增 `tests/test_probes_viz_proxy.py` 17 项：`resolve_upstream_base`/`build_ssl_kwargs`/`viz_url` scheme 切换、TLS/非 TLS 路由注册差异、`proxy_request` JSON 转发与 502、`_fetch_upstream` ssl_kwargs 透传）。
- `mypy src/hunter_sim`（strict）：89 文件无告警；`flake8` 变更文件零违规。

### 实机执行

`git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.2.5` → 配好 `ADAPTER_SSL_*` 后 `huntersim up` 正常以 mTLS 起 carla-manager 并检测就绪；浏览器自动打开 `http://127.0.0.1:8080/viz`，无需导入任何证书即可看到画面/传感器/事件/日志。

### 分发

镜像 tag `hunter-sim:3.2.5`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）。

### 待办 / 已知边界

- **mTLS 下本机 viz 浏览器访问**：已通过探针面（:8080 HTTP）反向代理解决，浏览器直接访问 `http://<IP>:8080/viz` 即可，无需导入证书。要求探针面与管理面同机部署（回环 mTLS 代理）。
- 沿用 3.2.2 待办项。

---

## V3.2.6 — 修复 viz 代理向管理面建连时服务端证书 EKU 校验失败（502）

**发布日期**：2026-11-05

### 现场

V3.2.5 后 `huntersim up` 启用下发 mTLS 能正常起 carla-manager 并自动打开 `http://127.0.0.1:8080/viz`，`/viz` 页面与 `/viz/assets/*` 静态资源直出正常（200）；但所有代理到管理面的 API 请求（`/viz/api/status`、`/viz/api/logs`、`/api/v1/instances` 等）全部返回 **502 Bad Gateway**，日志刷屏：

```
Viz proxy connect failed: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unsuitable certificate purpose (_ssl.c:1000)
```

页面表现为“加载中 / 未推流”，控制面本身健康（平台仍可正常下发）。

### 根因

管理面 `:8090` 的服务端证书 `ADAPTER_SSL_CERTFILE` 复用接入包内的**客户端证书**（EKU 为 `clientAuth`，用于节点向平台认证）。V3.2.5 的 `build_ssl_kwargs()` 在有 CA 时传 `verify=ssl_ca_certfile`，httpx（Python `ssl`）以 `SERVER_AUTH` purpose 校验管理面服务端证书链时，因该证书 EKU 不含 `serverAuth` 而报 `unsuitable certificate purpose`，握手直接失败。平台（Go 客户端）不强制校验 EKU，故平台→管理面正常，仅本机 Python 代理受影响。

### 修复内容

- `src/hunter_sim/probes/viz_proxy.py`：`build_ssl_kwargs()` 不再以 CA 校验服务端证书，固定返回 `verify=False`（本机回环不跨网，不验证服务端证书可接受）；仍保留 `cert=(ssl_certfile, ssl_keyfile)` 呈现客户端证书，管理面 `CERT_REQUIRED` 照常校验该证书完成 mTLS 握手。同步更新模块设计约束注释。
- `tests/test_probes_viz_proxy.py`：`test_cert_and_verify` 改为断言 `verify is False`（回环代理不验证服务端证书）。

### 向后兼容

无接口/配置变更；仅影响 mTLS 下的本机 viz 代理链路。非 TLS 部署不注册代理路由，完全不受影响；平台→管理面的 mTLS 契约不变。

### 风险

低。`verify=False` 仅作用于探针→管理面的 **127.0.0.1 回环**一跳（不经网络），不降低对外链路安全。

### 验证

- `pytest tests/` 全量：**1217 passed**（`tests/test_probes_viz_proxy.py` 17 项）。
- `mypy src/hunter_sim`（strict）与 `flake8` 变更文件均无告警。

### 实机执行

`git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.2.6` → 配好 `ADAPTER_SSL_*` 后 `huntersim up`，浏览器打开 `http://127.0.0.1:8080/viz` 后顶栏状态灯转绿、实例/日志/传感器面板正常拉取（不再 502）。

### 分发

镜像 tag `hunter-sim:3.2.6`（`Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步）。

### 待办 / 已知边界

- 无新增。沿用 3.2.2 待办项。

---

## V3.3.0 — VIL 消费端 Kafka 连接参数自动继承 CloudSettings（消除 .env 重复）

**发布日期**：2026-11-05
**版本定位**：功能变更——减少平台接入配置重复，简化 .env 复杂度。

### 背景与痛点

VIL 消费端（`KAFKA_*`）与数据回传端（`HUNTER_CORE_KAFKA/SASL/CLIENT_*`）指向同一平台 Kafka broker，凭据和证书完全相同，但旧版要求用户在 .env 中写两份。接入平台后 .env 臃肿且易出现两处不一致。

### 修复内容

- **`HunterSimSettings._inherit_kafka_from_cloud`**（`common/models.py`）：新增 `model_validator(mode="after")`，当用户显式配置了 `HUNTER_CORE_SASL_USERNAME`（非空）时，自动把 CloudSettings 的整套 Kafka 连接参数（bootstrap / 安全协议 / SASL 机制 / 凭据 / 证书路径）继承到 KafkaSettings 中未被显式设定的字段。
- **显式优先**：若用户单独配置了 `KAFKA_BOOTSTRAP_SERVERS` 等，不会被继承覆盖（保留精确控制能力）。
- **本机零影响**：未配置任何平台 SASL 凭据时不触发继承，KafkaSettings 维持默认 `localhost:9092 PLAINTEXT`。

### .env 简化效果

接入平台时用户只需配置 `HUNTER_CORE_*` 一组 Kafka 参数，不再需要写 `KAFKA_BOOTSTRAP_SERVERS` / `KAFKA_USERNAME` / `KAFKA_PASSWORD` / `KAFKA_CA_CERT_PATH` / `KAFKA_CLIENT_CERT_PATH` / `KAFKA_CLIENT_KEY_PATH` 等重复行。`.env.example` 已同步更新。

### 验证

- 全量 `pytest -q`：**1221 passed**（较 V3.2.6 +4 新增继承测试）。
- `mypy --strict`：无告警。
- `flake8`：0 告警。
- 新增测试 `TestKafkaInheritance`（4 例）：无平台配置保持默认、完整继承、显式不覆盖、留空不触发。

### 兼容性

- **默认零改动**：未配 `HUNTER_CORE_SASL_USERNAME` 的现场不受影响。
- **已配 KAFKA_* 的现场**：仍优先用自己的值，行为不变。
- **建议迁移**：平台接入现场可删除 .env 中整块 `KAFKA_*` 配置，仅保留 `HUNTER_CORE_*` 组（并补上 `HUNTER_CORE_CA_CERT_PATH`）。

### 分发

镜像 tag `hunter-sim:3.3.0`。

---

## V3.3.1 — 平台下发报「Carla 管理 API 连接超时」：管理面监听改绑 0.0.0.0（配置修复）

**发布日期**：2026-11-05
**版本定位**：配置修复——无源码变更，仅本机 `.env` 调整。

### 现场

平台以 `SIM_DISPATCH_MODE=node` 下发仿真任务，前端显示 **「Carla 管理 API 连接超时」**。排障时 `netstat` 只见：

```
TCP    127.0.0.1:8090    ...    ESTABLISHED
```

管理 API 仅监听本机回环，外部（平台主机）连接根本到不了 8090 端口——**防火墙规则不是根因**。

### 根因

仿真机按极简模板纯本机联调启动，未创建 `.env`（或其中未设置 `ADAPTER_HOST`），`AdapterSettings.host` 落回代码默认值 `127.0.0.1`（仅回环可达）。node 模式平台需按心跳学习到的地址回访本机 `:8090`，回环绑定导致回访必然超时。该配置点在 `docs/单机部署指南.md`（`ADAPTER_HOST` 行）与 `.env.example` 中早有说明，属现场漏配，非代码缺陷。

### 修复内容

- 新建仓库根目录 `.env`（按 `.env.example` 极简模板生成，`.gitignore` 已排除、不入库），显式设定：
  - `ADAPTER_HOST=0.0.0.0` —— 管理面接受所有网卡入站，平台可回访 `http://<本机内网IP>:8090`；
  - 本机段保留 `CARLA_HOST=127.0.0.1` / `CARLA_VERSION=0.9.16` / `HUNTER_SIM_LOG_LEVEL=INFO`；
  - 平台接入与 Kafka 回传凭据段留空（无凭据静默跳过，不阻断本机）。
- 无源码 / 镜像 / 接口 / 文档结构变更（`docs/单机部署指南.md` §ADAPTER_HOST 既有指引与本次修复一致）。

### 向后兼容

完全兼容。纯本机联调不受影响（不建 `.env` 仍是默认 `127.0.0.1`）；Docker / k8s 部署本就固定 `ADAPTER_HOST=0.0.0.0`，不受本次调整影响。

### 风险

中低。`0.0.0.0` + 8090 **无鉴权**扩大了可达面：须确保仅在受信内网、Windows 防火墙只放行平台主机地址、绝不加 Ingress/NodePort 或映射公网。更收紧的做法是把 `ADAPTER_HOST` 绑到内网网卡具体 IP 而非 `0.0.0.0`。

### 验证

- 配置加载验证：`AdapterSettings()` 读取 `.env` 后解析为 `host = 0.0.0.0, port = 8090`（已在本机执行确认）。
- `huntersim up`/`carla-manager` 启动路径与探针面按 settings 取值传参（`launcher.py` 以 `--host adapter.host` 透传），无硬编码冲突。

### 实机执行

重启管理面（`huntersim up` 或 `huntersim carla-manager`）后依次确认：

1. 启动日志打印 `CARLA manager listening on http://0.0.0.0:8090`；
2. `netstat -ano | findstr :8090` 出现 `0.0.0.0:8090 ... LISTENING`（不再是 `127.0.0.1:8090`）；
3. 平台主机回访通：`curl http://<仿真机内网IP>:8090/health` 返回 `status=ok`；
4. 平台重新下发仿真，「Carla 管理 API 连接超时」消除；本机 `/health` 的 `sim_node` 段可见 `endpoint_ip`/`endpoint_source`。

（端到端 2–4 步待现场重启后执行确认，本文档如实登记为待实机闭环。）

### 分发

镜像 tag `hunter-sim:3.3.1`（`pyproject.toml` / `Makefile` / 两套 compose / `k8s/deployment.yaml` / `README.md` 已同步递增；本次无源码变更，镜像内容较 3.3.0 完全一致）；`.env` 为本机私有配置，不随仓库/镜像分发，各仿真机需自行按本节补配。

### 待办 / 已知边界

- 本条目为**配置修复**，未改任何源码；若现场仍需 8090 鉴权或 TLS，走 V2.0 三步接入的 `ADAPTER_SSL_*`（绑 `0.0.0.0` 与 mTLS 可叠加）。
- 沿用 3.2.2 待办项。
- **本条目结论已被 V3.3.2 推翻**：现场后续排查证明，`ADAPTER_HOST=0.0.0.0` 当时早已正确配置且生效，最初的「Carla 管理 API 连接超时」根因并非绑定地址问题（详见 V3.3.2 根因小节）。本条目中的配置修复本身无害、可保留，但不应作为该次故障的定论。

---

## V3.3.2 — `endpoint_mismatch` 误报修复 + NAT 导致平台学到错误回调地址的排障定论

**发布日期**：2026-11-05
**版本定位**：代码修复——改进仿真节点自学习地址的校验逻辑；同时补充 V3.3.1 那次「Carla 管理 API 连接超时」的真实根因定论（此前误判为绑定地址问题）。

### 现场

现场机器网络拓扑为：仿真机接入一台路由器，路由器上联口地址 `192.168.31.20`（与平台 `192.168.31.35` 同网段），路由器自身为仿真机创建了下挂网段 `192.168.1.0/24`，仿真机实际地址是 `192.168.1.103`。仿真机对平台的注册/心跳出向请求经 NAT 出去，平台从入站请求头学到的源地址是路由器的上联口地址 `192.168.31.20`，而不是仿真机自己的 `192.168.1.103`。平台之后按学到的 `192.168.31.20:8090` 回访下发任务，路由器未做端口转发，导致连接必然超时——这正是最初「Carla 管理 API 连接超时」的真实根因，与 `ADAPTER_HOST` 绑定地址无关（V3.3.1 曾误判为绑定问题，见该节末尾的推翻说明）。

排障过程中还发现一个干扰项：HunterSim 自诊断日志会打印

```
sim_node_endpoint_mismatch: node_id=sim-node-01, platform_learned=192.168.31.20, local_ipv4=['192.168.1.103']
```

但如果机器本身就是多网卡（如同时接办公网和仿真网两个真实网段，均无 NAT），这行日志也会因为旧实现探测不到其它网卡的真实 IP 而误报，把「正常拓扑」误判成「平台学到了陌生地址」，干扰排障方向。

### 根因

`src/hunter_sim/platform_adapter/sim_node.py` 的 `_local_ipv4_addresses()` 旧实现只用 `socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)` 取地址。该方法在多网卡机器上通常只返回主机名解析到的那一个地址（一般是默认路由网卡），**不会枚举机器上其它网卡的真实 IP**。`_warn_endpoint_mismatch()` 直接用这个不完整的集合判断「平台学到的地址是否属于本机」，因此：

- 多网卡但无 NAT 的正常拓扑会被**误报**为 `endpoint_mismatch`；
- 真正经过 NAT 的拓扑（本案例）也会被这个笼统提示掩盖——旧文案只有一句「若经代理/NAT 属预期」，没给出可直接定位的证据，排障时容易先怀疑到绑定地址/防火墙上去。

### 修复内容

- `_local_ipv4_addresses()`：在原 `getaddrinfo` 结果基础上补一次 `socket.gethostbyname_ex(socket.gethostname())[2]`，取同一主机名下的别名地址，降低漏检概率；两个探测各自 `try/except OSError` 独立降级，不再因为其中一个失败而直接返回空集合。
- 新增 `_primary_source_ip_for(destination_host, port=80)`：用 `SOCK_DGRAM` 套接字对平台主机做一次 `connect()` + `getsockname()`（不发送任何报文，只借内核做一次路由查询），拿到「本机访问平台时内核实际选中的源 IPv4」——这是判断平台会看到哪个本机地址的可靠依据，不受主机名解析结果限制。
- `_warn_endpoint_mismatch()`：改为用「`_local_ipv4_addresses()` ∪ 源地址探测结果」这个更完整的集合判断是否真的不一致；确属不一致时，告警文案新增本机访问平台的实际源地址（`本机访问 <host> 的实际源地址=<ip>`）和更明确的排障指引（直连同网段 / 中间设备配端口转发 / 检查多网卡路由），不再是一句笼统的「若经代理/NAT 属预期」。诊断逻辑本身不变（仍只打 `warning` 日志，不阻断注册/心跳/仿真）。

### 向后兼容

无接口 / 配置 / 平台契约变更；`_local_ipv4_addresses()`、`_primary_source_ip_for()` 均为模块内部私有函数，不影响 `SimNodeClient` 对外的 `register()`/`heartbeat()`/`snapshot()` 行为。

### 风险

低。`_primary_source_ip_for()` 打开的 UDP 套接字不做任何实际收发（不发包），仅在操作系统路由表不可达时按 `OSError` 捕获降级为 `None`；不影响注册/心跳主流程，也不引入新依赖（纯标准库 `socket` 实现，未新增 `psutil` 等可选依赖，与项目「最小依赖面」口径一致）。

### 验证

- `pytest tests/` 全量：**1231 passed**（`tests/test_sim_node_client.py` 新增 3 个测试类、10 个用例：`TestLocalIpv4Addresses`（多网卡枚举合并 / 单个探测失败降级 / 两个都失败返回空集）、`TestPrimarySourceIpFor`（命中路由源地址 / 空主机名 / 不可达降级）、`TestWarnEndpointMismatch`（回环不告警 / 多网卡但源地址命中不告警——回归本次误报场景 / 别名枚举命中不告警 / 确属 NAT 时告警且提示带源地址与端口转发线索））。
- `mypy src/hunter_sim`（strict）与 `flake8` 变更文件均无告警。

### 实机执行

`git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.3.2` → 重启 `huntersim carla-manager`：

- 若换网后（仿真机与平台直连同网段、不经 NAT），`sim_node_endpoint_mismatch` 不再出现；
- 若仍经 NAT/代理，日志会给出「本机访问 <平台主机> 的实际源地址=<ip>」，据此判断平台学到的地址是中间设备地址而非本机地址，应改走直连同网段或在中间设备上为 `:8090` 配端口转发。

### 分发

镜像 tag `hunter-sim:3.3.2`（`pyproject.toml` / `Makefile` / 两套 compose / `k8s/deployment.yaml` / `README.md` 已同步递增）。

### 待办 / 已知边界

- 本版本修复的是**诊断准确性**，不改变网络拓扑本身：经过 NAT 的接入路径仍然需要现场按上文处理（直连同网段或端口转发），HunterSim 无法替现场穿透 NAT。
- 若后续要精确枚举机器上**所有**网卡真实 IP（而不只是「访问平台走的这一张」），仍建议评估引入 `psutil.net_if_addrs()`；本次为控制依赖面未采用。
- 沿用 3.2.2 待办项。

---

## V3.3.3 — 平台连续下发两个场景，第二个把整个控制面带崩：CARLA 客户端跨实例复用触发 LibCarla 原生断言

**发布日期**：2026-11-05
**版本定位**：代码修复——执行面装配不再跨实例缓存复用 `carla.Client`。

### 现场

平台在同一个 `carla-manager` 进程内连续下发两个 SIL 场景（均为 Town02）：

| 时刻 | 事件 |
|------|------|
| 16:32:42 | ① `inst-9c18806b…` queued/created，`Loading map: Town02` → 16:32:51 加载成功（9402ms），场景正常跑完 |
| 16:35:19 | ① `running → completed`、`Instance completed passed=True`、`Execution plane released` |
| 16:35:44 | ② `inst-130c7e930…` queued/created，`McapRecorder opened`，`Loading map: Town02` |
| 16:35:44 | **`Assertion failed: _episode != nullptr, file …/carla/client/detail/Simulator.h, line 129`** |
| 16:35:53 | `/viz` 代理建连失败（`Viz proxy connect failed`），`/api/v1/instances` 转 502/500 |
| 收尾 | **`[up] carla-manager 已退出（code=3221226505）`** → `[up] 已停止 probes` |

页面表现：`控制面 离线`、`场景 pending`、主视区「暂无活动场景，等待平台下发实例…」、实例状态「无活动实例」（②停在 `pending`，从未进入 `running`）。

### 根因

`platform_adapter/app.py` 的 `build_execution_plane()` 把 `carla.Client` 连同 `MapManager` **按端点缓存在 `managers` 字典里、永久复用**（`resolve()` 命中缓存直接返回）。现场日志正好构成一组对比：

- 首个实例（进程刚启动）用**新建**客户端 `load_world` → 成功；
- 上一场景 `destroy` / 执行面释放后，第二个实例在**同一个缓存客户端**上再次 `load_world` → 命中 LibCarla 的 `_episode != nullptr` 原生断言。

该断言在 C++ 侧（LibCarla），触发后直接 `abort()`，对应退出码 `3221226505` = `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN / fastfail）。**它不是 Python 异常，`try/except` 与看门狗都接不住**，整个控制面进程直接消失；因此②不是「慢」或「卡」，而是带着它的那个 `carla-manager` 已经死了。

两个实例相差 25 s、①的引擎租约已释放，因此**不是并发冲突**，而是同一端点上的**跨实例连接复用**。页面里成片的 `409 Conflict`（`screenshot` / `sensors/lidar` / `stream.mjpg`）是传感器帧未就绪的正常响应，与本次崩溃无关。

### 修复内容

- `src/hunter_sim/platform_adapter/app.py`：
  - 抽出模块级 `_make_map_manager_resolver(settings)`，其 `resolve(endpoint)` **每次调用都新建一个 `carla.Client` + `MapManager`**，不再返回缓存对象；
  - 删除 `managers` 缓存字典与 `managers_lock`（连带移除不再使用的 `import threading`）；
  - `build_execution_plane()` 改用 `_make_map_manager_resolver(settings)`，docstring 同步为「每次装配新建 CARLA 连接」。
- 代价评估：`carla.Client(...)` 构造是惰性的（首次 RPC 才真正建连），每次装配重建只多一次亚秒级握手，相对 `load_world` 的秒级耗时可忽略；换来的是干净的连接状态。
- 无接口 / 配置 / 平台契约变更；`ExecutionPlane`、`EnginePool`、`MapManager` 本身未改动。

### 向后兼容

完全兼容。仅影响控制面内部的装配方式；单引擎串行、多引擎并行、地图亲和调度、重启恢复、录制与上报链路行为均不变。升级后无需改 `.env`。

### 风险

低。每个实例多一个 `carla.Client` 对象，旧对象随运行器释放由 GC 回收；极端情况下若现场 CARLA 服务端对并发连接数有限制，旧连接回收延迟可能短暂占用一个槽位（不影响单世界串行形态）。

### 验证

- `pytest -q` 全量：**1234 passed**（较 V3.3.2 +3），覆盖率门禁达标。
- 新增回归测试 `tests/test_platform_adapter_app.py::TestMapManagerResolverFreshConnection`（3 例）：
  1. 连续两次 `resolve(同一端点)` 必须构造两个 `carla.Client`（替身计数断言）且返回不同 `MapManager`；
  2. 装配入口 `build_execution_plane()` 同样不缓存（两次解析得到两条新连接）——防止只在子函数修、接线处仍缓存；
  3. 无 CARLA SDK 时降级返回 `None`（报 `Adapter map manager unavailable`），不抛异常、不阻断启动。
- 先写测试后改代码：修改前该组用例因 `ImportError: cannot import name '_make_map_manager_resolver'` 收集失败（红灯已确认）。
- `mypy src/hunter_sim`（strict）无告警；`flake8 src tests` 0 告警。

### 实机执行

现场按下列顺序闭环：

1. `git pull` → `uv pip install -e .`（或 `pip install -e .`）→ `huntersim version` 应为 `3.3.3`；
2. 重启 `huntersim up`（CARLA 服务端不需重启）；
3. 平台连续下发两个场景（建议仍用同一地图以复现本次现场序列），第二个应正常 `pending → running → succeeded`，`carla-manager` 不再退出；
4. 若仍复现，先抓 `carla-manager` 日志确认是否仍是 `_episode != nullptr`，并检查 CARLA 服务端是否已先于控制面退出（看 `CarlaUE4` 窗口/日志）。

（本条目代码已落地并通过全量单测与静态检查；**尚未在带真实 CARLA 服务端的现场复验**，实机第 3–4 步待现场升级后执行确认。）

### 分发

镜像 tag `hunter-sim:3.3.3`（`pyproject.toml` / `Makefile` / 两套 compose / `k8s/deployment.yaml` 已同步递增）。文档同步：《部署与运维指南》§5.6 与 `docs/单机部署指南.md` §9 故障速查均新增本现象条目。

### 待办 / 已知边界

- **episode 失效的确切内部条件尚未确诊**：本版本按现场日志对比（新建客户端成功 / 复用客户端崩溃）消除了「跨实例复用」这个变量，但未能证明 LibCarla 不存在按 `host:port` 共享底层连接状态的情形。若升级后实机仍报 `_episode != nullptr`，下一个候选方案是「目标地图与端点已加载地图相同时跳过 `load_world`，改用 `get_world()`」（正好避开本次出错的那次调用），仍未验证，不得当作已修复。
- `huntersim up` 对 `carla-manager` 异常退出**只关停其余子进程、不自动重启**（本次现场即如此），缺少崩溃自愈与告警；建议后续补「异常退出自动重启 + 计数告警」。
- 同一份现场日志还暴露两个与本崩溃无关、**本版本未改动**的问题（如实登记为待处理）：
  - `SimDataProducer` / `NodeResultProducer` 初始化失败降级为 no-op：`SSLCertVerificationError … self-signed certificate in certificate chain`，属自签 CA 未导入（`HUNTER_CORE_CA_CERT_PATH` 等），后果是云端上行静默丢失；
  - `Ego blueprint 'HUNTER_SE' unavailable, fallback to 'vehicle.tesla.model3'`，属 CARLA 资源未安装 HUNTER_SE 蓝图，后果是自车外观/尺寸与实车不一致。
- 沿用 3.2.2 待办项。

---

## V3.3.4 — 实机「画面空白 + 传感器无数据 + 自车状态恒 0」：传感器取数按测试占位对象形态写死，真机一律落空

**发布日期**：2026-11-05
**版本定位**：代码修复——传感器取数改按 CARLA 官方 API 属性名，自车状态与 `ego_vehicle` 同源，帧失败不再静默。

### 现场

V3.3.3 升级后首次实机下发场景（`SCN-1501-SNO-STD-L1-v12.7`，雪天，`inst-4d020810…`），控制面本身健康（`doctor` 14 项全绿、mTLS 生效、实例 `running/RUNNING`、进度正常推至 13%），但可视化页面三处异常：

| 区域 | 现象 |
|------|------|
| 主视区 | 「相机未挂载（场景未加载或实例已结束）」 |
| 传感器流 | rgb **0 帧**、depth **0 帧**、点云「无点云数据」；imu **774 帧**但三轴恒 `0.000`、payload 只有 **34 B** |
| 自车状态 | x/y/z/heading/velocity/steering **全 0**，车辆栏 `—`，事件 0 条 |
| 日志 | `Sim sensors attached: 4 (profile=hunter_se_full, record=True, ros=False)` |

关键矛盾：日志明确显示 4 个传感器**挂载成功**、IMU **仍在持续出帧**（774 帧），说明回调线程、编码线程、FrameHub 链路都是活的——不是「没数据进来」，而是「进来的数据被当成空帧」。「相机未挂载」只是 `/sensors/...` 返回 409（FrameHub 无帧）时的前端文案，并不反映挂载状态。

### 根因

传感器管线（V3.0.0 引入）按**单测占位对象的属性名**而非**官方 PythonAPI**取数，三处形态不匹配（已对照 CARLA 0.9.16 官方文档逐项确认）：

1. **图像字节位置错**：官方 `carla.Image` 的像素在 `raw_data`，**没有 `data` 属性**；`_raw_image()` 写的是 `getattr(data, "data", b"")` → 真机得到 `b""` → `bgra_to_rgb()` 抛 `bgra buffer size mismatch: expect 1228800, got 0`。
2. **点云字节位置错**：`carla.LidarMeasurement` 同样只有 `raw_data`；`_frame_of()` 的 lidar 分支读 `.data` → 空点云，`_lidar_xyz()` 长度不合法直接返回 `(b"", 0)`，连异常都不抛。
3. **IMU 三轴形态错**：官方 `carla.IMUMeasurement.accelerometer/gyroscope` 是 `carla.Vector3D`，**只有 `x/y/z` 属性、不可迭代也不可下标**；`_vec3()` 用 `[float(item) for item in value]` 取值，`TypeError` 被吞后回退 `(0.0, 0.0, 0.0)`。34 B 恰好等于 `len(repr(((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))))`，而静止车辆的 IMU 本应含重力 9.81——这是形态判断错误的铁证。

上述异常全只写 `logger.debug(...)`，默认日志级别下不可见，**真机表现为「无数据且零线索」**，本次排查因此耗时。

**自车状态恒 0** 是独立的第二个缺陷：`scene_runner_service._get_ego_state()` 只试 `scenario.get_ego_vehicle()` 与 `scenario._ego_vehicle`，而 `BuiltinScenario` 暴露的是 `ego_actor` property（内部存 `_ego`）——两者皆无 → 永远返回零值 default。同文件上方的 `ego_vehicle` property 已经实现了正确的三级解析，只是没被复用。影响面不止页面：SQLite 实例记录的 `ego_state`、上行 `sim_status` / `sim_telemetry` 的位姿与速度、`sample_sink` 评估采样均被污染为静止假象。

同一坑 V2.2.1 曾在 `gui/camera_bridge` 修过并写入过 release（「`raw_data` 优先、`.data` 回退，否则表现为永不出图」），但该模块已在后续版本删除，修复经验没有沉进 V3.0.0 的新传感器管线。

**「ROS2 未启用」不是缺陷**：`SimulationExecutor._ros_bridge_for()` 仅在场景 `sensors[].ros_enabled` 为真时才创建桥接器，下发场景默认 `false`（日志 `ros=False` 已印证），且需本机安装 `rclpy`。属配置开关，本版本不改默认值。

### 修复内容

- `src/hunter_sim/scene_runner/builtin_scenario.py`：
  - 新增 `_measurement_bytes(data)`：**`raw_data` 优先、`data` 回退**（兼容旧版 SDK 与内部跨线程帧对象），`_raw_image()`、`_frame_of()` 的 lidar 分支、`_dispatch_sensor_data()` 的 lidar 分支统一改用它；
  - `_vec3()` 先按 `x/y/z` 属性取（官方 `carla.Vector3D`），再回退序列形态（兼容元组占位对象），非法输入仍回退零向量；`_dispatch_sensor_data()` 的 imu 分支不再自行展开，改调 `_vec3()`；
  - 新增 `_warn_frame_error(stage, stream, exc)`：帧取数/编码失败从 `debug` 提升为 **WARNING**（可视化日志面板可见），策略为「每流首次立即报 + 30s 窗口内聚合成一条」（`_SENSOR_ERROR_LOG_INTERVAL_S`，带锁计数，附 `(+N suppressed)`），既不留白也不形成 50Hz 日志风暴；`_on_sensor_data()` 与 `_frame_worker_loop()` 均改用它。
- `src/hunter_sim/scene_runner/scene_runner_service.py`：`_get_ego_state()` 改用同文件已有的 `self.ego_vehicle` property，不再自己拼一套（旧名 `_ego_vehicle` 仍作为 property 的最后一档保留，向后兼容）；`_vec3` 返回定长三元组后，mypy 报多余的 `# type: ignore[arg-type]` 已删。
- `src/hunter_sim/sensor_sim/image_codec.py`：模块注释「相机回调给出的是 `Image.data`」修正为 `Image.raw_data`，避免后人再按错属性名取数。
- `src/hunter_sim/probes/viz_proxy.py`（本次一并确诊并修复，上一轮排查中的疑似项）：流式转发从 `aiter_bytes(chunk_size=4096)` 改为 **`aiter_raw()`**。实测（真 socket 上游逐段 1s）：`chunk_size=4096` 下前两段小字节数据被攒住，直到第三段 5000B 到达才一次性下发（到达时刻 2.16s vs 0.00s）；mTLS 下浏览器只能走探针面 :8080 代理，因此该缓冲会拖住日志首屏回放与小尺寸 MJPEG 帧。`aiter_raw()` 同时保证不重组上游分帧边界。
- 无接口 / 配置 / 平台契约变更。

### 向后兼容

完全兼容。取数改为「`raw_data` 优先、`data` 回退」、`_vec3`「先属性后序列」，旧 SDK 与现有单测占位对象（`data=` / 元组）仍走回退分支；`_get_ego_state()` 对只提供 `get_ego_vehicle()` 的旧 scenario 行为不变。升级后无需改 `.env`、无需重新下发接入包。

### 风险

低。代理改 `aiter_raw()` 后转发的是**未解码字节流**（不再走 httpx 内容解码）；当前 viz 上游（MJPEG/SSE/JSON）均为 `identity` 不压缩，且响应头原样透传，行为一致；若未来给管理面加了 gzip 中间件，需连同 `content-encoding` 一起透传（已是现行为）。告警级别提升后，真机若仍有其他隐藏取数问题，会直接在日志面板暴露（预期行为，不是回归）。本版本不改变帧率、录制格式与上行 schema。

### 验证

- `pytest -q` 全量：**1246 passed**（较 V3.3.3 +12），覆盖率门禁达标；`mypy src/hunter_sim`（strict）无告警；`flake8 src tests` 0 告警。
- 新增回归测试 `tests/test_builtin_scenario_sensors.py::TestRealCarlaMeasurementShapes`（6 例）——**用官方 API 形态的占位对象**（`_carla_image_data` / `_carla_lidar_data` / `_carla_imu_data` / `_CarlaVector3D`，只给 `raw_data`、只给 `x/y/z`）：rgb/depth/lidar 帧必须进 FrameHub，IMU payload 必须含 `9.81` 且不等于全零 repr，四路走真实挂载回调后 `hub.stats()["total"] == 4`，失败必须留下 WARNING。
- 新增 `tests/test_scene_runner_service.py::TestEgoState`（5 例）：只暴露 `ego_actor` 的 scenario 必须取到位姿/速度/转向/加速度；`get_ego_state()` 与 `ego_vehicle` property 同源；旧 `get_ego_vehicle()` 形态仍可用；无场景时降级零值；上行 `sim_status.ego_state` 不再恒零。
- 新增 `tests/test_probes_viz_proxy.py::TestProxyRequest::test_stream_chunks_keep_upstream_boundaries`：用 `httpx.MockTransport` 逐段下发三个 SSE 事件，断言代理输出与上游**分段逐个相等**（修复前被合并成一个 4KB 缓冲块）。
- 先写测试后改代码：三组用例修改前均确认红灯（传感器 6 败 / 自车 3 败 / 代理 1 败；其中「四路只有 1 路」正对应现场的「只有 IMU 在空转出帧」）。

### 实机执行

1. `git pull` → `uv pip install -e .`（或 `pip install -e .`，必须重跑，否则 `huntersim version` 仍报旧版）→ 应为 `3.3.4`；
2. 重启 `huntersim up`（CARLA 服务端不需重启）；平台**重新下发新实例**（终态实例不补帧）；
3. 预期：主视区出现画面，rgb/depth/lidar 帧数随时间增长，IMU 三轴不再全 0（静止时 z 应接近 9.81），自车状态与进度同步变化；
4. 若仍无图，看日志面板——本版本后帧失败会直接报 `Sensor frame extract/dispatch failed (<stream>): ...`，按该告警定位下一环；报「buffer size mismatch」类仍属取数，报蓝图/渲染类则转服务端排查；
5. ROS2 话题仍为空属正常，除非场景配置 `sensors[].ros_enabled=true` 且本机装了 `rclpy`（平台侧在场景 JSON 里打开）。

（本条目代码已落地并通过全量单测与静态检查；**尚未在实机 CARLA 上复验**，第 3–5 步待现场升级后执行确认。）

### 分发

镜像 tag `hunter-sim:3.3.4`（`pyproject.toml` / `Makefile` 首行注释与 `IMAGE_TAG` / 两套 compose / `k8s/deployment.yaml` 已同步递增）。文档同步：《部署与运维指南》§5.7「画面与录制」新增 3 行（取数形态导致的无数据、ROS2 置灰属配置、代理分块缓冲），`docs/单机部署指南.md` §9 故障速查新增 2 行。

### 待办 / 已知边界

- **本版本修的是「取数形态」，不是「CARLA 渲染链路」**：升级后若帧数仍为 0，说明还存在第二个独立原因（地图/画质/传感器属性/渲染超时），不得提前当作已修复关闭工单。
- **未确诊（仅登记）**：ROS2 出口 `camera_image_to_ros_image` 将 BGRA 字节与 `rgb8` / `16UC1` 编码搭配的合理性存疑；因为本次现场 `ros=False` 从未走过该路径，无真实证据，且开启 ROS2 需平台侧配合，本轮未动。
- **未验证**：自车状态修复后，平台侧评估采样（`sample_sink`）与 `sim_telemetry` 的数据质量需实机复核；旧实例已写入的恒零 `ego_state` 不会回溯修正。
- `image_codec.py:170` 与 `visualization/sensor_view.py:179` 存在 Pillow DeprecationWarning（`Image.fromarray(..., mode=...)` 将在 Pillow 13 移除），本版本未改。
- 沿用 V3.3.3 待办项（含 `carla-manager` 异常退出不自动重启、HUNTER_SE 蓝图缺失回退、自签 CA 未导入导致上行静默丢失）。

---

## V3.3.5 — CARLA 运行时崩溃与自车状态冻结修复

**发布日期**：2026-11-06
**版本定位**：修复 V3.3.4 实机运行中暴露的两个缺陷：CARLA 服务端 GPU 过载导致 Fatal Error（退出码 3221226505）及可视化页面自车状态冻结。

### 问题现象

1. **CarlaUE 崩溃**：场景正常运行约 6 分钟后，CARLA 服务端报 `Fatal error!` 并退出，对应 Windows 退出码 3221226505（`STATUS_STACK_BUFFER_OVERRUN`）。日志可见：`Blueprint attribute 'fps' set failed: attribute 'fps' not found`（出现两次）。
2. **可视化黑屏 + 自车状态全零**：主视区画面全黑，x/y/z/heading/velocity/steering/acceleration 始终显示 0.00，但传感器计数持续增长（证明帧管线存活）。

### 根因分析

**崩溃根因**：`sensor_factory.py` 中 RGB 和 Depth 相机使用了不存在的蓝图属性名 `"fps"`，CARLA 0.9.16 的正确属性名为 `"sensor_tick"`（单位：秒，值 = 1/fps）。`_safe_set_attribute` 捕获异常后仅记日志，导致相机未被限帧——以世界 tick 率 50Hz 渲染而非配置的 30Hz，GPU 负载翻倍。两个 1280×720 相机 50Hz 渲染最终压垮服务端。

**状态冻结根因**：executor 轮询线程调用 `runner.get_ego_state()` 时，内部通过 CARLA RPC 访问 `vehicle.get_location()` 等，与 tick 线程的 `world.tick()` 并发。CARLA Python 客户端非线程安全，跨线程 RPC 可能失败抛异常，`_get_ego_state()` 捕获后返回零值 default 字典。

### 修复内容

- `src/hunter_sim/sensor_sim/sensor_factory.py`：
  - RGB 相机（line 288）与深度相机（line 299）的蓝图属性 `"fps"` → `"sensor_tick"`，值从 `str(config.fps)` 改为 `str(1.0 / config.fps)`，与 IMU/GNSS 分支写法统一；
- `src/hunter_sim/scene_runner/scene_runner_service.py`：
  - 新增 `_ego_state_cache: dict[str, Any]` 属性，由 tick 线程在 `_get_ego_state()` 成功读取后写入；
  - tick 循环中 `world.tick()` 后**无条件**调用 `_get_ego_state()` 更新缓存（不依赖 sample_sink / sim_data_producer 是否激活）；
  - `get_ego_state()`（公开 API，executor 线程调用）改为优先返回缓存副本，避免跨线程 CARLA RPC；首次 tick 前缓存为空时回退直接调用（无副作用）；
  - `_get_ego_state()` 返回前赋值 `self._ego_state_cache = state`，保证缓存与 tick 循环同步更新。
- 无接口 / 配置 / 平台契约变更。

### 向后兼容

完全兼容。`sensor_tick=0.0333` 与原期望 fps=30 语义等价；旧场景配置若未指定 fps 则仍用默认 30.0。`get_ego_state()` 返回类型不变，仅内部实现由「每次 RPC」改为「读缓存」，调用方无感。

### 风险

低。`sensor_tick` 修复后相机按 30Hz 出帧，GPU 负载预期降低约 40%，可消除运行时崩溃。自车状态缓存在极早期（第一 tick 前）有微小窗口仍走直接 RPC，但此时场景尚未启动不会导致问题。

### 验证

- `pytest -q` 全量：**1246 passed**，无失败。
- 日志对比：修复后不再出现 `Blueprint attribute 'fps' set failed` 告警，相机按 `sensor_tick=0.0333` 正确设置。

### 实机执行

1. `git pull` → `uv pip install -e .` → 应为 `3.3.5`；
2. 重启 `huntersim up`（CARLA 服务端需重启以释放之前可能残留的 GPU 资源）；
3. 平台重新下发新实例；
4. 预期：不再出现 `Fatal error!` 崩溃；主视区画面正常显示（非全黑）；自车状态随场景运行实时更新。

### 分发

镜像 tag `hunter-sim:3.3.5`。

### 待办 / 已知边界

- **黑屏另一个可能原因**：若平台下发的场景 `map.spawn_point` 为 `(0,0,0)`，自车会生成在地图原点附近（可能不在有效路面上）。此属场景配置问题，需平台侧确保 spawn_point 指向有效车道坐标。本版本的 `sensor_tick` 修复可解决因 GPU 过载导致的黑屏，但无法修正配置错误引起的渲染空白。
- **自动重启**：CARLA 服务端崩溃后 `carla-manager` 不会自动拉起，仍需运维手动介入（延续 V3.3.3/V3.3.4 待办）。
- **缓存窗口**：场景启动的第一帧前，若 executor 轮询已到，仍会尝试直接 RPC——此时 ego_vehicle 尚未生成，返回空 dict 不写入 DB，无副作用。

---

## V3.4.0 — 实现出生点有效坐标替换 + 传感器按实际外形挂载

**发布日期**：2026-11-07
**版本定位**：承接 HunterCore V1.25.0「出生点职责转移」契约与 V1.24.9「传感器挂载依据」契约，修复平台下发占位原点 `(0,0,0)` 导致的黑屏/全黑，以及相机按写死车身尺寸挂载导致「只见路面 / 镜头埋进车身」。

### 问题背景

1. **黑屏 / 场景全黑**：平台场景模板的 `map.spawn_point` 常为占位原点 `(0,0,0)`，仿真端直接使用会让自车落在世界原点，HUD 自车状态恒为 0，画面全黑。
2. **参与者挤在原点**：`spawn_semantics=relative_to_ego` 时，`actors[].spawn_point` 是相对自车基准的偏移；若自车基准未替换就叠加，参与者全部落在原点附近。
3. **只见路面 / 相机埋进车身**：`sensor_mount.py` 用写死的 `(0.40, 0.30, 0.70)` 等车身尺寸挂载；实际 spawn 到的回退蓝图尺寸不同，相机光心过低、pitch 过负，画面只剩沥青。

### 修复内容

**第一部分：出生点有效坐标替换（`scene_runner/spawn_resolver.py` 新增）**

- `map_type=carla_town` 且 `map.spawn_point` 为占位原点（`|x|,|y|,|z|,|yaw| < 0.1`）或经 `get_waypoint` 判定不在路面时，改用 `world.get_map().get_spawn_points()` 的合法点；非 `carla_town`（`hd_map` / `site_map`）绝不替换。
- 候选点按 `scene_key = scene_id::scene_version` 做 SHA-256 稳定散列选择，同一场景可复现；`extra_params.spawn_variant` 用于同批实例分散（`(base + variant) % count`）。
- 自车 spawn 失败（点被占用）时按候选序列依次重试，最多 16 个候选，并记录每次失败/回退日志。
- `spawn_semantics=relative_to_ego` 时，参与者坐标 = **替换后的自车实际 transform** + 平台偏移（世界平移 + yaw 累加并归一到 `[-180,180)`）；`absolute` 保持原样。
- 替换前后坐标、替换原因（`placeholder_origin` / `off_road`）均打日志，便于定位是否生效。

**第二部分：传感器挂载按实际蓝图包围盒换算（`sensor_sim/sensor_mount.py` 重写）**

- 新增 `SensorEnvelope` / `local_envelope()` / `resolve_envelope()`：优先使用平台 `sensor_mount_reference.status=resolved` 的 `envelope_m`；`blueprint` 与实际 actor `type_id` 不一致或字段不完整时降级本地实测。
- 本地实测读取已 spawn 车辆的包围盒 `location ± extent`，与平台 envelope 同一套算术；同时兼容 CARLA Python API 的 `actor.bounding_box` 属性与部分封装的 `get_bounding_box()` 方法（实机 CARLA 只提供属性形态，此前只认方法会误判为不支持而跳过全部传感器）。拿不到 bbox 或 extent 非法时抛 `SensorSimulationError`（错误信息含 `type_id` 与可用 bbox 属性），**绝不回落到写死车身尺寸**。
- 新增 `compute_local_transform()` / `resolve_mount_transforms()` / `load_mount_config()`，安装位统一为 `mount_loc = anchor(envelope_m) + offset_m`。
- `configs/sensor_configs.json` 的 `mounts` 改为 `anchor` + `offset_m` + `rot_deg` 结构并真正生效：前视相机锚点 `front_edge`、默认 `(-0.45, 0, -0.10)`、pitch `-8°`；LiDAR 锚点 `roof`、默认 `z=+0.15`；同时覆盖深度相机、IMU、GNSS、碰撞、车道偏离、障碍物。
- 实例创建时打一行 `mount source=platform|local status=<status> plat_bp=... actual_bp=... roof_z=... front_x=...`，运维可据此判断挂载依据来自平台还是本地实测。
- 配置文件缺失时使用内建「锚点 + 传感器余量」默认（不含车身尺寸）并告警；JSON 格式错误抛 `ConfigurationError`。

**场景契约（`scene_config.py` / `platform_adapter/scene_mapper.py`）**

- `SceneConfig` 新增 `map_type`、`spawn_semantics`、`sensor_mount_reference`；内部默认 `spawn_semantics="absolute"` 保持旧场景兼容。
- 平台 mapper 读取 `map.map_type` / `spawn_semantics` / `sensor_mount_reference`；缺失 `map_type` 时，`Town*` 地图推断为 `carla_town`（保证旧载荷也能触发原点兜底）；缺失 `spawn_semantics` 时按 V1.25.0 契约取 `relative_to_ego`。

### 向后兼容

- 直接构造 `SceneConfig` 的旧场景：`map_type=""`、`spawn_semantics="absolute"`，不会触发替换与偏移叠加，行为与 V3.3.5 一致。
- 平台非 `carla_town` 地图坐标逐字使用，不做任何替换。
- 传感器挂载数值会随实际蓝图包围盒变化，属于预期修复；若需固定特殊车辆安装位，仍可通过 `custom_mounts` 显式覆盖。

### 验证

- `pytest -q` 全量：**1274 passed**，无失败（新增 `tests/test_spawn_resolver.py` 及自车替换/参与者偏移/挂载换算用例，并覆盖 `bounding_box` 属性与 `get_bounding_box()` 方法两种 API 形态）。
- `mypy src/hunter_sim`：90 个源文件无问题；`flake8 src tests`：无告警。

### 实机执行

1. `git pull` → `uv pip install -e .` → 应为 `3.4.0`；
2. 重启 `huntersim up`；
3. 平台下发使用 `spawn_semantics=relative_to_ego` 的场景；
4. 预期日志出现 `ego_spawn_replaced reason=placeholder_origin ...` 与 `mount source=local status=<未解析状态> ...`；
5. 预期：HUD 自车 `x/y` 非 0、画面出现地平线/车道线、相机不再埋入车身、参与者跟随新自车基准。

### 分发

镜像 tag `hunter-sim:3.4.0`。

### 待办 / 已知边界

- 平台 `sensor_mount_reference` 在当前部署环境（HUNTER_SE 无蓝图 + dev build 采不出尺寸）常态为 `table_uncalibrated` / `blueprint_unmatched`，主路径仍是本地 `local_envelope()`；平台几何表入库后自动优先使用。
- 包围盒 `location` 与 `attach_to()` 参考原点是否同址，仍需按方案 §11 在与服务器同版本的客户端上实测确认。
- `side_edge_y_m` 只给绝对值，左右侧相机符号需按实机画面确认。

---

## V3.4.1 — 环视主视区 + 启动期日志治理 + Kafka TLS 证书自动发现

**发布日期**：2026-11-07
**版本定位**：V3.4.0 实机验证后的**问题修复 / 功能完善**（patch，第三位递增）。V3.4.0 已确认传感器按实际包围盒挂载生效（`Sim sensors attached: 4`、mcap 正常出包），但实机仍有三类问题：主视区只见车头/路面看不到整车周边；场景启动前日志被大量 `409 Conflict` 刷屏；`SimDataProducer` / `NodeResultProducer` 初始化失败于 `CERTIFICATE_VERIFY_FAILED`（自签证书链）。

### 问题背景

1. **主视区视野过窄**：前置 RGB 相机锚点 `front_edge`、pitch `-8°`，只能看到车头前方，无法判断整车左右/后方环境。
2. **启动期 409 噪声**：实例 `pending`（场景加载中）阶段前端已开始拉流与轮询，`/screenshot`、`/stream.mjpg`、`/viz/.../sensors/{lidar,depth,imu}` 全部以 `409 camera_not_attached` / `sensor_frame_unavailable` 作答，日志被无意义错误刷满，现场排查时难以分辨真实故障。
3. **Kafka TLS 握手失败**：平台 CA 已随接入包下发到 `data/certs/` 或 `data/kafka-certs/`，但未写入 `.env`，客户端退回系统信任链后报 `SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain`；失败日志只有一行原始异常，没有可操作提示。
4. **`HUNTER_SE` 回退告警误导**：未部署 `vehicle.hunter_se.*` 资产的环境常态回退 `vehicle.tesla.model3`，却以 WARNING 级别每次启动都报，容易被当成故障。

### 修复内容

**一、环视主视区（`overview` 流，仅可视化）**

- `sensor_sim/frame_hub.py` 新增 `STREAM_OVERVIEW`（加入 `KNOWN_STREAMS`）。
- `sensor_sim/sensor_mount.py` 新增 `overview_camera` 安装位：锚点 `rear_edge`，默认 `offset_m=(-5.0, 0, +2.5)`、`pitch=-20°`，配合 FOV 100° 可同时看到整车前后左右；新增 `get_mount_by_id()`、`attach_sensor(..., mount_id=...)`、`load_overview_camera_config()`。
- `configs/sensor_configs.json` 新增 `mounts.overview_camera` 与 `overview_camera` 参数段（`enabled/width/height/fov_deg/fps`，默认 `960×540 @ 10Hz`）。
- `scene_runner/builtin_scenario.py` 独立挂载环视相机（`_overview_sensor`）：**只推 `FrameHub`**，不进入 `_sensors` 计数、不录 mcap、不发 ROS，因此不改变平台数据契约与 mcap 话题集；`cleanup()` 一并停止销毁。
- `platform_adapter/service.py` 新增 `_latest_frame()`；`screenshot()` / `stream_frames()` 支持 `stream` 参数。
- `platform_adapter/app.py`：`/screenshot`、`/stream.mjpg` 支持 `?stream=overview`（缺省仍 `rgb`；`overview` 无帧时回退 `rgb`，保证平台调用方零改动可用）。
- `visualization/routes.py`：`/viz/.../sensors/overview` 支持同语义透传与回退；`visualization/sensor_view.py` 按 RGB 原样透传。
- `visualization/assets/app.js`：主视区优先请求 `overview`，未挂环视相机时自动回退前置 RGB。

**二、启动期日志治理（409 → 204 / running 门控）**

- 无首帧时 `/screenshot`、`/stream.mjpg`、`/viz/.../sensors/{stream}` 返回 **204 No Content**（替代 409），语义明确为「暂无内容，属启动早期正常状态」。
- 前端仅在实例状态 `running` 时启动 MJPEG 拉流与深度/点云/IMU 预览轮询；`pending` 阶段显示「等待实例启动…」，不发无效请求；204 显示「等待首帧…」。
- 保留终态（`succeeded/failed/canceled`）短路逻辑与一次收尾加载，面板仍能落到最终值。
- `Ego blueprint 'HUNTER_SE' unavailable` 由 WARNING 降为 INFO，并提示如需实车尺寸请安装 `vehicle.hunter_se.*` 蓝图（挂载仍按实际包围盒换算，不受影响）。

**三、Kafka TLS 证书自动发现与可操作失败提示**

- `common/models.py`：`CloudSettings` / `KafkaSettings` 新增 `ssl_verify`（默认 `true`）、`auto_discover_certs`（默认 `true`），并纳入 `_inherit_kafka_from_cloud` 继承规则。
- `cloud/kafka_security.py`：
  - 证书路径**全部未显式配置**时自动搜索 `data/certs/`、`data/kafka-certs/`、`certs/`（cwd 与仓库根各试一次）；`HUNTER_SIM_CERT_DIRS` 可追加证书目录（按证书目录语义解析，`;`/`,` 分隔）。
  - **任一证书路径被显式配置即不做自动发现**，避免拼出「显式 CA + 自动发现的客户端证书」这类易错组合。
  - `ssl_verify=false` 时构造 `ssl.SSLContext`（`check_hostname=False`、`verify_mode=CERT_NONE`，仍加载 mTLS 客户端证书），并打 WARNING 提示仅限内网自签环境；`ssl_context` 会覆盖 kafka-python 其他 `ssl_*` 参数，故不同时下发。
  - 新增 `ssl_failure_hint()`：TLS/证书类异常附加「放 CA 到默认目录 / 显式配置路径 / 临时关闭校验」的排查提示。
- `cloud/sim_data_producer.py` / `cloud/node_result_producer.py`：初始化失败日志追加 `ssl_failure_hint()` 输出。
- `.env.example` 补充 `HUNTER_CORE_AUTO_DISCOVER_CERTS` / `HUNTER_CORE_SSL_VERIFY` / `HUNTER_SIM_CERT_DIRS` 及说明。

### 向后兼容

- `overview` 为**新增可选流**：不传 `stream` 时行为与 V3.4.0 完全一致；平台侧无需改动。
- 无首帧状态码由 `409` 改为 `204`：如调用方或告警规则按 409 分支判断「相机未挂载」，需同步调整为「204 = 等待首帧 / 非错误」。实例不存在仍为 `404`，并发超限仍为 `503`。
- `ssl_verify` / `auto_discover_certs` 默认 `true`，未配置证书的场景行为不变；仅当默认目录下存在证书文件时才会自动启用。

### 验证

- `pytest -q` 全量：**1285 passed**，无失败（新增/更新 `tests/test_sensor_mount.py`、`tests/test_builtin_scenario_sensors.py`、`tests/test_adapter_stream.py`、`tests/test_viz_api.py`、`tests/test_sim_data_producer.py`）。
- `mypy src/hunter_sim`：90 个源文件无问题；`flake8 src tests`：无告警。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.4.1`；
2. 重启 `huntersim up`；
3. 预期日志出现 `Overview camera attached: mount=overview_camera fov=100 size=960x540`，`Sim sensors attached` 计数保持 4（环视相机不计入）；
4. 预期 `/viz` 主视区可见整车及前后左右环境；`curl -o f.jpg "$BASE/api/v1/instances/<ID>/screenshot?stream=overview"` 返回 200；
5. 预期启动期不再出现 `/screenshot`、`/stream.mjpg`、`/sensors/*` 的 409；
6. 证书放入 `data/certs/` 或 `data/kafka-certs/` 后重启，预期 `Kafka TLS 证书自动发现: ca=... cert=... key=...` 且 producer 初始化成功；若仍失败，日志会给出可操作提示。

### 分发

镜像 tag `hunter-sim:3.4.1`；wheel / 容器版本号与 `pyproject.toml` 的 `3.4.1` 一致。

---

## V3.4.2 — 场景车辆运动/行为生效 + 场景结束恢复异步 + 环视独立编码

**发布日期**：2026-11-09
**版本定位**：V3.4.1 实机验证后的**问题修复 / 功能完善**（patch，第三位递增）。V3.4.0 / V3.4.1 已确认地图、天气、传感器与环视主视区画面正常，但实机仍有四类问题：可视化里自车与交通参与者基本静止，只能看到静态地图/环境；场景声明的行为（匀速 / 减速 / 切入 / 横穿）在测试全程看不到；场景结束后 CARLA 模拟器无响应、后续 `load_map` 超时；高负载场景下环视主视区掉帧甚至无画面。

### 问题背景

1. **Traffic Manager 未同步**：世界已切到同步模式（50 Hz 固定步长），但 Traffic Manager 仍工作在异步模式；`set_autopilot(True)` 的车辆会等待世界 tick，表现为自车和参与者停在原地。
2. **`BuiltinScenario.tick()` 是空实现**：参与者只在装配时启用 autopilot，场景声明的行为从未在 tick 中推进；行人 / 骑行者也只静态生成，没有 AI 导航。
3. **释放路径未恢复异步模式**：场景结束（成功 / 失败 / 取消）后世界仍停留在同步模式，CARLA 持续等待不再到来的 `world.tick()`；且同步模式下销毁 Actor / 传感器会阻塞，表现为模拟器卡死、后续实例一直 `pending` / `load_map` 超时。
4. **环视流被数据编码队列饿死**：环视帧与 RGB / 深度 / LiDAR / IMU 共用 `hunter-sensor-encoder` 有界队列；数据传感器繁忙时环视帧被大量丢弃，主视区卡顿或黑屏。

### 修复内容

**一、Traffic Manager 与世界同步（`engine/map_manager.py`、`execution/plane.py`）**

- `MapManager` 新增 `client` 属性与 `get_traffic_manager(port)`（CARLA 0.9.16 的 TM 归属于 `carla.Client`，不在 `World` 上）；`CarlaClientProtocol` 同步补充 `get_trafficmanager`。
- `ExecutionPlane` 在装配时：世界切同步成功后，通过 `EngineEndpoint.tm_port` 取 TM 并 `set_synchronous_mode(True)`；TM 不可用只告警、不阻断场景（降级为参与者可能静止）。
- `traffic_manager` / `traffic_manager_port` 透传给 `SceneRunnerService` 与 `BuiltinScenario`，替换原先写死的 TM 端口。

**二、场景运动与行为真正生效（`scene_runner/builtin_scenario.py`）**

- `tick()` 由空实现改为每帧推进：自车初始速度保持（非 autopilot 时 P 控制油门 / 刹车）、参与者行为控制器更新、行人 AI 导航。
- 自车：`ego_vehicle.initial_speed > 0` 时优先通过 TM `set_desired_speed` 设置；TM 不可用时退回手动速度保持，避免自车不动。
- 车辆参与者：非 `static` 行为统一启用 TM autopilot 保持车道内行驶，再叠加场景行为——`constant_speed` / `accelerate` 设期望速度，`decelerate` / `stop_and_go` 逐步降到 0，`cut_in` / `cut_out` 触发后只发一次 `force_lane_change`。
- 变道方向遵循 CARLA 语义（`True` 向右、`False` 向左）：优先按自车相对参与者的横向位置选择切向自车所在车道；读不到位姿时退回 `CutInBehavior.lateral_offset_m` 的符号；`cut_out` 取反。
- 行人 / 骑行者：非 `static` 时挂 `controller.ai.walker` + `WalkerControllerWrapper`，按出生朝向横向 8 m 导航横穿；`static` 保持原地；CARLA SDK 或 AI 蓝图缺失时告警并保持静态，不阻断场景。
- `static` 车辆显式 `brake=1.0` 刹停，避免启用 autopilot 后自行开走。
- 行为触发条件复用 `traffic_sim/trigger_conditions`：`time` / `distance` / `velocity` / `event` 生效；`position` 因 `SceneEventTrigger` 不携带坐标而跳过并记 debug 日志。
- `CutInBehavior` 新增 `lateral_offset_m` 属性与 `behavior_id` 工厂参数，`ScriptedActorController` 暴露 `actor`，供方向判断与清理使用。
- `cleanup()` 停止行人导航、销毁 AI 控制器、清空行为控制器与变道去重集合。

**三、场景结束恢复异步，保证 CARLA 可继续使用（`execution/plane.py`、`scene_runner/scene_runner_service.py`）**

- `ExecutionPlane` 记录每实例 `(world, traffic_manager)`，释放 / 装配失败 / 取消时先恢复 TM 与世界为异步模式，再清理或归还端点。
- `SceneRunnerService` 新增 `_restore_async()`：运行循环退出、`stop()`、失败路径统一恢复异步后再 `_cleanup()`，避免同步模式下 `actor.destroy()` 阻塞等待 tick。
- 新增 `_ensure_sync_mode()`：同一运行器重新下发场景时重新切回同步（TM + 世界），避免上一轮恢复异步后 `world.tick()` 不再推进。
- `stop()` / `destroy()` 在 tick 线程未能在超时内退出时**不并发清理**（CARLA 客户端非线程安全），改为延迟到运行循环自行清理；仅在拿到引擎锁后销毁，和后续装配串行。

**四、环视独立编码（`scene_runner/builtin_scenario.py`）**

- 环视帧不再进入数据传感器队列，改走独立 `hunter-overview-encoder` 线程与小容量队列（仅保留最新帧，队满丢最旧）；数据传感器高负载时主视区不再被饿死。
- 环视仍只推 `FrameHub`，不录 mcap、不发 ROS，不改变平台数据契约；`cleanup()` 一并停止线程并清空队列。

### 向后兼容

- 场景 JSON 与平台数据契约不变；`overview` 仍是可选流，不传 `stream` 时行为不变。
- TM 不可用的旧版 CARLA / 受限客户端：仅记录 warning，自车退回手动速度保持、参与者可能静止，但场景仍可运行。
- `CutInBehavior.lateral_offset_m` 为新增内部属性，未改动现有场景字段；方向判断缺省仍按自车相对位置。
- `stop()` 在 tick 线程未退出时不再等待清理完成，调用方看到的实例终态与之前一致。

### 验证

- `pytest -q` 全量：**1301 passed**，无失败（新增 / 更新 `tests/test_builtin_scenario.py`、`tests/test_execution_plane_lifecycle.py`、`tests/test_scene_runner_service.py` 与 `tests/mocks/carla_mocks.py`）。
- `mypy src/hunter_sim`：无问题；`flake8 src tests`：无告警。
- 真机 CARLA 端到端验证待执行（当前开发环境 `.venv` 未安装 `carla` 模块）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.4.2`；
2. 重启 `huntersim up`；
3. 下发含交通参与者与行为（`constant_speed` / `decelerate` / `cut_in` / `pedestrian_cross`）的 SIL 场景；
4. 预期日志出现 `Traffic Manager switched to synchronous mode (port=...)`、`Ego initial speed ...`、`Walker '...' crossing at ...`；`/viz` 主视区可见自车与参与者持续行驶，切入 / 减速等行为按触发条件出现；
5. 场景结束后再下发同一地图的新场景，预期 `load_map` 不再超时；日志出现 `World restored to asynchronous mode: instance=...`。

### 分发

镜像 tag `hunter-sim:3.4.2`；wheel / 容器版本号与 `pyproject.toml` 的 `3.4.2` 一致。

### 待办 / 已知边界

- 真机需重点验证：TM `set_desired_speed` / `force_lane_change` 在目标 CARLA 0.9.16 上的实际效果；`stop_and_go` 仍以「减速到 0」近似，未实现停车后的再起步；`accelerate` 以更高目标速度近似。
- `TriggerType.POSITION` 参与者行为触发暂不支持（`SceneEventTrigger` 无坐标），需平台下发可解析的位置条件后再补。
- 行人横穿目的地为出生点横向固定 8 m，复杂路口 / 人行道场景可能需平台提供路径点。


## V3.4.3 — 主视区原生画质 + 深度图默认开启 + 交通参与者可见 + 自车状态真实上报

**发布日期**：2026-11-09  
**版本定位**：V3.4.2 实机验证后的**可视化验收缺陷修复**（patch，第三位递增）。四项问题：主视区画面模糊；深度图预览默认未勾选；下发「儿童追球」类场景时主视区只有自车、看不到交通参与者与行为；行驶中「自车状态」面板全 0。V3.4.2 已让参与者与行为在 CARLA 里运动起来，但可视化链路上还有四处断点，表现为「看不见、看不清、看不到数据」。

### 问题背景

1. **主视区被按数据流降采样**：`ADAPTER_STREAM_MAX_WIDTH`（默认 640）原本只应约束 RGB 数据流，却同样作用在 `overview` 环视帧上：原生画面被压到 640 宽（现场为 960x540 相机），再按 JPEG 质量 75 编码，放大到面板后明显模糊。只把相机分辨率提到 1080p 而不改编码分支无效。
2. **深度图预览默认关闭**：`app.js` 的 `depthOn` 初值为 false，HTML 复选框也没有 `checked`，每次打开页面都要手动勾选。
3. **参与者生成了但画面里看不到**：`actors=1`、UI「参与者 2」说明参与者确实进入了世界，但 `_spawn_participant()` 在蓝图缺失时只记一条 WARNING 就丢弃；`_setup_walker_ai()` 把**世界坐标**当作 `attach_to` 的相对偏移传给 `controller.ai.walker`（CARLA 语义：attach 时的 transform 是相对父级的偏移），控制器被摆到「出生点×2」的位置，导航目标错位，行人不横穿；成功日志里也没有坐标/距离，无法判断参与者是否落在相机视野内。
4. **自车状态被「零值兜底」掩盖**：`_get_ego_state()` 取不到自车或读取异常时返回**非空零值字典**，`SimulationExecutor._persist_progress()` 的 `if ego:` 判定为真，把 0.000 当成真实位姿写进实例记录，可视化面板与 `sim_telemetry` 上行就整场显示 0；异常本身被 `except Exception: return default` 静默吞掉，日志里没有任何线索。

### 修复内容

**一、主视区回归 CARLA 原生画质（`scene_runner/builtin_scenario.py`、`sensor_sim/sensor_mount.py`、`configs/sensor_configs.json`）**

- `_dispatch_sensor_data()` 的 `overview` 分支不再套用 `stream_max_width`（固定 `max_width=0`，不降采样），JPEG 质量下限提到 90（`_OVERVIEW_JPEG_QUALITY`）；`ADAPTER_STREAM_MAX_WIDTH` 只保留给 `rgb` 数据流。
- 环视相机分辨率默认 1920x1080（`configs/sensor_configs.json` 与代码内建默认值同步），日志改为 `Overview camera attached: ... size=1920x1080 (native encode, jpeg>=90, no downscale)`，可直接从日志确认生效参数。

**二、深度图预览默认开启（`visualization/assets/app.js`、`index.html`）**

- `state.depthOn` 初值 true、复选框加 `checked`；`bootstrap()` 以 DOM 勾选态回填 `state.depthOn`，刷新页面后状态与复选框不再脱节；预览区初始提示改为「等待深度帧…」。

**三、交通参与者与行为在画面里可见（`scene_runner/builtin_scenario.py`）**

- 蓝图回退：参与者蓝图未安装时按 `actor_type` 回退到同类通用蓝图（walker → `walker.pedestrian.0001` / `walker.pedestrian.*`，vehicle → `vehicle.tesla.model3` / `vehicle.*`），不再静默丢弃。
- 出生点重试：`try_spawn_actor` 返回 None（位置被占用）时，沿自车航向前后 / 左右偏移 2~6 m 重试至多 9 个候选点，全部失败才告警并给出起点坐标。
- 行人 AI 修正：`controller.ai.walker` 改回以**单位变换**附着（`carla.Transform()`），符合 `attach_to` 的相对语义；横穿目的地改为「垂直于自车航向、朝自车车道方向走 8~18 m」，不再依赖平台下发的行人 yaw 约定，行人一定会穿过自车行驶线。
- 脚本化兜底：AI 蓝图缺失、控制器生成失败或导航启动失败时登记 `_ScriptedWalker`，每个 tick 按 `speed_ms` 把行人直行推向目标点（到达即停），保证可视化里始终能看到「行人过街」。
- 可诊断日志：参与者生成成功打印 `actor / blueprint / type / behavior / spawn 坐标 / distance_to_ego`；距自车超过 80 m 且非 `static` 时告警提示「环视主视区可能拍不到」；`Builtin scenario ... ready` 改为 `actors=已生成/声明`，未全部生成时列出 ID。
- 自车生成后写入 `role_name=hero`，为自车引用兜底提供锚点。

**四、自车状态真实上报（`scene_runner/scene_runner_service.py`）**

- 新增 `_resolve_ego_actor()`：按 `ego_actor` / `_ego_vehicle` / `_ego` / `get_ego_vehicle()` 顺序取第一个**非 None** 的引用（旧实现取到 None 也直接返回），仍取不到时用世界兜底——`role_name=hero` 优先，其次「世界中唯一车辆」。
- `_get_ego_state()` 取不到自车或读取失败时返回**空字典**，不再返回零值 → `_persist_progress()` 不会把假位姿写进实例记录，面板显示「—」而不是误导性的 0.000。
- 失败按 30 s 窗口限频打 WARNING（`Ego state unavailable (stage=actor_missing|resolve|read): ...`，含被抑制条数），50 Hz 下不形成日志风暴，也不再吞掉根因。

### 向后兼容

- 平台数据契约不变：`ego_state` 仍为可选字段，取不到时为 `{}`（此前为零值字典）；`sim_status` / `sim_telemetry` 读取缺失键按 0 处理，行为不变。
- `ADAPTER_STREAM_MAX_WIDTH` 语义收窄为「rgb 数据流最大宽度」；如需限制主视区带宽请调 `configs/sensor_configs.json` 的 `overview_camera` 分辨率。
- 参与者蓝图回退只在原蓝图不可用时触发，已安装蓝图的场景行为不变；`_spawn_participant()` 返回值由 `None` 改为 `bool`（内部方法，无外部调用方）。
- 行人 AI 正常可用时不进入脚本化兜底，导航路径与 V3.4.2 一致（目的地距离由固定 8 m 扩到 8~18 m，更易持续可见）。

### 验证

- `pytest -q` 全量：**1305 passed**，无失败（更新 `tests/test_builtin_scenario.py`、`tests/test_scene_runner_service.py`、`tests/test_builtin_scenario_sensors.py`、`tests/mocks/carla_mocks.py`）。
- `mypy src/hunter_sim`：无问题；`flake8 src tests`：无告警。
- 真机 CARLA 端到端验证待执行（当前开发环境 `.venv` 未安装 `carla` 模块）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.4.3`；
2. 重启 `huntersim up`；
3. 预期日志出现 `Overview camera attached: mount=overview_camera fov=100 size=1920x1080 (native encode, jpeg>=90, no downscale)`；
4. 打开 `/viz`：主视区为原生分辨率画面（不再模糊），「深度图预览」默认已勾选；
5. 下发 `SCN-2141-CCB-STD-L1-v10.7`（儿童追球）等含行人的场景，预期日志出现 `Participant '...' spawned: ... distance_to_ego=...m`、`Walker '...' crossing at ... m/s: from=(...) to=(...)`；主视区可见儿童参与者与过街行为；
6. 观察「自车状态」面板：位置 x/y/z、航向、速度、转角、加速度随车辆运动变化；若仍为空，日志会给出 `Ego state unavailable (stage=...)` 的明确原因（`actor_missing` = 世界内找不到自车；`read` = CARLA 读取异常）。

### 分发

镜像 tag `hunter-sim:3.4.3`；`pyproject.toml` 版本 `3.4.3`；wheel / 容器版本号一致。

### 待办 / 已知边界

- 真机需重点确认：1920x1080@10fps + JPEG 90 的 MJPEG 带宽（约 2~4 MB/s 每客户端）在平台网络下的表现；带宽吃紧时可把 `overview_camera` 降到 1280x720（仍是原生编码，不再降采样）。
- 「儿童追球」目前映射为行人横穿（`child_chase_ball` → `PEDESTRIAN_CROSS`），球道具与追球跑位尚未建模，需要平台下发道具 Actor 后再补。
- 脚本化行人兜底用 `set_transform` 直行，不含步态动画；AI 控制器可用时走原生导航。
- 行人横穿目的地为「横穿自车车道 8~18 m」，复杂路口仍建议平台提供路径点。       


## V3.4.4 — 自车状态取数修复 + 主视区流畅度 + 参与者映射可诊断 + 场景收尾防原生崩溃

**发布日期**：2026-11-09
**版本定位**：V3.4.3 实机（`D:\python\HunterSim`）二次验收的 **patch** 修复。现场四项问题：下发场景仿真过程中主视区卡顿；「鬼探头」场景看不到行人横穿；自车状态整场「—」；场景结束后日志报错并以 `code=3221226505`（0xC0000409 fail-fast）退出。

### 问题背景（现场日志证据）

1. **自车状态取数踩到不存在的 API**：每帧打印 `Ego state unavailable (stage=read): AttributeError: 'Vehicle' object has no attribute 'get_rotation' (+1211 suppressed)`。CARLA `Actor` 位姿只能由 `get_transform()` 读出（`.location` / `.rotation`）——V3.4.3 的 `get_transform` → `get_rotation` 混合写法使整个读取块每帧抛异常，面板全「—」。
2. **卡顿来自编码侧 CPU 打满**：`overview` 固定 1920x1080 + JPEG 90，且编码前先 `bgra_to_rgb`（numpy 花式拷贝 6.2 MB）再 `Image.fromarray`（再拷 6.2 MB）。传感器实测帧率掉到 rgb 8.7 Hz / depth 6.8 Hz（配置 10 Hz），说明主视区编码已吃满 CPU。
3. **参与者不可见属「平台没下发」+「字段不匹配」双重可能**：现场 `Builtin scenario ... ready: ego=877, actors=1/1` 表明本次 `scene_config` 中只有 1 个 `constant_speed` 车辆，没有行人；而映射器只读 `actors` 一个键，平台若把行人放在 `traffic_participants`/`pedestrians` 等别名键下会被静默丢掉，且日志不留任何线索。
4. **场景结束后的原生崩溃**：`Phase RUNNING → COMPLETED` 之后没有任何后续 INFO，进程直接以 0xC0000409 退出。CARLA 的 fail-fast 不会给 Python traceback，只能靠「最后一条日志」定位到具体销毁阶段。

### 修复内容

**一、自车状态真实取数（`scene_runner/scene_runner_service.py`、`engine/hunter_se_vehicle.py`）**

- `SceneRunnerService._get_ego_state()`：`vehicle.get_rotation()` → `pose = vehicle.get_transform()`，`yaw` 取 `pose.rotation.yaw` 并转弧度。
- `engine/hunter_se_vehicle.py`：蓝图协议里的 `get_rotation` 改为 `get_transform`；新增模块级 `_read_pose()`（优先 `get_transform`，回退 `get_location` + `get_rotation`），两处 `get_state()` 统一走它，避免同类误用在 VIL 控制器里复现。
- 单测桩同步为真机形态：`tests/test_scene_runner_service.py::_vehicle()` 与 `tests/test_hunter_se_vehicle.py::_StubActor` 不再提供 `get_rotation`，只提供 `get_transform()`——桩比真机「更宽容」正是该 bug 逃过 V3.4.3 单测的原因。

**二、主视区流畅度（`sensor_sim/image_codec.py`、`scene_runner/builtin_scenario.py`）**

- 新增 `encode_bgra_jpeg()`：用 `Image.frombuffer("RGBA", ..., "raw", "BGRA")` 零拷贝读 CARLA 原始缓冲区，直接 `convert("RGB")` 编码 JPEG，省掉 numpy 侧两次整帧拷贝（1080p 每帧约 12 MB 的内存带宽热点）；`encode_rgb_frame()` 在「不降采样」时优先走该快路径，异常自动回落原有慢路径。
- 环视 JPEG 质量 90 → 85（`_OVERVIEW_JPEG_QUALITY`）：分辨率不变，单帧体积与编码耗时降约 20%~30%。
- 环视编码线程改为「追最新帧」：编码前先清空队列里积压的旧帧，慢编码后不会把延迟越积越大。
- 新增慢编码诊断：单帧耗时超 120 ms 时限频（60 s 窗口）WARNING `Overview encode slow: ... ms/frame (> ... ms budget), main view may stutter`，把「卡顿」变成日志里可量化的证据。
- 顺带清理 Pillow 13 移除的 `mode=` 参数（`Image.fromarray(rgb)` / `Image.fromarray(gray)`），避免升级 Pillow 后编码直接失败。

**三、参与者映射可诊断且兼容别名键（`platform_adapter/scene_mapper.py`）**

- 参与者容器改为合并读取：`actors` / `traffic_participants` / `participants` / `pedestrians` / `road_users` / `figures`；支持 `{"items": [...]}` / `{"list": [...]}` 等嵌套包装；跨键完全相同的条目去重，避免同时下发两个键时重复生成。
- 新增映射汇总日志：`Scene participants mapped: count=... sources=[actors=1] types={...} behaviors={...} detail=[...]`；count=0 时升为 WARNING 并打印 `scene_config keys=[...]` 与已检索的键，一次日志即可判定是「平台没下发行人」还是「字段名不匹配」；无任何 walker 时额外提示核对 `type` 字段。
- 行人行为兜底：行为是横穿类（`pedestrian_crossing`/`sudden_cross`/`child_chase_ball`/`occluded_dash`）但平台未声明 `type` 时按 `walker` 映射，不再生成「横穿马路的特斯拉」。

**四、场景收尾防原生崩溃（`scene_runner/scene_runner_service.py`、`scene_runner/builtin_scenario.py`）**

- `_run_loop` 收尾四步各落一条 INFO：`publishing terminal status` → `restoring async world mode` → `cleaning up scenario resources` → `cleanup finished, thread exiting`。
- `BuiltinScenario.cleanup()` 分阶段日志（含待销毁数量）：`cleanup begin: sensors=.., overview=.., participants=.., ego=..` → `stopping encoder threads` → `stopping participant controllers` → `destroying sensors` → `destroying actors` → `cleanup done`。崩溃后由最后一条日志即可定位到具体阶段（编码线程 / 控制器 / 传感器 / Actor）。
- 编码线程 join 超时不再静默：`is_alive()` 为真时 WARNING（`did not stop within 1.0s`），说明有线程卡在编码/写盘。
- 时序确认：执行器的 `_await_completion()` 以 `runner.is_running`（tick 线程存活）为退出条件，清理完成前不会 `plane.release()`，因此不存在「清理中世界被拆掉」的竞态。

### 向后兼容

- 平台契约不变：`ego_state` 字段、`sim_status` / `sim_telemetry` 结构均未改动，仅修复读取来源。
- 参与者读取新增别名键与嵌套包装，原有 `actors` 载荷行为完全一致；重复条目去重只在「跨键完全相同」时触发。
- 行为映射表未增删；行人兜底仅在 `type` 缺失且行为为横穿类时生效。
- 新增日志均为 INFO/WARNING，不改变任何返回结构；环视质量 90 → 85 只影响画质参数，可用 `_OVERVIEW_JPEG_QUALITY` 调回。

### 验证

- `pytest -q` 全量：**1316 passed**（新增 `tests/test_image_codec.py` 6 项；`tests/test_platform_adapter_mapper.py` 新增 `TestActorContainers` 6 项；更新 `tests/test_scene_runner_service.py`、`tests/test_hunter_se_vehicle.py`）。
- `mypy src/hunter_sim`：**90 source files, Success: no issues found**；`flake8 src tests`：无告警。
- 真机 CARLA 端到端验证待执行（开发环境 `.venv` 未安装 `carla` 模块）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.4.4`；
2. 重启 `huntersim up`；
3. 下发任一 SIL 场景，预期「自车状态」面板的位置/航向/速度/转角/加速度随车变化，且日志不再出现 `stage=read` 的 `get_rotation` 报错；
4. 主视区观察流畅度：日志出现 `Overview encode slow` 说明编码仍超预算（此时把 `configs/sensor_configs.json` 的 `overview_camera` 降到 1280x720）；无该告警且画面连贯即优化生效；
5. 下发「鬼探头」场景，先看一条 `Scene participants mapped: ...`：
   - `count=0` → 平台未下发行人/行人，请核对场景模板；
   - `count>=1` 但 `types` 里没有 `walker` → 平台 `type` 字段写错（已按行为兜底为 walker，日志会提示）；
   - 有 `walker` → 主视区应可见行人横穿，日志另有 `Walker '...' crossing at ...` 或脚本化兜底说明；
6. 场景结束（`COMPLETED`）后按 `Scenario cleanup: ...` 逐阶段日志确认走到 `Scenario cleanup done` / `Scene loop: cleanup finished, thread exiting`；若进程仍异常退出，最后一条 `Scenario cleanup:` 即为崩溃阶段。

### 分发

镜像 tag `hunter-sim:3.4.4`；`pyproject.toml` 版本 `3.4.4`；wheel / 容器版本号一致。

### 待办 / 已知边界

- 若现场 `Scene participants mapped: count=0` 反复出现，需要平台侧补齐 `actors`（或约定别名键）并包含 `type: pedestrian` + `behavior_type: pedestrian_crossing`。
- 0xC0000409 属 CARLA 原生 fail-fast，本次只做到「分阶段定位 + 线程守卫」，若日志停在 `destroying actors` 仍需在 CARLA 侧排查（如 `destroy()` 与传感器回调竞争、跨实例 client 复用等既有已知问题）。
- 主视区仍为 1920x1080 原生编码；单客户端 MJPEG 带宽约 2~3 MB/s，多客户端同时观看时受机器磁盘/CPU 限制，必要时降低 `overview_camera` 分辨率。
- 「儿童追球」仍映射为行人横穿，球道具与追球跑位需平台下发道具 Actor 后再补。


## V3.5.0 — HunterCore V1.26.0 场景语义对齐（actor 三分类 / 真实触发 / 映射报告）

**发布日期**：2026-11-10
**版本定位**：对齐平台 HunterCore **V1.26.0** 场景语义（minor：新增 actor 类型、行为、触发类型与映射报告产物）。此前平台已用 `semantics.resolve_value` 把 `parameters` 中的变量名解析为真实数值，仿真端按真实数值执行；未知行为继续 **fail-loud**，未知/缺失触发条件**显式禁用**，不再回落 `TIME`。

### 修复的问题

1. **静态物体被当成行人**：平台 `type=other` 的锥桶/护栏/散落物一律映射为 walker，画面里出现「站立行人」。
2. **自行车被当成行人**：`vehicle_kind=bicycle` 未识别，cyclist 场景实际生成行人。
3. **速度语义丢失**：平台下发的 `crossing_speed` / `target_speed` / `cyclist_speed` 等别名读不到，统一回落 5 m/s；行人又被夹到 1.4 m/s，快跑/鬼探头变成慢动作。
4. **触发条件是死代码**：`time_headway` / `time_to_collision` 被映射成 `EVENT` 但从未 `fire()`；`traveled_distance` 被当成「与自车距离」；缺失 `condition_type` 一律回落 `TIME=0`，导致 `t=0` 即触发。
5. **平台事件没有每 tick 消费者**：`EventDetector.check_tick` 全仓无调用，`cut_in` / `pedestrian_crossing` / `obstacle_appearance` 等事件永远进不了 `events.jsonl`。
6. **无法离线定位映射问题**：平台升级后只能等真机跑场景，缺少参与者/事件/告警的结构化报告。

### 新增与变更

**一、数据模型（`scene_runner/scene_config.py`）**

- `TriggerType` 新增 `ACCELERATION` / `OFFROAD` / `TRAVELED_DISTANCE` / `TIME_HEADWAY` / `TIME_TO_COLLISION` / `DISABLED`。
- `SceneEventTrigger` 新增 `comparator`（`>` `>=` `<` `<=` `==` `!=`）、`relation`（longitudinal/lateral/euclidean/sightline）、`source_condition_type`、`is_approximate`。
- `TrafficParticipantConfig.actor_type` 扩展为 `vehicle/walker/cyclist/prop`，并新增 `source_actor_type` / `source_behavior_type` / `object_kind` / `vehicle_kind` / `animal_kind` / `blueprint_candidates` / `behavior_params` / `target_distance_m`。
- `SpawnPoint` 新增 `lane_id`（原值透传，`"lane-1"` 等占位值不参与选道）与 `lane_index`（仅可转整数时填充）；`EgoVehicleConfig` 新增 `initial_steer`。
- `SceneEventType.TRIGGER_REACHED`：平台触发器命中标记，不参与碰撞/失败统计。
- `SceneEventDefinition` 新增 `action_type` / `action_parameters` / `is_runtime_evaluable`；`SceneConfig` 新增 `mapping_warnings`。

**二、平台映射（`platform_adapter/scene_mapper.py`）**

- **actor 三分类**：`vehicle_kind=bicycle` → `cyclist`；`object_kind` 非空 → `prop`；`animal_kind` → 资产降级为 walker 并告警；`type=other` 且无任何 kind → 保持 walker 兼容并打 `actor_type_legacy_other`。
- **行为对齐**：`cross_road` 修正为 `PEDESTRIAN_CROSS`；`borrow_oncoming` / `door_open` / `boarding` 可执行但标记 `behavior_approx`；未知 `behavior_type` 仍抛 `SceneMappingError(unsupported_behavior:*)`。
- **速度**：支持 `speed/speed_ms/target_speed/target_speed_ms/velocity/crossing_speed/cut_in_speed/cyclist_speed/animal_speed`；按 actor 类型分档；行人夹取到 0.3–4.0 m/s 并告警；参数值仍是变量名时记 `unresolved_param` 后使用默认值。
- **prop 蓝图链**：`object_kind` → `static.prop.*` 候选链（锥桶/护栏/集装箱/轮胎/路牌/垃圾桶等）；停放/事故/故障车走 `vehicle.*`；`ball` / `bus_stop_shelter` / `building_corner` / `gravel` / `liquid` 标记 `prop_asset_degraded`；未知 kind 打 `object_kind_unknown` 后按锥桶近似。
- **触发条件**：未知/缺失 `condition_type` → `DISABLED`（`condition_unknown` / `condition_missing`），**绝不回落 TIME**；`reach_position` 近似为 `TRAVELED_DISTANCE`，`offroad` 标注近似；保留平台 `rule` 比较符与 `relative_distance_type`。
- **映射报告**：`extra_params["mapping_report"]` 汇总 actors（declared/mapped/by_source_type/by_actor_type/by_behavior）、events（total/runtime_evaluable/disabled/by_condition）、warnings 与 `warning_counts`。

**三、执行端（`scene_runner/builtin_scenario.py`、`scene_runner/scene_converter.py`）**

- 新增 prop / cyclist 生成分支：`static.prop` 不进入车辆控制；车类 prop 显式驻车（含手刹）；`door_open` 探测 `open_door()`，缺失时日志 `door_api_unavailable` 并降级为路边停车；`boarding` 走向最近停放车辆（>15 m 或不存在时退化为向自车走 2.5 m 并告警）。
- 行人速度按 0.3–4.0 m/s 执行，不再截断到 1.4；`WalkerControllerWrapper` 同步放宽上限。
- 自车 `initial_steer` 在手动模式应用；autopilot 模式下忽略并告警。
- 每 tick 评估平台事件：积分已行驶里程、读取加速度/越界/相对距离/THW/TTC，命中后发出 `TRIGGER_REACHED`（边沿闩锁，只发一次）。
- `SceneConfigConverter`：`prop → static_prop`、`cyclist → cyclist`；autopilot 仅对 vehicle/cyclist 且非 `STATIC` 开启。

**四、产物与 CLI（`platform_adapter/executor.py`、`cli/main.py`）**

- 实例产物新增 `mapping.json`（映射报告），`run.log` 汇总 actors/events/warnings。
- 新增 `huntersim scene-check --input <json> [--json-out] [--strict]`：离线执行平台 → 内部映射，输出参与者/事件/告警统计；`--strict` 下映射失败或存在 `unresolved_param` / `condition_unknown` 返回非零退出码，可直接用于平台升级门禁。

### 向后兼容与行为变化（需公告）

- **事件流会变化**：V3.4.4 及以前平台事件基本不会进入 `events.jsonl`；V3.5.0 起会按真实条件触发并输出 `TRIGGER_REACHED`，同场景评估基线会变化。`TRIGGER_REACHED` 不计入碰撞/失败统计。
- 旧载荷（无 `condition_type`）仍可加载，但对应事件为 `DISABLED`，不参与运行时评估；碰撞事件仍由碰撞传感器驱动，不受影响。
- `type=other` 且无 kind 保持 walker 兼容（仅新增告警）；原有 `actors` 载荷与别名键读取逻辑不变。
- `lane_id` 仅透传，不改变出生点选择；`initial_steer` 仅在非 autopilot 模式生效。

### 验证

- `pytest -q` 全量：**1399 passed**（新增 `tests/test_scene_fixtures.py` 24 项；场景语义相关单测覆盖 mapper/scene_config/event_detector/trigger_conditions/scene_converter/builtin_scenario/actor_behaviors/walker_controller）。
- `mypy src/hunter_sim`：**90 source files, Success: no issues found**；`flake8 src tests`：无告警。
- 新增 fixture：`tests/fixtures/scenes/semantics_sample_v1_26.json`、`legacy_v1_25_sample.json`、`unknown_behavior.json`、`unknown_condition.json`、`missing_condition.json`、`prop_kinds.json`。
- 离线 CLI 验证：`huntersim scene-check --input tests/fixtures/scenes/semantics_sample_v1_26.json --json-out build/mapping_report.json` → `scenes=6 ok=6 actors=11 events=9`。
- 真机 CARLA 0.9.16 端到端验收待执行（开发环境 `.venv` 未安装 `carla` 模块）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.5.0`；
2. 升级前先用 `huntersim scene-check --input <平台样本> --strict` 核对映射报告，确认 `condition_unknown` / `unresolved_param` 为 0（旧载荷的 `condition_missing` 属预期）；
3. 重启 `huntersim up`，下发场景后检查日志：`actors=N/N`、`Scene participants mapped: ...`、无未知 `object_kind` 的 `spawn skipped`；
4. 检查实例产物 `mapping.json` 的 `warning_counts` 与 `run.log` 汇总；
5. 复核评估：`events.jsonl` 中的 `TRIGGER_REACHED` 不应计入碰撞/失败。

### 分发

镜像 tag `hunter-sim:3.5.0`；`pyproject.toml` 版本 `3.5.0`；wheel / 容器版本号一致。

### 待办 / 已知边界

- `reach_position` 仅按 `traveled_distance` 近似（平台只下发标量距离），复杂路口仍建议平台提供路径点。
- `offroad` 使用 waypoint 车道类型判断；无 `get_waypoint` 接口或地图数据缺失时按「未越界」容错。
- 动物资产在 CARLA 0.9.16 缺失，仍降级为行人外观并报 `animal_asset_missing`。
- 车类 prop 的开门动画依赖 CARLA `open_door()`；无该 API 时只做路边停车降级。
- 事件 `action`（如 `emergency_stop`）当前只随 `TRIGGER_REACHED.data` 留存，不在仿真端执行。

---

## V3.5.1 — 实例装配报 `load_map(Town06) time-out of 10000ms`：地图加载沿用普通 RPC 超时

**发布日期**：2026-11-10
**版本定位**：patch 修复。平台下发 3-actor 行人横穿场景（SCN-8990-CP-NRN-14）后，实例 11s 内直接 `failed`：`execution_unavailable:Failed to assemble scene runtime on engine '127.0.0.1:2000' for map 'Town06': [carla_engine][load_map(Town06)] time-out of 10000ms while waiting for the simulator`；`sensors_r1.mcap` 为 `frames=0`（331B 空包）。

### 根因

1. **超时口径错配**：`MapManager.load_map` 直接走 `client.load_world()`，而 CARLA 客户端的 RPC 超时只有一个（`CARLA_TIMEOUT_SECONDS`，默认 **10s**，V3.3.3 起为每次装配新建连接并 `set_timeout`）。`load_world` 不是普通 RPC：首次加载/切换地图需服务端重建世界、加载建筑与导航数据，Town06 这类大地图冷启动普遍 **20~60s**，于是服务端还在加载、客户端已抛 `time-out of 10000ms while waiting for the simulator`。时间线亦吻合：`10:15:20` 开始加载 → `10:15:31` 失败（≈ 10s 超时 + 开销）。
2. **排障信息不足**：错误只透传 LibCarla 原文，看不出是「加载慢」还是「CARLA 卡在同步模式等 tick」，也看不到真实加载耗时（成功分支才有 `loaded in ...ms`）。

### 修复

- **`common/models.py`**：`CarlaSettings` 新增 `map_load_timeout_seconds`（`CARLA_MAP_LOAD_TIMEOUT_SECONDS`，默认 **180s**，上限 1800s），与常规 `timeout_seconds` 分离。
- **`engine/map_manager.py`**：新增 `_map_load_timeout()` 上下文管理器——仅 `load_world` / `generate_opendrive_world` 期间把客户端超时临时放宽到 `map_load_timeout_seconds`，**无论成功或异常都恢复**为常规超时（生成、推进、取快照等仍受 10s 约束，避免真卡死时长时间阻塞）。配置写反（加载超时 < 常规超时）时按常规超时执行，不缩减。客户端无 `set_timeout`（Mock/受限实现）时退化为原行为。
- **`platform_adapter/app.py`**：装配器把两个超时同时注入 `MapManager`。
- **错误信息**：超时失败追加处置提示（调大 `CARLA_MAP_LOAD_TIMEOUT_SECONDS` / 检查 CARLA 是否停在同步模式等 tick），非超时错误保持原样；成功日志新增 `rpc timeout raised to ...s`，便于判断真实加载耗时。
- **文档/模板**：`.env.example`、`user_manual.md` FAQ 补充该变量与排障口径。

### 向后兼容与行为变化

- 新增变量有默认值，`.env` 不填照样生效（默认 180s）；旧配置文件零改动。
- 上限从「所有 RPC 10s」变为「地图加载 180s + 其余 10s」：仅当地图确实加载超过 10s 时行为变化（从失败变为等待）。若 CARLA server 真死，实例会在 180s 后失败（仍受 `ADAPTER_WATCHDOG_*` 看门狗兜底）。

### 验证

- `pytest -q` 全量：**1404 passed**（新增 `tests/test_map_manager.py::TestMapLoadTimeout` 5 项：加载期间放宽并恢复、失败路径恢复 + 提示文案、非超时错误无提示、无 `set_timeout` 的客户端仍可加载、配置写反不缩超时）。`tests/mocks/carla_mocks.py` 的 `MockCarlaClient` 补齐 `set_timeout`/`timeout`/`timeout_seen_during_load`。
- `mypy src/hunter_sim`：**90 source files, Success: no issues found**；`flake8 src tests`：无告警。
- 真机 CARLA 0.9.16 复测待执行（开发环境 `.venv` 未安装 `carla` 模块，无法本地拉起引擎）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.5.1`；
2. 重启 `huntersim up`（配置无需修改，默认即生效）；
3. 重新下发该场景，日志应出现 `Map 'Town06' loaded in <ms> (rpc timeout raised to 180s)`，其后正常进入 `Scene participants mapped` 与传感器推流；
4. 若仍报超时：先看 `<ms>` 与 CARLA server 控制台；若 `load_world` 在 180s 内无任何进度输出，按「CARLA 停在同步模式等待 tick」处理——重启 CARLA server，必要时调 `CARLA_MAP_LOAD_TIMEOUT_SECONDS`。

### 分发

镜像 tag `hunter-sim:3.5.1`；`pyproject.toml` 版本 `3.5.1`；wheel / 容器版本号一致。

---

## V3.5.2 — 跟车场景主车前看不到目标车：参与者出生点未按自车航向旋转

**发布日期**：2026-11-10
**版本定位**：patch 修复（含配套可视化补强）。平台下发跟车场景 `SCN-0176-FOL_LVV-L1-LVV-1a1e`（参与者 `lead-1`，`spawn_semantics=relative_to_ego` x=18.47 m / y=0 m，行为 `lane_follow`）后：

1. 测试主车（自车）**前方看不到任何车辆**，但日志 `actors=1/1`、`Participant 'lead-1' spawned: spawn=(18.5, 0.0, 0.0, yaw=0) distance_to_ego=18.5m` 一切「正常」；
2. `/viz` 只能看到主视区画面，**无法确认场景声明了哪些参与者、各自期望行为、为什么画面里没有**；
3. 日志夹杂三类噪声/真实故障：`SimDataProducer 初始化失败，降级为 no-op：KafkaConfigurationError: Must set delivery_timeout_ms higher than linger_ms + request_timeout_ms`、`ERROR: Exception in ASGI application` + `httpcore.ReadError`（HTTP 500）、`Viz proxy connect failed`（502）。

### 根因

1. **相对偏移只做了平移、没做旋转（核心）**。`spawn_resolver.resolve_actor_spawn` 对 `relative_to_ego` 直接 `world = base + offset`，未按自车 `yaw` 把**车体系**（x 前向 / y 左向）转到世界系。本例自车占位被替换为 `(392.47, 212.90, 0.30, yaw=90)`，于是「正前方 18.47 m」被放到了**自车正右方 18.47 m**（`world≈(410.94, 212.90)`）；环视相机前向 FOV 100°，自然拍不到。日志三处自相矛盾正是证据：`ego ... yaw=90` + `spawn=(18.5, 0.0, 0.0, yaw=0)`（车体系原值直接当世界坐标输出）+ `distance_to_ego=18.5m`（距离对、方位错）。自车 `yaw=0` 时平移恰好等价，因此历史场景未暴露。
2. **Kafka producer 超时组合不合法**。kafka-python 校验 `delivery_timeout_ms >= linger_ms + request_timeout_ms`；三个 producer 只设了 `delivery_timeout_ms=5000/10000`，而 `request_timeout_ms` 未设时默认 30000 → 构造即 `KafkaConfigurationError`，`SimDataProducer` 降级为 **no-op**：整场 `sensor_data` / `telemetry` / 节点结果**静默不上报**，平台侧同样「看不到数据」。
3. **viz 反向代理未兜住读取期异常**。探针面 `:8080` 反代控制面 `:8090` 时只把 `ConnectError` / `TimeoutException` 做了映射；`httpcore.ReadError` / `RemoteProtocolError` 属 `httpx.HTTPError` 但不在捕获链内，冒泡成 ASGI 500 + traceback（日志被刷屏），流式中途断开还会连带 `Viz proxy connect failed` 502。
4. **可观测性缺口**：可视化只有「画面 / 传感器 / 事件 / 日志」，没有「参与者」维度；日志也不打印参与者相对自车的前向/侧向分解与视区判定，无法一眼定责「是平台偏移下错、还是仿真端放错、还是相机拍不到」。

### 修复

**一、出生点旋转（`scene_runner/spawn_resolver.py`、`scene_runner/builtin_scenario.py`）**

- `resolve_actor_spawn` 改为按 `base.yaw_deg` 旋转：`world = base + x*forward + y*left`（`forward=(cos,sin)`、`left=(-sin,cos)`），`yaw = base.yaw + offset.yaw`；新增 `relative_offset_from_ego(ego_transform, point) -> (ahead, left)` 反解工具（世界→车体系），并入 `__all__`。
- `builtin_scenario` 的出生基准改为**自车实时位姿**（`_participant_base()` 读 `get_transform()`，取不到才回落 `_ego_spawn` / 配置值），修掉「占位原点被替换后仍按原配置算偏移」的隐患；新增 `_actor_spawn_point` / `_estimate_participant_visible` / `_participant_base`，`_collect_parked_spawns` / `_participant_spawn_candidates` / `_distance_to_ego` 统一走实时基准。
- 新增 `participant_states()`：按 `traffic_participants` 逐个输出 `{id, actor_type, behavior, blueprint, spawned, speed_ms, x/y/z, yaw_deg, ahead_m, left_m, distance_to_ego_m, visible}`，供可视化只读消费；`custom_scenario` 补默认 `participant_states() -> []`。

**二、参与者可观测性（`scene_runner_service` → `platform_adapter` → `/viz`）**

- `scene_runner_service`：新增 `_ACTOR_STATE_REFRESH_TICKS = 10`（tick 50 Hz → 快照 5 Hz）与 `_actor_state_cache`，tick 线程写、HTTP 线程无锁读，`actor_states()` 返回副本；异常限频 WARNING 并保留旧快照，释放时清空。
- `executor.actors(instance_id)` / `service.actors(instance_id)`：只读降级（运行器已释放/未运行一律空列表，不 500）；`service` 侧新增 `ActorItems` 类型别名（类内 `list` 方法会遮蔽内置 `list`）。
- 新增 `GET /viz/api/instances/{id}/actors`（`include_in_schema=False`）：返回 `{instance_id, count, spawned, visible, items}`，`visible` 按环视相机几何粗估（后向 2.5 m 内、80 m 外、侧向超出 100° FOV 判为不可见）。
- `/viz` 新增「参与者」面板：`ID / 类型 / 期望行为 / 车速 / 相对自车（前·左）/ 距自车 / 环视主视区`，1 s 轮询，不可见行置灰（`stale`）；`count>0 && visible==0` 时面板顶部直接提示「检查平台下发的相对偏移（x 前向 / y 左向，米）」。

**三、日志口径（`builtin_scenario.py`）**

- 新增 `Participant '<id>' spawn resolve: semantics=... offset=(...) base=(...) world=(...)`：把「平台给的偏移 / 自车基准 / 最终世界位姿」三段一次打全，可直接判定错在哪一层。
- `Participant '<id>' spawned:` 追加 `ego=(x,y,yaw)`、`relative=(ahead=..m, left=..m)`；判为视区外时输出 WARNING（车后、超 80 m 或侧向偏出 FOV），点名核对平台偏移语义。
- `setup()` 末尾新增 `Scene expectations:`：列出声明的参与者清单与行为，并显式提示「事件 `action_type` 仅留存不执行，需由参与者行为体现」（避免把「前车减速/急刹」误判为仿真端漏做）。

**四、Kafka 上报（`cloud/sim_data_producer.py`、`telemetry_producer.py`、`node_result_producer.py`）**

- 三个 producer 统一补 `_LINGER_MS=5`、`_REQUEST_TIMEOUT_MS=10_000`、`_DELIVERY_TIMEOUT_MS=30_000`，显式传 `request_timeout_ms` / `delivery_timeout_ms`，满足 `delivery ≥ linger + request` 校验，不再降级 no-op。

**五、viz 反向代理容错（`probes/viz_proxy.py`）**

- 建连/发送：`TimeoutException → 504`，`httpx.HTTPError`（含 `httpcore.ReadError`、`RemoteProtocolError`）`→ 502`，其余兜底 `→ 502`，新增 `_proxy_error(status, detail)` 统一错误体。
- 流式与读体：`_stream()` 捕获异常后打 WARNING 正常结束（不再冒泡成 ASGI 500），新增 `_safe_aclose()` 保证释放；`aread()` 失败返回 502。

### 向后兼容与行为变化（需公告）

- **相对偏移语义修正**：`spawn_semantics=relative_to_ego` 且**自车 `yaw≠0`** 的场景，参与者出生位置会变（此前落在错误方位）；`yaw=0` 的场景结果与旧版一致。平台侧偏移定义（x 前向 / y 左向 / 米）不变。
- **平台会重新收到数据**：Kafka 从「配置非法 → no-op」变为真正上报，此前静默缺失的 `sensor_data` / `telemetry` / 节点结果将恢复，平台侧注意去重与基线。
- **HTTP 状态码**：viz 反代失败由 `500` 改为 `502`（上游不可达/协议中断）/`504`（超时），前端与告警按新码归类。
- **新增只读接口**：`GET /viz/api/instances/{id}/actors`，只增不改，不进 OpenAPI；页面资源无版本号，升级后需 **Ctrl+F5 强刷**。

### 验证

- `pytest -q` 全量：**1412 passed**（新增 `tests/test_spawn_resolver.py` 航向旋转/左向/往返/后方 5 项，`tests/test_builtin_scenario.py` 参与者相对方位与车后判定 2 项，`tests/test_viz_api.py` 参与者面板聚合 2 项）。
- `mypy src/hunter_sim`：**90 source files, Success: no issues found**；`flake8 src tests`：无告警。
- 真机 CARLA 0.9.16 端到端复测待执行（开发环境 `.venv` 未安装 `carla` 模块）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.5.2`；
2. 重启 `huntersim up`，重新下发该跟车场景，日志应出现 `Participant 'lead-1' spawn resolve: semantics=relative_to_ego offset=(18.47,0.00,0.00,yaw=0.00) base=(392.47,212.90,yaw=90.00) world=(392.47,231.37,yaw=90.00)`，其后 `relative=(ahead=18.5m, left=0.0m)` 且**无**「落在环视主视区外」WARNING；
3. 打开 <http://127.0.0.1:8090/viz>（Ctrl+F5），「参与者」面板应为 `声明 1 / 已生成 1 / 主视区 1`，行内 `相对自车 ≈ 前 18.5 m / 左 0.0 m`，环视画面中目标车位于自车正前方约 18 m 处；
4. 确认 `SimDataProducer` 不再打印 `KafkaConfigurationError`，`/health` 的 `cloud` 段为启用态；viz 日志不再出现 `Exception in ASGI application`。

### 分发

镜像 tag `hunter-sim:3.5.2`；`pyproject.toml` 版本 `3.5.2`；wheel / 容器版本号一致。

### 待办 / 已知边界

- 自车蓝图 `hunter_se` 未部署时仍回退 `tesla.model3`（日志 `ego blueprint ... 回退`），包围盒与相机挂载尺寸按回退蓝图计算，属 V3.4.x 既有行为；需要精确外廓时请在 `assets` 中提供 `hunter_se`。
- 平台事件 `action_type`（如 `emergency_stop`）仍**仅随 `TRIGGER_REACHED` 留存、不在仿真端执行**；「前车减速/急刹」要可见，需平台把动作映射为参与者行为（如 `lane_follow` 叠加减速度）。
- `visible` 为按环视相机位姿与 FOV 的几何粗估，未做遮挡判断；自车正前方被大车遮挡时可能「标记可见但实际被挡」。

---

## V3.5.3 — 跟车场景仍然跑不起来：V3.5.2 的旋转基准取自「未同步的实时位姿」

**发布日期**：2026-11-10
**版本定位**：patch 修复。V3.5.2 修正了「相对偏移未旋转」，但**旋转所绕的基准点仍不可信**，同一跟车场景在真机上依旧失败。实例 `inst-87ef52e5104444fb8bb4d6052f72adb5`，场景 `SCN-0176-FOL-LVV-L1-LVV-1a1e-lead`（ID `85bf2b73-be71-45a3-bf48-f9f4a057e5a8`），两次下发（13:47 / 13:50）现象完全一致。

### 现象与「平台场景 ↔ 仿真端实际场景」对账

| 项目 | 平台下发（场景编辑页） | 仿真端实际落地（日志 / `/viz`） | 判定 |
| --- | --- | --- | --- |
| 自车出生点 | 占位原点 `(0,0,0,yaw=0)`（平台不给自车坐标） | `ego_spawn_replaced ... after=(392.47,212.90,0.30,yaw=90.0) candidates=16` | ✅ 符合契约（仿真端按地图替换） |
| `lead-1` 偏移 | `relative_to_ego`，`x=18.47 m / y=0`（正前方）、行为 `lane_follow` | `spawn resolve: ... base=(0.0,0.0,0.0,yaw=0) world=(18.5,0.0,0.0,yaw=0)` | ❌ **基准取了占位原点**，前车被放到世界 `(18.5, 0)` |
| 相对方位 | 期望「自车前方 18.5 m」 | `spawned: ... ego=(0.0,0.0,yaw=0)`（自车已在 392/213）；面板实测 `距自车 435.4 m（后 241.6 / 左 362.2）`、视区外 | ❌ 相距数百米且**不在路面上** |
| 运动状态 | 自车 8.2 m/s（TM 期望）、前车 `lane_follow` | 自车实测 2.27 m/s、前车实测 0.9 m/s | ❌ TM 指令未生效 |
| 事件 | `time_headway < 2.0 s` | 场景事件 **0 条**，日志无任何归因 | ❌ 前车不在前方 → `lead_gap=None` |
| 日志 | — | 8 条 `Viz proxy stream interrupted: ReadError:` / `Viz proxy upstream error: ReadError:`（WARNING，消息体为空） | ⚠️ 非故障，长连接正常断开 |

### 根因

1. **旋转基准取自未同步的实时位姿（核心）**。`_participant_base()` 在 V3.5.2 改为优先读自车 `get_transform()`，但日志第 203 行的 `World switched to synchronous mode (50Hz)` 发生在场景 `setup()` **之前**：CARLA 同步模式下，`world.tick()` 之前新生成的 Actor `get_transform()` 仍返回世界原点 `(0,0,0)`。于是「按自车航向旋转」作用在**占位原点**上，`offset=(18.5,0)` 原封不动变成世界坐标 `(18.5,0)` —— 与自车真实出生点相距 `√((392.47-18.5)² + 212.90²) ≈ 435 m`，且该点不在任何车道上。
2. **方位「一会在前、一会在后」是上一条的必然结果**。前车被钉死在世界 `(18.5,0)`，自车则沿 `yaw=90` 的车道行驶，两者连线与自车航向的夹角每帧都在变，`ahead/left` 自然在正负之间漂移，跟车行为无从验证。
3. **Traffic Manager 指令在首 tick 前下发被静默丢弃**。`set_autopilot` / `set_desired_speed` 都在 `setup()` 里调用（首 tick 之前），TM 当时尚未掌握这些 Actor，故自车只以 2.27 m/s 爬行、路面外的前车 0.9 m/s 抖动。
4. **可观测性仍有缺口**：`lead_gap=None` 时不产生任何日志，「场景事件 0 条」无从归因；`Scene expectations` 把车体系偏移印成 `lead-1[.../@(18.5,0.0)]`，极易被误读成世界坐标。
5. `ReadError:` 空消息：`httpx.ReadError` 的 `str()` 为空串，`f"{type}: {exc}"` 只剩前缀；且把浏览器关页这类正常断连按 WARNING 逐条刷屏。

### 修复

**一、基准改为确定性出生点（`scene_runner/builtin_scenario.py`）**

- `_participant_base()`：**优先返回 `self._ego_spawn`**（原点替换/兜底后真正生效的出生位姿），其次才是实时位姿，最后回落配置值 —— 相对偏移从此绕「自车实际所在车道」旋转，且同一场景重复下发布局可复现。
- 新增 `_ego_live_pose()` / `_reference_pose()` 与模块级 `_pose_not_synced(pose, reference)`：以「读数为占位原点、而已知预期位置不是」为判据识别未同步读数；运行期相对方位走 `_reference_pose()`（实时位姿优先，未同步时回落出生点），保证面板既跟手又不骗人。
- `_actual_ego_spawn()`：实时位姿判为未同步时**保留候选出生点**并 WARNING `ego_transform_not_synced`，不再让 `(0,0,0)` 顶掉真实出生点。
- `participant_states()`：参与者位姿同样做未同步判定，未同步时回落到已登记的出生点（面板不再显示 `(0,0)` 假位置）。

**二、让车真正跑起来**

- 新增 `_reapply_post_spawn_controls_once()`，由 `tick()` 在**首个 tick 之后**补发一次：自车与所有 TM 托管参与者的 `set_autopilot` + `set_desired_speed`，并输出 `post_tick_controls_reapplied: ego=[autopilot speed=8.2m/s] tm_participants=1`（只执行一次）。`_enable_autopilot()` 顺带登记 `(actor, speed_ms)` 到 `_tm_managed`。
- 新增 `_snap_to_driving_lane()`：车辆类参与者解析后的坐标若**不在行车道上**（`get_waypoint(project_to_road=False, lane_type=Driving) is None`），投影到最近行车道中心并取车道航向，输出 `participant_spawn_snapped`；已在车道内的坐标一律不动，保留平台意图。避免「前车生成在路肩/隔离带 → TM 不接管 → 原地抖动」。

**三、可观测性与日志口径**

- 新增 `_warn_follow_condition_unmet()`：场景声明了 `time_headway` / `time_to_collision` 但自车前方找不到目标车时，30 s 限频 WARNING `follow_condition_unmet`，并附各参与者当前 `ahead/left` 布局（旧版此处完全静默）。
- `Scene expectations` 改为 `id[type/behavior speed=8.0m/s offset=(18.5,0.0) world=(392.5,231.4,yaw=90)]`：车体系偏移与叠加结果同时给出，一次对账即可定责。
- `probes/viz_proxy.py`：新增 `_describe_exc()`（异常 `str()` 为空时只输出类型名，不再留 `ReadError:` 空消息）；`httpx.ReadError` / `RemoteProtocolError` / `StreamClosed` 归类为**长连接正常断开** —— 流式路径降为 DEBUG 并每 60 s 汇总一条 INFO，建连路径保留 502 但改为 60 s 限频（`_rate_limited_warning`）；`TimeoutException → 504`、其余 `httpx.HTTPError → 502` 的映射不变。

### 向后兼容与行为变化（需公告）

- **`relative_to_ego` 场景的参与者落点会变**：凡「同步模式 + 自车 `yaw≠0`」的已下发场景，V3.5.2 生成的参与者位置是错的，本版起才会真正落在自车车体系偏移处；`absolute` 语义场景不受影响。
- **TM 参与者的运动状态变化**：首 tick 后补发指令，此前「爬行/静止」的自车与背景车将按场景速度行驶，平台侧的事件数量与耗时基线会变。
- **生成坐标可能被车道投影改写**：仅当解析结果不在行车道上时触发，并有 `participant_spawn_snapped` 日志可追溯；行人/骑行者/prop 不参与投影。
- **日志级别**：viz 反代断连由「每条一警」变为 DEBUG + 限频汇总，依赖该类 WARNING 做告警的规则需改盯 `Viz proxy 长连接断开汇总`。

### 验证

- `pytest -q` 全量：**1419 passed**（新增 `tests/test_builtin_scenario.py::TestSyncModeEgoBase` 5 项、`::TestDrivingLaneSnap` 2 项；含同步模式 Mock `_SyncMockWorld`：首 `tick()` 前 Actor 位姿保持世界原点）。
- 关键回归断言：`base=(392.47,212.90,yaw=90)`、`world=(392.5,231.4,yaw=90)`、`ahead_m≈18.5 / left_m≈0.0`（未同步与已同步两种情况均成立）。
- `mypy src`：**90 source files, Success: no issues found**；`flake8`（改动文件）：无告警。
- 真机 CARLA 0.9.16 端到端复测待执行（开发环境 `.venv` 未安装 `carla` 模块）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.5.3`；
2. 重启 `huntersim up`，重新下发该跟车场景，日志应依次出现：`ego_spawn_replaced ... after=(392.47,212.90,0.30,yaw=90.0)` → `Participant 'lead-1' spawn resolve: ... base=(392.5,212.9,0.3,yaw=90) world=(392.5,231.4,0.3,yaw=90)` → `Participant 'lead-1' spawned: ... ego=(392.5,212.9,yaw=90) relative=(ahead=18.5m, left=0.0m)`，且**无**「落在环视主视区外」WARNING；
3. 启动后第一个 tick 应出现 `post_tick_controls_reapplied: ego=[autopilot speed=8.2m/s] tm_participants=1`，随后自车与前车速度都应接近场景速度（不再是个位数 m/s）；
4. `/viz`（Ctrl+F5）「参与者」面板：`相对自车 ≈ 前 18.5 m / 左 0.0 m` 且随行驶**始终为正**（不再前后跳变），`环视主视区=可见`；跟车距离进入阈值后应产生 `time_headway` 事件；
5. 若仍出现 `follow_condition_unmet`，按其中打印的 `ahead/left` 直接核对平台偏移符号约定（x 前向为正）；若出现 `participant_spawn_snapped`，说明平台偏移把车放到了车道外，需修正场景；
6. viz 页面反复开关后，日志应只剩周期性的 `Viz proxy 长连接断开汇总`，不再出现空消息 `ReadError:`。

### 分发

镜像 tag `hunter-sim:3.5.3`；`pyproject.toml` 版本 `3.5.3`；wheel / 容器版本号一致。

### 待办 / 已知边界

- **VIL 模式不适用本补发逻辑**：自车由实车状态驱动时，`set_autopilot` 会被 VIL 同步覆盖；跟车类场景仍建议在 SIL/autopilot 模式下验证。
- 前车 `speed` 平台未下发时仍取默认 `8.0 m/s`（`scene_mapper._DEFAULT_SPEED`）；若平台期望「与前车同速」需显式下发，映射侧不告警。
- 补发只做一次（首 tick 后）：若 CARLA server 在运行中重启，Actor 引用与 TM 注册同时失效，属实例重建范畴，不在本版本处理。
- `/viz` 实例列表的「参与者」计数与参与者面板的「声明」口径尚未统一（前者含 prop/自车等），待下一版对齐。

---

## V3.5.4 — 场景跑完 `carla-manager` 被原生 fail-fast 带走，`up` 顺手停掉 probes：仿真端整体下线

**发布日期**：2026-11-10
**版本定位**：可靠性修复——`huntersim up` 具备崩溃自愈能力，子进程异常退出不再牵连其余服务，仿真端跑完一个场景后继续常驻等待下一个任务。

### 现场

单实例 SIL 场景（`inst-9cde30160c1d42029a064806b496d14e`，Town06，3 名骑车人参与者）在真机 CARLA 0.9.16 上完整跑完，随后整个仿真端消失：

| 时刻 | 事件 |
|------|------|
| 14:45:25 | `ego_spawn_replaced ... after=(-99.96,145.85,0.30,yaw=-179.9)`、`ego_transform_not_synced` WARNING、`Participant 'cyclist-1' spawn resolve: ... base=(-100.0,145.8,0.3,yaw=-180) world=(-143.7,144.7,...)`（V3.5.3 修复在真机已生效） |
| 14:45:26 | `post_tick_controls_reapplied: ego=[autopilot speed=13.7m/s] tm_participants=3`（首 tick 补发生效，自车不再停在个位数 m/s） |
| 14:48:53 | `running → completed` → `Scene loop exited: publishing terminal status` → `restoring async world mode` → `cleaning up scenario resources` → `Scenario cleanup begin/…` |
| 14:48:54 | `Scenario cleanup done`；**其后 `Scene loop: cleanup finished`、`Instance completed`、`Execution plane released` 三条全部缺失** |
| 14:48:59 | probes：`Viz proxy upstream disconnected: ReadError ... （管理面重启/连接被中断，已返回 502）`，同一秒 `/api/v1/instances/{id}`、`/viz/api/.../actors`、`/viz/api/status` 集体 502 |
| 收尾 | `[up] carla-manager 已退出（code=3221226505）` → `[up] 已停止 probes` → 回到 PowerShell 提示符 |

### 逐条对账

1. **日志里的「报错」（502）不是代理 bug，而是症状**：`httpx.ReadError` 是「已建连、连接被强行中断」（服务拒连会是 `ConnectError`），说明控制面进程当场消失，`/viz` 只能回 502；`http_ok` TCP 探测也失败 → 页面「控制面 离线」。
2. **`carla-manager` 不是「主动退出」**：退出码 `3221226505` = `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN / fastfail），与 V3.3.3 记录的 `Assertion failed: _episode != nullptr`（LibCarla `Simulator.h`）同类。**它在 C++ 侧 `abort()`，Python 的 `try/except`、看门狗、`finally` 都接不住**，因此不可能靠「场景结束后别退」这个开关阻止。
3. **一次原生崩溃被放大为整个仿真端下线**：旧 `_guard` 语义是「任一子进程退出 → 返回其退出码 → `finally` 回收其余子进程」。本例只有控制面崩了，probes 也被带走，平台侧看不到任何可轮询的仿真端节点（心跳中断）。
4. **本次不是 V3.3.3 那个变量**：本轮日志只有一个实例、也没第二次 `load_world`，仍然崩在「收尾/空闲」窗口，说明触发面不止跨实例复用连接。具体原生调用点无法从日志定位（三条收尾锚点全丢），本版本不臆测，而是「外层自愈 + 补齐锚点」。

### 修复内容

- **`src/hunter_sim/visualization/launcher.py`（核心）**：`_guard()` 重写为自愈守护循环。
  - 子进程**非零退出**时不再返回退出码，而是按退避（`_RESTART_BACKOFF_S = 3/5/10/30s`，末位重复）重新拉起同一命令行（`_relaunch()`），并等待就绪；其余子进程**保持运行不受牵连**；
  - 计数口径：`SERVICE_STABLE_SECONDS`（默认 30 s）内算「未稳定」，连续未稳定重启达 `_RESTART_MAX_ATTEMPTS`（默认 4）次则停止守护，并给出「先确认 CarlaUE4.exe / :2000 与端口占用再重跑 `huntersim up`」的明确指引；已稳定后再崩则重新计一次额度（避免长跑偶尔崩一次就耗尽额度）；
  - **不重启的情形**：`code == 0`（正常停止）、已收到 Ctrl+C（`self._stopping`）、`restart_on_failure=False`；
  - 重启成功提示：`[up] carla-manager 已重启并就绪（https://127.0.0.1:8090），继续等待平台下发场景`；
  - `Launcher(..., restart_on_failure: bool = True)` 新增开关，类/模块 docstring 同步补「崩溃自愈」要点。
- **`src/hunter_sim/cli/main.py`**：`up` 新增 `--no-restart`（保留旧行为）；启动计划后一行打印自愈声明，便于现场知道默认已变。
- **`src/hunter_sim/platform_adapter/executor.py`**：实例收尾后新增 INFO `Manager idle: waiting for next scene (instance=...)`——「没退、在等下一个场景」的凭证，也是下次定位崩溃点的尾锚。
- **`src/hunter_sim/execution/plane.py`**：`release()` 开头新增 INFO `Execution plane releasing: instance=..., runner=yes/no`，把「崩在释放前还是释放中」区分开（沿 V3.4.4「收尾每一步都落 INFO」的做法）。
- **`src/hunter_sim/probes/viz_proxy.py`**：502 提示补全为「管理面重启/连接被中断，已返回 502；huntersim up 会自动拉起控制面」，避免现场把 502 误读为代理自身故障。

### 向后兼容与行为变化

- **有意的默认行为变化**：`huntersim up` 不再因单个子进程异常退出而整体下线，而是自动拉起并继续守护；Ctrl+C 与正常停止路径不变（仍统一回收全部子进程）。
- 控制面重启后，上次遗留的非终态实例仍由既有 `mark_restarted_instances_failed` 标为 `failed`，平台需重新下发该场景（本轮实例已 `succeeded`，不受影响）；节点注册/心跳为启动即重建，重启窗口（退避 3 s）远小于 `lease_seconds=90`，平台侧不会判定节点掉线。
- **仅适用于 `up` 托管路径**：直接跑 `huntersim carla-manager`（systemd / Docker / K8s）时自愈不生效，重启仍由编排器（`restart: always` / `restartPolicy`）负责，本版本未改动其语义。
- 无接口/配置/平台契约变更；`pyproject.toml` 版本 `3.5.3` → `3.5.4`（修复/完善按第三位递增）。

### 风险

- 自动重启可能「隐藏」反复崩溃：已用退避 + 上限 + 计数日志三重限制，触顶后不静默退出而是给出排查指引；`--no-restart` 可回到旧行为。
- 重启期间（退避 3 s + 启动约 2 s）平台下发会碰到 502/拒连；属预期，重试即可（平台侧已有重连语义）。
- 新增两条 INFO 只增加日志量（每实例各一条），不影响行为。

### 验证

- `pytest -q` 全量：**1428 passed**（较 V3.5.3 +9），覆盖率门禁达标。
- 新增回归测试：
  - `tests/test_viz_launcher.py::TestGuardAutoRestart`（6 例）：原生崩溃码 `3221226505` 触发重启且**兄弟 probes 未被牵连 terminate**、重启后回到守护循环继续等下一个场景、`code=0` 不重启、`restart_on_failure=False` 保留旧行为、连续未稳定触顶后「停止守护」并提示 CarlaUE4、稳定后再崩重新计额度；
  - `tests/test_viz_launcher.py::TestUpCommand`（+2 例）：默认开启自愈、`--no-restart` 关闭并打印提示；`test_parser_defaults` 补 `args.no_restart` 默认值断言；
  - `tests/test_platform_adapter_executor.py::TestExecution.test_finished_scene_leaves_manager_idle`：实例终态后恰好一条 `Manager idle: waiting for next scene` 且带 instance id。
- `mypy src`：**90 source files, Success: no issues found**；`flake8 src tests`：无告警。
- 真机复测待执行（开发环境 `.venv` 未安装 `carla` 模块，原生 fail-fast 无法在本地复现）。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.5.4`；
2. `huntersim up`（启动计划后应多一行「子进程异常退出将自动重启，并继续等待平台下发仿真场景」）；
3. 下发场景并等其跑完：日志尾部必须出现 `Scenario cleanup done` → `Execution plane releasing: instance=..., runner=yes` → `Execution plane released` → `Instance completed ... passed=...` → **`Manager idle: waiting for next scene (instance=...)`**；若仍缺尾部行且进程消失，则说明崩在更早一步，按缺哪一行定位；
4. 如果仍发生原生崩溃，控制台必须只出现 `[up] carla-manager 异常退出（code=3221226505），3s 后自动重启（第 1/4 次），其余服务保持运行` → `[up] carla-manager 已重启并就绪（https://127.0.0.1:8090），继续等待平台下发场景`，**不得再出现 `[up] 已停止 probes`**；`/viz` 应短暂 502 后自动恢复；
5. 重启后向同一节点重新下发场景，应正常 `pending → loading → running`（控制面启动时会把上次遗留的非终态实例标 `failed`，属既有行为）；
6. 需要旧语义（子进程退出就退出 `up`，便于外部监控接管）时，用 `huntersim up --no-restart`。

### 分发

镜像 tag `hunter-sim:3.5.4`；`pyproject.toml` 版本 `3.5.4`；wheel / 容器版本号一致。文档四件套（`README.md`、`user_manual.md`、`deployment_and_operations.md`、`docs/单机部署指南.md`）已同步。

### 待办 / 已知边界

- **原生 fail-fast 的确切调用点仍未定位**：V3.3.3 消除了「跨实例复用 `carla.Client`」，本轮日志证明「单实例收尾/空闲」也会崩。下一步靠本版新增的尾锚（`Execution plane releasing` / `Manager idle`）二分：尾锚齐全 = 崩在释放后的空闲期（候选：`carla.Client`/`world` 引用被 GC）；缺尾锚 = 崩在评估/发布/录制收尾中。
- **自愈只在 `huntersim up` 层**：`carla-manager` 自身无法接住原生 `abort()`；非 `up` 托管部署需由 systemd/Docker/K8s 的 restart 策略承担（文档已写明）。
- **不做「崩溃后自动接着跑完上一个实例」**：重启后遗留实例标 `failed`、等平台重下发，是既有重启恢复语义；自动续跑属功能级变更（需递增次版本号）。
- 沿用 V3.5.3 待办（VIL 模式不适用首 tick 补发、前车默认 8.0 m/s、补发仅一次、`/viz` 参与者计数口径）与 V3.3.3 待办（自签 CA 未导入导致上行静默丢失、HUNTER_SE 蓝图缺失回落 model3）。

---

## V3.5.5 — 主视区「幻灯片化」专项：降级不可逆 + 推流名额滞留 + 深度预览抢编码核（按 i7-14700KF / RTX 4070 校准到 1080p@20fps）

**发布日期**：2026-11-10
**版本定位**：性能修复——消除三类会把可视化画面长期锁在 ~1.4 fps 的路径，并按仿真端机器配置（Intel i7-14700KF 3.40 GHz / 32 GB / NVIDIA RTX 4070 12 GB）把主视区帧率上限从 10 fps 提到 20 fps，同时保留高清分辨率。接口面、平台契约、录制与上行链路零改动。

### 现场

平台下发场景后 `/viz` 主视区严重卡顿（页面版本 v3.5.3·dev）：

| 读数 | 值 | 结论 |
|------|----|------|
| 画面模式提示 | `单帧轮询（MJPEG 不可用（相机未挂载或推流已达上限））` | 画面走 `pollScreenshot`，`POLL_MS.screenshot = 700 ms` → **上限 ~1.4 fps**，与分辨率/带宽无关 |
| overview 流 | 1707 帧 / 10.0 Hz / 1920×1080 / 单帧 218 KB | 相机已挂载且稳定出帧 → 排除「相机未挂载」，只剩**名额已满**或首帧 6 s 看门狗超时 |
| depth 流 | 1280×720 32FC1 / 9.6 Hz | 单帧原始 3.69 MB（3.52 MiB），且深度预览勾选后**每个请求**都要重算一次 |
| 参与者 | 3 名 cyclist 均在车后 138~151 m | 主视区看不到它们属场景偏移（V3.5.3 已处理），与卡顿无关 |

### 逐条对账

1. **降级是单向永久门**：`switchToScreenshot()` 把 `state.videoMode` 置为 `screenshot` 后，代码里**没有任何回到 MJPEG 的路径**，页面挂得越久越卡，只能人工刷新。
2. **推流名额会滞留**：路由先 `await anext(frames)` 拉首帧，异常路径不关闭生成器，名额释放只发生在 `finally`，而异步生成器回收要等事件循环空闲。反复刷新/代理掉线会把 `stream_max_clients=8` 吃满 → 之后 MJPEG 永远 503 → 前端永久降级（正好解释读数 1）。
3. **深度预览抢的是编码核**：`/sensors/depth` 旧实现对 921,600 个 float32 先 `nan_to_num` + 归一化 + ×255 + PNG，**再**跨步抽样——单次请求的运算量与输出尺寸无关。这些运算与环视编码挤在同一个核（JPEG 编码单线程 + GIL），一勾深度预览环视就跟不上下集。
4. **帧率天花板是 10 fps**：`overview_camera.fps=10` 与 `ADAPTER_STREAM_FPS=10` 取小者，即使 MJPEG 正常也只有 10 fps，肉眼看仍是不流畅。
5. **现场无法定性**：名额占用数、上限、当前帧率、当前画质档在页面上全都看不到，只能猜「相机未挂载还是名额满」。

### 机器配置校准（为什么选 1080p@20fps 而不是 720p@30fps）

| 项 | 依据与取值 |
|----|------------|
| CPU i7-14700KF（3.40 GHz，强单核） | JPEG 编码是**单线程 + GIL**，多核补不上，只能压单帧成本；1080p q85 单帧约 25~45 ms，20 fps 占一个核的 40~70%，仍留余量给 CARLA 主循环与监督线程 |
| RAM 32 GB | 环视独立队列 `maxsize=4` + drop-stale，20 fps 下积压上限 <1 s，增量 <10 MB，不构成约束 |
| RTX 4070 12 GB | 渲染侧产出 1080p 环视 + 数据传感器充足，瓶颈在 CPU 编码不在 GPU；故保持 `1920×1080` 不降分辨率 |
| 内网带宽 | 218 KB × 20 fps ≈ 4.4 MB/s/客户端，8 客户端 ≈ 35 MB/s，千兆（125 MB/s）可承受 |

结论：取 **1920×1080 @ 20 fps**（高清优先），用抽样先行 + 并发闸 + 自适应降质三重限制兜住跑不动的情形；弱机一个环境变量即可回退（见「行为变化」）。

### 修复内容

- **`src/hunter_sim/visualization/assets/app.js`（① 降级可逆）**：新增 `scheduleMjpegReprobe()` / `reprobeMjpeg()` / `reprobeDelayMs()` / `clearReprobe()` 与 `state.videoReprobe` / `state.mjpegFailures`。降级到单帧轮询后按 **10 → 20 → 40 → 80 s 封顶**的递增退避回探；探测用 `fetch` + `AbortController` **只读响应头**（判 `multipart` 可用性、拿到即断），不打断当前单帧画面、不长占推流名额；探到即 `startVideo()` 并提示「图像流已可用，切回 MJPEG」，首帧 `img.onload` 到手才复位退避。回探前复查 `videoMode === "screenshot"`、实例未进终态且仍在运行，`stopVideo()` 拆掉待触发定时器。
- **`src/hunter_sim/visualization/sensor_view.py`（② 深度预览降载）**：`depth_png` 拆为 `_depth_render`（校验 + 缓存/并发闸）与 `_encode_depth`（抽样 + 归一化 + 编码）。**抽样先行**：先按目标宽度跨步取稀再参与运算，1280×720 + `max_width=480`（step=3）下参与运算的元素从 921,600 降到 102,480（约 1/9）；**并发闸** `_DEPTH_COMPUTE_GATE` 非阻塞获取，编码不排队，已有在算则复用上一帧（无上一帧返回 None → 路由 503）；**上一帧缓存** `_DEPTH_CACHE` 按 `宽x高@目标宽` 分键，同一 `seq` 重复请求零重算（前端 1.5 s 轮询快于深度出帧）。
- **`src/hunter_sim/platform_adapter/service.py`（③ 名额租约）**：`_stream_slots`（token → 最近活跃时刻）+ `_stream_slot_seq` 取代裸计数；`stream_frames` 每次出帧 `_touch_stream_slot()`，`_acquire_stream_slot()` 申请前先 `_sweep_stale_stream_slots()` 回收闲置 > `_STREAM_SLOT_STALE_S`（30 s）的僵尸租约并打 WARNING `Stream slots reclaimed as stale`；`_release_stream_slot()` 用 `pop` 保证不重复减计数（Gauge 仍 `inc()/dec()`，保留多实例叠加语义）。新增 `_stream_view()`，`health_snapshot()` 与 `runtime_status()` 各输出 `stream.{clients,limit,fps}`。
- **`src/hunter_sim/platform_adapter/app.py`（③ 异常归还）**：`stream.mjpg` 拉首帧的 `try` 补 `except BaseException: await frames.aclose(); raise`——首帧等待期间客户端断开/被取消时，本次迭代已占用的名额必须立即归还，不再只靠事件循环空闲时的异步生成器回收兜底。
- **`src/hunter_sim/visualization/routes.py`（③ 可观测）**：`/viz/api/status` 默认 payload 补 `"stream": {"clients": 0, "limit": 0, "fps": 0.0}`，服务无 `runtime_status` 时面板显示「—」而不报错。
- **`src/hunter_sim/scene_runner/builtin_scenario.py`（④ 自适应降质）**：新增阶梯 `_OVERVIEW_QUALITY_LADDER = (85, 78, 72)`、`_OVERVIEW_SLOW_STREAK = 8`、`_OVERVIEW_FAST_STREAK = 150` 与 `_overview_quality()` / `_track_overview_encode_cost()` / `_adapt_overview_quality()` / `_warn_overview_floor()`。环视编码线程逐帧计时：连续 8 帧超预算（>120 ms）逐档降质、连续 150 帧宽裕升回；**逐档推进到生效值真的变化才落日志**（被 `screenshot_quality` 夹住的档位不报假变化），触底则限频提示改 `overview_camera` 分辨率/帧率（需重建传感器，不自动做）。编码使用当前档并把 `quality` 推进 FrameHub meta；挂载日志改为 `jpeg 85→78→72 adaptive, no downscale`。V3.4.4 的 `Overview encode slow` 告警保留。
- **配置（⑤ 帧率校准）**：`configs/sensor_configs.json` `overview_camera.fps` 10.0 → **20.0**（description 补「fps 与 `ADAPTER_STREAM_FPS` 需一致，否则取两者较小值」）；`platform_adapter/config.py` `stream_fps` 默认 10.0 → **20.0**。

### 向后兼容与行为变化

- **主视区默认帧率 10 → 20 fps**（`overview_camera.fps` 与 `ADAPTER_STREAM_FPS` 两处都要动，实际取小者）。弱机回退：`ADAPTER_STREAM_FPS=10` 且 `overview_camera.fps` 改回 `10.0`，其余修复与帧率无关可全部保留。
- **降级不再是终点**：落到单帧轮询后会自动回探 MJPEG（10/20/40/80 s），提示语补「· 稍后自动重试图像流」，成功切回时显示「图像流已可用，切回 MJPEG」。
- **推流名额租约化**：客户端异常断开留下的名额最长滞留 30 s，下一次申请时自动回收并告警；`StreamLimitExceeded` → 503 语义不变。
- **画质可自动变化**：持续超预算时 JPEG 质量 85 → 78 → 72（**不降分辨率**），长期宽裕升回；面板元信息新增 `q85`/`q78`，不静默。
- **新增可观测**：`/health`（探针代理）与 `runtime_status`、`/viz/api/status` 新增 `stream.{clients,limit,fps}`；`/viz` 模块信息新增「推流客户端 n / 8（20 fps）」行。
- 无接口/配置项增删（仅两处默认值调整）；`pyproject.toml` 版本 `3.5.4` → `3.5.5`（修复/完善按第三位递增）。

### 风险

- 20 fps 使编码线程负载翻倍：若同时开深度预览，弱机可能反而更卡——由抽样先行、并发闸让路、自适应降质三重限制；仍不够时按触底提示降分辨率/帧率。
- 回探会额外发起 MJPEG 请求：拿到响应头即 `abort()`，不消费流；服务端首帧异常路径立即归还名额，最坏滞留 30 s 后被 sweep 回收。
- 画质降到 78/72 档块效应可见，但只在持续超预算时发生且会自行升回，面板 `q` 值可读。
- 租约闲置判定基于 `time.time()`（与同模块 `_STREAM_IDLE_TIMEOUT_S` 同源）：系统时钟大幅回拨可能提前回收名额，后果仅为一次画面重连。
- 深度预览「忙时给上一帧」会让画面短暂滞后一帧（<1.5 s），换取不阻塞编码线程；刻意取舍，已在 docstring 写明。

### 验证

- `pytest -q` 全量：**1452 passed**（较 V3.5.4 +24），覆盖率门禁达标。
- 新增/改动测试：
  - **`tests/test_viz_sensor_view.py`（新文件，12 例）**：抽样必须发生在逐像素运算之前（1280×720 + `max_width=480` → `nan_to_num` 入参只能是 `240×427`）、PNG 头尺寸即抽样结果、`max_width=0` 不抽样、payload/meta 异常返回 None、同 `seq` 重复请求零重算、新 `seq` 必重算、并发让路拿上一帧 / 无缓存返回 None、缓存按尺寸分键、`render_stream` 走同一实现；
  - **`tests/test_adapter_stream.py::TestStreamSlotLease`（4 例）**：首帧拉取异常必须 `aclose()` 并归还名额（路由层，`raise_server_exceptions=False`）、僵尸租约在下一次申请前回收且**不重复减计数**、活跃连接每帧 touch 不被误回收、`health_snapshot`/`runtime_status` 的 `stream` 三字段随连接变化；既有 4 项名额契约（`active_streams`/Gauge 增减、`StreamLimitExceeded.limit`、503 `stream_limit_reached`）全部保持；
  - **`tests/test_builtin_scenario_sensors.py::TestOverviewQuality`（7 例）**：默认档仍 85（不为「优化」先降质）、单帧抖动不跳档、连续 8 帧超预算降 78 并落日志、`meta.quality` 随档位变化且分辨率不变、150 帧宽裕升回 85、被 `screenshot_quality=80` 夹住的档不报假变化（只报 `85→80`）、触底提示限频（60 s 内一条）；
  - **`tests/test_viz_api.py`**：新增 `test_mjpeg_fallback_is_not_terminal`（app.js 源码契约：回探调度、递增退避、探测只读响应头、成功复位、名额与 `q` 档可读），`/viz/api/status` 的 `stream` 字段与缺 `runtime_status` 时的零值降级；
  - **`tests/test_settings_env.py`**：`stream_fps` 默认值断言 10.0 → 20.0。
- `mypy src`：**90 source files, Success: no issues found**；`flake8 src tests`：无告警。
- 真机（CARLA 0.9.16 + 该台 14700KF/4070）复测待执行：开发环境 `.venv` 未安装 `carla` 模块，20 fps 编码耗时、回探实际节奏与画面流畅度需上机核对。

### 实机执行

1. `git pull` → `uv pip install -e .` → `huntersim version` 应为 `3.5.5`（页面左上角应显示 `v3.5.5`）；
2. `huntersim up` 后下发场景，日志应出现 `Overview camera attached: ... jpeg 85→78→72 adaptive, no downscale`；传感器面板 overview 行应为 **20.0 Hz** 量级；
3. `/viz`（Ctrl+F5）模块信息应出现「推流客户端 1 / 8（20 fps）」；画面提示应为「图像流（MJPEG 实时）」，元信息带 `q85`；
4. 反复刷新页面 5~6 次（模拟异常断开）后：名额读数应回落，最迟下一次申请时日志出现 `Stream slots reclaimed as stale: count=N, idle>30s`，画面不得永久停在「单帧轮询」；
5. 若确实落到单帧轮询，提示应带「· 稍后自动重试图像流」，并在 ≤80 s 内自动切回「图像流已可用，切回 MJPEG」（不必人工刷新）；
6. 勾选深度预览连续观察 2 min：环视不应再出现 `Overview encode slow`；若出现且随后有 `Overview encode adapted: jpeg quality 85→78`，说明自适应生效；若已有 `still slow at lowest jpeg quality` 提示，则按提示下调 `overview_camera` 分辨率/帧率（或用 `ADAPTER_STREAM_FPS=10` 回退）并重启。

### 分发

镜像 tag `hunter-sim:3.5.5`；`pyproject.toml` 版本 `3.5.5`；wheel / 容器版本号一致。文档四件套（`README.md`、`user_manual.md`、`deployment_and_operations.md`、`docs/单机部署指南.md`）已同步。

### 待办 / 已知边界

- **未启用 GPU/硬编码路径**：RTX 4070 的 NVENC 与 `imagecodecs` 类加速未接入（会引入 CUDA 依赖与容器差异）。本版本只压单帧成本与抢核路径；若 20 fps 仍不达标，硬编码是下一候选（属功能级变更，需递增次版本号）。
- **自适应只降 JPEG 质量，不自动降分辨率/帧率**：后者需重建传感器、会打断画面与录制链路，故只给日志提示，不自动做。
- 回探依赖代理不把首个 `multipart` 块整体缓冲；被缓冲时探测走异常分支继续退避，画面不受影响（仍是单帧轮询）。
- 主视区、深度预览与日志 SSE 共用浏览器同源连接上限（HTTP/1.1 每主机 6 条）；三者同时开启时仍可能排队，属客户端限制，未在本版本处理。
- 沿用 V3.5.4 待办（原生 fail-fast 调用点未定位、自愈只在 `up` 层）与 V3.5.3 待办（VIL 模式不适用首 tick 补发、前车默认 8.0 m/s）。

---
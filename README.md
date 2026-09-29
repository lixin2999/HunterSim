# HunterSim

基于 [CARLA](https://carla.org/) 的自动驾驶仿真**数据采集 + 算法分析 + 场景评估**一体化系统。

- 语言：Python 3.12
- 仿真引擎：CARLA 0.9.16（以独立进程运行，通过 `carla.Client` 连接）
- 架构：严格五层分层（L1 仿真核心 → L5 应用调度），上层依赖下层，禁止反向依赖

## 分层架构

| 层 | 包 | 职责 |
|----|----|------|
| L1 | `hunter_sim.simulation` | CARLA 连接 / 车辆控制 / 场景管理 / 地图系统 / 天气与环境 |
| L2 | `hunter_sim.acquisition` | 传感器管理 / 数据缓冲 / 写入器 |
| L3 | `hunter_sim.processing` | 时间同步 / 数据转换 / 算法对接 |
| L4 | `hunter_sim.evaluation` | 指标计算 / 报告生成 / 数据回放 |
| L5 | `hunter_sim.app` | CLI / 场景编排 / 任务调度 |
| 基础 | `hunter_sim.core` | 契约 / 事件 / 配置 / 日志 / 异常 |

## 开发状态

**V0.1.0–V0.6.0 已完成严格五层架构（L1–L5）端到端贯通 + VIL 虚实映射模块**，质量门禁全绿（`ruff` / `mypy --strict` / `pytest`，覆盖率 94%，275 项测试通过）：

- `core/contracts.py` — 不可变数据契约（`VehicleState` / `CameraFrame` / `LidarFrame` / `SynchronizedFrame` 等）
- `core/events.py` — 事件类型 + `core/event_bus.py` 线程安全的进程内事件总线
- `core/protocols.py` — 跨层 `Protocol`（`EventBus`）
- `core/config.py` — pydantic 配置模型 + YAML/JSON 加载器（含仿真步进模式 `simulation_mode`）
- `core/logging.py` — loguru 结构化日志与 `run_id`/`frame_id` 上下文
- `core/exceptions.py` — 统一异常体系
- `container.py` — 轻量依赖注入容器
- `simulation/` — CARLA 连接管理（指数退避重连）、主车控制（VIL 直接位姿 / SIL 控制指令双模式）、场景状态机与交通流生成、地图系统（内置地图注册表 / OpenDRIVE 自定义地图 / 坐标系元数据）、天气与环境（全参数 + 8 套预设环境）
- `acquisition/` — 传感器生命周期、环形缓冲（背压 + 跨线程投递）、异步原子写入器
- `processing/` — 原始测量→契约帧转换、最近邻时间同步、数组清洗去噪
- `evaluation/` — 轨迹/舒适/安全/覆盖指标、报告生成与图表渲染、数据回放
- `app/` — 采集调度器、运行编排器、组合根装配容器、惰性 `typer` CLI
- `app/vil/` — VIL 虚实映射模块（Kafka 遥测消费、坐标映射、状态同步、可视化、同步控制、VIL 引擎）

详见《发布说明》（`release.md`）与《用户手册》（`docs/user_manual.md`）。

## 仿真模式（同步 / 异步）

CARLA 世界步进模式在 `scenario.simulation_mode` 中声明，采集启动时由 `ScenarioManagerImpl` 自动写入
`carla.WorldSettings`：

- **`synchronous`（默认，VIL 实时）**：仿真由客户端 `world.tick()` 驱动，固定步长（`fixed_delta_seconds`，
  为 `null` 时由 `tick_rate` 推算），时序严格对齐且可复现——数据采集与 VIL 实车在环用它。
- **`asynchronous`（回放 / SIL）**：`fixed_delta_seconds=None` 可变步长，按真实时间自动推进——在线 SIL/交互调试用它。
- 物理子步（`substepping` / `max_substep_delta_time` / `max_substeps`）保证高步长下的仿真稳定性。

CARLA Server（0.9.16）的启动参数与模式选型详见《部署运维手册》§3.1（`docs/deployment_and_operations.md`）。

## 车辆控制模式（VIL / SIL）

L1 `VehicleController` 提供两种主车驱动方式（`ControlMode`），控制接口与 `simulation_mode` 步进模式配合使用：

- **VIL（实车在环，直接位姿控制）**：`set_transform` / `set_velocity` / `set_angular_velocity` 或一次性
  `set_kinematic_state(VehicleKinematicState)` 直接注入真实位姿与速度，虚拟车辆完全跟随实车状态、不经仿真物理；
  `spawn(..., mode=ControlMode.VIL)` 时自动关闭 Traffic Manager。通常与 `synchronous` 步进搭配保证时序对齐。
- **SIL（仿真在环，控制指令控制）**：`apply_control(VehicleCommand)` 下发油门/刹车/转向/档位，
  由 CARLA 物理引擎积分计算车辆运动。可配合 `asynchronous` 接近实时推进。

车辆控制 API 与示例详见《用户手册》§4.6。

## 地图系统

L1 `simulation` 提供地图系统（`MapManager` 契约 + `MapManagerImpl`，已在组合根注册供上层解析）：

- **内置地图**：`Town01`~`Town10` 共 8 张（简单城市 / 复杂城市 / 高速 / 乡村 / CBD），由
  `BUILTIN_MAP_REGISTRY` 提供说明与适用场景，支持按类别检索与 `load_map` 加载；场景配置的
  `scenario.map` 取其中地图名。
- **自定义地图**：`load_opendrive(path)` / `load_opendrive_xml(str)` 从 OpenDRIVE 1.4 / 1.6
  （`.xodr` 或 XML）经 `client.generate_opendrive_world` 生成世界，参数由 `OpenDriveOptions` 配置。
- **坐标系**：`CARLA_COORDINATE_FRAME` 描述左手系（X 东 / Y 北 / Z 上，单位米）；与实车坐标系的
  转换由 VIL 映射模块处理（见下方「VIL 虚实映射模块」）。

内置地图清单、自定义 OpenDRIVE 用法与配置详见《用户手册》§4.5。

## VIL 虚实映射模块

L5 `app/vil` 提供实车在环（Vehicle-in-the-Loop）全链路映射（开发提示词 §4）：

- **数据接入**：从 Kafka `telemetry_clean` topic 消费实车遥测（定位/底盘/感知/IMU），独立 daemon 线程 + 最新帧缓存。
- **坐标映射**：实车 odom 右手系 → CARLA 地图左手系的仿射变换（旋转 + 平移 + yaw 取反）。
- **状态同步**：位姿/线速度/角速度实时注入虚拟车辆（VIL 直接位姿控制）。
- **可视化**：CARLA `world.debug` 叠加浮动文字（速度/行为状态）、感知包围盒、规划轨迹线。
- **同步控制**：20ms 固定步长 tick；延迟 >500ms 暂停场景；>50ms 外推补偿（仅影响可视化）。

启用方式：在场景 YAML 中配置 `vil.enabled: true` 并提供标定参数，运行 `huntersim vil-run --config configs/vil_scenario.yaml`。
详细配置与算法说明见《用户手册》§4.8。

## 天气与环境

L1 `simulation` 提供天气与环境模型（`simulation/weather.py`），场景启动时由 `ScenarioManagerImpl`
加载地图后经 `world.set_weather(...)` 应用到 `carla.WeatherParameters`：

- **全参数（§3.4.1）**：`cloudiness` / `precipitation` / `precipitation_deposits` / `wind_intensity` /
  `sun_azimuth_angle` / `sun_altitude_angle` 六项，量程对齐 CARLA（0-100 / 0-360 / -90~90），由不可变
  `WeatherParameters` 携带并在构造点校验。
- **预设环境（§3.4.2）**：`PresetEnvironment` + `WEATHER_PRESET_REGISTRY` 提供 `clear_noon` / `overcast` /
  `light_rain` / `heavy_rain` / `foggy` / `night` / `dusk` / `dawn` 共 8 套预设，由 `list_presets()` /
  `resolve_preset()` 查询。
- **配置驱动**：`weather.preset` 指定预设名（设置时优先于显式字段）；未指定时逐项应用 `weather` 下的显式参数。

参数表、预设表与配置/API 示例详见《用户手册》§4.7。

## 环境准备

```bash
# 安装 uv 后（本项目使用 uv 管理）
uv sync --group dev          # 创建 3.12 虚拟环境并安装依赖
uv sync --extra vil          # VIL 实车在环（额外安装 kafka-python）

uv run ruff check .          # lint
uv run ruff format .         # 格式化
uv run mypy                  # 严格类型检查
uv run pytest                # 运行测试
```

## 目录结构

```
src/hunter_sim/     # 源码（src 布局）
tests/              # 单元 / 集成测试（CARLA 全部 mock）
configs/            # 场景与传感器配置示例（含 vil_scenario.yaml）
data/runs/          # 运行数据输出（gitignore）
```

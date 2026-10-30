# HunterSim · HUNTER SE 车辆在环（VIL）仿真端

HunterSim 是面向 **HUNTER SE 自动驾驶底盘车** 的仿真端：按平台下发的场景驱动 CARLA 0.9.16 世界，用实车遥测驱动虚拟自车，把状态、事件、指标、传感器文件与实时画面回传给云端。

仿真端只做「执行」——用户、租户、场景库、批量任务、审计、OTA、远程操控等**治理能力全部归云端**，本仓库不重复实现。

- **车辆规格**：820×640×310 mm，整备质量 60 kg，轴距 0.46 m，后轮驱动 + 前轮阿克曼转向，最大速度 4.8 m/s。
- **当前版本**：`3.1.0`（破坏性变更清单见 [`release.md`](release.md) 的 V3.0.0 章节，补丁修复见 V3.0.1 / V3.0.2 / V3.0.3 / V3.0.4 章节，V3.1.0 新增一键启动与可视化客户端）。
- **运行环境**：Python 3.12 · CARLA 0.9.16 · Kafka 3.6 ·（可选）ROS2 Humble

---

## 1. 架构

两个进程，职责互斥：

```
huntersim carla-manager  :8090   唯一业务入口（内网、无鉴权）
├── 控制面   platform_adapter/    创建/下发/暂停/恢复/停止/进度/结果 + 画面回传
├── VIL Bridge  vil_mapper/       收帧 → 10Hz 栅格降采样 → 时间对齐插值 → 位姿/速度/舵角/灯光 → 感知叠加
├── Scene Manager  scene_runner/ + engine/ + traffic_sim/   地图/参与者/天气/脚本/事件
├── Sensor Emulator  sensor_sim/  三型传感器挂载 → FrameHub + mcap 录制 → ROS2（可选）
└── Monitor & Recorder            MJPEG/截图 + 产物 + Prometheus 指标

huntersim probes  :8080          只读探针面（/health + /metrics）

huntersim up                     一键启动（V3.1.0：预检 → 两进程 → 打开可视化客户端）
└── 可视化客户端  visualization/  /viz 单页应用：画面(MJPEG) / 传感器(ROS) / 事件 / 日志（只读）
```

**数据面契约不变**：

| 方向 | Topic | 内容 |
|------|-------|------|
| 消费 | `carla.{sim_id}.vil_state` | 平台转发的实车状态（位姿/速度/底盘/感知/期望控制） |
| 发布 | `carla.{sim_id}.sensor_data` | 6 类 type：`sim_status` / `sim_telemetry` / `sim_event` / `sim_metrics` / `sensor_file` / `artifact` |
| 死信 | `carla.{sim_id}.sensor_data.dlq` | 反序列化失败与坏消息 |

**画面出口**：`GET /api/v1/instances/{id}/stream.mjpg`（MJPEG，默认 10fps）与 `/screenshot`（单帧 JPEG）；可视化客户端 `http://127.0.0.1:8090/viz` 直接复用这两个出口，不新增第二套业务面。

---

## 2. 模块职责

| 模块 | 职责 |
|------|------|
| `platform_adapter/` | 控制平面骨架：10 个实例端点 + `/health` + `/metrics`、单世界串行队列、SQLite 实例注册表（重启恢复/幂等）、看门狗、VIL 运行时装配、评估与产物回传、mcap 录制与上传 |
| `vil_mapper/` | 遥测环形缓冲、`FrameAligner`（10Hz 栅格 + 最短弧插值/外推）、位姿外推、坐标标定、灯光与转向写入、感知叠加、Kafka 消费自愈与 DLQ |
| `scene_runner/` | 场景契约与校验、状态机、事件检测、内置场景（含传感器挂载）、ScenarioRunner 适配 |
| `sensor_sim/` | 传感型号剖面（`profiles.py`）、蓝图工厂、安装位、数据转换、`FrameHub` 帧汇聚、`McapRecorder` 录制、ROS2 桥接（可选） |
| `traffic_sim/` | 交通流、Actor 行为、行为树、行人、触发条件 |
| `engine/` | CARLA 原子能力：地图/天气/自车/蓝图/坐标/OpenDRIVE/控制器/服务端配置/本地进程自愈 |
| `execution/` | 引擎端点库存与独占租约（`engine_pool`）、装配与释放（`plane`）、地图亲和选端、泄漏与僵尸租约回收 |
| `cloud/` | `SimDataProducer`（有界队列 + DLQ）、`sim_data_mapper`、`vil_state_parser`、`BagUploader` |
| `eval_service/` | 轨迹/舒适/安全/覆盖率四维评估与 S/A/B/C/D 评级 |
| `probes/` | 只读探针应用：`/health` 聚合管理面快照，`/metrics` 只导出本进程注册表 |
| `visualization/` | 只读可视化客户端：`/viz` 页面 + `/viz/api/*`（状态/事件/传感器/日志 SSE）、`logbus` 日志旁路、`launcher` 一键启动编排 |
| `common/` | pydantic 配置模型、异常体系、日志、Prometheus 指标 |
| `cli/` | `huntersim carla-manager` / `probes` / `up` / `doctor` / `version` |

---

## 3. 目录结构

```
HunterSim/
├── src/hunter_sim/
│   ├── platform_adapter/   # 控制面：app / service / executor / vil / store / mapper / models / config
│   ├── vil_mapper/         # VIL：frame_aligner / vil_sync_service / vil_data_consumer / telemetry_buffer / pose_extrapolator / calibration_manager / state_visualizer / perception_overlay
│   ├── scene_runner/       # 场景：scene_config / validator / state_manager / event_detector / builtin_scenario
│   ├── sensor_sim/         # 传感器：profiles / sensor_factory / sensor_mount / data_converters / frame_hub / mcap_recorder / carla_ros_bridge
│   ├── traffic_sim/        # 交通流、行为树、行人、触发条件
│   ├── engine/             # CARLA 原子能力 + 环境检查 + 进程自愈 supervisor
│   ├── execution/          # engine_pool（租约/亲和）+ plane（装配/释放/快照）
│   ├── cloud/              # sim_data_producer / sim_data_mapper / vil_state_parser / bag_uploader / connector
│   ├── eval_service/       # 评估引擎与报告
│   ├── probes/             # 只读探针面
│   ├── visualization/      # 可视化客户端：logbus / sensor_view / routes / launcher + assets(index.html/app.js/style.css)
│   ├── cli/                # 运维 CLI
│   └── common/             # 配置 / 异常 / 日志 / 指标
├── configs/                # weather_profiles / sensor_configs / scene_templates / traffic_config
├── docker/                 # Dockerfile / Dockerfile.carla / compose（主 + Windows）/ prometheus / alerts / grafana
├── k8s/                    # 单 Pod 双容器（sim-service + probes）+ CARLA GPU Pod + PVC
├── tests/                  # 单元 + 集成（覆盖率门禁 ≥80%）
├── docs/                   # 单机部署指南
├── Makefile                # doctor / up-local / manager / probes / test / coverage / typecheck / lint / image / up
├── pyproject.toml          # 依赖与工具配置（pytest / mypy / coverage / console script）
└── .env.example            # 全部环境变量与默认值（逐项中文注释）
```

---

## 4. 快速开始

### 4.1 安装

```bash
python -m venv .venv
.venv\Scripts\activate                        # Windows（Linux: source .venv/bin/activate）
pip install -e ".[dev]"
pip install carla==0.9.16                     # 不在 pyproject 依赖内，需与 server 严格同版本
```

### 4.2 预检

```bash
huntersim doctor                              # 配置/端口/磁盘/GPU/CARLA/实例库/录制目录/mcap/Kafka
huntersim doctor --json --strict               # 脚本消费 / 上线门禁（warn 提为 fail）
```

未装 CARLA SDK 时服务仍可启动，但下发场景返回 `503`；未装 `mcap` 时录制降级为 no-op。

### 4.3 启动

```bash
copy .env.example .env                        # Windows（Linux: cp）

huntersim up                                  # 一键：预检 → 控制面 :8090 → 探针面 :8080 → 打开 /viz
huntersim up --no-browser                     # 不起浏览器，只打印可视化地址
huntersim up --skip-doctor --no-probes        # 跳过预检 / 不拉探针面
```

需要分进程排障时仍可手工起：

```bash
huntersim carla-manager                       # 控制面 :8090（唯一业务入口）
huntersim probes                              # 只读探针面 :8080（另开终端）
```

交互式文档：<http://127.0.0.1:8090/docs>；可视化客户端：<http://127.0.0.1:8090/viz>。

> **8090 无鉴权**，只允许内网/本机访问，禁止加 Ingress/NodePort 或映射公网。CARLA 的 2000–2002 同理。

### 4.4 最小闭环

```bash
BASE=http://127.0.0.1:8090

# 1) 创建实例（立即返回，世界在后台加载）
curl -X POST $BASE/api/v1/instances -H "Content-Type: application/json" -d '{
  "scene_id": "smoke-001",
  "sim_id": "sim-local-001",
  "mode": "sil",
  "scene_config": {
    "map": {"map_id": "Town03", "spawn_point": {"x": 0, "y": 0, "z": 0.3, "yaw": 0}},
    "ego_vehicle": {"model": "hunter_se"},
    "duration": 30
  }
}'

# 2) 轮询（pending → running → succeeded/failed/canceled）
curl $BASE/api/v1/instances/<ID>

# 3) 结果（非终态也返回 200 + 当前状态）
curl $BASE/api/v1/instances/<ID>/result

# 4) 实时画面
curl -o frame.jpg $BASE/api/v1/instances/<ID>/screenshot
# 浏览器：<img src="http://127.0.0.1:8090/api/v1/instances/<ID>/stream.mjpg">
# 可视化看板：http://127.0.0.1:8090/viz （画面 + 传感器 + 事件 + 日志）
```

### 4.5 容器化

```bash
copy .env.example .env
docker compose -f docker/docker-compose.yml up -d --build
# 服务：kafka · sim-service(:8090) · probes(:8080) · prometheus(:9090) · alertmanager(:9093) · grafana(:3000)
# Linux + NVIDIA Container Toolkit 可加 --profile carla 起容器内 CARLA 引擎
```

Windows 单机（CARLA 也在 Docker 里）：`docker compose -f docker/docker-compose.windows.yml up -d`（`carla-server` + `sim-service` + `kafka`）。

Kubernetes：`kubectl apply -f k8s/`（单 Pod 双容器，`replicas: 1`，引擎自愈由 carla-engine Pod liveness 承担）。

---

## 5. 配置

优先级：**环境变量 > `.env` > 默认值**。全部由 `common/models.py` 的 pydantic `BaseSettings` 聚合；完整清单见 [`.env.example`](.env.example)。

| 前缀 | 组 | 关键项（默认值） |
|------|------|------------------|
| `HUNTER_SIM_` | 全局 | `env`(dev) · `log_level`(INFO) · `log_json`(false) · `log_file`(空) · `log_max_bytes` · `log_backup_count` |
| `CARLA_` | CARLA 连接 | `host` · `rpc_port`(2000) · `stream_port`(2001) · `fixed_delta_seconds`(0.02 = 50Hz) · `timeout_seconds`(10) · `supervisor_*`（本地进程自愈，仅托管回环端点） |
| `KAFKA_` | 数据总线 | `bootstrap_servers` · `telemetry_topic`(`carla.{sim_id}.vil_state`) · `sim_id` · `legacy_telemetry_topic` · `enable_self_heal`(true) · `rebuild_after_errors`(10) · `dlq_topic` |
| `VIL_` | 虚实映射 | `target_hz`(10.0) · `extrapolation_ms`(150) · `max_data_latency_ms`(500) · `warning_latency_ms`(50) · `buffer_max_frames`(10) · `ordering_guard_enabled`(true) · `telemetry_frequency_hz`(10.0) · `auto_buffer_depth`(true) · `overlay_enabled`(true) · `lights_enabled`(true) · `clock_offset_auto`(false) |
| `RESOURCE_` | 生命周期 | `instance_max_lifetime_seconds`(7200) · `health_check_interval_seconds`(10) |
| `EXECUTION_` | 执行面 | `engine_endpoints`(空=单引擎) · `max_engines`(8) · `restore_scene_on_assemble`(true) · `lease_heartbeat_enabled`(true) · `lease_ttl_seconds`(120) |
| `ADAPTER_` | 控制面 | `host`(127.0.0.1) · `port`(8090) · `db_path` · `artifacts_dir` · `artifact_bucket` · `artifact_retention_hours`(72) · `recordings_dir` · `recording_retention_hours`(24) · `record_enabled`(true) · `record_min_free_gb`(0.5) · `upload_on_failure`(false) · `stream_fps`(10.0) · `stream_max_width`(640) · `stream_max_clients`(8) · `screenshot_quality`(75) · `watchdog_factor`(3.0) · `watchdog_grace_seconds`(120) · `progress_interval_seconds`(0.5) · `default_duration_seconds`(60) · `max_scene_seconds`(7200) · `busy_timeout_seconds`(5.0) |
| `PROBE_` | 探针面 | `host`(127.0.0.1) · `port`(8080) · `adapter_url`(`http://127.0.0.1:8090`) |
| `VIZ_` | 可视化客户端 | `enabled`(true) · `auto_open_browser`(true) · `log_buffer_size`(2000) · `log_history_limit`(500) · `event_limit`(200) · `lidar_max_points`(4000) · `depth_max_width`(480) |
| `HUNTER_CORE_` | 云端接入 | `enabled`(false) · `kafka_bootstrap` · SASL 凭据 · `sensor_data_topic_template` · `sensor_data_dlq_template` · `gateway_url` · `jwt_token` · `rosbag_bucket` · `upload_queue_max_size`(32) · `upload_max_retries`(2) · `upload_retry_backoff_s`(1.0) |

> V3.0.0 起已移除 `API_*` / `STATE_*` / `PLATFORM_*` / `GUI_*` 前缀与 `EXECUTION_ENGINE_ENDPOINTS` 的双引擎示例（受支持形态 = 单世界串行，端点库存留空即单引擎）。

---

## 6. 传感器剖面

实车型号参数固化在 [`src/hunter_sim/sensor_sim/profiles.py`](src/hunter_sim/sensor_sim/profiles.py)，与 [`configs/sensor_configs.json`](configs/sensor_configs.json) 双向对应（单测 `test_sensor_profiles.py` 逐字段断言一致）。

| 剖面 | 覆盖传感器 | 关键参数 |
|------|------------|----------|
| `helios_16p` | RS-Helios-16P LiDAR | 16 线 / 150 m / 10 Hz / 垂直 FOV ±15° / 噪声 σ=0.02 m / 131072 pts·s⁻¹ |
| `d435` | Intel D435 RGB + Depth | RGB 1280×720 / HFOV 69° / 30 fps；Depth 1280×720 / HFOV 87° / 0.2–10 m |
| `ch10x` | CH10X IMU | 100 Hz / accel σ=0.01 m/s² / gyro σ=0.001 rad/s |
| `hunter_se_full`（默认） | 上述三件套全挂 | 另含 collision / lane_invasion / obstacle 事件传感器 |

场景按剖面挂载：

```json
{
  "sensors": [
    {"profile": "hunter_se_full", "enabled": true, "ros_enabled": false, "record": true}
  ]
}
```

`ros_enabled=true` 时额外投递 ROS2 话题（需 `rclpy`，缺失自动降级为不出 ROS；桥接器装配后会调用 `initialize()` 建立发布者）；`record=true` 时写入 `ADAPTER_RECORDINGS_DIR` 下的 `.mcap`。录制还受 `ADAPTER_RECORD_ENABLED`（总开关）与 `ADAPTER_RECORD_MIN_FREE_GB`（磁盘余量闸门）约束，运行期每 5s 复查、跌破阈值主动停录；`rgb`/`depth`/`lidar`/`imu` 四路均进入 `FrameHub`（`depth` 为 `32FC1` 米制字节）。

传感器回调只做内存拷贝并入有界队列（上限 32 帧），编码/落盘/ROS 发布由 `hunter-sensor-encoder` 线程串行执行，避免把 CARLA `world.tick()` 线程拖出 50 Hz 预算；队满时丢帧并计入 `huntersim_sensor_frames_dropped_total{stream}`。mcap 写入连续失败 10 次即熔断（`RecordStats.write_errors` 上报），已写入部分仍会 `finish()` 落盘。

---

## 7. 监控

控制面 `:8090/metrics` 与探针面 `:8080/metrics` 各自导出标准 Prometheus 文本格式；探针 `/health` 通过 `PROBE_ADAPTER_URL` 向控制面聚合 `carla_connected` / `active_instance` / `vil_state` / `sensor` / `cloud`，上游不可达时返回 **200 + `status=degraded`**（避免容器 liveness 反复杀进程）。

V3.0.0 新增指标：

| 指标 | 类型 | 说明 |
|------|------|------|
| `huntersim_vil_frames_downsampled_total{mode}` | Counter | 栅格推进计数（`interpolated`/`extrapolated`/`nearest`），总和应稳定 10/s |
| `huntersim_vil_align_skew_ms` | Histogram | 栅格对齐误差，P95 < 60 ms 视为健康 |
| `huntersim_sensor_frames_total{stream}` | Counter | 传感器帧计数（`rgb`/`depth`/`lidar`/`imu`） |
| `huntersim_sensor_frames_dropped_total{stream}` | Counter | 编码队列满导致的丢帧计数（持续增长 → 磁盘/CPU 跟不上采集） |
| `huntersim_sensor_record_bytes_total` | Counter | mcap 累计写入字节（磁盘容量告警依据） |
| `huntersim_recording_events_total{event}` | Counter | 录制生命周期事件（`started`/`skipped_disabled`/`skipped_low_disk`/`stopped_low_disk`） |
| `huntersim_cloud_upload_total{result}` | Counter | bag/mcap 上传终态（自动重试耗尽才计 `failed`） |
| `huntersim_stream_clients` | Gauge | MJPEG 在线客户端数 |
| `huntersim_probe_upstream_reachable` | Gauge | 探针到控制面的可达性 |

---

## 8. 开发与测试

```bash
make test          # pytest -q（含覆盖率门禁 fail_under=80）
make coverage      # pytest -q --cov=src/hunter_sim --cov-report=term-missing
make typecheck     # mypy strict
make lint          # flake8 src tests
make doctor        # 部署预检
```

当前门禁基线：

- **测试**：`1126 passed`（单元 + 集成），全仓行覆盖 **84.40%**（门禁 ≥80%）。
- **类型**：`mypy src/hunter_sim`（strict）**86 个源文件 0 错误**。
- **Lint**：`flake8 src tests` **0 告警**。
- **死代码**：`tests/test_no_orphan_exports.py` 断言 `__init__.py` 导出的符号在包内有生产引用（本期接入的 `StateVisualizer`/`FrameHub`/`McapRecorder`/`FrameAligner`/`CarlaRosBridge` 均有回归守卫）。
- CARLA / ROS2 / mcap 未安装的机器上，测试通过 `sys.modules` 桩注入（`tests/mocks/carla_mocks.py`）无引擎运行。

---

## 9. 容量边界

| 项 | 口径 |
|------|------|
| 控制面副本数 | **1**（单世界串行；`replicas: 1`，重启后非终态实例置 `failed`，由平台重新创建） |
| 状态库 | SQLite（WAL + 单写者），需 RWO 卷；丢失会失去幂等与历史查询 |
| 水平扩展 | **不受支持**（多副本需外置共享状态与分布式租约） |
| 录制磁盘 | 三传感器约 **1.5–3 GB/分钟**（LiDAR 为主），默认保留 24 小时；`doctor` 校验剩余 ≥ 5 GiB |
| MJPEG 带宽 | 默认 10fps + `screenshot_quality=75` + `stream_max_width=640`；按客户端数线性增长 |
| 并行 | 需要并行时部署多个控制面进程/端口并在平台侧分片（不共享注册表） |

---

## 10. 文档索引

| 文件 | 内容 |
|------|------|
| `README.md` | 项目总览与快速开始（本文件） |
| [`user_manual.md`](user_manual.md) | 控制面接口使用手册：实例/场景/VIL/画面/录制、状态与结果、FAQ |
| [`deployment_and_operations.md`](deployment_and_operations.md) | 部署、平台接入、监控指标、故障排查、受支持形态 |
| [`docs/单机部署指南.md`](docs/%E5%8D%95%E6%9C%BA%E9%83%A8%E7%BD%B2%E6%8C%87%E5%8D%97.md) | Windows 单机安装、启动、联调、冒烟验证、故障速查 |
| [`release.md`](release.md) | 版本发布说明（V3.0.0 含破坏性变更清单） |
| [`HunterSim.ai-rules.md`](HunterSim.ai-rules.md) | 工程约束与开发规范 |

---

## 11. 许可

本项目基于 [LICENSE](LICENSE) 发布。

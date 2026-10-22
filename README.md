# HunterSim · HUNTER SE 车辆在环（VIL）虚拟仿真平台

HunterSim 是面向 **HUNTER SE 自动驾驶底盘车** 的高保真虚拟仿真与虚实映射（VIL）平台。它以 CARLA 0.9.16 为渲染与物理引擎，以 ROS2 Humble 为传感器数据语义总线，通过 FastAPI 对外提供统一的 REST / WebSocket 控制接口，支持 **车辆在环（VIL）**、**软件在环（SIL）** 与 **数据回放（Replay）** 三种运行模式。自 V2.2.0 起额外提供 **Windows 桌面仿真客户端**（CarlaSceneX 风格，进程内直连 CARLA）与 **HunterCore 数据采集分析平台接入**。

- **车辆规格**：820×640×310 mm，整备质量 60 kg，轴距 0.46 m，后轮驱动 + 前轮阿克曼转向，最大速度 4.8 m/s。
- **当前版本**：`2.6.0`（V2.6.0 为**阶段四「并发安全与稳定性加固」（P0）**：运行器注册表与装配锁启动期预建（消除首次并发竞态）、引擎级装配锁（多引擎并行装配不再被全局锁串行）、按 IP 限流与 `/auth/token` 失败指数退避（429 + `Retry-After`）、cloud bag 上传队列有界化（队满丢最旧并计数）、`/metrics` 改标准 prometheus_client 导出并预置 Grafana 总览盘，详见 [`release.md`](release.md)）。上一版本 `2.5.0` 为**阶段三「平台化」**：多租户隔离（`tenant` + 租户配额）、服务账号 API Key（`X-API-Key`）、RBAC 角色层级（guest < viewer < operator < admin）、场景库版本化 CRUD、批量任务与审计事件落 SQLite、`GET /platform/overview` 平台概览。更早：`2.4.0` 为**阶段二「执行面分离与状态持久化」**——引擎端点库存 `EXECUTION_ENGINE_ENDPOINTS` 与独占租约、实例/场景绑定落 SQLite 重启恢复、`GET /execution` 观测；`2.3.0` 为**阶段一「可用性止血」**——打通 REST 场景闭环（Token → 创建实例 → 下发场景 → 启动 → 进度 → 停止/销毁），补齐 `/auth/token`、生产启动闸门、WebSocket 鉴权、实例级归属校验、日志级别与指标，镜像内置 `carla==0.9.16` SDK。功能版本 `2.2.0` 新增 **Windows 桌面仿真客户端**、**Windows 单机全容器编排**与 **HunterCore 数据接入**。
- **运行环境**：Python 3.12 · CARLA 0.9.16 · ROS2 Humble · Kafka 3.6 · FastAPI

---

## 1. 系统定位

| 能力 | 说明 |
|------|------|
| VIL 虚实映射 | 订阅实车遥测（Kafka），经位姿外推 + 坐标标定驱动 CARLA 虚拟车，实现虚实同步 |
| SIL 场景驱动 | 加载 OpenDRIVE / 场景 JSON，在 CARLA 中自动生成交通流与事件，驱动被测算法 |
| 传感器仿真 | LiDAR / RGB / Depth / IMU / GNSS / Collision / LaneInvasion，对齐 ROS2 话题 |
| 数据录制与回放 | mcap/自定义容器录制，数字孪生回放，支持变速与跳帧 |
| 场景评估 | 轨迹、舒适性、安全性、覆盖率四维评分（S/A/B/C/D），支持批量场景测试与汇总报告 |
| 资源编排 | GPU 资源池 + 实例生命周期管理，按画质限制单 GPU 并发；单用户/全局并发配额与实例守护（崩溃重试、超时回收） |
| 执行面分离（V2.4.0） | 引擎端点库存（`EXECUTION_ENGINE_ENDPOINTS`）按实例独占租约，多 CARLA 引擎可并行跑多场景；`GET /execution` 观测引擎占用、租约与运行器 |
| 状态持久化（V2.4.0） | 实例与场景绑定落 SQLite（WAL），服务重启后实例可查询、场景定义在装配时自动恢复；`/health` 与 `/execution` 暴露持久化开关与恢复计数 |
| 平台化（V2.5.0） | 多租户隔离与租户配额；服务账号 API Key（`hsk_...`，仅存哈希）；RBAC 角色层级；场景库版本化（同名自增版本/软删除/按 ID 下发）；批量任务与审计事件落库；平台概览 `GET /platform/overview` 与审计查询 `GET /audit` |
| 安全治理 | JWT/API Key 统一主体与角色分级（创建实例需 operator+，销毁实例与地图上传、管理面需 admin）、车辆模型白名单、场景安全审核、地图上传审核（含路径穿越双重校验）、租户级隔离、审计落库 |
| 并发安全与限流（V2.6.0） | 装配锁与运行器注册表启动期预建；引擎级装配锁支撑多引擎并行 `load_world`；按 IP 令牌桶限流（429 + `Retry-After`）与 `/auth/token` 失败指数退避；cloud bag 上传队列有界化；标准化 Prometheus 指标 + Grafana 预置盘 |

---

## 2. 分层架构（C/S 三层）

```
┌─────────────────────────────────────────────────────────────┐
│  接口层  api/  —— FastAPI 应用、路由、中间件、统一响应/异常      │
│    health · auth · instances · scenes · scene_library ·        │
│    scenarios · audit · platform · resources · websocket       │
├─────────────────────────────────────────────────────────────┤
│  执行面  execution/ —— 引擎端点库存与独占租约（V2.4.0）          │
│    engine_pool（端点解析/租约） · plane（装配/释放/快照）        │
├─────────────────────────────────────────────────────────────┤
│  服务层  —— 业务编排（无 CARLA/ROS2 直接依赖，可单测）           │
│    scene_runner  ·  vil_mapper  ·  sensor_sim  ·  traffic_sim  │
│    replay_service ·  eval_service ·  resource_manager          │
├─────────────────────────────────────────────────────────────┤
│  引擎层  engine/ —— CARLA 原子能力封装（依赖 carla 模块）        │
│    map_manager · weather_manager · hunter_se_vehicle ·          │
│    vehicle_blueprint_generator · coordinate_converter ·        │
│    opendrive_parser · vehicle_controller · carla_server_config  │
├─────────────────────────────────────────────────────────────┤
│  公共层  common/ —— 配置模型、领域数据类、异常、日志工具          │
│  持久化  persistence/ —— SQLite：实例/场景绑定 + API Key/场景库/  │
│    批量任务/审计（V2.4.0 / V2.5.0）                              │
└─────────────────────────────────────────────────────────────┘
```

`common/` 为所有层的共享基础：`models.py`（pydantic 配置与数据类）、`exceptions.py`（分层异常体系）、`utils.py`（日志与工具）。

---

## 3. 目录结构

```
HunterSim/
├── src/hunter_sim/          # 主包（见下方模块职责）
│   ├── api/                 # 接口层：main, runtime, deps, error_handlers, middleware, models, routers
│   ├── common/              # 公共层：models, exceptions, utils
│   ├── execution/           # 执行面：engine_pool（端点库存/租约）, plane（装配/释放/快照）
│   ├── persistence/         # 状态持久化：state_store（实例/场景绑定 + API Key/场景库/批量任务/审计）
│   ├── engine/              # 引擎层：CARLA 原子能力 + 环境检查 + 服务端配置
│   ├── scene_runner/        # 场景加载、校验、状态机、事件检测、自定义场景
│   ├── vil_mapper/          # 遥测缓冲、位姿外推、坐标标定、虚实同步
│   ├── sensor_sim/          # 传感器工厂、安装位、数据转换、ROS2 桥接、录制
│   ├── traffic_sim/         # 交通流、Actor 行为、行为树、行人、触发条件
│   ├── replay_service/      # 数据加载、回放引擎、数字孪生
│   ├── eval_service/        # 评估引擎、评估报告
│   ├── resource_manager/   # 实例管理、GPU 资源池、健康监控
│   ├── gui/                 # 桌面仿真客户端（V2.2.0，PySide6 可选依赖，惰性导入）
│   └── cloud/               # HunterCore 数据接入（V2.2.0，Kafka 遥测 + MinIO bag 直传）
├── configs/                 # 预设配置：weather_profiles / sensor_configs / scene_templates / traffic_config
├── docker/                  # Dockerfile / Dockerfile.carla / docker-compose / prometheus / alerts / alertmanager / grafana
├── k8s/                     # namespace / configmap / secret / deployment（含 CARLA GPU Pod + PVC）/ service
├── hunter_assets/           # CARLA 引擎镜像构建资源（Dockerfile.carla COPY 上下文）
├── tests/                   # 单元 + 集成测试（覆盖率门禁 ≥80%）
├── pyproject.toml           # 依赖与工具配置（setuptools / pytest / flake8 / mypy / coverage）
├── setup.cfg                # flake8 质量门禁配置
└── requirements.txt         # 运行依赖清单
```

---

## 4. 快速开始

### 4.1 环境准备

- **GPU 主机（推荐 Windows，CARLA 原生平台）**：安装 NVIDIA 驱动（≥510）、CUDA（≥11.0）、CARLA 0.9.16。
- **服务主机（Linux 或 Windows）**：Python 3.12、Kafka 3.6、（可选）ROS2 Humble。

验证运行环境（在 CARLA 主机上执行，检查 OS / Python≥3.12 / 驱动 / CUDA / 显存）：

```bash
python -c "from hunter_sim.engine.check_environment import check_environment as c; r=c(); print(r); raise SystemExit(0 if r.passed else 1)"
```

启动 CARLA 服务端（Windows）：

```bash
# 由 engine/carla_server_config.py 生成启动命令，示例：
CarlaUE4.exe -carla-rpc-port=2000 -RenderOffScreen -quality
```

### 4.2 安装与启动服务

```bash
# 创建虚拟环境并安装
python -m venv .venv
.venv\Scripts\activate                       # Windows
pip install -e ".[dev]"                       # 或 pip install -r requirements.txt

# 配置 JWT 密钥（生产必须覆盖默认值）
set API_JWT_SECRET=your-strong-secret         # Windows CMD
# export API_JWT_SECRET=your-strong-secret    # PowerShell / Linux

# 启动 API 服务
uvicorn hunter_sim.api.main:app --host 0.0.0.0 --port 8080

# （可选）启用状态持久化：实例与场景绑定落 SQLite，重启后自动恢复
set STATE_ENABLED=true
set STATE_DB_PATH=data/state/hunter_sim.db

# （可选）平台化（V2.5.0）：默认租户与租户级并发配额
set PLATFORM_DEFAULT_TENANT=default
set PLATFORM_MAX_INSTANCES_PER_TENANT=20
```

启动后访问交互式文档：<http://localhost:8080/api/v1/sim/docs>

> V2.3.0 起需先换取 Token（dev 默认 `admin/admin`，生产由 `API_ADMIN_USERNAME` / `API_ADMIN_PASSWORD` 覆盖）：
>
> ```bash
> curl -X POST http://localhost:8080/api/v1/sim/auth/token \
>   -H "Content-Type: application/json" \
>   -d '{"username":"admin","password":"admin"}'
> ```

### 4.3 容器化 / 编排（CARLA 跑在宿主机）

```bash
# 1) 准备环境变量（开发默认值可直接用；生产改 API_JWT_SECRET / API_ADMIN_PASSWORD）
cp .env.example .env                              # Windows: copy .env.example .env

# 2) 构建并启动（镜像内置 carla==0.9.16 Python SDK，可直接驱动宿主机 CARLA）
docker compose -f docker/docker-compose.yml up -d --build

# Linux + NVIDIA GPU 主机可额外启用容器化 CARLA 引擎（profile，需 NVIDIA Container Toolkit）
# 同时拉起 carla-engine(:2000) 与 carla-engine-2(:2100) 双引擎并行
EXECUTION_ENGINE_ENDPOINTS=carla-engine:2000,carla-engine-2:2000 \
  docker compose -f docker/docker-compose.yml --profile carla up -d

# Kubernetes（需 NVIDIA GPU Operator；含 sim-service + carla-engine GPU Pod + 共享 PVC）
kubectl apply -f k8s/
```

> 前置条件：宿主机已启动 CARLA 0.9.16（Windows 双击 `CarlaUE4.exe`）。服务启动后用 `GET /api/v1/sim/health` 确认 `carla_connected=true`，再按 §4.7 跑 REST 场景闭环。详见 [`deployment_and_operations.md`](deployment_and_operations.md) 与 [`docs/单机部署指南.md`](docs/单机部署指南.md)。

### 4.4 Windows 单机全容器（V2.2.0）（CARLA 不在宿主机在Docker里跑）

在 Windows 11 上用 Docker Desktop（WSL2 后端）+ NVIDIA Container Toolkit 一键拉起 CARLA 服务、ROS2 Bridge 与仿真管理服务：

```powershell
# 前提：Docker Desktop 切换到 WSL2 后端；已安装 NVIDIA Container Toolkit（WSL2 GPU 直通）
docker compose -f docker/docker-compose.windows.yml up -d
# 服务：carla-server(:2000/:2001/:8000) · ros2-bridge · sim-management(:8080) · kafka(:9092)
```

> CARLA 官方镜像为 Linux 构建，Windows 经 WSL2 GPU 直通运行；也可在宿主机原生启动 `CarlaUE4.exe`，桌面客户端两种都支持。

### 4.5 桌面仿真客户端（V2.2.0）

PySide6 原生 Windows 客户端，进程内直连 CARLA，对标 CarlaSceneX：连接、地图/Actor/传感器面板、实时画面、Autopilot/Manual、ScenarioRunner 场景运行、VIL/SIL 模式。

```powershell
pip install -e ".[gui]"                 # 安装 PySide6；CARLA 同版本 0.9.16 wheel 需另装
hunter-sim-gui                          # 或： python -m hunter_sim.gui.app
```

### 4.6 HunterCore 数据接入（V2.2.0）

将数字孪生车作为“车端”，向 HunterCore（`192.168.31.35`）回传遥测与 bag：

```powershell
set HUNTER_CORE_ENABLED=true
set HUNTER_CORE_VEHICLE_ID=SIM_HUNTER_SE_001
set HUNTER_CORE_SASL_USERNAME=<账号>
set HUNTER_CORE_SASL_PASSWORD=<口令>
set HUNTER_CORE_JWT_TOKEN=<data-collector Bearer JWT>
```

未配置凭据（`enabled=false`）时连接器整体降级为 no-op，不影响本地仿真。详见 [`deployment_and_operations.md`](deployment_and_operations.md)。

### 4.7 REST 场景闭环（V2.3.0 引入，V2.4.0 端点独占）

`docker compose up -d` + 宿主机 CARLA 就绪后，最小闭环只需 5 个请求（`BASE=http://localhost:8080/api/v1/sim`）：

```bash
# 1) 取 Token（dev 默认 admin/admin）
TOKEN=$(curl -s -X POST $BASE/auth/token -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r .data.access_token)

# 2) 创建实例（V2.5.0 起需 operator+；此处用管理员 Token）→ 返回 sim_instance_id
curl -X POST $BASE/instances -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"sil","map":"Town03","quality":"medium"}'

# 3) 下发场景（首次会 load_world + 切换同步模式 + 生成自车，约 10~30s）
curl -X POST $BASE/instances/<INSTANCE_ID>/scene -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"scene_id":"smoke-001","scene_config":{"scene_id":"smoke-001","map_id":"Town03","duration_seconds":30,"ego_vehicle":{"vehicle_blueprint":"hunter_se","spawn_point":{"x":1.0,"y":2.0},"autopilot":true}}}'

# 4) 启动 + 查询进度
curl -X POST $BASE/instances/<INSTANCE_ID>/start -H "Authorization: Bearer $TOKEN"
curl $BASE/instances/<INSTANCE_ID>/status -H "Authorization: Bearer $TOKEN"     # data.scene.progress

# 5) 停止（释放场景 Actor）→ 销毁实例
curl -X POST $BASE/instances/<INSTANCE_ID>/stop -H "Authorization: Bearer $TOKEN"
curl -X DELETE $BASE/instances/<INSTANCE_ID> -H "Authorization: Bearer $TOKEN"
```

> **端点独占（V2.4.0）**：配置 `EXECUTION_ENGINE_ENDPOINTS` 后每个实例独占一个引擎端点，可并行运行；未配置时所有实例共享 `CARLA_HOST:CARLA_RPC_PORT` 单引擎，同一时刻仅一个实例能持有场景运行器，第二个实例下发场景返回 `503`（`GET /execution` 可查看占用）。CARLA 未就绪时下发场景返回 `503`（`/health` 的 `carla_probe_error` 给出原因），`start` 仍返回 200 但 `scene_runner=unavailable`。Windows 用户可把 `curl` 换成 PowerShell `Invoke-RestMethod`，完整示例见 [`user_manual.md`](user_manual.md)。

### 4.8 平台化能力快速验证（V2.5.0）

```bash
BASE=http://localhost:8080/api/v1/sim
TOKEN=$(curl -s -X POST $BASE/auth/token -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r .data.access_token)

# 1) 签发服务账号 API Key（admin；明文仅返回一次，之后用 X-API-Key 调用）
curl -X POST $BASE/auth/api-keys -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"ci-bot","role":"operator","expires_in_days":30}'

# 2) 场景入库（同租户同名再次保存版本自增，返回 data.library_scene_id）
curl -X POST $BASE/scenes/library -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"smoke","description":"冒烟场景","config":{"scene_id":"smoke-001","map_id":"Town03","ego_vehicle":{"spawn_point":{"x":1.0,"y":2.0}}}}'

# 3) 按场景库 ID 下发（与 scene_config 二选一）
curl -X POST $BASE/instances/<INSTANCE_ID>/scene -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"library_scene_id":"<LIBRARY_SCENE_ID>"}'

# 4) 任务中心 / 审计 / 平台概览（需 admin；tenant=* 为全局）
curl "$BASE/scenarios" -H "Authorization: Bearer $TOKEN"
curl "$BASE/audit?action=instance.create&limit=20" -H "Authorization: Bearer $TOKEN"
curl "$BASE/platform/overview?tenant=*" -H "Authorization: Bearer $TOKEN"
```

> API Key 需 `STATE_ENABLED=true`（否则签发/列表/吊销返回 `503`）；`PLATFORM_API_KEYS_ENABLED=false` 时关闭 API Key 认证。租户隔离对非 admin 生效：按 ID 读取他租户资源返回 `404`，显式指定他租户的列表/创建请求返回 `403`，admin 可跨租户运维。

---

## 5. 配置

配置优先级：**环境变量 > `.env` / 配置文件 > 默认值**。所有配置由 `common/models.py` 的 pydantic `BaseSettings` 聚合，按前缀分组：

| 前缀 | 组 | 关键项（默认值） |
|------|------|------------------|
| `HUNTER_SIM_` | 全局 | `env`(dev) · `log_level`(INFO) |
| `CARLA_` | CARLA 连接 | `host`(127.0.0.1) · `rpc_port`(2000) · `stream_port`(2001) · `tm_port`(8000) · `fixed_delta_seconds`(0.02) |
| `KAFKA_` | 数据总线 | `bootstrap_servers`(localhost:9092) · `telemetry_topic`(telemetry_clean) |
| `VIL_` | 虚实映射 | `extrapolation_ms`(150) · `max_data_latency_ms`(500) · `buffer_max_frames`(10) · `calibration_*` |
| `API_` | 接口服务 | `host`(0.0.0.0) · `port`(8080) · `jwt_secret` · `max_concurrent_instances`(2) · `stream_base_url`(webrtc://localhost:8080) · **V2.6.0**：`rate_limit_enabled`(true) · `rate_limit_per_minute`(1000) · `login_max_failed_attempts`(5) · `login_backoff_base_seconds`(2.0) · `login_backoff_max_seconds`(60) |
| `RESOURCE_` | 资源管理 | `instance_max_lifetime_seconds`(7200) · `max_retry_count`(3) · `max_instances_per_user`(5) · `max_instances_total`(50) · `gpu_memory_warning_threshold`(0.90) · `resources_dir`(resources) |
| `EXECUTION_` | 执行面（V2.4.0） | `engine_endpoints`(空=单引擎) · `max_engines`(8) · `restore_scene_on_assemble`(true) |
| `STATE_` | 状态持久化（V2.4.0） | `enabled`(false，compose/K8s 为 true) · `db_path`(data/state/hunter_sim.db) · `recover_instances`(true) · `restore_scenes`(true) · `busy_timeout_seconds`(5.0) |
| `PLATFORM_` | 平台化（V2.5.0） | `default_tenant`(default) · `max_instances_per_tenant`(20) · `api_keys_enabled`(true) · `audit_enabled`(true) |
| `HUNTER_CORE_` | HunterCore 接入 | `enabled`(False) · `vehicle_id`(SIM_HUNTER_SE_001) · `kafka_bootstrap`(192.168.31.35:9092) · `security_protocol`(SASL_SSL) · `sasl_mechanism`(SCRAM-SHA-512) · `sasl_username/password` · `gateway_url`(http://192.168.31.35:8080) · `jwt_token` · `rosbag_bucket`(hunter-rosbag) · **V2.6.0**：`upload_queue_max_size`(32，bag 上传后台队列容量，队满丢最旧) |
| `GUI_` | 桌面客户端 | `theme`(dark) · `default_map`(Town03) · `stream_fps`(30) · `scenario_runner_path`(None) |

预设资源文件位于 `configs/`：8 种天气环境、7 类传感器、7 个场景模板、5 套交通流档位。

---

## 6. API 一览

统一前缀 `/api/v1/sim`，响应体 `{code, message, data}`。先调 `POST /auth/token` 换取 JWT（HS256，密钥 `API_JWT_SECRET`）；其余接口（除健康检查与 WebSocket 握手鉴权外）均需 `Authorization: Bearer <JWT>`，服务间调用可用 `X-API-Key`（WebSocket 握手仅支持 JWT）。角色层级 `guest < viewer < operator < admin`：**创建实例需 `operator` 及以上；销毁实例、地图上传与管理面（API Key/审计/平台概览/执行面）需 `admin`**。

| 分组 | 方法与路径 |
|------|-----------|
| 认证 | `POST /auth/token`（换取 JWT） · `POST /auth/api-keys`⁺（签发服务账号） · `GET /auth/api-keys`⁺ · `DELETE /auth/api-keys/{key_id}`⁺ |
| 健康/指标 | `GET /health` · `GET /health/live` · `GET /health/ready` · `GET /health/performance-targets` · `GET /metrics` |
| 实例 | `POST /instances`（operator+） · `GET /instances` · `GET /instances/{id}` · `POST /instances/{id}/{start\|stop\|pause\|resume}` · `DELETE /instances/{id}`⁺ |
| 实例子资源 | `GET /instances/{id}/status` · `POST /instances/{id}/scene`（`scene_config` 或 `library_scene_id`） · `POST /instances/{id}/weather` · `POST /instances/{id}/vil/calibrate` · `GET /instances/{id}/screenshot` · `GET /instances/{id}/stream` |
| 场景库（V2.5.0） | `POST /scenes/library`（operator+，同名版本自增） · `GET /scenes/library` · `GET /scenes/library/{id}` · `DELETE /scenes/library/{id}`（operator+，软删除） |
| 场景（兼容） | `POST /scenes/load` · `GET /scenes/{id}/status` · `POST /scenes/{id}/{start\|stop\|pause\|resume}` · `POST /scenes/{id}/{weather\|calibrate}` · `GET /scenes/{id}/screenshot` |
| 批量测试 | `POST /scenarios/batch`（operator+） · `GET /scenarios`（任务中心） · `GET /scenarios/{task_id}/report` |
| 资源 | `GET /maps` · `POST /maps/upload`⁺（multipart） · `GET /vehicles` · `GET /environments` · `GET /resources/gpu` · `GET /resources/quota` |
| 平台治理（V2.5.0） | `GET /execution`⁺（引擎库存/租约/运行器） · `GET /audit`⁺（审计查询） · `GET /platform/overview`⁺（平台概览） |
| 实时推送 | `WS /ws/sim/{instance_id}/status?token=<JWT>`（握手需令牌，缺失/非法 4401，越权 4403） |

> ⁺ 需 `role=admin`；`POST /instances` 需 `operator` 及以上（admin 天然满足）。完整请求/响应示例见 [`user_manual.md`](user_manual.md)。

> **限流与登录退避（V2.6.0）**：`/api/v1/sim/**` 按客户端 IP 令牌桶限流（`API_RATE_LIMIT_PER_MINUTE`，默认 1000/min；`/health*`、`/metrics`、`/ws/` 豁免，`API_RATE_LIMIT_ENABLED=false` 可关闭），超限返回 `429` + `Retry-After`；`/auth/token` 同 IP 连续失败达 `API_LOGIN_MAX_FAILED_ATTEMPTS`（默认 5）后指数退避（`API_LOGIN_BACKOFF_BASE_SECONDS` 2s 起、封顶 `API_LOGIN_BACKOFF_MAX_SECONDS` 60s），成功登录即复位。

---

## 7. 开发与测试

```bash
pytest tests -q --cov=hunter_sim --cov-report=term-missing   # 覆盖率门禁 fail_under=80
flake8 src tests                                               # Lint（配置见 setup.cfg）
mypy src                                                       # 类型检查 strict 模式
```

- **测试**：822 项单元 + 集成测试全绿（较 V2.5.0 的 776 项新增 46 项：`tests/test_rate_limit.py` 令牌桶/429+Retry-After/登录退避集成、`tests/test_metrics.py` 指标导出与路径归一化、`test_execution_plane.py` 引擎级锁并行装配、`test_runtime.py` 启动期预建单锁、`test_cloud_connector.py` 有界队列背压）；行覆盖率 **81.11%**（≥80% 门禁，`pytest --cov` 验证）。
- CARLA / ROS2 未安装的机器上，测试通过 `sys.modules` 桩注入（`tests/mocks/carla_mocks.py`）实现无引擎运行。
- 代码规范：flake8 + mypy（`setup.cfg` + `pyproject.toml`），命名遵循 PEP8，公共 API 全量类型注解。

---

## 8. 文档索引

| 文件 | 内容 |
|------|------|
| `README.md` | 项目总览与快速开始（本文件） |
| [`user_manual.md`](user_manual.md) | API 使用方法、场景/天气/标定示例、错误码 |
| [`deployment_and_operations.md`](deployment_and_operations.md) | 环境准备、部署、监控、故障排查 |
| [`release.md`](release.md) | 版本发布说明 |
| `HunterSim.ai-rules.md` | 工程约束与开发规范 |

---

## 9. 许可

本项目基于 [LICENSE](LICENSE) 发布。

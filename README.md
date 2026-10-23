# HunterSim · HUNTER SE 车辆在环（VIL）虚拟仿真平台

HunterSim 是面向 **HUNTER SE 自动驾驶底盘车** 的高保真虚拟仿真与虚实映射（VIL）平台。它以 CARLA 0.9.16 为渲染与物理引擎，以 ROS2 Humble 为传感器数据语义总线，通过 FastAPI 对外提供统一的 REST / WebSocket 控制接口，支持 **车辆在环（VIL）**、**软件在环（SIL）** 与 **数据回放（Replay）** 三种运行模式。自 V2.2.0 起额外提供 **Windows 桌面仿真客户端**（CarlaSceneX 风格，进程内直连 CARLA）与 **HunterCore 数据采集分析平台接入**。

- **车辆规格**：820×640×310 mm，整备质量 60 kg，轴距 0.46 m，后轮驱动 + 前轮阿克曼转向，最大速度 4.8 m/s。
- **当前版本**：`2.9.0`（V2.9.0 为**阶段七「代码审查缺陷修复」（Blocker + High）**：多引擎部署按端点隔离 `MapManager` / `carla.Client`（不再把所有 `load_world` 打到 base 引擎，也不跨端点共用非线程安全 Client）、`/auth/refresh` 续期回查用户表（禁用/删除/降级即时生效）、僵尸租约强制回收收紧为仅限 `stale=true` 并联动实例置 `failed` 释放端点占用、引擎重启下沉工作线程（不阻塞事件循环）、装配窗口内销毁实例的取消闸门（杜绝孤儿运行器与僵尸租约），详见 [`release.md`](release.md)）。上一版本 `2.8.0` 为**阶段六「多用户 / 调度智能化 / 引擎自愈 / 部署体验」（P2）**：平台用户表 + PBKDF2 慢哈希与角色/租户绑定（`/auth/users`、`/auth/password`，审计落到具体人）、地图感知调度（同地图优先复用空闲引擎，多引擎不再被「全局限一地图」误伤）、本地 CARLA 进程自愈看门狗（`CARLA_SUPERVISOR_*`，事件写审计）、部署预检 CLI `huntersim doctor` + `Makefile` + compose 健康依赖条件，并文档明确「单副本 + 重启恢复」为受支持形态，详见 [`release.md`](release.md)）。`2.7.0` 为**阶段五「稳定性与数据链路加固」（P1）**：JWT 吊销/滑动续期、引擎租约心跳与僵尸回收、Kafka 消费自愈与 DLQ、VIL 乱序闸门与自动环深、快照写节流、structlog 统一日志 + request_id 贯穿、端到端集成测试。更早：`2.6.0` 为**阶段四「并发安全与稳定性加固」（P0）**——运行器注册表与装配锁启动期预建、引擎级装配锁、按 IP 限流与登录退避、cloud bag 上传队列有界化、标准化 Prometheus 指标 + Grafana 预置盘；`2.5.0` 为**阶段三「平台化」**（多租户隔离与配额、服务账号 API Key、RBAC 角色层级、场景库版本化、批量任务与审计落库、平台概览）；`2.4.0` 为**阶段二「执行面分离与状态持久化」**（引擎端点库存与独占租约、实例/场景绑定落 SQLite 重启恢复、`GET /execution`）；`2.3.0` 为**阶段一「可用性止血」**（打通 REST 场景闭环、`/auth/token`、生产启动闸门、WebSocket 鉴权、实例归属校验）。功能版本 `2.2.0` 新增 **Windows 桌面仿真客户端**、**Windows 单机全容器编排**与 **HunterCore 数据接入**。
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
| 稳定性与链路加固（V2.7.0） | JWT 吊销/滑动续期（`jti` + 吊销名单）；引擎租约心跳 + 僵尸判定 + admin 强制回收（`DELETE /execution/leases/{id}`）；Kafka 消费自愈与坏消息 DLQ；VIL 乱序/去重闸门、环深自动推导、延迟 P50/P95/P99；快照写节流（状态跃迁必写）；structlog 统一日志 + request_id 贯穿 |
| 多用户与运维演进（V2.8.0） | 平台用户表 + PBKDF2-HMAC-SHA256 慢哈希（逐条存迭代次数、恒时比对、用户名枚举时序对齐），登录写 `role/tenant` 入 JWT，审计归属到具体人；**地图感知调度**（同图优先复用空闲引擎，冲突判定下沉到端点级）；**本地 CARLA 进程自愈**（看门狗退避重启 + 重启预算封顶 + 事件写审计）；`huntersim doctor` 部署预检与 `Makefile` 统一日常回路；受支持形态 = **单副本 + 重启恢复**（见 §4.10） |
| 审查缺陷修复（V2.9.0） | 多引擎**按端点隔离** `MapManager`/`carla.Client`（Blocker：此前所有端点共用 base 引擎的 Client）；`/auth/refresh` 回查用户表（禁用/删除/降级即时生效，令牌续期不再"一签永续"）；僵尸租约回收**仅限 `stale=true`**并联动实例置 `failed`（活跃租约 409）；引擎重启下沉工作线程（不阻塞事件循环）；装配-销毁竞态取消闸门（无孤儿运行器/僵尸租约） |

---

## 2. 分层架构（C/S 三层）

```
┌─────────────────────────────────────────────────────────────┐
│  接口层  api/  —— FastAPI 应用、路由、中间件、统一响应/异常      │
│    health · auth · instances · scenes · scene_library ·        │
│    scenarios · audit · platform · resources · websocket       │
├─────────────────────────────────────────────────────────────┤
│  执行面  execution/ —— 引擎端点库存与独占租约（V2.4.0）          │
│    engine_pool（端点库存/租约/地图亲和选端） · plane（装配/释放） │
├─────────────────────────────────────────────────────────────┤
│  服务层  —— 业务编排（无 CARLA/ROS2 直接依赖，可单测）           │
│    scene_runner  ·  vil_mapper  ·  sensor_sim  ·  traffic_sim  │
│    replay_service ·  eval_service ·  resource_manager          │
├─────────────────────────────────────────────────────────────┤
│  引擎层  engine/ —— CARLA 原子能力封装（依赖 carla 模块）        │
│    map_manager · weather_manager · hunter_se_vehicle ·          │
│    vehicle_blueprint_generator · coordinate_converter ·        │
│    opendrive_parser · vehicle_controller · carla_server_config  │
│    carla_supervisor（本地引擎进程自愈，V2.8.0 P2-15）           │
├─────────────────────────────────────────────────────────────┤
│  公共层  common/ —— 配置模型、领域数据类、异常、日志工具          │
│  持久化  persistence/ —— SQLite：实例/场景绑定 + API Key/场景库/  │
│    批量任务/审计/平台用户（V2.4.0 / V2.5.0 / V2.8.0）            │
│  运维    cli/ —— huntersim doctor 部署预检 / version（V2.8.0）    │
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
│   ├── execution/           # 执行面：engine_pool（端点库存/租约/地图亲和选端）, plane（装配/释放/快照）
│   ├── persistence/         # 状态持久化：state_store（实例/场景绑定 + API Key/场景库/批量任务/审计/平台用户）
│   ├── engine/              # 引擎层：CARLA 原子能力 + 环境检查 + 服务端配置 + 进程自愈 supervisor
│   ├── scene_runner/        # 场景加载、校验、状态机、事件检测、自定义场景
│   ├── vil_mapper/          # 遥测缓冲、位姿外推、坐标标定、虚实同步
│   ├── sensor_sim/          # 传感器工厂、安装位、数据转换、ROS2 桥接、录制
│   ├── traffic_sim/         # 交通流、Actor 行为、行为树、行人、触发条件
│   ├── replay_service/      # 数据加载、回放引擎、数字孪生
│   ├── eval_service/        # 评估引擎、评估报告
│   ├── resource_manager/   # 实例管理、GPU 资源池、健康监控
│   ├── gui/                 # 桌面仿真客户端（V2.2.0，PySide6 可选依赖，惰性导入）
│   ├── cli/                 # 运维 CLI（V2.8.0 P2-16）：huntersim doctor 部署预检 / version
│   └── cloud/               # HunterCore 数据接入（V2.2.0，Kafka 遥测 + MinIO bag 直传）
├── configs/                 # 预设配置：weather_profiles / sensor_configs / scene_templates / traffic_config
├── docker/                  # Dockerfile / Dockerfile.carla / docker-compose / prometheus / alerts / alertmanager / grafana
├── k8s/                     # namespace / configmap / secret / deployment（含 CARLA GPU Pod + PVC）/ service
├── hunter_assets/           # CARLA 引擎镜像构建资源（Dockerfile.carla COPY 上下文）
├── tests/                   # 单元 + 集成测试（覆盖率门禁 ≥80%）
├── Makefile                 # 一键入口（V2.8.0）：doctor / serve / test / typecheck / image / up …
├── pyproject.toml           # 依赖与工具配置（setuptools / pytest / flake8 / mypy / coverage / console script）
├── setup.cfg                # flake8 质量门禁配置
└── requirements.txt         # 运行依赖清单
```

---

## 4. 快速开始

### 4.1 环境准备

- **GPU 主机（推荐 Windows，CARLA 原生平台）**：安装 NVIDIA 驱动（≥510）、CUDA（≥11.0）、CARLA 0.9.16。
- **服务主机（Linux 或 Windows）**：Python 3.12、Kafka 3.6、（可选）ROS2 Humble。

验证运行环境（**V2.8.0 推荐**：一次跑完配置/密钥强度/端口/磁盘/GPU/CARLA/状态库，失败即非零退出）：

```bash
huntersim doctor                    # 或： python -m hunter_sim.cli.main doctor
huntersim doctor --json --strict     # 脚本消费 / 上线门禁（warn 提为 fail）
```

仅验证 GPU/CUDA/驱动/Python 环境（旧方式，仍可用）：

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

# （可选）本地 CARLA 进程自愈（V2.8.0 P2-15，仅回环端点；远程/K8s 不要开）
set CARLA_SUPERVISOR_ENABLED=true
set CARLA_SUPERVISOR_BINARY_PATH=D:/CARLA_0.9.16/CarlaUE4.exe
```

启动后访问交互式文档：<http://localhost:8080/api/v1/sim/docs>

> **Makefile 一键入口（V2.8.0 P2-16）**：`make doctor`（预检）· `make serve`（前台起 API）· `make test` / `make coverage` / `make typecheck`（质量回路）· `make image` / `make up` / `make up-carla` / `make logs`（容器）。配方不依赖 sh 专有语法（只调 `python` / `docker`），可覆盖变量 `PYTHON`（Windows 默认 `.venv/Scripts/python.exe`）/ `COMPOSE_FILE` / `IMAGE_TAG`；**注意 `make` 本体为 Unix 工具，Windows 不自带**（需另装 GNU make 或在 Git Bash / WSL 内执行），未安装时直接执行配方里的等价命令即可。

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

> **健康依赖条件（V2.8.0 P2-16）**：Kafka / Prometheus / Alertmanager / Grafana 均带 healthcheck，`sim-service` 以 `depends_on.kafka.condition: service_healthy` 等 Kafka 就绪后再起，Grafana 等 Prometheus 健康后再起（避免数据源首连失败反复重试）；镜像内已装 `huntersim` console script（V2.8.0 起 `Dockerfile` 已将 `--user` 脚本目录 `/home/hunter/.local/bin` 加入 `PATH`），预检可直接 `docker compose exec sim-service huntersim doctor --strict`；沿用旧镜像时改用 `python -m hunter_sim.cli.main doctor`。

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

### 4.9 多用户与引擎运维（V2.8.0）

```bash
BASE=http://localhost:8080/api/v1/sim
# 内置管理员（引导账号）先换 Token
TOKEN=$(curl -s -X POST $BASE/auth/token -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r .data.access_token)

# 1) 建平台用户（admin；口令 PBKDF2 摘要入库，不再依赖明文配置）
curl -X POST $BASE/auth/users -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username":"zhangsan","password":"Ops-Team-2026","role":"operator","tenant":"team-a"}'

# 2) 用户登录（role/tenant 写入 JWT，审计落到具体人）· 自助改密 · 管理面
curl -X POST $BASE/auth/token -H "Content-Type: application/json" \
  -d '{"username":"zhangsan","password":"Ops-Team-2026"}'
curl "$BASE/auth/users?tenant=team-a" -H "Authorization: Bearer $TOKEN"
curl -X PATCH $BASE/auth/users/zhangsan -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"disabled":true}'
curl -X POST $BASE/auth/password -H "Authorization: Bearer $ZHANGSAN_TOKEN" -H "Content-Type: application/json" \
  -d '{"old_password":"Ops-Team-2026","new_password":"Ops-Team-2027"}'

# 3) 执行面：地图亲和调度与 CARLA 进程托管状态（admin）
curl "$BASE/execution" -H "Authorization: Bearer $TOKEN" | jq '.data | {engines, carla_supervisor}'
curl -X POST "$BASE/execution/engines/restart?endpoint=127.0.0.1:2000" -H "Authorization: Bearer $TOKEN"
```

> **用户体系边界**：需 `STATE_ENABLED=true`（无状态库时返回 `503`）；`PLATFORM_USERS_ENABLED=false` 时管理接口整体 `403`。口令策略：长度 ≥ `PLATFORM_MIN_PASSWORD_LENGTH`（默认 10）、同时含字母与数字、不得等于或包含登录名（不分大小写）。内置管理员**不入库**，作为引导账号保留（用户表未命中时回退明文比对，既有脚本不变）；接口不可修改它（`409`），且最后一名可用 admin 不得降级/禁用/删除（`409` 锁死保护）。

> **调度与自愈语义（V2.8.0）**：配置 `EXECUTION_ENGINE_ENDPOINTS` 后，创建实例优先复用**已加载目标地图的空闲引擎**（省一次 `load_world`），其次选未加载过地图的空闲引擎；地图冲突判定从「全局限一地图」下沉为「**同端点互斥**」，因此不同引擎可同时跑不同地图。`GET /execution` 的空闲端点也展示最后已知地图与 `map_affinity_enabled`。进程自愈仅托管**回环端点**（`127.*`/`localhost`/`::1`），远程引擎由其自身编排器负责；`POST /execution/engines/restart` 会重置世界，该端点上的场景需重新装配。

### 4.10 运行形态与容量边界（V2.8.0 P2-17）

**受支持形态：单副本 + 重启恢复**（本文档、compose 与 k8s 清单均按此形态交付与验证）：

| 项 | 口径 |
|------|------|
| API 服务副本数 | **1**（`uvicorn --workers 1`；k8s `replicas: 1`） |
| 状态库 | SQLite（WAL + 单写者）；k8s 形态需 **RWO 卷**（`STATE_ENABLED=true`） |
| 进程内状态 | IP 令牌桶限流、登录退避计数、JWT 吊销名单、引擎租约与地图记忆、CARLA 进程托管、场景运行器注册表 |
| 重启恢复 | 实例与场景绑定从 SQLite 恢复（`STATE_RECOVER_INSTANCES` / `STATE_RESTORE_SCENES`），恢复后场景回到 `ready` 可重新启动 |
| 水平扩展 | **不受支持**：多副本需先把上述状态外置（PostgreSQL/Redis + 引擎注册中心与分布式租约），属长期项 |
| 多引擎并行 | **受支持**：单服务副本可挂多个 CARLA 引擎端点（`EXECUTION_ENGINE_ENDPOINTS`），引擎级装配锁支撑并行 `load_world` |

> 如需多副本活性，请前置满足：共享状态库、共享限流/吊销名单、租约改为分布式抢占；否则会出现两个副本各自认为拥有同一引擎端点的脑裂情况。`huntersim doctor` 的 `state_store` 与 `users` 检查项会提示当前形态是否具备持久化能力。

---

## 5. 配置

配置优先级：**环境变量 > `.env` / 配置文件 > 默认值**。所有配置由 `common/models.py` 的 pydantic `BaseSettings` 聚合，按前缀分组：

| 前缀 | 组 | 关键项（默认值） |
|------|------|------------------|
| `HUNTER_SIM_` | 全局 | `env`(dev) · `log_level`(INFO) · **V2.7.0**：`log_json`(false，JSON 行渲染) · `log_file`(空=仅 stdout，非空启用轮转文件) · `log_max_bytes`(10485760) · `log_backup_count`(5) |
| `CARLA_` | CARLA 连接 | `host`(127.0.0.1) · `rpc_port`(2000) · `stream_port`(2001) · `tm_port`(8000) · `fixed_delta_seconds`(0.02) · **V2.8.0**：`supervisor_enabled`(false，仅托管回环端点) · `supervisor_binary_path`(空=只观测不自愈) · `supervisor_extra_args` · `supervisor_max_restarts`(3/300s 窗口) · `supervisor_restart_backoff_seconds`(5) · `supervisor_ready_timeout_seconds`(60) · `supervisor_graceful_shutdown_seconds`(10) |
| `KAFKA_` | 数据总线 | `bootstrap_servers`(localhost:9092) · `telemetry_topic`(telemetry_clean) · **V2.7.0**：`enable_self_heal`(true) · `rebuild_after_errors`(10) · `dlq_topic`(空=不转发) |
| `VIL_` | 虚实映射 | `extrapolation_ms`(150) · `max_data_latency_ms`(500) · `buffer_max_frames`(10) · `calibration_*` · **V2.7.0**：`ordering_guard_enabled`(true) · `telemetry_frequency_hz`(10.0) · `auto_buffer_depth`(true，环深按窗口×频率推导) |
| `API_` | 接口服务 | `host`(0.0.0.0) · `port`(8080) · `jwt_secret` · `max_concurrent_instances`(2) · `stream_base_url`(webrtc://localhost:8080) · **V2.6.0**：`rate_limit_enabled`(true) · `rate_limit_per_minute`(1000) · `login_max_failed_attempts`(5) · `login_backoff_base_seconds`(2.0) · `login_backoff_max_seconds`(60) · **V2.7.0**：`jwt_revocation_enabled`(true) · `jwt_refresh_enabled`(true)，均需状态库 |
| `RESOURCE_` | 资源管理 | `instance_max_lifetime_seconds`(7200) · `max_retry_count`(3) · `max_instances_per_user`(5) · `max_instances_total`(50) · `gpu_memory_warning_threshold`(0.90) · `resources_dir`(resources) |
| `EXECUTION_` | 执行面（V2.4.0） | `engine_endpoints`(空=单引擎) · `max_engines`(8) · `restore_scene_on_assemble`(true) · **V2.7.0**：`lease_heartbeat_enabled`(true) · `lease_ttl_seconds`(120，超时未续租判定僵尸) · **V2.8.0**：同图优先复用空闲引擎（无新开关，自动生效） |
| `STATE_` | 状态持久化（V2.4.0） | `enabled`(false，compose/K8s 为 true) · `db_path`(data/state/hunter_sim.db) · `recover_instances`(true) · `restore_scenes`(true) · `busy_timeout_seconds`(5.0) · **V2.7.0**：`snapshot_write_interval_seconds`(1.0，同状态落库最小间隔；跃迁必写；0 不节流) |
| `PLATFORM_` | 平台化（V2.5.0） | `default_tenant`(default) · `max_instances_per_tenant`(20) · `api_keys_enabled`(true) · `audit_enabled`(true) · **V2.8.0**：`users_enabled`(true，需状态库) · `min_password_length`(10) · `password_hash_iterations`(100000，逐条入库，调高不影响存量用户) |
| `HUNTER_CORE_` | HunterCore 接入 | `enabled`(False) · `vehicle_id`(SIM_HUNTER_SE_001) · `kafka_bootstrap`(192.168.31.35:9092) · `security_protocol`(SASL_SSL) · `sasl_mechanism`(SCRAM-SHA-512) · `sasl_username/password` · `gateway_url`(http://192.168.31.35:8080) · `jwt_token` · `rosbag_bucket`(hunter-rosbag) · **V2.6.0**：`upload_queue_max_size`(32，bag 上传后台队列容量，队满丢最旧) |
| `GUI_` | 桌面客户端 | `theme`(dark) · `default_map`(Town03) · `stream_fps`(30) · `scenario_runner_path`(None) |

预设资源文件位于 `configs/`：8 种天气环境、7 类传感器、7 个场景模板、5 套交通流档位。

---

## 6. API 一览

统一前缀 `/api/v1/sim`，响应体 `{code, message, data}`。先调 `POST /auth/token` 换取 JWT（HS256，密钥 `API_JWT_SECRET`）；其余接口（除健康检查与 WebSocket 握手鉴权外）均需 `Authorization: Bearer <JWT>`，服务间调用可用 `X-API-Key`（WebSocket 握手仅支持 JWT）。角色层级 `guest < viewer < operator < admin`：**创建实例需 `operator` 及以上；销毁实例、地图上传与管理面（API Key/审计/平台概览/执行面）需 `admin`**。

| 分组 | 方法与路径 |
|------|-----------|
| 认证 | `POST /auth/token`（换取 JWT；V2.8.0 先校用户表再回退内置管理员） · `POST /auth/refresh`（V2.7.0，滑动续期+旧 Token 轮换吊销） · `POST /auth/revoke`（V2.7.0，退出登录） · `POST /auth/password`（V2.8.0，自助改密） · `POST /auth/api-keys`⁺（签发服务账号） · `GET /auth/api-keys`⁺ · `DELETE /auth/api-keys/{key_id}`⁺ |
| 用户管理（V2.8.0） | `POST /auth/users`⁺（创建） · `GET /auth/users`⁺（可按 `tenant`/`include_disabled` 过滤） · `GET /auth/users/{username}`⁺ · `PATCH /auth/users/{username}`⁺（角色/租户/重置口令/禁用） · `DELETE /auth/users/{username}`⁺ |
| 健康/指标 | `GET /health` · `GET /health/live` · `GET /health/ready` · `GET /health/performance-targets` · `GET /metrics` |
| 实例 | `POST /instances`（operator+） · `GET /instances` · `GET /instances/{id}` · `POST /instances/{id}/{start\|stop\|pause\|resume}` · `DELETE /instances/{id}`⁺ |
| 实例子资源 | `GET /instances/{id}/status` · `POST /instances/{id}/scene`（`scene_config` 或 `library_scene_id`） · `POST /instances/{id}/weather` · `POST /instances/{id}/vil/calibrate` · `GET /instances/{id}/screenshot` · `GET /instances/{id}/stream` |
| 场景库（V2.5.0） | `POST /scenes/library`（operator+，同名版本自增） · `GET /scenes/library` · `GET /scenes/library/{id}` · `DELETE /scenes/library/{id}`（operator+，软删除） |
| 场景（兼容） | `POST /scenes/load` · `GET /scenes/{id}/status` · `POST /scenes/{id}/{start\|stop\|pause\|resume}` · `POST /scenes/{id}/{weather\|calibrate}` · `GET /scenes/{id}/screenshot` |
| 批量测试 | `POST /scenarios/batch`（operator+） · `GET /scenarios`（任务中心） · `GET /scenarios/{task_id}/report` |
| 资源 | `GET /maps` · `POST /maps/upload`⁺（multipart） · `GET /vehicles` · `GET /environments` · `GET /resources/gpu` · `GET /resources/quota` |
| 平台治理（V2.5.0） | `GET /execution`⁺（引擎库存/租约/运行器，V2.7.0 含 `stale` 僵尸标志，V2.8.0 含端点 `map_id` 与 `carla_supervisor` 快照） · `DELETE /execution/leases/{instance_id}`⁺（V2.7.0，强制回收僵尸租约；V2.9.0 起仅接受 `stale=true` 的租约且回收后实例置 `failed`） · `POST /execution/engines/restart?endpoint=`⁺（V2.8.0，手动重启托管进程） · `GET /audit`⁺（审计查询） · `GET /platform/overview`⁺（平台概览） |
| 实时推送 | `WS /ws/sim/{instance_id}/status?token=<JWT>`（握手需令牌，缺失/非法 4401，越权 4403） |

> ⁺ 需 `role=admin`；`POST /instances` 需 `operator` 及以上（admin 天然满足）。完整请求/响应示例见 [`user_manual.md`](user_manual.md)。

> **限流与登录退避（V2.6.0）**：`/api/v1/sim/**` 按客户端 IP 令牌桶限流（`API_RATE_LIMIT_PER_MINUTE`，默认 1000/min；`/health*`、`/metrics`、`/ws/` 豁免，`API_RATE_LIMIT_ENABLED=false` 可关闭），超限返回 `429` + `Retry-After`；`/auth/token` 同 IP 连续失败达 `API_LOGIN_MAX_FAILED_ATTEMPTS`（默认 5）后指数退避（`API_LOGIN_BACKOFF_BASE_SECONDS` 2s 起、封顶 `API_LOGIN_BACKOFF_MAX_SECONDS` 60s），成功登录即复位。

> **令牌生命周期与僵尸回收（V2.7.0）**：`POST /auth/refresh` 用有效 Token 换新 Token 并就地吊销旧 Token（轮换），`POST /auth/revoke` 立即吊销当前 Token，均依赖状态库（`STATE_ENABLED=true`）；引擎租约心跳超 `EXECUTION_LEASE_TTL_SECONDS` 未续在 `GET /execution` 标记 `stale=true`，admin 可 `DELETE /execution/leases/{instance_id}` 强制回收（**V2.9.0**：仅接受心跳超时的 `stale=true` 租约，活跃租约返回 `409`；回收后实例同步置 `failed`，端点不会被控制面继续计为已分配）；日志携带 `request_id` 贯穿全链路，客户端可传 `X-Request-ID` 复用自定义追踪号。

> **多用户与引擎自愈（V2.8.0）**：`/auth/users*` 仅限 `admin`（无状态库 `503`，`PLATFORM_USERS_ENABLED=false` 则 `403`），对外视图不回显摘要/salt/迭代次数；`POST /auth/password` 需当前有效 Token 并校对旧口令（错误 `401`，内置账号 `409`）。用户表未命中时回退内置管理员，既有凭据与脚本不变；角色/租户写入 JWT，审计可按人回溯。`POST /execution/engines/restart?endpoint=127.0.0.1:2000`（admin）仅对**托管回环端点**生效：未启用托管 `409`、仅观测（未配 `CARLA_SUPERVISOR_BINARY_PATH`）`409`、端点不受管 `404`。

---

## 7. 开发与测试

```bash
make test          # 等价于： pytest tests -q（含覆盖率门禁 fail_under=80）
make coverage      # pytest -q --cov=src/hunter_sim --cov-report=term-missing
make typecheck     # mypy strict
make doctor        # 部署预检（V2.8.0）
flake8 src tests   # Lint（配置见 setup.cfg）
```

- **测试**：1008 项单元 + 集成测试全绿（较 V2.7.0 的 867 项新增 141 项：`tests/test_users_router.py` 用户 CRUD/口令策略/锁死保护/自助改密/JWT 声明、`tests/test_carla_supervisor.py` 状态机与重启预算、`tests/test_doctor_cli.py` 11 个预检项与退出码、`tests/test_map_scheduling.py` 地图亲和与双重占用防护）；行覆盖率 **84.3%**（≥80% 门禁，`pytest --cov` 验证）。
- **类型**：`mypy src`（strict）**113 个源文件无问题**。
- CARLA / ROS2 未安装的机器上，测试通过 `sys.modules` 桩注入（`tests/mocks/carla_mocks.py`）实现无引擎运行。
- 代码规范：flake8 + mypy（`setup.cfg` + `pyproject.toml`），命名遵循 PEP8，公共 API 全量类型注解。

---

## 8. 文档索引

| 文件 | 内容 |
|------|------|
| `README.md` | 项目总览与快速开始（本文件） |
| [`user_manual.md`](user_manual.md) | API 使用方法、场景/天气/标定示例、错误码 |
| [`deployment_and_operations.md`](deployment_and_operations.md) | 环境准备、部署（含 `huntersim doctor` 预检）、监控、故障排查、受支持部署形态与边界 |
| [`release.md`](release.md) | 版本发布说明 |
| `HunterSim.ai-rules.md` | 工程约束与开发规范 |

---

## 9. 许可

本项目基于 [LICENSE](LICENSE) 发布。

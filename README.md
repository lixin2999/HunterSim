# HunterSim · HUNTER SE 车辆在环（VIL）虚拟仿真平台

HunterSim 是面向 **HUNTER SE 自动驾驶底盘车** 的高保真虚拟仿真与虚实映射（VIL）平台。它以 CARLA 0.9.16 为渲染与物理引擎，以 ROS2 Humble 为传感器数据语义总线，通过 FastAPI 对外提供统一的 REST / WebSocket 控制接口，支持 **车辆在环（VIL）**、**软件在环（SIL）** 与 **数据回放（Replay）** 三种运行模式。

- **车辆规格**：820×640×310 mm，整备质量 60 kg，轴距 0.46 m，后轮驱动 + 前轮阿克曼转向，最大速度 4.8 m/s。
- **当前版本**：`2.1.1`（对齐《Carla 仿真系统详细设计文档》V4.0，并完成 PR 审查阻塞问题修复（A–F）与建议级问题修复（G–T）：天气端到端闭环、上传安全与配额账本收敛、事件检测边沿闩锁、并发端口递增分配、lifespan 生产装配、评估步长推导、Alertmanager 接入、CARLA 启动参数修正等。详见 [`release.md`](release.md)）
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
| 安全治理 | JWT 角色分级（创建/销毁实例与地图上传需 admin）、车辆模型白名单、场景安全审核、地图上传审核（含路径穿越双重校验）、审计日志 |

---

## 2. 分层架构（C/S 三层）

```
┌─────────────────────────────────────────────────────────────┐
│  接口层  api/  —— FastAPI 应用、路由、中间件、统一响应/异常      │
│    health · instances · scenes · resources · websocket        │
├─────────────────────────────────────────────────────────────┤
│  服务层  —— 业务编排（无 CARLA/ROS2 直接依赖，可单测）           │
│    scene_runner  ·  vil_mapper  ·  sensor_sim  ·  traffic_sim  │
│    replay_service ·  eval_service ·  resource_manager          │
├─────────────────────────────────────────────────────────────┤
│  引擎层  engine/ —— CARLA 原子能力封装（依赖 carla 模块）        │
│    map_manager · weather_manager · hunter_se_vehicle ·          │
│    vehicle_blueprint_generator · coordinate_converter ·         │
│    opendrive_parser · vehicle_controller · carla_server_config  │
├─────────────────────────────────────────────────────────────┤
│  公共层  common/ —— 配置模型、领域数据类、异常、日志工具          │
└─────────────────────────────────────────────────────────────┘
```

`common/` 为所有层的共享基础：`models.py`（pydantic 配置与数据类）、`exceptions.py`（分层异常体系）、`utils.py`（日志与工具）。

---

## 3. 目录结构

```
HunterSim/
├── src/hunter_sim/          # 主包（见下方模块职责）
│   ├── api/                 # 接口层：main, deps, error_handlers, middleware, models, routers
│   ├── common/              # 公共层：models, exceptions, utils
│   ├── engine/              # 引擎层：CARLA 原子能力 + 环境检查 + 服务端配置
│   ├── scene_runner/        # 场景加载、校验、状态机、事件检测、自定义场景
│   ├── vil_mapper/          # 遥测缓冲、位姿外推、坐标标定、虚实同步
│   ├── sensor_sim/          # 传感器工厂、安装位、数据转换、ROS2 桥接、录制
│   ├── traffic_sim/         # 交通流、Actor 行为、行为树、行人、触发条件
│   ├── replay_service/      # 数据加载、回放引擎、数字孪生
│   ├── eval_service/        # 评估引擎、评估报告
│   └── resource_manager/    # 实例管理、GPU 资源池、健康监控
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
```

启动后访问交互式文档：<http://localhost:8080/api/v1/sim/docs>

### 4.3 容器化 / 编排

```bash
# 依赖编排（Kafka + 仿真服务 + Prometheus + Grafana，CARLA 跑在宿主机）
docker-compose -f docker/docker-compose.yml up -d

# Linux + NVIDIA GPU 主机可额外启用容器化 CARLA 引擎（profile，需 NVIDIA Container Toolkit）
docker-compose -f docker/docker-compose.yml --profile carla up -d

# Kubernetes（需 NVIDIA GPU Operator；含 sim-service + carla-engine GPU Pod + 共享 PVC）
kubectl apply -f k8s/
```

详见 [`deployment_and_operations.md`](deployment_and_operations.md)。

---

## 5. 配置

配置优先级：**环境变量 > `.env` / 配置文件 > 默认值**。所有配置由 `common/models.py` 的 pydantic `BaseSettings` 聚合，按前缀分组：

| 前缀 | 组 | 关键项（默认值） |
|------|------|------------------|
| `HUNTER_SIM_` | 全局 | `env`(dev) · `log_level`(INFO) |
| `CARLA_` | CARLA 连接 | `host`(127.0.0.1) · `rpc_port`(2000) · `stream_port`(2001) · `tm_port`(8000) · `fixed_delta_seconds`(0.02) |
| `KAFKA_` | 数据总线 | `bootstrap_servers`(localhost:9092) · `telemetry_topic`(telemetry_clean) |
| `VIL_` | 虚实映射 | `extrapolation_ms`(150) · `max_data_latency_ms`(500) · `buffer_max_frames`(10) · `calibration_*` |
| `API_` | 接口服务 | `host`(0.0.0.0) · `port`(8080) · `jwt_secret` · `max_concurrent_instances`(2) · `stream_base_url`(webrtc://localhost:8080) |
| `RESOURCE_` | 资源管理 | `instance_max_lifetime_seconds`(7200) · `max_retry_count`(3) · `max_instances_per_user`(5) · `max_instances_total`(50) · `gpu_memory_warning_threshold`(0.90) · `resources_dir`(resources) |

预设资源文件位于 `configs/`：8 种天气环境、7 类传感器、7 个场景模板、5 套交通流档位。

---

## 6. API 一览

统一前缀 `/api/v1/sim`，响应体 `{code, message, data}`。除健康检查与 WebSocket 外均需 `Authorization: Bearer <JWT>`（HS256，密钥 `API_JWT_SECRET`）；**创建/销毁实例与地图上传额外要求 Token 载荷 `role=admin`**（§14.2）。

| 分组 | 方法与路径 |
|------|-----------|
| 健康/指标 | `GET /health` · `GET /health/live` · `GET /health/ready` · `GET /health/performance-targets` · `GET /metrics` |
| 实例 | `POST /instances`⁺ · `GET /instances` · `GET /instances/{id}` · `POST /instances/{id}/{start\|stop\|pause\|resume}` · `DELETE /instances/{id}`⁺（⁺ 需 admin） |
| 实例子资源 | `GET /instances/{id}/status` · `POST /instances/{id}/scene` · `POST /instances/{id}/weather` · `POST /instances/{id}/vil/calibrate` · `GET /instances/{id}/screenshot` · `GET /instances/{id}/stream` |
| 场景（兼容） | `POST /scenes/load` · `GET /scenes/{id}/status` · `POST /scenes/{id}/{start\|stop\|pause\|resume}` · `POST /scenes/{id}/{weather\|calibrate}` · `GET /scenes/{id}/screenshot` |
| 批量测试 | `POST /scenarios/batch` · `GET /scenarios/{task_id}/report` |
| 资源 | `GET /maps` · `POST /maps/upload`⁺（multipart） · `GET /vehicles` · `GET /environments` · `GET /resources/gpu` · `GET /resources/quota` |
| 实时推送 | `WS /ws/sim/{instance_id}/status` |

完整请求/响应示例见 [`user_manual.md`](user_manual.md)。

---

## 7. 开发与测试

```bash
pytest tests -q --cov=hunter_sim --cov-report=term-missing   # 覆盖率门禁 fail_under=80
flake8 src tests                                               # Lint（配置见 setup.cfg）
mypy src                                                       # 类型检查 strict 模式
```

- **测试**：593 项单元 + 集成测试全绿；覆盖率门禁 `fail_under=80`。
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

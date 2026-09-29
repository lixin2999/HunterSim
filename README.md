# HunterSim

基于 [CARLA](https://carla.org/) 的自动驾驶仿真**数据采集 + 算法分析 + 场景评估**一体化系统。

- 语言：Python 3.12
- 仿真引擎：CARLA 0.9.16（以独立进程运行，通过 `carla.Client` 连接）
- 架构：严格五层分层（L1 仿真核心 → L5 应用调度），上层依赖下层，禁止反向依赖

## 分层架构

| 层 | 包 | 职责 |
|----|----|------|
| L1 | `hunter_sim.simulation` | CARLA 连接 / 车辆控制 / 场景管理 |
| L2 | `hunter_sim.acquisition` | 传感器管理 / 数据缓冲 / 写入器 |
| L3 | `hunter_sim.processing` | 时间同步 / 数据转换 / 算法对接 |
| L4 | `hunter_sim.evaluation` | 指标计算 / 报告生成 / 数据回放 |
| L5 | `hunter_sim.app` | CLI / 场景编排 / 任务调度 |
| 基础 | `hunter_sim.core` | 契约 / 事件 / 配置 / 日志 / 异常 |

## 开发状态

**V0.1.0 已完成严格五层架构（L1–L5）端到端贯通**，质量门禁全绿（`ruff` / `mypy --strict` / `pytest`，覆盖率 98%）：

- `core/contracts.py` — 不可变数据契约（`VehicleState` / `CameraFrame` / `LidarFrame` / `SynchronizedFrame` 等）
- `core/events.py` — 事件类型 + `core/event_bus.py` 线程安全的进程内事件总线
- `core/protocols.py` — 跨层 `Protocol`（`EventBus`）
- `core/config.py` — pydantic 配置模型 + YAML/JSON 加载器（含仿真步进模式 `simulation_mode`）
- `core/logging.py` — loguru 结构化日志与 `run_id`/`frame_id` 上下文
- `core/exceptions.py` — 统一异常体系
- `container.py` — 轻量依赖注入容器
- `simulation/` — CARLA 连接管理（指数退避重连）、主车控制、场景状态机与交通流生成
- `acquisition/` — 传感器生命周期、环形缓冲（背压 + 跨线程投递）、异步原子写入器
- `processing/` — 原始测量→契约帧转换、最近邻时间同步、数组清洗去噪
- `evaluation/` — 轨迹/舒适/安全/覆盖指标、报告生成与图表渲染、数据回放
- `app/` — 采集调度器、运行编排器、组合根装配容器、惰性 `typer` CLI

详见《发布说明》（`release.md`）与《用户手册》（`docs/user_manual.md`）。

## 仿真模式（同步 / 异步）

CARLA 世界步进模式在 `scenario.simulation_mode` 中声明，采集启动时由 `ScenarioManagerImpl` 自动写入
`carla.WorldSettings`：

- **`synchronous`（默认，VIL 实时）**：仿真由客户端 `world.tick()` 驱动，固定步长（`fixed_delta_seconds`，
  为 `null` 时由 `tick_rate` 推算），时序严格对齐且可复现——数据采集用它。
- **`asynchronous`（回放 / SIL）**：`fixed_delta_seconds=None` 可变步长，按真实时间自动推进——在线 SIL/交互调试用它。
- 物理子步（`substepping` / `max_substep_delta_time` / `max_substeps`）保证高步长下的仿真稳定性。

CARLA Server（0.9.16）的启动参数与模式选型详见《部署运维手册》§3.1（`docs/deployment_and_operations.md`）。

## 环境准备

```bash
# 安装 uv 后（本项目使用 uv 管理）
uv sync --group dev          # 创建 3.12 虚拟环境并安装依赖

uv run ruff check .          # lint
uv run ruff format .         # 格式化
uv run mypy                  # 严格类型检查
uv run pytest                # 运行测试
```

## 目录结构

```
src/hunter_sim/     # 源码（src 布局）
tests/              # 单元 / 集成测试（CARLA 全部 mock）
configs/            # 场景与传感器配置示例
data/runs/          # 运行数据输出（gitignore）
```

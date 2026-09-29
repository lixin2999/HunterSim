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

当前完成 **项目脚手架 + `core` 基础层**：

- `core/contracts.py` — 不可变数据契约（`VehicleState` / `CameraFrame` / `LidarFrame` / `SynchronizedFrame` 等）
- `core/events.py` — 事件类型 + `core/event_bus.py` 线程安全的进程内事件总线
- `core/protocols.py` — 跨层 `Protocol`（`EventBus`）
- `core/config.py` — pydantic 配置模型 + YAML/JSON 加载器
- `core/logging.py` — loguru 结构化日志与 `run_id`/`frame_id` 上下文
- `core/exceptions.py` — 统一异常体系
- `container.py` — 轻量依赖注入容器

L1–L5 各层为占位，按开发提示词 §11 的任务序列增量实现。

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

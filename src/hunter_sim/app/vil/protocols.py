"""VIL 虚实映射模块 Protocol 契约（模块 4）。

依赖方向：
- 面向 L5 编排器 / CLI 暴露 :class:`VILEngine`；
- 内部各子模块（消费 / 映射 / 同步 / 可视化 / 控制）通过下列 Protocol 解耦，
  实现类以 ``Impl`` 结尾，遵循项目"契约先行 + Mock 隔离"实践。

CARLA 原生类型不出现在契约签名中，位姿 / 向量等以本层已有的 :class:`Transform`
/ :class:`Vector3D` 表达（复用 L1 数据模型）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hunter_sim.app.vil.models import VehicleTelemetry, VILCalibration
from hunter_sim.simulation.models import Transform


@runtime_checkable
class TelemetrySource(Protocol):
    """实车遥测数据源抽象（Kafka / 回放文件 / Mock 数据源均可）。"""

    async def start(self) -> None:
        """启动数据源（建立连接 / 后台线程等）。"""
        ...

    async def poll_latest(self) -> VehicleTelemetry | None:
        """返回当前缓存的最新一帧遥测；无数据时返回 ``None``。"""
        ...

    async def stop(self) -> None:
        """停止数据源并释放资源（幂等）。"""
        ...


@runtime_checkable
class CoordinateMapper(Protocol):
    """实车 odom 坐标系 → CARLA 地图坐标系的映射契约（§4.3）。"""

    @property
    def calibration(self) -> VILCalibration:
        """当前标定（起始位姿）。"""
        ...

    def odom_to_map(
        self, x_odom: float, y_odom: float, yaw_odom: float
    ) -> tuple[float, float, float]:
        """将车辆位姿从 odom 系转换到地图系。

        Args:
            x_odom: 纵向位移（米，odom）。
            y_odom: 横向位移（米，odom）。
            yaw_odom: 航向角（弧度，odom 右手系）。

        Returns:
            ``(x_map, y_map, yaw_map)``，单位米 / 弧度（CARLA 左手系定义）。
        """
        ...

    def object_to_map(self, x_odom: float, y_odom: float) -> tuple[float, float]:
        """将感知目标中心点从 odom 系转换到地图系（不做 yaw 映射，用于绘制包围盒）。"""
        ...

    def heading_to_map(self, heading_odom: float) -> float:
        """将目标朝向从 odom 系转换到地图系（右手 → 左手：取反）。"""
        ...


@runtime_checkable
class SyncController(Protocol):
    """时间同步与延迟补偿契约（§4.5）。"""

    def should_pause(self, delay_ms: float) -> bool:
        """延迟超过阈值→应暂停仿真（默认 >500ms）。"""
        ...

    def should_extrapolate(self, delay_ms: float) -> bool:
        """延迟超过阈值→使用最近数据外推（默认 >50ms）。"""
        ...

    def compensate(
        self, telemetry: VehicleTelemetry, elapsed_s: float
    ) -> VehicleTelemetry:
        """基于速度与角速度将位姿外推至当前时刻。

        Args:
            telemetry: 原始遥测帧。
            elapsed_s: 距数据时间戳的实际经过时间（秒）。

        Returns:
            外推后的新 :class:`VehicleTelemetry`（不可变），原对象保持不变。
        """
        ...


@runtime_checkable
class StateSynchronizer(Protocol):
    """虚实状态同步契约（§4.4.1）。"""

    async def sync(self, telemetry: VehicleTelemetry) -> Transform:
        """将实车状态注入虚拟车辆（位姿 + 线速度 + 角速度）。

        Args:
            telemetry: 待同步的实车帧。

        Returns:
            实际下发的地图系目标位姿（供可视化叠加使用）。
        """
        ...


@runtime_checkable
class VILVisualizer(Protocol):
    """VIL 状态与感知叠加可视化契约（§4.4.2 / §4.4.3）。"""

    def draw_vehicle_status(
        self, telemetry: VehicleTelemetry, map_pose: Transform
    ) -> None:
        """绘制速度、行为状态等浮动文字（不做阻塞）。"""
        ...

    def draw_perception_objects(self, telemetry: VehicleTelemetry, ego_map_pose: Transform) -> None:
        """在地图中叠加绘制感知到的目标包围盒（绿色半透明）。"""
        ...

    def draw_planning_trajectory(
        self, telemetry: VehicleTelemetry, ego_map_pose: Transform
    ) -> None:
        """在地图中绘制规划轨迹（彩色线段序列）。"""
        ...


@runtime_checkable
class VILEngine(Protocol):
    """VIL 引擎顶层契约（面向 CLI / 编排器）。"""

    async def start(self) -> None:
        """启动遥测消费 + 场景 + 主循环准备。"""
        ...

    async def step(self) -> None:
        """推进一步：等待数据 → 同步虚拟车 → 场景 tick → 可视化。"""
        ...

    async def run(self, *, max_ticks: int | None = None) -> int:
        """持续步进直至 ``max_ticks`` 或停止，返回实际 tick 数。"""
        ...

    async def stop(self) -> None:
        """停止引擎并释放资源（幂等）。"""
        ...


__all__ = [
    "CoordinateMapper",
    "StateSynchronizer",
    "SyncController",
    "TelemetrySource",
    "VILEngine",
    "VILVisualizer",
]

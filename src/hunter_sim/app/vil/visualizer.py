"""模块 4.4.2 / 4.4.3：VIL 可视化叠加（同步阻塞调用 → ``asyncio.to_thread``）.

CARLA 提供两种"轻"绘制方式：

- ``carla.World.debug.draw_box`` / ``draw_line`` / ``draw_arrow``：绘制几何图元，
  支持 ``color`` (RGBA) 与 ``life_time``；
- ``carla.World.debug.draw_string``：在指定位置绘制浮动文字。

本实现仅通过 :class:`CarlaConnectionManager` 获取 ``world``，不持有 carla 对象；
每个绘制方法都同步返回（调用方可选择是否 await），内部使用 ``to_thread`` 避免
阻塞事件循环。为便于单测，绘制入口以 :class:`WorldDebugProvider` 抽象 world 提供
方；单测可注入 mock。

注意：需求 §4.4.2 的"转向灯 / 刹车灯 / 前轮转向角"依赖车辆蓝图支持（CARLA 内置
蓝图通常不开放这些通道），本实现覆盖**文字 + 感知框 + 轨迹线**三类可视化，其它
表现交由上游渲染层处理。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Protocol, runtime_checkable

from hunter_sim.app.vil.models import VehicleTelemetry
from hunter_sim.app.vil.protocols import CoordinateMapper
from hunter_sim.simulation.models import Transform
from hunter_sim.simulation.protocols import CarlaConnectionManager

# 感知目标框：绿色半透明；life_time 与 §4.4.3 一致（0.1s）。
_OBJECT_COLOR = (0.0, 1.0, 0.0, 0.5)
_OBJECT_LIFE_TIME = 0.1
# 轨迹线：蓝→绿渐变；life_time 略长以便观察。
_TRAJ_LIFE_TIME = 0.2
# 状态文字：黄色。
_TEXT_COLOR = (1.0, 1.0, 0.0, 1.0)
_TEXT_HEIGHT = 0.25
_TEXT_LIFE_TIME = 0.15


@runtime_checkable
class _DebugDrawer(Protocol):
    """CARLA ``world.debug`` 子集（便于 mock）。"""

    def draw_string(
        self,
        location: Any,
        text: str,
        color: Any,
        life_time: float,
        *,
        thickness: float = ...,
        **kwargs: Any,
    ) -> None: ...

    def draw_box(
        self,
        center: Any,
        rotation: Any,
        extent: Any,
        color: Any,
        life_time: float,
        **kwargs: Any,
    ) -> None: ...

    def draw_line(
        self,
        begin: Any,
        end: Any,
        color: Any,
        life_time: float,
        *,
        thickness: float = ...,
        **kwargs: Any,
    ) -> None: ...


class CARLAVisualizerImpl:
    """满足 :class:`~hunter_sim.app.vil.protocols.VILVisualizer` 契约。"""

    def __init__(
        self,
        *,
        connection: CarlaConnectionManager,
        mapper: CoordinateMapper,
    ) -> None:
        """初始化可视化器。

        Args:
            connection: CARLA 连接管理器（用于取 world.debug）。
            mapper: 坐标映射器（把 odom 系目标/轨迹转换到 map 系）。
        """
        self._connection = connection
        self._mapper = mapper

    # ---------- 内部工具 ----------

    def _get_debug(self) -> _DebugDrawer:
        world = self._connection.get_world()
        return cast_debug(world.debug)

    @staticmethod
    def _to_carla_location(x: float, y: float, z: float) -> Any:
        import carla

        return carla.Location(x=x, y=y, z=z)

    @staticmethod
    def _to_carla_rotation(yaw_deg: float, pitch_deg: float = 0.0, roll_deg: float = 0.0) -> Any:
        import carla

        return carla.Rotation(pitch=pitch_deg, yaw=yaw_deg, roll=roll_deg)

    @staticmethod
    def _to_carla_vector(x: float, y: float, z: float) -> Any:
        import carla

        return carla.Vector3D(x=x, y=y, z=z)

    @staticmethod
    def _to_carla_color(rgba: tuple[float, float, float, float]) -> Any:
        import carla

        return carla.Color(r=rgba[0], g=rgba[1], b=rgba[2], a=rgba[3])

    # ---------- 可视化入口（同步阻塞 API；调用方按需 to_thread）----------

    def draw_vehicle_status(self, telemetry: VehicleTelemetry, map_pose: Transform) -> None:
        """在虚拟车辆上方绘制速度 + 行为状态文字。"""
        debug = self._get_debug()
        loc = map_pose.location
        # 位置略高于车辆（Z + 1.0）以便文字浮于车顶。
        anchor = self._to_carla_location(loc.x, loc.y, loc.z + 1.0)
        speed_text = f"v={telemetry.chassis.velocity:.1f} m/s"
        behavior_text = f"behavior={telemetry.behavior_state}"
        color = self._to_carla_color(_TEXT_COLOR)
        debug.draw_string(
            anchor,
            speed_text,
            color,
            _TEXT_LIFE_TIME,
            thickness=_TEXT_HEIGHT,
        )
        debug.draw_string(
            self._to_carla_location(loc.x, loc.y, loc.z + 1.4),
            behavior_text,
            color,
            _TEXT_LIFE_TIME,
            thickness=_TEXT_HEIGHT,
        )

    def draw_perception_objects(
        self, telemetry: VehicleTelemetry, ego_map_pose: Transform
    ) -> None:
        """在地图中绘制感知目标包围盒（绿色半透明）。

        Args:
            telemetry: 目标位于 odom 系下的相对坐标；本方法负责映射。
            ego_map_pose: 自车地图位姿（保留参数以匹配契约，当前未使用）。
        """
        del ego_map_pose  # 保留签名，映射由 CoordinateMapper 完成
        debug = self._get_debug()
        color = self._to_carla_color(_OBJECT_COLOR)
        for obj in telemetry.perception.objects:
            x_map, y_map = self._mapper.object_to_map(obj.x, obj.y)
            center = self._to_carla_location(x_map, y_map, obj.height / 2.0)
            heading_map = self._mapper.heading_to_map(obj.heading)
            rotation = self._to_carla_rotation(math.degrees(heading_map))
            extent = self._to_carla_vector(obj.length / 2.0, obj.width / 2.0, obj.height / 2.0)
            debug.draw_box(center, rotation, extent, color, _OBJECT_LIFE_TIME)

    def draw_planning_trajectory(
        self, telemetry: VehicleTelemetry, ego_map_pose: Transform
    ) -> None:
        """绘制规划轨迹（起点蓝→终点绿的线段序列）。"""
        del ego_map_pose
        points = telemetry.perception.planning_trajectory
        if len(points) < 2:
            return
        debug = self._get_debug()
        mapped = [self._mapper.object_to_map(px, py) for px, py in points]
        n = len(mapped) - 1
        for i in range(n):
            x0, y0 = mapped[i]
            x1, y1 = mapped[i + 1]
            t_ratio = i / max(1, n - 1)
            # 蓝→绿：r=0→0, g=0.3→1, b=1→0
            r = 0.0
            g = 0.3 + 0.7 * t_ratio
            b = 1.0 - t_ratio
            color = self._to_carla_color((r, g, b, 0.9))
            debug.draw_line(
                self._to_carla_location(x0, y0, 0.3),
                self._to_carla_location(x1, y1, 0.3),
                color,
                _TRAJ_LIFE_TIME,
                thickness=0.15,
            )

    # ---------- 异步包装（供编排器 await）----------

    async def adraw_all(
        self, telemetry: VehicleTelemetry, map_pose: Transform
    ) -> None:
        """并发调度三类绘制（阻塞调用 → 线程池），不互相阻塞。"""
        await asyncio.to_thread(self.draw_vehicle_status, telemetry, map_pose)
        await asyncio.to_thread(self.draw_perception_objects, telemetry, map_pose)
        await asyncio.to_thread(self.draw_planning_trajectory, telemetry, map_pose)


def cast_debug(debug_obj: Any) -> _DebugDrawer:
    """将 ``world.debug`` 视为 :class:`_DebugDrawer`（薄封装以便 mypy 通过）。"""
    return debug_obj  # type: ignore[no-any-return]


__all__ = ["CARLAVisualizerImpl"]

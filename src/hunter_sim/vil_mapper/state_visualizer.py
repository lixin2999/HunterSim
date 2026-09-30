"""VIL 状态可视化（PROMPT-ENG-002-C）。

在 CARLA 仿真中可视化实车状态：速度、转向、刹车、感知目标。
使用 world.debug.draw_box() 绘制半透明绿色感知框（life_time=0.1s）。
"""

from __future__ import annotations

from typing import Any, Optional

from hunter_sim.common.models import DetectedObject, PerceptionResult, VehicleState
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class StateVisualizer:
    """实车状态可视化辅助类。

    Args:
        world: CARLA World 对象。
        enabled: 是否启用可视化。
    """

    def __init__(self, world: Any, enabled: bool = True) -> None:
        self._world = world
        self._enabled = enabled

    def draw_vehicle_state(self, state: VehicleState) -> None:
        """绘制车辆速度/状态指示（调试文本，可选实现）。

        Args:
            state: 车辆状态。
        """
        if not self._enabled:
            return
        # CARLA debug API 不直接支持文本绘制，此处通过 debug.line 绘制速度向量
        try:
            import carla  # noqa: PLC0415
            vel = state.velocity
            speed_mag = state.vehicle_speed
            if speed_mag > 0.1:
                # 绘制速度向量线（从车辆中心出发）
                start = carla.Location(
                    x=state.transform.x,
                    y=state.transform.y,
                    z=state.transform.z + 0.5,
                )
                scale = 2.0  # 显示放大系数
                end = carla.Location(
                    x=state.transform.x + vel[0] * scale,
                    y=state.transform.y + vel[1] * scale,
                    z=state.transform.z + 0.5,
                )
                self._world.debug.draw_line(
                    start, end,
                    thickness=0.05,
                    color=carla.Color(0, 100, 255),
                    life_time=0.1,
                    persistent_lines=False,
                )
        except Exception as exc:
            logger.debug(f"draw_vehicle_state skipped: {exc}")


class PerceptionOverlay:
    """感知结果叠加渲染器。

    在 CARLA 场景中绘制实车感知目标（绿色半透明框），
    与仿真真实目标做对比可视化。

    Args:
        world: CARLA World 对象。
        enabled: 是否启用叠加渲染。
        box_color: 感知框颜色 (R, G, B)。
        box_life_time: 绘制框的存活时间（秒）。
    """

    def __init__(
        self,
        world: Any,
        enabled: bool = True,
        box_color: tuple[int, int, int] = (0, 200, 0),
        box_life_time: float = 0.1,
    ) -> None:
        self._world = world
        self._enabled = enabled
        self._color = box_color
        self._life_time = box_life_time
        self._draw_count: int = 0

    def draw_perception_result(
        self,
        perception: PerceptionResult,
        ego_carla_transform: Any = None,
    ) -> int:
        """绘制一帧感知结果中所有检测目标。

        Args:
            perception: 感知结果。
            ego_carla_transform: 自车 CARLA 位姿（用于坐标偏移，可选）。

        Returns:
            本帧实际绘制的目标数量。
        """
        if not self._enabled:
            return 0

        count = 0
        for obj in perception.objects:
            try:
                self._draw_object_box(obj)
                count += 1
            except Exception as exc:
                logger.debug(f"Failed to draw object {obj.object_id}: {exc}")

        self._draw_count += count
        return count

    def _draw_object_box(self, obj: DetectedObject) -> None:
        """绘制单个检测目标的 3D 包围盒。"""
        import carla  # noqa: PLC0415
        import math

        t = obj.transform
        length, width, height = obj.size

        # 计算包围盒中心（CARLA 坐标系）
        center = carla.Location(x=t.x, y=t.y, z=t.z + height / 2.0)

        # 使用 debug.draw_box 绘制（以朝向为准的中心盒子）
        # CARLA debug.draw_box 参数：center, extent, rotation, color, life_time
        extent = carla.Vector3D(x=length / 2.0, y=width / 2.0, z=height / 2.0)
        rotation = carla.Rotation(pitch=0.0, yaw=math.degrees(t.yaw), roll=0.0)
        color = carla.Color(*self._color, 128)

        self._world.debug.draw_box(
            box_extent=extent,
            location=center,
            rotation=rotation,
            color=color,
            life_time=self._life_time,
            persistent_lines=False,
        )

    @property
    def total_draw_count(self) -> int:
        """累计绘制目标数。"""
        return self._draw_count

    def reset_counter(self) -> None:
        """重置绘制计数器。"""
        self._draw_count = 0

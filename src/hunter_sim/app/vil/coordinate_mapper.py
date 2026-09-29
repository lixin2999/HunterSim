"""模块 4.3：实车 odom → CARLA 地图坐标系映射实现。

坐标系定义（AI 编码规则 §9.1）：

- 实车 odom：原点为车辆启动点，X 前 / Y 左 / Z 上（**右手系**）；
- CARLA 地图：X 东 / Y 北 / Z 上（**左手系**）。

映射算法（§4.3.2）：

1. **旋转 + 平移**：将 odom 位移经起始朝向 ``yaw0`` 旋转后叠加标定原点 (x0, y0)；
2. **手性修正**：右手 → 左手在水平面上的差异体现在 **yaw 取反**（等价于 Y 轴反向），
   因此 ``yaw_map = yaw0 - yaw_odom``；
3. **目标点**：感知目标在 odom 系下的相对坐标按同一仿射变换映射至地图系。

内部一律弧度制；对外若需要度制由调用方转换（AI 规则 §9.2）。
"""

from __future__ import annotations

import math

from hunter_sim.app.vil.models import VILCalibration


class CoordinateMapperImpl:
    """满足 :class:`~hunter_sim.app.vil.protocols.CoordinateMapper` 契约。"""

    def __init__(self, calibration: VILCalibration) -> None:
        """初始化映射器。

        Args:
            calibration: VIL 启动时的标定值（实车对应地图起点位姿）。
        """
        self._cal = calibration
        # 预计算三角函数，避免热路径重复调用。
        self._cos_yaw0 = math.cos(calibration.yaw0)
        self._sin_yaw0 = math.sin(calibration.yaw0)

    @property
    def calibration(self) -> VILCalibration:
        """返回当前标定（不可变对象，可安全共享）。"""
        return self._cal

    def odom_to_map(
        self, x_odom: float, y_odom: float, yaw_odom: float
    ) -> tuple[float, float, float]:
        """车辆位姿：odom → map。

        Args:
            x_odom: odom 纵向位移（米）。
            y_odom: odom 横向位移（米）。
            yaw_odom: odom 航向角（弧度）。

        Returns:
            ``(x_map, y_map, yaw_map)``，其中 ``yaw_map`` 为 CARLA 定义的航向（弧度）。
        """
        # 1. 旋转（odom → map 平面，未考虑手性反转，通过 yaw 取反补偿）
        x_rot = x_odom * self._cos_yaw0 - y_odom * self._sin_yaw0
        y_rot = x_odom * self._sin_yaw0 + y_odom * self._cos_yaw0
        # 2. 平移
        x_map = self._cal.x0 + x_rot
        y_map = self._cal.y0 + y_rot
        # 3. 航向（右手 → 左手：yaw 取反）
        yaw_map = self._cal.yaw0 - yaw_odom
        return x_map, y_map, yaw_map

    def object_to_map(self, x_odom: float, y_odom: float) -> tuple[float, float]:
        """目标点（无朝向）：odom → map。

        Args:
            x_odom: 目标在 odom 系下的 x（前向）。
            y_odom: 目标在 odom 系下的 y（左向）。

        Returns:
            ``(x_map, y_map)``。
        """
        x_rot = x_odom * self._cos_yaw0 - y_odom * self._sin_yaw0
        y_rot = x_odom * self._sin_yaw0 + y_odom * self._cos_yaw0
        return self._cal.x0 + x_rot, self._cal.y0 + y_rot

    def heading_to_map(self, heading_odom: float) -> float:
        """目标朝向：odom → map（左手系取反）。"""
        return self._cal.yaw0 - heading_odom


__all__ = ["CoordinateMapperImpl"]

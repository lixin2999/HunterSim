"""模块 4.4.1：VIL 位姿与状态同步实现。

流程：

1. 依据 ``data_timeout_ms`` 判断是否 **暂停**（由编排器决定，不在此处抛出）；
2. 依据 ``extrapolation_threshold_ms`` 判断是否 **外推**（通过
   :class:`SyncController` 完成）；
3. **坐标转换**：odom → map（含 CARLA 左手系的 yaw 取反）；
4. **注入虚拟车辆**：位姿（``set_transform``）+ 线速度（``set_velocity``）
   + 角速度（``set_angular_velocity``）；Z 由配置固定（实车 2D 定位无高程）。

不做的事：不订阅事件、不驱动 tick；纯粹"收到遥测 → 落到 vehicle"的转换层。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from hunter_sim.app.vil.models import VehicleTelemetry, VILConfig
from hunter_sim.app.vil.protocols import CoordinateMapper, SyncController
from hunter_sim.simulation.models import Location, Rotation, Transform, Vector3D
from hunter_sim.simulation.protocols import VehicleController


class StateSynchronizerImpl:
    """满足 :class:`~hunter_sim.app.vil.protocols.StateSynchronizer` 契约。"""

    def __init__(
        self,
        *,
        vehicle: VehicleController,
        mapper: CoordinateMapper,
        sync_ctrl: SyncController,
        config: VILConfig,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化状态同步器。

        Args:
            vehicle: 虚拟车辆控制器（VIL 模式）。
            mapper: 坐标映射器。
            sync_ctrl: 延迟补偿控制器。
            config: VIL 运行配置（读取 ``ego_z_offset`` / 阈值）。
            clock: Unix 墙钟可注入（单测便于确定性），默认 :func:`time.time`。
        """
        self._vehicle = vehicle
        self._mapper = mapper
        self._sync_ctrl = sync_ctrl
        self._config = config
        self._clock = clock or time.time

    async def sync(self, telemetry: VehicleTelemetry) -> Transform:
        """将实车遥测注入虚拟车辆并返回目标位姿。

        Args:
            telemetry: 实车最新一帧遥测。

        Returns:
            实际下发的地图系目标位姿（供可视化叠加使用）。
        """
        # 延迟：遥测 Unix 时间戳 vs 当前墙钟（秒 → 毫秒）。
        now_s = self._clock()
        delay_ms = max(0.0, (now_s - telemetry.timestamp) * 1000.0)

        # 若延迟大于外推阈值 → 补偿；否则原样使用。
        if self._sync_ctrl.should_extrapolate(delay_ms):
            compensated = self._sync_ctrl.compensate(telemetry, delay_ms / 1000.0)
        else:
            compensated = telemetry

        loc = compensated.localization
        x_map, y_map, yaw_map = self._mapper.odom_to_map(loc.x, loc.y, loc.heading)

        # 俯仰/横滚：若有 IMU 则注入，否则 0；CARLA 使用度制。
        pitch_deg = 0.0
        roll_deg = 0.0
        if compensated.imu is not None:
            pitch_deg = math.degrees(compensated.imu.pitch)
            roll_deg = math.degrees(compensated.imu.roll)

        transform = Transform(
            location=Location(x=x_map, y=y_map, z=self._config.ego_z_offset),
            rotation=Rotation(pitch=pitch_deg, yaw=math.degrees(yaw_map), roll=roll_deg),
        )

        # 线速度：沿目标朝向分解。
        v = compensated.chassis.velocity
        velocity = Vector3D(
            x=v * math.cos(yaw_map),
            y=v * math.sin(yaw_map),
            z=0.0,
        )
        # 角速度：CARLA 左手系下 z 分量与 odom 右手系相反。
        angular = Vector3D(
            x=0.0,
            y=0.0,
            z=-compensated.chassis.angular_velocity,
        )

        await self._vehicle.set_transform(transform)
        await self._vehicle.set_velocity(velocity)
        await self._vehicle.set_angular_velocity(angular)
        return transform


__all__ = ["StateSynchronizerImpl"]

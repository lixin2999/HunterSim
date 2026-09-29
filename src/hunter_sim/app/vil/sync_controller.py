"""模块 4.5：VIL 同步控制与延迟补偿。

设计要点（§4.5.1 / §4.5.2）：

- **判断接口**：``should_pause``/``should_extrapolate`` 基于配置的阈值给出决策，
  编排器据此选择"暂停仿真 / 外推 / 直接使用最新数据"三条路径；
- **外推补偿**：由于网络 + 处理链路存在 100~200ms 延迟，对**位姿**做匀速 + 常角速度
  外推（``dt = elapsed``），仅用于可视化；不影响仿真物理；
- **不可变**：``compensate`` 返回新的 :class:`VehicleTelemetry` 实例，原对象保持不变，
  方便并发下安全访问。
"""

from __future__ import annotations

import math

from hunter_sim.app.vil.models import (
    LocalizationData,
    VehicleTelemetry,
    VILConfig,
)


class SyncControllerImpl:
    """满足 :class:`~hunter_sim.app.vil.protocols.SyncController` 契约。"""

    def __init__(self, config: VILConfig) -> None:
        """初始化同步控制器。

        Args:
            config: VIL 运行配置（含 ``delay_compensation_ms`` /
                ``data_timeout_ms`` / ``extrapolation_threshold_ms``）。
        """
        self._config = config

    def should_pause(self, delay_ms: float) -> bool:
        """延迟超过 ``data_timeout_ms`` → 暂停仿真（默认 500ms）。"""
        return delay_ms > self._config.data_timeout_ms

    def should_extrapolate(self, delay_ms: float) -> bool:
        """延迟超过 ``extrapolation_threshold_ms`` → 使用最近数据外推（默认 50ms）。"""
        return delay_ms > self._config.extrapolation_threshold_ms

    def compensate(self, telemetry: VehicleTelemetry, elapsed_s: float) -> VehicleTelemetry:
        """位姿外推：以当前速度与角速度推进 ``elapsed_s`` 秒。

        Args:
            telemetry: 原始（可能已过时）的遥测帧。
            elapsed_s: 数据时间戳到当前仿真时刻的间隔（秒），非负。

        Returns:
            外推后的新 :class:`VehicleTelemetry`；若 ``elapsed_s <= 0`` 则返回原对象。
        """
        if elapsed_s <= 0.0:
            return telemetry
        loc = telemetry.localization
        v = telemetry.chassis.velocity
        w = telemetry.chassis.angular_velocity
        # 匀速 + 常角速度外推（小角度近似足够，仅用于可视化）。
        new_heading = loc.heading + w * elapsed_s
        new_x = loc.x + v * math.cos(new_heading) * elapsed_s
        new_y = loc.y + v * math.sin(new_heading) * elapsed_s
        return telemetry.model_copy(
            update={
                "localization": LocalizationData(
                    x=new_x, y=new_y, heading=new_heading
                )
            }
        )


__all__ = ["SyncControllerImpl"]

"""L3 数据处理层接口契约（``typing.Protocol``）。

职责：将 L2 采集的原始 CARLA 测量转换为强类型 ``core`` 契约帧（:class:`Converter`），
跨传感器按时间戳对齐聚合为 :class:`~hunter_sim.core.contracts.SynchronizedFrame`
（:class:`Synchronizer`），并对数组型数据做去噪清洗（:class:`Cleaner`）。

依赖方向：L3 依赖 ``core``（契约/配置/异常）与 L2 的缓冲产物，禁止反向依赖 L4/L5。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from hunter_sim.core.contracts import (
    SynchronizedFrame,
    TimestampedData,
    VehicleState,
)


@runtime_checkable
class Converter(Protocol):
    """原始 CARLA 测量 → ``core`` 契约帧（模块 3.1）。"""

    def convert(
        self,
        sensor_type: str,
        measurement: Any,
        *,
        sensor_id: str,
        frame_id: int,
    ) -> TimestampedData:
        """按传感器类型转换单条测量。

        Args:
            sensor_type: 与 :class:`~hunter_sim.core.config.SensorConfig` 一致的 ``type`` 串。
            measurement: CARLA 原生测量对象（不透明句柄）。
            sensor_id: 传感器标识。
            frame_id: 帧序号（由上层管线分配）。

        Returns:
            对应的不可变契约帧。

        Raises:
            ConversionError: 类型不支持或数据解析/形状非法。
        """
        ...


@runtime_checkable
class Synchronizer(Protocol):
    """多传感器时间同步（模块 3.2）。"""

    def synchronize(
        self,
        streams: dict[str, list[TimestampedData]],
        *,
        reference: str,
        vehicle_states: list[VehicleState],
    ) -> list[SynchronizedFrame]:
        """以参考传感器的帧时刻为锚，为各数据流匹配容差内最近邻样本。

        Args:
            streams: ``sensor_id -> 该传感器帧列表``（帧须带 ``timestamp``）。
            reference: 作为时间锚点的 ``sensor_id``。
            vehicle_states: 主车状态列表，用于逐锚点匹配。

        Returns:
            按锚点时间升序的同步帧列表；无匹配主车状态的锚点被跳过。
        """
        ...


@runtime_checkable
class Cleaner(Protocol):
    """数组型数据清洗去噪（模块 3.3）。"""

    def clean(self, frame: TimestampedData) -> TimestampedData:
        """返回清洗后的同类型不可变帧（非数组帧原样返回）。"""
        ...


__all__ = ["Cleaner", "Converter", "Synchronizer"]

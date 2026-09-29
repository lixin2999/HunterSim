"""L2 数据采集层 (Data Acquisition)。

模块：sensor_manager（传感器生命周期）/ buffer（线程安全缓冲）/ writer（异步持久化）。
对外暴露各 Protocol 与实现类，供上层通过依赖注入使用；不反向依赖 L3+。
"""

from __future__ import annotations

from hunter_sim.acquisition.buffer import BufferRegistryImpl, RingSensorBuffer
from hunter_sim.acquisition.protocols import (
    BufferRegistry,
    DataWriter,
    HasTimestamp,
    SensorBuffer,
    SensorManager,
)
from hunter_sim.acquisition.sensor_manager import SensorManagerImpl
from hunter_sim.acquisition.writer import DiskDataWriterImpl

__all__ = [
    "BufferRegistry",
    "BufferRegistryImpl",
    "DataWriter",
    "DiskDataWriterImpl",
    "HasTimestamp",
    "RingSensorBuffer",
    "SensorBuffer",
    "SensorManager",
    "SensorManagerImpl",
]

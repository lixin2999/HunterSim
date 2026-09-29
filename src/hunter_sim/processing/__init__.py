"""L3 数据处理与分析层 (Processing & Analysis)。

模块：converter（原始测量→契约帧）/ synchronizer（多传感器时间同步）/ cleaner（数据清洗去噪）。
对外暴露各 Protocol 与实现类，供上层通过依赖注入使用；不反向依赖 L4/L5。

注：开发提示词中的 ``algorithm_connector``（外部算法/自动标注接入）涉及 ROS2/Kafka 等外部
集成，归入 L5 应用调度与集成阶段实现。
"""

from __future__ import annotations

from hunter_sim.processing.cleaner import CleanerImpl
from hunter_sim.processing.converter import ConverterImpl
from hunter_sim.processing.protocols import Cleaner, Converter, Synchronizer
from hunter_sim.processing.synchronizer import SynchronizerImpl

__all__ = [
    "Cleaner",
    "CleanerImpl",
    "Converter",
    "ConverterImpl",
    "Synchronizer",
    "SynchronizerImpl",
]

"""GPU 资源池管理（PROMPT-ENG-008-A）。

管理单块/多块 NVIDIA GPU 的分配和回收，
支持画质-并发限制和过载降级策略。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from hunter_sim.common.models import QualityLevel
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 单 GPU 并发实例上限（按画质）
_QUALITY_CAPACITY: dict[QualityLevel, int] = {
    QualityLevel.LOW: 8,
    QualityLevel.MEDIUM: 4,
    QualityLevel.EPIC: 2,
}


@dataclass
class GPUDevice:
    """单块 GPU 的状态记录。

    Attributes:
        gpu_id: GPU 设备编号。
        name: GPU 型号名称。
        total_memory_gb: 总显存（GB）。
        used_memory_gb: 已使用显存（GB）。
        active_instances: 当前活跃实例数（按画质）。
    """

    gpu_id: int
    name: str = "Unknown"
    total_memory_gb: float = 12.0
    used_memory_gb: float = 0.0
    active_instances: dict[QualityLevel, int] = field(
        default_factory=lambda: {QualityLevel.LOW: 0, QualityLevel.MEDIUM: 0, QualityLevel.EPIC: 0}
    )

    def has_capacity(self, quality: QualityLevel) -> bool:
        """检查是否还能接受指定画质的新实例。"""
        current = self.active_instances.get(quality, 0)
        max_count = _QUALITY_CAPACITY.get(quality, 2)
        return current < max_count

    def memory_ok(self, quality: QualityLevel) -> bool:
        """检查显存是否足够。"""
        req_gb: float = {QualityLevel.LOW: 4.0, QualityLevel.MEDIUM: 6.0, QualityLevel.EPIC: 10.0}[quality]
        return (self.used_memory_gb + req_gb) <= self.total_memory_gb


class GPUResourcePool:
    """GPU 资源池管理器。

    使用优先级队列（最少实例数优先分配），支持过载降级。

    Args:
        gpu_count: GPU 总数。
        gpu_names: 各 GPU 名称列表。
        total_memory_gb: 单块 GPU 的总显存（GB），默认 12.0。
    """

    def __init__(
        self,
        gpu_count: int = 1,
        gpu_names: Optional[list[str]] = None,
        total_memory_gb: float = 12.0,
    ) -> None:
        names = gpu_names or [f"GPU-{i}" for i in range(gpu_count)]
        self._devices: list[GPUDevice] = [
            GPUDevice(
                gpu_id=i,
                name=names[i] if i < len(names) else f"GPU-{i}",
                total_memory_gb=total_memory_gb,
            )
            for i in range(gpu_count)
        ]
        self._lock: threading.Lock = threading.Lock()
        logger.info(f"GPUResourcePool initialized with {gpu_count} device(s)")

    def allocate(self, quality: QualityLevel) -> int:
        """分配一块 GPU，返回 gpu_id；无可用 GPU 时返回 -1。

        选择负载最低（active_instances 最少）且有容量的 GPU。
        """
        with self._lock:
            candidates = [d for d in self._devices if d.has_capacity(quality) and d.memory_ok(quality)]
            if not candidates:
                logger.warning(f"No GPU capacity for quality={quality.value}")
                return -1
            # 按总活跃实例数最少排序
            candidates.sort(key=lambda d: sum(d.active_instances.values()))
            chosen = candidates[0]
            chosen.active_instances[quality] = chosen.active_instances.get(quality, 0) + 1
            req_gb: float = {QualityLevel.LOW: 4.0, QualityLevel.MEDIUM: 6.0, QualityLevel.EPIC: 10.0}[quality]
            chosen.used_memory_gb += req_gb
            logger.info(
                f"GPU {chosen.gpu_id} allocated for quality={quality.value} "
                f"(instances={sum(chosen.active_instances.values())})"
            )
            return chosen.gpu_id

    def release(self, gpu_id: int, quality: QualityLevel) -> None:
        """释放 GPU 资源。"""
        with self._lock:
            device = next((d for d in self._devices if d.gpu_id == gpu_id), None)
            if device is None:
                logger.warning(f"GPU {gpu_id} not found for release")
                return
            if device.active_instances.get(quality, 0) > 0:
                device.active_instances[quality] -= 1
            req_gb: float = {QualityLevel.LOW: 4.0, QualityLevel.MEDIUM: 6.0, QualityLevel.EPIC: 10.0}[quality]
            device.used_memory_gb = max(0.0, device.used_memory_gb - req_gb)
            logger.info(f"GPU {gpu_id} released (quality={quality.value})")

    def get_status(self) -> list[dict[str, object]]:
        """返回所有 GPU 当前状态列表。"""
        with self._lock:
            return [
                {
                    "gpu_id": d.gpu_id,
                    "name": d.name,
                    "used_memory_gb": round(d.used_memory_gb, 1),
                    "total_memory_gb": d.total_memory_gb,
                    "active_low": d.active_instances[QualityLevel.LOW],
                    "active_medium": d.active_instances[QualityLevel.MEDIUM],
                    "active_epic": d.active_instances[QualityLevel.EPIC],
                }
                for d in self._devices
            ]

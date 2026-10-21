"""HunterCore 数据接入聚合入口（V2.2.0）。

`CloudConnector` 组合 `TelemetryProducer` 与 `BagUploader`，对桌面客户端暴露
`start / stop / publish_state / publish_event / upload_bag` 五个动作。未启用或
凭据缺失时整体降级为 no-op（各方法安全返回），绝不阻断仿真主循环。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

from hunter_sim.cloud.bag_uploader import BagUploader
from hunter_sim.cloud.telemetry_producer import TelemetryProducer
from hunter_sim.common.models import CloudSettings, VehicleState
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class CloudConnector:
    """HunterCore 数据接入聚合器。

    Args:
        settings: HunterCore 接入配置。
        producer_factory: 可选 Kafka 生产者工厂（单测注入）。
        client_factory: 可选 httpx 客户端工厂（单测注入）。
    """

    def __init__(
        self,
        settings: CloudSettings,
        producer_factory: Any = None,
        client_factory: Any = None,
    ) -> None:
        self._settings = settings
        self.producer = TelemetryProducer(settings, producer_factory=producer_factory)
        self.uploader = BagUploader(settings, client_factory=client_factory)
        self._started = False
        self._last_publish = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """是否启用数据回传。"""
        return self._settings.enabled

    @property
    def started(self) -> bool:
        """连接器是否已启动。"""
        return self._started

    def start(self) -> bool:
        """启动生产者与上传器；返回是否至少一条链路可用。"""
        if not self._settings.enabled:
            logger.info("CloudConnector 未启用，跳过启动")
            return False
        producer_ok = self.producer.connect()
        uploader_ok = self.uploader.connect()
        self._started = producer_ok or uploader_ok
        logger.info(f"CloudConnector 启动：kafka={producer_ok}, upload={uploader_ok}")
        return self._started

    def stop(self) -> None:
        """停止连接器（刷新缓冲、关闭生产者）。"""
        self.producer.close()
        self._started = False

    def publish_state(self, state: VehicleState) -> bool:
        """按配置节流发布一帧遥测（key=vehicle_id）。

        未启用/未启动时安全返回 False。距上次发布不足 `telemetry_interval_s`
        时跳过（返回 True 表示"已按节拍处理"，避免高频 tick 打爆 broker）。
        """
        if not self._started:
            return False
        now = time.time()
        with self._lock:
            if now - self._last_publish < self._settings.telemetry_interval_s:
                return True
            self._last_publish = now
        return self.producer.publish_telemetry(state, seq=self.producer.next_seq())

    def publish_event(self, event_type: str, timestamp: Optional[float] = None, **kwargs: Any) -> bool:
        """发布一条事件（碰撞/急刹等）。

        未知 `event_type`（不在 19 类契约枚举内）等映射失败时记 WARN 并返回
        False，绝不冒泡阻断仿真主循环。
        """
        if not self._started:
            return False
        ts = timestamp if timestamp is not None else time.time()
        try:
            return self.producer.publish_event(ts, event_type, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"事件发布失败（已忽略，不阻断仿真）：{type(exc).__name__}: {exc}")
            return False

    def publish_health(self, status: str = "auto_driving", timestamp: Optional[float] = None) -> bool:
        """发布一条健康心跳。"""
        if not self._started:
            return False
        ts = timestamp if timestamp is not None else time.time()
        return self.producer.publish_health(ts, status=status)

    def upload_bag(self, file_path: Path, data_type: str = "rosbag") -> Optional[Any]:
        """上传 rosbag 文件；未启用时返回 None。

        presign/PUT/complete 任一环节失败（文件缺失、HTTP≥400、业务码非 0 等）
        时记 WARN 并返回 None，绝不冒泡。
        """
        if not self._started:
            return None
        try:
            return self.uploader.upload(file_path, data_type=data_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bag 上传失败（已忽略，不阻断仿真）：{type(exc).__name__}: {exc}")
            return None

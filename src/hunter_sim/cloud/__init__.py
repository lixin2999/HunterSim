"""HunterCore 数据接入层（V2.2.0）。

把 CARLA 数字孪生车当作"车端"，向 HunterCore 数据采集分析平台回传：

- `telemetry_producer`：Kafka 遥测/事件/健康（SASL_SSL/SCRAM-SHA-512）；
- `bag_uploader`：rosbag 经 data-collector 预签名直传 MinIO；
- `telemetry_mapper`：`VehicleState` → HunterCore 遥测契约载荷；
- `connector`：聚合入口，凭据缺失时整体降级为 no-op。

本层不依赖 PySide6 / CARLA，可独立单测。
"""

from __future__ import annotations

from hunter_sim.cloud.bag_uploader import BagUploadResult, BagUploader, UploadError
from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.cloud.telemetry_mapper import build_health, build_telemetry
from hunter_sim.cloud.telemetry_producer import TelemetryProducer

__all__ = [
    "BagUploadResult",
    "BagUploader",
    "CloudConnector",
    "TelemetryProducer",
    "UploadError",
    "build_health",
    "build_telemetry",
]

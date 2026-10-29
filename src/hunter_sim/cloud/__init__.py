"""HunterCore 数据接入层（V2.10.0：平台契约重构）。

本层封装 CARLA 仿真向 HunterCore 数据采集分析平台回传的所有组件：

- ``sim_data_producer``：发布 ``carla.{sim_id}.sensor_data``（6 类 type）；
- ``kafka_security``：CA / mTLS 客户端证书 → kafka-python ``ssl_*`` 参数；
- ``sim_data_mapper``：6 类消息构造函数（纯函数，可独立单测）；
- ``vil_state_parser``：解析 ``carla.{sim_id}.vil_state`` 消息；
- ``bag_uploader``：rosbag 经 data-collector 预签名直传 MinIO；
- ``connector``：聚合入口，凭据缺失时整体降级为 no-op。

本层不依赖 PySide6 / CARLA，可独立单测。
"""

from __future__ import annotations

from hunter_sim.cloud.bag_uploader import BagUploadResult, BagUploader, UploadError
from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.cloud.sim_data_mapper import (
    SimStatusPayload,
    build_artifact,
    build_sensor_file,
    build_sim_event,
    build_sim_metrics,
    build_sim_status,
    build_sim_telemetry,
)
from hunter_sim.cloud.sim_data_producer import SimDataProducer
from hunter_sim.cloud.vil_state_parser import parse_vil_state

__all__ = [
    "BagUploadResult",
    "BagUploader",
    "CloudConnector",
    "SimDataProducer",
    "SimStatusPayload",
    "UploadError",
    "build_artifact",
    "build_sensor_file",
    "build_sim_event",
    "build_sim_metrics",
    "build_sim_status",
    "build_sim_telemetry",
    "parse_vil_state",
]

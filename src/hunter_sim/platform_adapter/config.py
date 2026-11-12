"""平台适配器配置（ADAPTER_* 环境变量）。"""

from __future__ import annotations

from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdapterSettings(BaseSettings):
    """CARLA 管理面适配器配置。

    Attributes:
        host: 监听地址（仅内网/本机，不暴露 Ingress/NodePort）。
        port: 监听端口（平台 CARLA_MANAGEMENT_ENDPOINT 默认 8090）。
        db_path: 实例注册表 SQLite 路径（重启恢复/幂等）。
        artifacts_dir: 本地产物目录（上传前的暂存区）。
        artifact_bucket: 产物 Bucket（平台预签名下载约定）。
        recordings_dir: 传感器录制目录（mcap 落盘 + 保留期清理）。
        recording_retention_hours: 录制文件保留时长（小时），看门狗周期清理。
        record_enabled: 录制总开关（false 时忽略 ``sensors[].record``）。
        record_min_free_gb: 录制所需最小空闲磁盘（GB），运行期低于阈值自动停录。
        upload_on_failure: 失败/取消路径是否也上传已录 mcap（默认仅成功路径）。
        artifact_retention_hours: 产物暂存目录保留时长（小时），0 表示不清理。
        stream_fps: MJPEG 推流帧率（V3.5.5 默认 20；实际画面帧率取它与
            ``configs/sensor_configs.json`` 的 ``overview_camera.fps`` 较小值）。
        stream_max_width: 回传画面最大宽度（0 表示不降采样）。
        stream_max_clients: MJPEG 并发客户端上限（0 表示不限）。
        screenshot_quality: JPEG 质量（1-100，默认 75）。
        busy_timeout_seconds: SQLite 忙等待超时。
        watchdog_factor: 看门狗时长倍数（``duration * factor + grace``）。
        watchdog_grace_seconds: 看门狗基础宽限（秒）。
        progress_interval_seconds: 进度落库间隔（秒）。
        default_duration_seconds: 场景未提供 duration 时的默认时长。
        max_scene_seconds: 平台下发时长上限（超出直接失败，避免占满单世界）。
        ssl_certfile: 管理 API TLS 服务端证书（V2.0 下发 mTLS；空=明文 http）。
        ssl_keyfile: 管理 API TLS 服务端私钥（须与 ssl_certfile 成对）。
        ssl_ca_certfile: 信任的平台 CA 证书（校验客户端 sim-platform-{node_id}）。
        ssl_client_auth_required: 是否强制双向 TLS（要求平台回访携带客户端证书）。
        ssl_allowed_client_cn: 允许的客户端证书 CN（默认由 node_id 推导，留空不校验）。
    """

    model_config = SettingsConfigDict(
        env_prefix="ADAPTER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = Field("127.0.0.1", description="适配器监听地址（仅内网）")
    port: int = Field(8090, ge=1024, le=65535, description="适配器监听端口")
    db_path: str = Field(
        "data/adapter/carla_instances.db", description="实例注册表 SQLite 路径"
    )
    artifacts_dir: str = Field("data/adapter/artifacts", description="本地产物暂存目录")
    # 必须落在 data-collector UploadBucket 枚举内（hunter-raw-data|hunter-rosbag|hunter-video），
    # 旧默认 hunter-scene-assets 不在枚举 → presign 422（实机 SCN-2056-1a1e-animal 暴露）。
    artifact_bucket: str = Field(
        "hunter-raw-data", description="产物对象存储 Bucket（须为 UploadBucket 枚举值）"
    )
    # ── V3.0.0 Monitor & Recorder ──
    recordings_dir: str = Field(
        "data/adapter/recordings", description="传感器录制目录（mcap 落盘）"
    )
    recording_retention_hours: float = Field(
        24.0, ge=0.0, le=720.0, description="录制文件保留时长（小时），0 表示不清理"
    )
    record_enabled: bool = Field(
        True, description="传感器录制总开关（false 时忽略 sensors[].record）"
    )
    record_min_free_gb: float = Field(
        0.5,
        ge=0.0,
        le=1024.0,
        description="录制所需最小空闲磁盘（GB）；0 表示不检查，运行期低于阈值自动停录",
    )
    upload_on_failure: bool = Field(
        False, description="失败/取消路径是否也上传已录 mcap（默认仅成功路径上传）"
    )
    artifact_retention_hours: float = Field(
        72.0,
        ge=0.0,
        le=8760.0,
        description="产物暂存目录保留时长（小时），看门狗周期清理；0 表示不清理",
    )
    stream_fps: float = Field(20.0, gt=0.0, le=60.0, description="MJPEG 推流帧率（Hz）")
    stream_max_width: int = Field(
        640, ge=0, le=7680, description="回传画面最大宽度（0 表示不降采样）"
    )
    stream_max_clients: int = Field(
        8, ge=0, le=256, description="MJPEG 并发客户端上限（0 表示不限）"
    )
    screenshot_quality: int = Field(75, ge=1, le=100, description="截图 JPEG 质量")
    busy_timeout_seconds: float = Field(5.0, gt=0.0, le=60.0, description="SQLite 忙等待超时")
    watchdog_factor: float = Field(3.0, ge=1.0, le=10.0, description="看门狗时长倍数")
    watchdog_grace_seconds: float = Field(
        120.0, ge=0.0, le=3600.0, description="看门狗基础宽限（秒）"
    )
    progress_interval_seconds: float = Field(
        0.5, gt=0.0, le=10.0, description="进度落库间隔（秒）"
    )
    default_duration_seconds: float = Field(
        60.0, gt=0.0, le=7200.0, description="缺省场景时长（秒）"
    )
    max_scene_seconds: float = Field(
        7200.0, gt=0.0, description="平台下发场景时长上限（秒）"
    )
    # ── V2.0 下发通道 mTLS（接入清单 §1.5 步骤 3；注册体 protocol 相应改 https）──
    ssl_certfile: Optional[str] = Field(
        None, description="管理 API TLS 服务端证书（空=明文 http，平台无需 TLS）"
    )
    ssl_keyfile: Optional[str] = Field(
        None, description="管理 API TLS 服务端私钥（须与 ssl_certfile 成对）"
    )
    ssl_ca_certfile: Optional[str] = Field(
        None, description="信任的平台 CA 证书路径（校验回访客户端证书）"
    )
    ssl_client_auth_required: bool = Field(
        False, description="是否强制双向 TLS（true 时仅接受平台 sim-platform 客户端证书）"
    )
    ssl_allowed_client_cn: str = Field(
        "", description="允许的客户端证书 CN（留空则不做 CN 白名单校验）"
    )

    @field_validator("ssl_certfile", "ssl_keyfile", "ssl_ca_certfile", mode="after")
    @classmethod
    def _blank_cert_is_none(cls, value: Optional[str]) -> Optional[str]:
        """空白证书路径归一为 ``None``（``ADAPTER_SSL_*=`` 视为未启用 TLS）。"""
        if value is None or not value.strip():
            return None
        return value.strip()

    @property
    def tls_enabled(self) -> bool:
        """管理 API 是否以 TLS 提供服务（服务端证书与私钥成对配置时为真）。"""
        return bool(self.ssl_certfile and self.ssl_keyfile)

    def watchdog_timeout(self, duration_seconds: float) -> float:
        """计算看门狗超时：``duration * factor + grace``。"""
        base = duration_seconds if duration_seconds > 0 else self.default_duration_seconds
        return base * self.watchdog_factor + self.watchdog_grace_seconds


__all__ = ["AdapterSettings"]

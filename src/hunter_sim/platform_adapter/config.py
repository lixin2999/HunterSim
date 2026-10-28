"""平台适配器配置（ADAPTER_* 环境变量）。"""

from __future__ import annotations

from pydantic import Field
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
        stream_fps: MJPEG 推流帧率（默认 10，与 VIL 栅格一致）。
        stream_max_width: 回传画面最大宽度（0 表示不降采样）。
        screenshot_quality: JPEG 质量（1-100，默认 75）。
        busy_timeout_seconds: SQLite 忙等待超时。
        watchdog_factor: 看门狗时长倍数（``duration * factor + grace``）。
        watchdog_grace_seconds: 看门狗基础宽限（秒）。
        progress_interval_seconds: 进度落库间隔（秒）。
        default_duration_seconds: 场景未提供 duration 时的默认时长。
        max_scene_seconds: 平台下发时长上限（超出直接失败，避免占满单世界）。
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
    artifact_bucket: str = Field("hunter-scene-assets", description="产物对象存储 Bucket")
    # ── V3.0.0 Monitor & Recorder ──
    recordings_dir: str = Field(
        "data/adapter/recordings", description="传感器录制目录（mcap 落盘）"
    )
    recording_retention_hours: float = Field(
        24.0, ge=0.0, le=720.0, description="录制文件保留时长（小时），0 表示不清理"
    )
    stream_fps: float = Field(10.0, gt=0.0, le=60.0, description="MJPEG 推流帧率（Hz）")
    stream_max_width: int = Field(
        640, ge=0, le=7680, description="回传画面最大宽度（0 表示不降采样）"
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

    def watchdog_timeout(self, duration_seconds: float) -> float:
        """计算看门狗超时：``duration * factor + grace``。"""
        base = duration_seconds if duration_seconds > 0 else self.default_duration_seconds
        return base * self.watchdog_factor + self.watchdog_grace_seconds


__all__ = ["AdapterSettings"]

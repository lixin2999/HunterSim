"""适配器编排层（平台控制面语义）。

职责：
- ``create``：按 ``sim_id``/``scene_id`` 幂等创建实例，立即入队并返回（不等待
  地图加载，``load_world`` 可达 10~30s）；
- ``submit``：平台在 create 后紧接着下发场景；若配置与 create 一致则**不重启**
  正在装配的场景（避免重复加载世界），仅当配置变化时才中断并重跑；
- ``pause/resume/stop``：幂等控制；
- ``get/list/result``：平台轮询视图（状态统一为平台词汇）。

本层不直接操作 CARLA，全部交给 ``SimulationExecutor``。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from typing import Any, Callable, Iterator, Optional, cast

from hunter_sim.common.metrics import INSTANCE_LIFECYCLE_TOTAL, STREAM_CLIENTS
from hunter_sim.common.models import HunterSimSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor
from hunter_sim.platform_adapter.models import (
    ACTIVE_STATUSES,
    CreateInstanceRequest,
    InstanceRecord,
    PLATFORM_RUNNING,
    SubmitScenarioRequest,
)
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.sensor_sim.frame_hub import STREAM_RGB, Frame, FrameHub

logger = get_logger(__name__)

_DEFAULT_DURATION_S = 60.0
_QUALITY_KEY = "quality"
# MJPEG 无帧容忍时长（秒）：超过即结束响应，避免客户端无限等空流
_STREAM_IDLE_TIMEOUT_S = 5.0


class AdapterService:
    """控制面编排（create/submit/control/query）。

    Args:
        settings: 全局配置（CARLA/Kafka/Cloud）。
        adapter_settings: 适配器配置（时长上限/产物目录等）。
        store: 实例注册表。
        executor: 串行执行器。
        frame_hub_provider: 帧汇聚中心查询器（默认委托执行器；单测可注入）。
    """

    def __init__(
        self,
        settings: HunterSimSettings,
        adapter_settings: AdapterSettings,
        store: InstanceStore,
        executor: SimulationExecutor,
        frame_hub_provider: Optional[Callable[[str], Optional[FrameHub]]] = None,
    ) -> None:
        self._settings = settings
        self._adapter = adapter_settings
        self._store = store
        self._executor = executor
        self._frame_hub_provider = frame_hub_provider or _executor_frame_hub(executor)

    # ── 创建 / 下发 ─────────────────────────────────────────────────────────

    def create(self, request: CreateInstanceRequest) -> InstanceRecord:
        """创建实例并立即入队（幂等）。

        幂等键：
        - 平台 ``sim_id`` 相同 → 返回已有实例（避免同一 Topic 出现两个世界）；
        - 同一 ``scene_id`` 已有进行中实例 → 返回已有实例（单世界场景语义）。
        """
        sim_id = request.resolved_sim_id
        if sim_id:
            existing = self._store.find_by_sim_id(sim_id)
            if existing is not None:
                logger.info(
                    f"Adapter create idempotent by sim_id: sim_id={sim_id}, "
                    f"instance={existing.instance_id}"
                )
                return existing
        else:
            sim_id = f"sim-{uuid.uuid4().hex}"

        existing_scene = self._store.find_active_by_scene(request.scene_id)
        if existing_scene is not None:
            logger.warning(
                "Adapter create idempotent by scene_id: scene_id=%s, instance=%s, "
                "platform_sim_id=%s, adapter_sim_id=%s",
                request.scene_id,
                existing_scene.instance_id,
                sim_id,
                existing_scene.sim_id,
            )
            return existing_scene

        scene_config = dict(request.scene_config or {})
        quality = (request.quality or "").strip()
        if quality:
            scene_config[_QUALITY_KEY] = quality

        record = InstanceRecord(
            instance_id=f"inst-{uuid.uuid4().hex}",
            sim_id=sim_id,
            scene_id=request.scene_id,
            scene_name=request.scene_name,
            mode=(request.mode or "sil").strip().lower(),
            vehicle_id=(request.vehicle_id or "").strip(),
            scene_config=scene_config,
            vil_config=request.vil,
            total_time_s=_duration_of(scene_config, self._adapter.default_duration_seconds),
            actors_count=_actors_of(scene_config),
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._store.insert(record)
        INSTANCE_LIFECYCLE_TOTAL.labels(event="created").inc()
        self._executor.submit(record)
        logger.info(
            f"Adapter instance created: instance={record.instance_id}, "
            f"scene={record.scene_id}, sim_id={record.sim_id}, mode={record.mode}"
        )
        return self._store.get(record.instance_id) or record

    def submit(self, instance_id: str, request: SubmitScenarioRequest) -> Optional[InstanceRecord]:
        """下发（或重新下发）场景配置，立即返回。

        平台 create 时已携带完整 ``scene_config``，随后的 submit 通常是同一份配置：
        此时不重启装配中的世界，只确认实例存在。配置真正变化时才提高 ``revision``
        并中断当前运行，由串行队列按最新版本重跑。
        """
        record = self._store.get(instance_id)
        if record is None:
            return None

        incoming = dict(request.scene_config or record.scene_config or {})
        quality = record.scene_config.get(_QUALITY_KEY) if record.scene_config else None
        if quality is not None:
            incoming.setdefault(_QUALITY_KEY, quality)

        same_config = _same_scene_config(record.scene_config, incoming)
        same_mode = _same_optional(request.mode, record.mode)
        same_vehicle = _same_optional(request.vehicle_id, record.vehicle_id)

        if same_config and same_mode and same_vehicle and record.status in ACTIVE_STATUSES:
            logger.info(
                f"Adapter scenario submit coalesced: instance={instance_id}, "
                f"status={record.status} (create/submit race)"
            )
            return record

        record.scene_config = incoming
        if request.mode:
            record.mode = request.mode.strip().lower()
        if request.vehicle_id:
            record.vehicle_id = request.vehicle_id.strip()
        record.total_time_s = _duration_of(incoming, self._adapter.default_duration_seconds)
        record.actors_count = _actors_of(incoming)
        restart = record.status == PLATFORM_RUNNING
        self._executor.submit(record, restart=restart)
        logger.info(
            f"Adapter scenario submitted: instance={instance_id}, "
            f"restart={restart}, revision={record.revision}"
        )
        return self._store.get(instance_id)

    # ── 控制 ────────────────────────────────────────────────────────────────

    def pause(self, instance_id: str) -> Optional[InstanceRecord]:
        """暂停实例（幂等）。"""
        return self._executor.pause(instance_id)

    def resume(self, instance_id: str) -> Optional[InstanceRecord]:
        """恢复实例（幂等）。"""
        return self._executor.resume(instance_id)

    def stop(self, instance_id: str) -> Optional[InstanceRecord]:
        """停止实例并标记 canceled（幂等）。"""
        record = self._executor.cancel(instance_id, reason="canceled")
        if record is not None:
            INSTANCE_LIFECYCLE_TOTAL.labels(event="canceled").inc()
        return record

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get(self, instance_id: str) -> Optional[InstanceRecord]:
        """按实例 ID 查询。"""
        return self._store.get(instance_id)

    def list(self, scene_id: Optional[str] = None) -> list[InstanceRecord]:
        """按场景查询（无 ``scene_id`` 时返回全部，按创建时间倒序）。"""
        scene = (scene_id or "").strip()
        return self._store.list_by_scene(scene) if scene else self._store.list_all()

    def result(self, instance_id: str) -> Optional[InstanceRecord]:
        """查询结果（非终态也返回当前状态，由平台映射为 3003）。"""
        return self._store.get(instance_id)

    def mark_restarted_instances_failed(self) -> int:
        """启动恢复：把重启前未终结的实例标记为 failed。"""
        return self._store.mark_restarted_instances_failed("adapter restarted")

    # ── 实时画面 ─────────────────────────────────────────────────────────────

    def health_snapshot(self) -> dict[str, Any]:
        """管理面健康快照（供只读探针 ``huntersim probes`` 代理转发）。

        只汇总本进程**真实掌握**的信息：无法采样的项显式置 ``None``，
        避免探针与业务面各自探测导致结论不一致。

        Returns:
            含 ``carla_connected`` / ``active_instance`` / ``sensor`` / ``cloud``
            等键的扁平字典（字段语义与探针 ``/health`` 一致）。
        """
        active = [
            record for record in self._store.list_all() if record.status in ACTIVE_STATUSES
        ]
        current = active[0] if active else None
        streams: dict[str, int] = {}
        if current is not None:
            hub = self._frame_hub_provider(current.instance_id)
            if hub is not None:
                streams = dict(hub.stats())
        return {
            "carla_connected": None if current is None else True,
            "carla_probe_error": None,
            "active_instance": None
            if current is None
            else {
                "instance_id": current.instance_id,
                "scene_id": current.scene_id,
                "status": current.status,
                "carla_status": current.carla_status,
                "progress_percent": round(current.progress_percent, 3),
            },
            "vil_state": None,
            "sensor": {
                "recording": bool(current.recording_path) if current is not None else False,
                "recording_bytes": int(current.recording_bytes) if current is not None else 0,
                "streams": streams,
            },
            "cloud": {"enabled": bool(self._settings.cloud.enabled)},
        }

    def runtime_gauges(self) -> dict[str, Any]:
        """采样运行时 Gauge 值（抓取 ``/metrics`` 时刷新）。"""
        active = [
            record for record in self._store.list_all() if record.status in ACTIVE_STATUSES
        ]
        return {
            "active_instances": float(len(active)),
            "carla_connected": bool(active),
            "disk_usage_ratio": _disk_usage_ratio(self._adapter.recordings_dir),
        }

    def screenshot(self, instance_id: str) -> Optional[Frame]:
        """取实例最新 RGB 帧（无活动场景/未挂相机时返回 None）。"""
        hub = self._frame_hub_provider(instance_id)
        if hub is None:
            return None
        return hub.latest(STREAM_RGB)

    def stream_frames(
        self, instance_id: str, fps: Optional[float] = None
    ) -> Iterator[Frame]:
        """持续产出最新 RGB 帧（客户端断开即终止）。

        无相机时立即结束；无新帧时重发上一帧以维持 MJPEG 连接；
        连续 ``_STREAM_IDLE_TIMEOUT_S`` 秒无任何帧则结束（由路由层拒绝空流）。

        Args:
            instance_id: 实例 ID。
            fps: 推流帧率（None 时取 ``ADAPTER_STREAM_FPS``）。
        """
        rate = float(fps if fps is not None else self._adapter.stream_fps)
        interval = 1.0 / max(0.5, rate)
        last_frame: Optional[Frame] = None
        last_seen_at = time.time()
        STREAM_CLIENTS.inc()
        try:
            while True:
                hub = self._frame_hub_provider(instance_id)
                if hub is None:
                    return
                frame = hub.latest(STREAM_RGB)
                if frame is not None:
                    last_frame = frame
                    last_seen_at = time.time()
                elif time.time() - last_seen_at > _STREAM_IDLE_TIMEOUT_S:
                    return
                if last_frame is not None:
                    yield last_frame
                time.sleep(interval)
        finally:
            STREAM_CLIENTS.dec()


def _disk_usage_ratio(path: str) -> Optional[float]:
    """录制目录所在盘的占用率（目录不可用时返回 None，不产出样本）。"""
    try:
        usage = shutil.disk_usage(path or ".")
    except OSError:
        return None
    return usage.used / usage.total if usage.total else None


def _executor_frame_hub(
    executor: Any,
) -> Callable[[str], Optional[FrameHub]]:
    """取执行器的帧汇聚中心查询器（测试替身可能未提供，降级为“无画面”）。"""
    provider = getattr(executor, "frame_hub", None)
    if callable(provider):
        return cast(Callable[[str], Optional[FrameHub]], provider)
    return lambda _instance_id: None


def _same_scene_config(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """比较场景配置是否等价（忽略 create 时附加的 ``quality`` 元数据）。"""
    return _canonical(left) == _canonical(right)


def _canonical(config: dict[str, Any]) -> str:
    """规范化 JSON（键排序，忽略 quality 键）。"""
    if not isinstance(config, dict):
        return ""
    payload = {key: value for key, value in config.items() if key != _QUALITY_KEY}
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


def _same_optional(value: Optional[str], current: str) -> bool:
    """可选字段比较：None/空串表示“不修改”，视为一致。"""
    text = (value or "").strip().lower()
    return not text or text == (current or "").strip().lower()


def _duration_of(scene_config: dict[str, Any], default: float) -> float:
    """读取场景时长（秒）。"""
    try:
        value = float(scene_config.get("duration", default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _actors_of(scene_config: dict[str, Any]) -> int:
    """参与者数量（含自车）。"""
    actors = scene_config.get("actors")
    return (len(actors) if isinstance(actors, list) else 0) + 1


__all__ = ["AdapterService"]

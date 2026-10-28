"""串行仿真执行器（单 CARLA 世界）。

职责：
- 串行消费实例队列（同一时刻只运行一个场景，满足 CARLA 单世界约束）；
- ``create``/``submit`` 立即返回，地图加载（10~30s）在后台线程完成；
- 场景映射、VIL 运行时装配、看门狗与终态结果发布；
- 传感器帧汇聚（FrameHub → MJPEG/截图）与 mcap 录制（→ sensor_file）；
- 重启恢复：由 ``InstanceStore`` 在启动时把非终态实例标记为 failed。

看门狗：``duration * 3 + 120s``（见 ``AdapterSettings.watchdog_timeout``）。
"""

from __future__ import annotations

import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.common.exceptions import (
    CarlaSimulationError,
    ConfigurationError,
)
from hunter_sim.common.models import HunterSimSettings, SimMode
from hunter_sim.common.utils import get_logger
from hunter_sim.eval_service.evaluation_engine import EvaluationEngine, SceneEvaluationResult
from hunter_sim.execution.plane import ExecutionPlane
from hunter_sim.platform_adapter.artifacts import ArtifactManager, ArtifactResult
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.models import (
    InstanceRecord,
    PLATFORM_CANCELED,
    PLATFORM_FAILED,
    PLATFORM_PENDING,
    PLATFORM_RUNNING,
    PLATFORM_SUCCEEDED,
    TERMINAL_STATUSES,
    to_platform_status,
)
from hunter_sim.platform_adapter.scene_mapper import (
    SceneMappingError,
    map_scene_config,
    resolve_mode,
)
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.platform_adapter.vil import VILRuntime
from hunter_sim.scene_runner.scene_config import SceneConfig
from hunter_sim.scene_runner.scene_state_manager import SceneStateSnapshot
from hunter_sim.sensor_sim.frame_hub import FrameHub
from hunter_sim.sensor_sim.mcap_recorder import McapRecorder, RecordStats

logger = get_logger(__name__)

# 评估采样频率（Hz）：10Hz 足够计算速度/加速度/距离指标，避免 50Hz 全量占用内存
_SAMPLE_INTERVAL_S = 0.1
_MAX_SAMPLES = 50_000
# 进度落库最小间隔（秒）
_PROGRESS_MIN_INTERVAL_S = 0.5
# 录制文件保留清理周期（秒）：看门狗每轮顺带检查
_RETENTION_SWEEP_INTERVAL_S = 300.0
# 完成时等待录制上传排空的超时（秒）：不阻断终态落库，但给上传线程机会
_UPLOAD_DRAIN_TIMEOUT_S = 10.0


@dataclass
class _RunContext:
    """一次运行的上下文（仅工作线程/看门狗访问）。"""

    record: InstanceRecord
    scene: SceneConfig
    runner: Any = None
    vil: Optional[VILRuntime] = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    last_sample_time: float = -1.0
    last_progress_at: float = 0.0
    stop_reason: str = ""
    final_message: str = ""
    frame_hub: Optional[FrameHub] = None
    recorder: Optional[McapRecorder] = None
    record_stats: Optional[RecordStats] = None

    def add_sample(self, ego: dict[str, Any], sim_time: float) -> None:
        """按 10Hz 采样自车状态（供评估引擎消费）。"""
        if sim_time - self.last_sample_time < _SAMPLE_INTERVAL_S:
            return
        if len(self.samples) >= _MAX_SAMPLES:
            return
        self.last_sample_time = sim_time
        self.samples.append(
            {
                "time_stamp": sim_time,
                "timestamp": sim_time,
                "vehicle_speed": float(ego.get("velocity", 0.0)),
                "acceleration": (float(ego.get("acceleration", 0.0)), 0.0, 0.0),
                "steering": float(ego.get("steering", 0.0)),
                "x": float(ego.get("x", 0.0)),
                "y": float(ego.get("y", 0.0)),
            }
        )


class SimulationExecutor:
    """单世界串行执行器。

    Args:
        settings: 全局配置（CARLA/Kafka/Cloud/VIL）。
        adapter_settings: 适配器配置（产物目录/看门狗等）。
        store: 实例注册表。
        plane: 执行平面（可注入测试替身）。
        runner_factory: 透传执行平面的运行器工厂（测试用）。
    """

    def __init__(
        self,
        settings: HunterSimSettings,
        adapter_settings: AdapterSettings,
        store: InstanceStore,
        plane: ExecutionPlane,
    ) -> None:
        self._settings = settings
        self._adapter = adapter_settings
        self._store = store
        self._plane = plane
        self._queue: "queue.Queue[tuple[str, int]]" = queue.Queue()
        self._lock = threading.RLock()
        self._contexts: dict[str, _RunContext] = {}
        self._scene_instances: dict[str, str] = {}
        self._deadlines: dict[str, float] = {}
        self._stop_reasons: dict[str, str] = {}
        self._pause_requested: set[str] = set()
        self._worker: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._evaluator = EvaluationEngine()
        # 传感器录制上传（复用 CloudConnector 的有界队列 + 后台线程）
        self._connector_lock = threading.Lock()
        self._connector: Optional[CloudConnector] = None
        self._last_retention_sweep = 0.0

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动工作线程与看门狗。"""
        if self._worker is not None and self._worker.is_alive():
            return
        self._shutdown.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="CarlaAdapterWorker", daemon=True
        )
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="CarlaAdapterWatchdog", daemon=True
        )
        self._worker.start()
        self._watchdog.start()
        logger.info("Simulation executor started (serial single-world queue)")

    def stop(self) -> None:
        """停止工作线程、看门狗并释放执行平面。"""
        self._shutdown.set()
        worker = self._worker
        watchdog = self._watchdog
        if worker is not None:
            worker.join(timeout=5.0)
        if watchdog is not None:
            watchdog.join(timeout=5.0)
        self._worker = None
        self._watchdog = None
        self._stop_connector()
        try:
            self._plane.release_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Execution plane release_all failed: {exc}")
        logger.info("Simulation executor stopped")

    # ── 入队/控制 ───────────────────────────────────────────────────────────

    def submit(self, record: InstanceRecord, *, restart: bool = False) -> None:
        """把实例（按当前 revision）加入串行队列并立即返回。

        平台 ``submit_scenario`` 可能在实例运行中到达：``restart=True`` 时先停止
        当前运行器，工作线程随后按新 revision 重新装配（同一场景只跑最新版本）。
        """
        record.revision += 1
        record.status = PLATFORM_PENDING
        record.carla_status = "CREATED"
        record.message = None
        record.progress_percent = 0.0
        record.current_time_s = 0.0
        record.result = None
        record.finished_at = None
        record.watchdog_deadline = 0.0
        self._store.update(record)
        if restart:
            self._interrupt(record.instance_id, "restart")
        self._queue.put((record.instance_id, record.revision))
        logger.info(
            f"Adapter instance queued: instance={record.instance_id}, "
            f"scene={record.scene_id}, revision={record.revision}"
        )

    def pause(self, instance_id: str) -> Optional[InstanceRecord]:
        """暂停实例（幂等）；终态实例原样返回。"""
        record = self._store.get(instance_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return record
        with self._lock:
            self._pause_requested.add(instance_id)
            context = self._contexts.get(instance_id)
        if context is not None and context.runner is not None:
            try:
                context.runner.pause()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Pause failed for {instance_id}: {exc}")
        self._store.patch(
            instance_id,
            status=PLATFORM_RUNNING,
            carla_status="PAUSED",
            message="paused",
        )
        return self._store.get(instance_id)

    def resume(self, instance_id: str) -> Optional[InstanceRecord]:
        """恢复实例（幂等）；终态实例原样返回。"""
        record = self._store.get(instance_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return record
        with self._lock:
            self._pause_requested.discard(instance_id)
            context = self._contexts.get(instance_id)
        if context is not None and context.runner is not None:
            try:
                context.runner.resume()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Resume failed for {instance_id}: {exc}")
        self._store.patch(
            instance_id,
            status=PLATFORM_RUNNING,
            carla_status="RUNNING",
            message=None,
        )
        return self._store.get(instance_id)

    def cancel(self, instance_id: str, reason: str = "canceled") -> Optional[InstanceRecord]:
        """停止实例并标记 canceled（幂等）。"""
        record = self._store.get(instance_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return record
        with self._lock:
            self._pause_requested.discard(instance_id)
        self._interrupt(instance_id, "canceled")
        self._store.patch(
            instance_id,
            status=PLATFORM_CANCELED,
            carla_status="CANCELED",
            message=reason,
            finished_at=time.time(),
        )
        return self._store.get(instance_id)

    def frame_hub(self, instance_id: str) -> Optional[FrameHub]:
        """当前运行的传感器帧汇聚中心（无活动场景返回 None）。

        供控制面 MJPEG/截图端点取最新帧。
        """
        with self._lock:
            context = self._contexts.get(instance_id)
        return context.frame_hub if context is not None else None

    def _interrupt(self, instance_id: str, reason: str) -> None:
        """登记中断原因并停止当前运行器（不持锁调用 CARLA）。"""
        with self._lock:
            self._stop_reasons[instance_id] = reason
            context = self._contexts.get(instance_id)
        if context is not None and context.runner is not None:
            try:
                context.runner.stop(timeout_s=2.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Stop ({reason}) failed for {instance_id}: {exc}")

    # ── 工作线程 ────────────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """串行消费队列；邻近 revision 被合并（只跑最新场景）。"""
        while not self._shutdown.is_set():
            try:
                instance_id, revision = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                record = self._store.get(instance_id)
                if record is None or record.revision != revision:
                    continue
                if record.status in TERMINAL_STATUSES:
                    continue
                self._run(record)
            except SceneMappingError as exc:
                self._fail_instance(instance_id, exc.code)
            except Exception as exc:  # noqa: BLE001  单实例失败不拖垮工作线程
                logger.error(f"Adapter worker crashed on {instance_id}: {exc}", exc_info=True)
                self._fail_instance(instance_id, f"adapter_internal_error:{type(exc).__name__}")
            finally:
                with self._lock:
                    self._contexts.pop(instance_id, None)
                    self._deadlines.pop(instance_id, None)
                    self._stop_reasons.pop(instance_id, None)
                    self._pause_requested.discard(instance_id)
                    for scene_id, mapped in list(self._scene_instances.items()):
                        if mapped == instance_id:
                            self._scene_instances.pop(scene_id, None)

    def _run(self, record: InstanceRecord) -> None:
        """装配并运行单个实例（阻塞直到场景结束/失败/取消）。"""
        instance_id = record.instance_id
        mode = resolve_mode(record.mode or "sil", "sil")

        scene = map_scene_config(
            record.scene_config,
            scene_id=record.scene_id,
            scene_name=record.scene_name,
            mode=mode,
            default_duration_seconds=self._adapter.default_duration_seconds,
            max_duration_seconds=self._adapter.max_scene_seconds,
            quality=_quality_of(record),
        )
        if self._is_stale(record):
            logger.info(
                f"Adapter instance superseded before loading: instance={instance_id}, "
                f"revision={record.revision}"
            )
            return

        started_at = time.time()
        self._store.patch(
            instance_id,
            status=PLATFORM_PENDING,
            carla_status="LOADING",
            message="loading",
            total_time_s=scene.duration_seconds,
            actors_count=len(scene.traffic_participants) + 1,
            started_at=started_at,
            finished_at=None,
            watchdog_deadline=started_at + self._adapter.watchdog_timeout(
                scene.duration_seconds
            ),
        )

        context = _RunContext(record=self._store.get(instance_id) or record, scene=scene)
        with self._lock:
            self._contexts[instance_id] = context
            self._scene_instances[scene.scene_id] = instance_id
            self._deadlines[instance_id] = started_at + self._adapter.watchdog_timeout(
                scene.duration_seconds
            )

        # 传感器出口：FrameHub 总是创建（画面回传），录制器按 sensors[].record 打开，
        # ROS2 桥接仅在 sensors[].ros_enabled 时惰性创建（rclpy 缺失时降级为 None）
        context.frame_hub = FrameHub()
        self._start_recording(context)

        vil: Optional[VILRuntime] = None
        try:
            hooks: dict[str, Any] = {
                "sample_sink": context.add_sample,
                "frame_hub": context.frame_hub,
                "recorder": context.recorder,
                "ros_bridge": self._ros_bridge_for(scene),
                "screenshot_quality": self._adapter.screenshot_quality,
                "stream_max_width": self._adapter.stream_max_width,
            }
            if mode is SimMode.VIL:
                vil = VILRuntime(
                    self._settings,
                    sim_id=record.sim_id,
                    vehicle_id=record.vehicle_id,
                    vil_config=record.vil_config,
                    runner_provider=lambda: self._plane.get_runner(instance_id),
                )
                context.vil = vil
                hooks["pose_driver"] = vil.step
                hooks["status_provider"] = vil.status_override

            plane_instance = self._plane_instance(record, scene)
            runner = self._plane.ensure_runner(
                plane_instance,
                on_state_change=self._on_state_change,
                on_event=None,
                runner_hooks=hooks,
            )
            context.runner = runner
            if self._is_stale(record) or self._stop_reason_of(instance_id):
                return
            runner.load_scene(scene)
            if self._is_stale(record) or self._stop_reason_of(instance_id):
                return
            if vil is not None:
                vil.start()
            runner.start()
            if self._pause_requested_of(instance_id):
                runner.pause()
                self._store.patch(
                    instance_id,
                    status=PLATFORM_RUNNING,
                    carla_status="PAUSED",
                    message="paused",
                )
            else:
                self._store.patch(
                    instance_id,
                    status=PLATFORM_RUNNING,
                    carla_status="RUNNING",
                    message=None,
                    progress_percent=0.0,
                    current_time_s=0.0,
                )
            self._await_completion(context)
        except SceneMappingError as exc:
            self._fail_instance(instance_id, exc.code)
        except (ConfigurationError, CarlaSimulationError) as exc:
            self._fail_instance(instance_id, f"scene_error:{exc}")
        except Exception as exc:  # noqa: BLE001  引擎不可用/装载失败
            logger.warning(f"Instance {instance_id} assembly failed: {exc}")
            self._fail_instance(instance_id, f"execution_unavailable:{exc}")
        finally:
            self._close_recording(context)
            if vil is not None:
                vil.stop()
            try:
                self._plane.release(instance_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Release failed for {instance_id}: {exc}")

    def _stop_reason_of(self, instance_id: str) -> str:
        """当前实例的中断原因（无则空串）。"""
        with self._lock:
            return self._stop_reasons.get(instance_id, "")

    def _is_stale(self, record: InstanceRecord) -> bool:
        """记录是否已被更新的 revision 取代（场景重新下发）。"""
        latest = self._store.get(record.instance_id)
        return latest is None or latest.revision != record.revision

    def _pause_requested_of(self, instance_id: str) -> bool:
        """实例是否在装配期间收到暂停请求。"""
        with self._lock:
            return instance_id in self._pause_requested

    def _await_completion(self, context: _RunContext) -> None:
        """等待运行器线程结束，期间落库进度并发布终态结果。"""
        instance_id = context.record.instance_id
        runner = context.runner
        while not self._shutdown.is_set():
            if self._stop_reason_of(instance_id):
                break
            if not _runner_alive(runner):
                break
            self._persist_progress(context)
            time.sleep(0.2)

        # 录制收尾：成功/失败/取消/看门狗路径均到达此处（幂等）
        context.record_stats = self._close_recording(context)

        if self._shutdown.is_set():
            return
        reason = self._stop_reason_of(instance_id)
        if reason == "restart":
            # 平台重新下发场景：不写终态，工作线程会立即消费新 revision
            logger.info(f"Instance {instance_id} interrupted for scenario restart")
            return
        if reason == "canceled":
            self._store.patch(
                instance_id,
                status=PLATFORM_CANCELED,
                carla_status="CANCELED",
                message="canceled",
                finished_at=time.time(),
            )
            return
        if reason == "watchdog_timeout":
            self._fail_instance(instance_id, "watchdog_timeout")
            return
        snapshot = _safe_snapshot(runner)
        if snapshot is not None and snapshot.status.value == "failed":
            self._fail_instance(instance_id, snapshot.error_message or "scene_failed")
            return
        self._complete_instance(context, snapshot)

    # ── 结果与发布 ──────────────────────────────────────────────────────────

    def _complete_instance(
        self, context: _RunContext, snapshot: Optional[SceneStateSnapshot]
    ) -> None:
        """场景正常结束：评估、写产物、发布 sim_metrics/artifact。"""
        record = context.record
        instance_id = record.instance_id
        elapsed = snapshot.elapsed_seconds if snapshot is not None else record.total_time_s
        evaluation = self._evaluate(context, completed=True)
        artifacts = self._write_artifacts(context, evaluation)
        result = {
            "success": bool(evaluation.passed),
            "success_criteria_result": evaluation.success_criteria,
            "evaluation": evaluation.to_dict(),
            "artifacts": [artifact.to_public() for artifact in artifacts],
        }
        self._store.patch(
            instance_id,
            status=PLATFORM_SUCCEEDED,
            carla_status="COMPLETED",
            message=None,
            progress_percent=100.0,
            current_time_s=round(elapsed, 3),
            finished_at=time.time(),
            result=result,
            watchdog_deadline=0,
        )
        self._upload_recording(context)
        self._publish(record, evaluation, artifacts)
        logger.info(f"Instance completed: {instance_id}, passed={evaluation.passed}")

    def _fail_instance(self, instance_id: str, message: str) -> None:
        """标记 failed（保持平台可读的失败原因）。"""
        record = self._store.get(instance_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return
        self._store.patch(
            instance_id,
            status=PLATFORM_FAILED,
            carla_status="FAILED",
            message=message,
            finished_at=time.time(),
            watchdog_deadline=0,
        )
        logger.warning(f"Adapter instance failed: {instance_id}: {message}")

    def _evaluate(
        self, context: _RunContext, completed: bool
    ) -> SceneEvaluationResult:
        """用已采样轨迹与事件计算评估结果。"""
        runner = context.runner
        events = [_event_to_dict(event) for event in _detected_events(runner)]
        collisions = [event for event in events if event.get("event_type") == "collision"]
        return self._evaluator.evaluate_scene(
            scene_id=context.scene.scene_id,
            vehicle_states=context.samples,
            events=events,
            collision_events=collisions,
            ttc_samples=[],
            min_distances=[],
            speed_limit_ms=10.0,
            scene_duration_s=context.scene.duration_seconds,
            completed=completed,
        )

    def _write_artifacts(
        self, context: _RunContext, evaluation: SceneEvaluationResult
    ) -> list[ArtifactResult]:
        """写 eval.json/events.jsonl/run.log（上传失败不阻断）。"""
        record = context.record
        current = self._store.get(record.instance_id) or record
        manager = ArtifactManager(
            self._settings.cloud,
            artifacts_dir=self._adapter.artifacts_dir,
            bucket=self._adapter.artifact_bucket,
            sim_id=record.sim_id,
            scene_id=record.scene_id,
        )
        artifacts = [
            manager.write_json("eval.json", evaluation.to_dict()),
            manager.write_jsonl(
                "events.jsonl", [_event_to_dict(event) for event in _detected_events(context.runner)]
            ),
            manager.write_text(
                "run.log",
                "\n".join(
                    [
                        f"sim_id={record.sim_id}",
                        f"scene_id={record.scene_id}",
                        f"instance_id={record.instance_id}",
                        f"status={current.status}",
                        f"message={current.message or ''}",
                    ]
                )
                + "\n",
            ),
        ]
        return artifacts

    def _publish(
        self,
        record: InstanceRecord,
        evaluation: SceneEvaluationResult,
        artifacts: list[ArtifactResult],
    ) -> None:
        """发布 sim_metrics 与 artifact（无 producer 时安全跳过）。"""
        producer = self._plane.producer_for_sim_id(record.sim_id)
        if producer is None:
            logger.debug(f"No SimDataProducer for sim_id={record.sim_id}, skip metrics/artifacts")
            return
        try:
            from hunter_sim.cloud.sim_data_mapper import build_sim_metrics  # noqa: PLC0415

            metrics = build_sim_metrics(
                sim_id=record.sim_id,
                vehicle_id=record.vehicle_id or None,
                scene_id=record.scene_id,
                seq=producer.next_seq(),
                ts=time.time(),
                grade=evaluation.grade.value,
                passed=evaluation.passed,
                safety=evaluation.safety.to_dict(),
                efficiency=evaluation.efficiency.to_dict(),
                comfort=evaluation.comfort.to_dict(),
                task={
                    "completion_rate": evaluation.efficiency.completion_rate,
                    "scene_duration_s": evaluation.efficiency.scene_duration_s,
                },
                success_criteria_result=evaluation.success_criteria,
                takeover_required=evaluation.takeover,
            )
            producer.publish_metrics(metrics)
            for artifact in artifacts:
                producer.publish_artifact(
                    name=artifact.name,
                    bucket=self._adapter.artifact_bucket,
                    object_key=artifact.object_key,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    media_type=artifact.media_type,
                    vehicle_id=record.vehicle_id or None,
                    scene_id=record.scene_id,
                )
        except Exception as exc:  # noqa: BLE001  发布失败不阻断结果落库
            logger.warning(f"Publish metrics/artifacts failed: {type(exc).__name__}: {exc}")

    # ── 回调/看门狗 ─────────────────────────────────────────────────────────

    def _on_state_change(self, snapshot: SceneStateSnapshot) -> None:
        """状态跃迁落库（运行器持锁回调，不能回查运行器状态）。"""
        raw = snapshot.status.value.upper()
        with self._lock:
            instance_id = self._scene_instances.get(snapshot.scene_id)
            if instance_id is None or instance_id in self._stop_reasons:
                return
        record = self._store.get(instance_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return
        message = "paused" if raw == "PAUSED" else (snapshot.error_message or None)
        self._store.patch(
            instance_id,
            status=to_platform_status(raw),
            carla_status=raw,
            progress_percent=round(snapshot.progress * 100.0, 3),
            current_time_s=round(snapshot.elapsed_seconds, 3),
            message=message,
        )

    def _persist_progress(self, context: _RunContext) -> None:
        """周期落库进度（避免每 tick 写 SQLite）。"""
        snapshot = _safe_snapshot(context.runner)
        if snapshot is None:
            return
        now = time.time()
        if now - context.last_progress_at < _PROGRESS_MIN_INTERVAL_S:
            return
        context.last_progress_at = now
        runner = context.runner
        events = _detected_events(runner)
        fields: dict[str, Any] = {
            "progress_percent": round(snapshot.progress * 100.0, 3),
            "current_time_s": round(snapshot.elapsed_seconds, 3),
            "status": to_platform_status(snapshot.status.value.upper()),
            "carla_status": snapshot.status.value.upper(),
            "collisions": sum(
                1 for event in events if getattr(event, "event_type", "") == "collision"
            ),
            "lane_invasions": sum(
                1 for event in events if getattr(event, "event_type", "") == "lane_invasion"
            ),
        }
        ego = _runner_ego_state(runner)
        if ego:
            fields["ego_state"] = ego
        self._store.patch(context.record.instance_id, **fields)

    def _watchdog_loop(self) -> None:
        """看门狗：超时实例标记 failed 并停止运行器。"""
        while not self._shutdown.is_set():
            now = time.time()
            expired: list[str] = []
            with self._lock:
                for instance_id, deadline in list(self._deadlines.items()):
                    if deadline and now > deadline:
                        expired.append(instance_id)
            for instance_id in expired:
                logger.error(f"Watchdog timeout: instance={instance_id}")
                with self._lock:
                    self._stop_reasons[instance_id] = "watchdog_timeout"
                    context = self._contexts.get(instance_id)
                if context is not None and context.runner is not None:
                    try:
                        context.runner.stop(timeout_s=2.0)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Watchdog stop failed for {instance_id}: {exc}")
                self._fail_instance(instance_id, "watchdog_timeout")
                with self._lock:
                    self._deadlines.pop(instance_id, None)
            if now - self._last_retention_sweep >= _RETENTION_SWEEP_INTERVAL_S:
                self._last_retention_sweep = now
                try:
                    self._sweep_recordings()
                except Exception as exc:  # noqa: BLE001  清理失败不影响看门狗
                    logger.warning(f"Recording retention sweep failed: {exc}")
            time.sleep(0.5)

    # ── 传感器录制与上传 ─────────────────────────────────────────────────────

    @staticmethod
    def _ros_bridge_for(scene: Any) -> Optional[Any]:
        """按 ``sensors[].ros_enabled`` 惰性创建 ROS2 桥接器（默认关闭）。

        ``rclpy`` 或桥接器构造失败时返回 None（场景照常运行，仅少一路出口）。
        """
        specs = [spec for spec in getattr(scene, "sensors", []) if spec.enabled]
        if not any(spec.ros_enabled for spec in specs):
            return None
        try:
            from hunter_sim.sensor_sim.carla_ros_bridge import CarlaRosBridge  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001  可选依赖缺失降级
            logger.warning(f"ROS2 bridge unavailable: {type(exc).__name__}: {exc}")
            return None
        try:
            return CarlaRosBridge()
        except Exception as exc:  # noqa: BLE001  初始化失败不阻断场景
            logger.warning(f"ROS2 bridge init failed: {type(exc).__name__}: {exc}")
            return None

    def _start_recording(self, context: _RunContext) -> None:
        """按 ``scene.sensors[].record`` 打开 mcap 录制（不满足条件则不录制）。"""
        enabled = [spec for spec in context.scene.sensors if spec.enabled]
        if not any(spec.record for spec in enabled):
            return
        record = context.record
        recorder = McapRecorder()
        path = (
            Path(self._adapter.recordings_dir)
            / _safe_segment(record.instance_id)
            / f"sensors_r{record.revision}.mcap"
        )
        if not recorder.open(path):
            logger.warning(f"Sensor recording disabled for {record.instance_id} (degraded)")
            return
        context.recorder = recorder

    def _close_recording(self, context: _RunContext) -> Optional[RecordStats]:
        """结束录制并回写路径/字节数（幂等）。

        Returns:
            本次录制的统计（未录制或已关闭返回 None）。
        """
        recorder = context.recorder
        if recorder is None:
            return context.record_stats
        context.recorder = None
        stats = recorder.close()
        context.record_stats = stats
        if not stats.path:
            return stats
        try:
            self._store.patch(
                context.record.instance_id,
                recording_path=stats.path,
                recording_bytes=stats.size_bytes,
            )
        except Exception as exc:  # noqa: BLE001  记录回写失败不影响终态
            logger.debug(f"Recording metadata persist failed: {exc}")
        logger.info(
            f"Sensor recording closed: instance={context.record.instance_id}, "
            f"frames={stats.frame_count}, size={stats.size_bytes}B"
        )
        return stats

    def _upload_recording(self, context: _RunContext) -> None:
        """把录制文件投递到上传队列（后台线程，不阻塞终态落库）。"""
        stats = context.record_stats
        if stats is None or not stats.path or stats.frame_count <= 0:
            return
        path = Path(stats.path)
        if not path.is_file():
            logger.warning(f"Recording file missing, skip upload: {path}")
            return
        connector = self._connector_for(context.record)
        if connector is None:
            logger.info("Cloud disabled, sensor recording kept locally only")
            return
        connector.upload_bag(
            path,
            data_type="rosbag",
            stats=stats,
            scene_id=context.record.scene_id,
            vehicle_id=context.record.vehicle_id or None,
        )

    def _connector_for(self, record: InstanceRecord) -> Optional[CloudConnector]:
        """取（并初始化）上传连接器；未启用云端或 producer 不可用时返回 None。

        复用执行平面已连接的 ``SimDataProducer``，避免同一 sim_id 双生产者。
        """
        if not self._settings.cloud.enabled:
            return None
        producer = self._plane.producer_for_sim_id(record.sim_id)
        if producer is None:
            return None
        with self._connector_lock:
            if self._connector is None:
                self._connector = CloudConnector(self._settings.cloud)
            connector = self._connector
        connector.attach_producer(producer, sim_id=record.sim_id)
        return connector

    def _stop_connector(self) -> None:
        """停止上传线程（不等待未完成上传）。"""
        with self._connector_lock:
            connector, self._connector = self._connector, None
        if connector is None:
            return
        try:
            connector.wait_upload_idle(_UPLOAD_DRAIN_TIMEOUT_S)
            connector.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cloud connector stop failed: {exc}")

    def _sweep_recordings(self) -> None:
        """按保留期清理录制文件（看门狗周期调用，异常不外抛）。"""
        retention = float(self._adapter.recording_retention_hours)
        if retention <= 0:
            return
        root = Path(self._adapter.recordings_dir)
        if not root.is_dir():
            return
        cutoff = time.time() - retention * 3600.0
        removed = 0
        for entry in root.iterdir():
            try:
                if not entry.is_dir():
                    continue
                newest = max(
                    (child.stat().st_mtime for child in entry.rglob("*") if child.is_file()),
                    default=entry.stat().st_mtime,
                )
                if newest < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError as exc:
                logger.debug(f"Recording retention skip '{entry}': {exc}")
        if removed:
            logger.info(f"Recording retention removed {removed} directory(ies)")

    # ── 辅助 ─────────────────────────────────────────────────────────────────

    def _plane_instance(self, record: InstanceRecord, scene: SceneConfig) -> Any:
        """构造执行平面所需的实例视图（字段名与 SimInstance 对齐）。"""
        carla = self._settings.carla
        return SimpleNamespace(
            sim_instance_id=record.instance_id,
            sim_id=record.sim_id,
            scene_id=record.scene_id,
            vehicle_id=record.vehicle_id,
            map_id=scene.map_id,
            carla_host=carla.host,
            carla_rpc_port=carla.rpc_port,
            carla_stream_port=carla.stream_port,
            carla_tm_port=carla.tm_port,
        )


def _safe_segment(value: str) -> str:
    """路径片段安全化（只保留字母数字与 ``._-``）。"""
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in value)
    return cleaned or "unknown"


def _quality_of(record: InstanceRecord) -> Optional[str]:
    """从记录读取平台画质（create 时保存在 scene_config 外层）。"""
    value = record.scene_config.get("quality") if isinstance(record.scene_config, dict) else None
    return str(value) if value else None


def _runner_alive(runner: Any) -> bool:
    """运行器线程是否存活。"""
    try:
        return bool(getattr(runner, "is_running", False))
    except Exception:  # noqa: BLE001
        return False


def _safe_snapshot(runner: Any) -> Optional[SceneStateSnapshot]:
    """安全读取运行器状态快照。"""
    try:
        getter = getattr(runner, "get_state_snapshot", None)
        return getter() if callable(getter) else None
    except Exception:  # noqa: BLE001
        return None


def _detected_events(runner: Any) -> list[Any]:
    """安全读取运行器已检测事件。"""
    try:
        events = getattr(runner, "detected_events", None)
        return list(events) if events else []
    except Exception:  # noqa: BLE001
        return []


def _runner_ego_state(runner: Any) -> dict[str, Any]:
    """安全读取运行器自车状态（用于进度视图）。"""
    try:
        getter = getattr(runner, "get_ego_state", None)
        if callable(getter):
            return dict(getter() or {})
        getter = getattr(runner, "_get_ego_state", None)
        return dict(getter() or {}) if callable(getter) else {}
    except Exception:  # noqa: BLE001
        return {}


def _event_to_dict(event: Any) -> dict[str, Any]:
    """DetectedEvent → 评估引擎事件字典。"""
    return {
        "event_id": getattr(event, "event_id", ""),
        "event_type": getattr(event, "event_type", ""),
        "time_stamp": float(getattr(event, "time_stamp", 0.0)),
        "sim_time": float(getattr(event, "sim_time", 0.0)),
        "description": getattr(event, "description", ""),
        "event_level": getattr(event, "event_level", "info"),
        "data": getattr(event, "data", {}) or {},
    }


__all__ = ["SimulationExecutor"]

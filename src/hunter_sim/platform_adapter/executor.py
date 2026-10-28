"""串行仿真执行器（单 CARLA 世界）。

职责：
- 串行消费实例队列（同一时刻只运行一个场景，满足 CARLA 单世界约束）；
- ``create``/``submit`` 立即返回，地图加载（10~30s）在后台线程完成；
- 场景映射、VIL 运行时装配、看门狗与终态结果发布；
- 重启恢复：由 ``InstanceStore`` 在启动时把非终态实例标记为 failed。

看门狗：``duration * 3 + 120s``（见 ``AdapterSettings.watchdog_timeout``）。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

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

logger = get_logger(__name__)

# 评估采样频率（Hz）：10Hz 足够计算速度/加速度/距离指标，避免 50Hz 全量占用内存
_SAMPLE_INTERVAL_S = 0.1
_MAX_SAMPLES = 50_000
# 进度落库最小间隔（秒）
_PROGRESS_MIN_INTERVAL_S = 0.5


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

        vil: Optional[VILRuntime] = None
        try:
            hooks: dict[str, Any] = {"sample_sink": context.add_sample}
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
            time.sleep(0.5)

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

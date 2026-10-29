"""HunterCore 数据接入聚合入口（V2.10.0：平台契约重构）。

``CloudConnector`` 组合 ``SimDataProducer`` 与 ``BagUploader``，对桌面客户端和
API 服务层暴露统一发布接口：

- ``start(sim_id)``     — 启动生产者与上传器
- ``publish_telemetry()`` — sim_telemetry（10Hz）
- ``publish_status()``  — sim_status
- ``publish_event()``   — sim_event
- ``publish_metrics()`` — sim_metrics
- ``publish_artifact()`` — artifact
- ``upload_bag()``      — 上传完成后自动发布 sensor_file

未启用或凭据缺失时整体降级为 no-op（各方法安全返回），绝不阻断仿真主循环。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hunter_sim.cloud.bag_uploader import BagUploader
from hunter_sim.cloud.sim_data_producer import SimDataProducer
from hunter_sim.common.metrics import CLOUD_UPLOAD_DROPPED_TOTAL, CLOUD_UPLOAD_TOTAL
from hunter_sim.common.models import CloudSettings
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
        self._producer: Optional[SimDataProducer] = None
        self._producer_factory_raw: Optional[Callable[..., Any]] = producer_factory
        self._client_factory = client_factory
        self._sim_id: str = settings.sim_id  # 可在 start() 时覆盖
        self.uploader = BagUploader(settings, sim_id=self._sim_id, client_factory=client_factory)
        self._started = False
        self._last_publish = 0.0
        self._lock = threading.Lock()
        # bag 上传（文件哈希 + httpx PUT，可达数秒~分钟）放后台线程，绝不阻塞调用线程。
        self._worker_lock = threading.Lock()
        # 串行化上传执行：即便 stop() 因长 IO 使 join 超时、旧收尾线程与新线程短暂并存，
        # 也保证任意时刻至多一次在途 uploader.upload，杜绝并发双上传。
        self._upload_exec_lock = threading.Lock()
        self._upload_q: "Optional[queue.Queue[Any]]" = None
        self._upload_thread: Optional[threading.Thread] = None
        self._upload_stop: Optional[threading.Event] = None
        # 上传结果/溢出丢弃计数
        self._upload_counters_lock = threading.Lock()
        self._upload_success = 0
        self._upload_failed = 0
        self._upload_dropped = 0

    @property
    def enabled(self) -> bool:
        """是否启用数据回传。"""
        return self._settings.enabled

    @property
    def started(self) -> bool:
        """连接器是否已启动。"""
        return self._started

    @property
    def producer(self) -> Optional[SimDataProducer]:
        """当前实例的 SimDataProducer（启动后可用）。"""
        return self._producer

    def attach_producer(self, producer: Optional[SimDataProducer], sim_id: str = "") -> None:
        """复用外部已创建的 SimDataProducer（避免同一 sim_id 出现两个生产者）。

        Args:
            producer: 已连接的生产者（None 时忽略）。
            sim_id: 当前实例的 sim_id（用于重建上传器的 vehicle_id）。
        """
        if producer is None:
            return
        resolved_id = sim_id or self._sim_id or self._settings.sim_id
        self._sim_id = resolved_id
        self._producer = producer
        self.uploader = BagUploader(
            self._settings, sim_id=resolved_id,
            client_factory=getattr(self, "_client_factory", None),
        )
        self._started = True

    def start(self, sim_id: str = "") -> bool:
        """启动生产者与上传器；返回是否至少一条链路可用。

        Args:
            sim_id: 当前实例的 sim_id（空则使用 settings.sim_id）。
        """
        if not self._settings.enabled:
            logger.info("CloudConnector 未启用，跳过启动")
            return False
        resolved_id = sim_id or self._sim_id or self._settings.sim_id
        if not resolved_id:
            resolved_id = f"{self._settings.sim_id_prefix}-manual"
        self._sim_id = resolved_id
        # 重建 uploader 以更新 sim_id
        self.uploader = BagUploader(
            self._settings, sim_id=resolved_id,
            client_factory=getattr(self, "_client_factory", None),
        )
        self._producer = SimDataProducer(
            self._settings,
            sim_id=resolved_id,
            producer_factory=self._producer_factory_raw,
        )
        producer_ok = self._producer.connect()
        uploader_ok = self.uploader.connect()
        self._started = producer_ok or uploader_ok
        logger.info(
            f"CloudConnector[{resolved_id}] 启动：kafka={producer_ok}, upload={uploader_ok}"
        )
        return self._started

    def stop(self) -> None:
        """停止连接器（关闭上传后台线程、刷新缓冲、关闭生产者）。"""
        with self._worker_lock:
            thread = self._upload_thread
            q = self._upload_q
            old_stop = self._upload_stop
            self._upload_thread = None
            self._upload_q = None
            self._upload_stop = None
        if thread is not None and q is not None:
            q.put(None)  # 唤醒并终止循环（已入队任务尽力处理）
            thread.join(timeout=3.0)
            if thread.is_alive() and old_stop is not None:
                old_stop.set()
        if self._producer is not None:
            self._producer.close()
        self._started = False

    def publish_telemetry(self, message: dict[str, Any]) -> bool:
        """发布一条 sim_telemetry 消息（由调用方通过 build_sim_telemetry 构造）。

        未启用/未启动时安全返回 False。距上次发布不足
        ``telemetry_proxy_interval_s`` 时跳过（返回 True 表示"已按节拍处理"）。
        """
        if not self._started or self._producer is None:
            return False
        now = time.time()
        with self._lock:
            if now - self._last_publish < self._settings.telemetry_proxy_interval_s:
                return True
            self._last_publish = now
        return self._producer.publish_telemetry(message)

    def publish_status(self, message: dict[str, Any]) -> bool:
        """发布一条 sim_status 消息（由调用方通过 build_sim_status 构造）。"""
        if not self._started or self._producer is None:
            return False
        return self._producer.publish_status(message)

    def publish_event(
        self,
        event_type: str,
        event_level: str = "info",
        sim_time: float = 0.0,
        description: str = "",
        scene_id: str = "",
        vehicle_id: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> bool:
        """发布一条 sim_event（由 SimDataProducer 自动构建消息体）。

        未知 ``event_type`` 映射失败时记 WARN 并返回 False，绝不冒泡阻断仿真主循环。
        """
        if not self._started or self._producer is None:
            return False
        try:
            return self._producer.publish_event(
                vehicle_id=vehicle_id or self._sim_id,
                scene_id=scene_id,
                event_type=event_type,
                event_level=event_level,
                sim_time=sim_time,
                description=description,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"事件发布失败（已忽略，不阻断仿真）：{type(exc).__name__}: {exc}")
            return False

    def publish_metrics(self, message: dict[str, Any]) -> bool:
        """发布一条 sim_metrics 消息（场景终止后由 EvaluationEngine 调用）。"""
        if not self._started or self._producer is None:
            return False
        return self._producer.publish_metrics(message)

    def publish_artifact(
        self,
        name: str,
        bucket: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
        media_type: str,
        scene_id: str = "",
    ) -> bool:
        """发布一条 artifact 消息（eval.json / events.jsonl / run.log 等）。"""
        if not self._started or self._producer is None:
            return False
        return self._producer.publish_artifact(
            name=name,
            bucket=bucket,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
            media_type=media_type,
            vehicle_id=None,
            scene_id=scene_id,
        )

    def upload_bag(
        self,
        file_path: Path,
        data_type: str = "rosbag",
        *,
        stats: Optional[Any] = None,
        scene_id: str = "",
        vehicle_id: Optional[str] = None,
    ) -> None:
        """将 bag/mcap 上传任务投递到后台有界队列（fire-and-forget，不阻塞调用线程）。

        上传完成后自动发布 sensor_file 消息到 carla.{sim_id}.sensor_data。
        队列容量由 ``upload_queue_max_size`` 限定，避免 MinIO/网络长时故障
        时任务无界堆积在内存。队满时丢弃**最旧**待传任务为新任务腾位。

        Args:
            file_path: 待上传文件。
            data_type: 数据类型（``rosbag`` / ``camera_raw`` 等）。
            stats: 录制统计（``McapRecorder.close()`` 返回的 ``RecordStats``）；
                提供时 ``sensor_file`` 的时长/帧数/主题/格式取真实值。
            scene_id: 场景 ID（写入 sensor_file）。
            vehicle_id: 孪生车 ID（写入 sensor_file）。
        """
        if not self._started:
            return
        self._ensure_upload_worker()
        q = self._upload_q
        if q is None:
            return
        job: tuple[Path, str, Optional[Any], str, Optional[str], Any] = (
            file_path,
            data_type,
            stats,
            scene_id,
            vehicle_id,
            # 捕获投递时刻的 producer：单世界串行下下一个实例可能已切换 sim_id，
            # 若执行时再读 self._producer 会把 A 的 sensor_file 发到 B 的 Topic
            self._producer,
        )
        try:
            q.put_nowait(job)
            return
        except queue.Full:
            pass
        # 队满：丢弃最旧任务腾位，保证最新采集的 bag 优先上传
        dropped_job: Any = None
        try:
            dropped_job = q.get_nowait()
        except queue.Empty:
            dropped_job = None
        if dropped_job is not None and isinstance(dropped_job, tuple):
            self._count_drop(f"queue full, dropped oldest bag '{dropped_job[0]}'")
        try:
            q.put_nowait(job)
        except queue.Full:
            # 腾位失败（并发消费者已补位等极端情况）：丢弃新任务本身
            self._count_drop(f"queue full, dropped new bag '{file_path}'")

    def _count_drop(self, reason: str) -> None:
        """记一次上传丢弃（计数器 + Prometheus + 告警日志）。"""
        with self._upload_counters_lock:
            self._upload_dropped += 1
        CLOUD_UPLOAD_DROPPED_TOTAL.inc()
        logger.warning(f"bag 上传丢弃（{reason}）")

    def wait_upload_idle(self, timeout: float = 10.0) -> bool:
        """等待上传队列排空（仅用于关停/终态前的尽力等待）。

        Args:
            timeout: 最长等待秒数。

        Returns:
            True 表示已空闲（无待传任务且在途上传已结束）。
        """
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            q = self._upload_q
            if q is None or q.empty():
                # 空闲标志：能立即拿到执行锁说明无在途上传
                if self._upload_exec_lock.acquire(timeout=0.05):
                    self._upload_exec_lock.release()
                    return True
                continue
            time.sleep(0.05)
        return False

    @property
    def upload_stats(self) -> dict[str, int]:
        """上传后台计数（success/failed/dropped，供 /health 与测试读取）。"""
        with self._upload_counters_lock:
            return {
                "success": self._upload_success,
                "failed": self._upload_failed,
                "dropped": self._upload_dropped,
            }

    # ── 上传后台线程 ────────────────────────────────────────
    def _ensure_upload_worker(self) -> None:
        """惰性启动上传工作线程（幂等；V2.6.0 起队列为有界队列）。"""
        with self._worker_lock:
            if self._upload_thread is not None and self._upload_thread.is_alive():
                return
            self._upload_stop = threading.Event()
            self._upload_q = queue.Queue(maxsize=self._settings.upload_queue_max_size)
            self._upload_thread = threading.Thread(
                target=self._upload_loop, name="cloud-bag-uploader", daemon=True
            )
            self._upload_thread.start()

    def _upload_loop(self) -> None:
        """上传线程主体：消费队列项并执行，None 为终止哨兵。"""
        q = self._upload_q
        stop = self._upload_stop
        assert q is not None and stop is not None
        while not stop.is_set():
            try:
                job = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            path, dtype, stats, scene_id, vehicle_id, producer = job
            self._perform_upload(
                path, dtype, stats, scene_id, vehicle_id, producer=producer
            )

    def _perform_upload(
        self,
        file_path: Path,
        data_type: str,
        stats: Optional[Any] = None,
        scene_id: str = "",
        vehicle_id: Optional[str] = None,
        producer: Optional[Any] = None,
    ) -> None:
        """实际执行上传（在上传线程）；串行化保证任意时刻至多一次在途上传。

        上传成功后自动发布 sensor_file 消息（字段取 ``stats`` 真值）。
        异常收口为 WARN，绝不冒泡。

        Args:
            file_path: 待上传文件。
            data_type: 数据类型。
            stats: 录制统计（写入 sensor_file）。
            scene_id: 场景 ID。
            vehicle_id: 孪生车 ID。
            producer: 投递任务时捕获的生产者（None 时回退到当前生产者）。
        """
        publisher = producer if producer is not None else self._producer
        with self._upload_exec_lock:
            try:
                result = self.uploader.upload(file_path, data_type=data_type)
                with self._upload_counters_lock:
                    self._upload_success += 1
                CLOUD_UPLOAD_TOTAL.labels(result="success").inc()
                logger.info(f"bag 上传完成：{file_path.name} → {result}")
                # 自动发布 sensor_file 消息（用任务自带的生产者，避免跨实例串台）
                if result is not None and publisher is not None:
                    publisher.publish_sensor_file(
                        bucket=result.bucket,
                        object_key=result.object_key,
                        size_bytes=result.size_bytes,
                        sha256=result.sha256,
                        sensor_type=data_type,
                        start_time=float(getattr(stats, "start_time", 0.0) or 0.0),
                        end_time=float(getattr(stats, "end_time", 0.0) or 0.0),
                        frame_count=int(getattr(stats, "frame_count", 0) or 0),
                        vehicle_id=vehicle_id,
                        scene_id=scene_id,
                        md5=getattr(result, "md5", "") or "",
                        file_format="mcap" if stats is not None else "",
                        topics=list(getattr(stats, "topics", ()) or ()),
                    )
            except Exception as exc:  # noqa: BLE001
                with self._upload_counters_lock:
                    self._upload_failed += 1
                CLOUD_UPLOAD_TOTAL.labels(result="failed").inc()
                logger.warning(f"bag 上传失败（已忽略，不阻断仿真）：{type(exc).__name__}: {exc}")

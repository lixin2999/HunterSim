"""HunterCore 数据接入聚合入口（V2.2.0）。

`CloudConnector` 组合 `TelemetryProducer` 与 `BagUploader`，对桌面客户端暴露
`start / stop / publish_state / publish_event / upload_bag` 五个动作。未启用或
凭据缺失时整体降级为 no-op（各方法安全返回），绝不阻断仿真主循环。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hunter_sim.cloud.bag_uploader import BagUploader
from hunter_sim.cloud.telemetry_producer import TelemetryProducer
from hunter_sim.common.metrics import CLOUD_UPLOAD_DROPPED_TOTAL, CLOUD_UPLOAD_TOTAL
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
        # bag 上传（文件哈希 + httpx PUT，可达数秒~分钟）放后台线程，绝不阻塞调用线程。
        self._worker_lock = threading.Lock()
        # 串行化上传执行：即便 stop() 因长 IO 使 join 超时、旧收尾线程与新线程短暂并存，
        # 也保证任意时刻至多一次在途 uploader.upload，杠绝并发双上传。
        self._upload_exec_lock = threading.Lock()
        self._upload_q: "Optional[queue.Queue[Any]]" = None
        self._upload_thread: Optional[threading.Thread] = None
        self._upload_stop: Optional[threading.Event] = None
        # V2.6.0：上传结果/溢出丢弃计数（可经 upload_stats 读取，指标同步写 Prometheus）
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

    def upload_bag(self, file_path: Path, data_type: str = "rosbag") -> None:
        """将 bag 上传任务投递到后台有界队列（fire-and-forget，不阻塞调用线程）。

        V2.6.0：队列容量由 ``upload_queue_max_size`` 限定，避免 MinIO/网络长时故障
        时任务无界堆积在内存。队满时丢弃**最旧**待传任务为新任务腾位（丢弃计入
        指标与告警日志）；未启用/未启动时直接返回。
        """
        if not self._started:
            return
        self._ensure_upload_worker()
        q = self._upload_q
        if q is None:
            return
        job: tuple[Path, str] = (file_path, data_type)
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
            path, dtype = job
            self._perform_upload(path, dtype)

    def _perform_upload(self, file_path: Path, data_type: str) -> None:
        """实际执行上传（在上传线程）；串行化保证任意时刻至多一次在途上传。

        即便某次 stop() 时上传线程正卡在长网络 IO、join(3.0) 超时导致"旧收尾线程 +
        新线程"短暂并存，执行锁也杠绝并发双上传（不同 bag 串行）；旧线程完成当前
        上传后因 stop 信号退出，不阻塞后续。异常收口为 WARN，绝不冒泡。
        """
        with self._upload_exec_lock:
            try:
                result = self.uploader.upload(file_path, data_type=data_type)
                with self._upload_counters_lock:
                    self._upload_success += 1
                CLOUD_UPLOAD_TOTAL.labels(result="success").inc()
                logger.info(f"bag 上传完成：{file_path.name} → {result}")
            except Exception as exc:  # noqa: BLE001
                with self._upload_counters_lock:
                    self._upload_failed += 1
                CLOUD_UPLOAD_TOTAL.labels(result="failed").inc()
                logger.warning(f"bag 上传失败（已忽略，不阻断仿真）：{type(exc).__name__}: {exc}")

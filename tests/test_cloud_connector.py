"""CloudConnector 聚合入口单测（V2.2.0，注入 fake producer/client）。

校验未启用 no-op、启用后 start 聚合两条链路、publish_state 按 telemetry_interval_s
节流、upload_bag 透传；V2.6.0 补充有界上传队列的溢出丢弃与成功/失败计数（P0-4）。
不依赖真实 Kafka/httpx。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.common.metrics import CLOUD_UPLOAD_DROPPED_TOTAL, counter_value
from hunter_sim.common.models import CloudSettings, Transform, VehicleState


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> None:  # noqa: ANN001, D102
        self.sent.append((topic, key, value))

    def flush(self, timeout: float = 3.0) -> None:  # noqa: ANN001, D102
        return None

    def close(self) -> None:  # noqa: D102
        return None


def _settings(**overrides: object) -> CloudSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "vehicle_id": "SIM_001",
        "jwt_token": "t0k",
        "telemetry_interval_s": 0.1,
    }
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


def _state() -> VehicleState:
    return VehicleState(
        time_stamp=1700000000.0,
        transform=Transform(x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0, roll=0.0),
        vehicle_speed=1.0,
    )


def test_disabled_noop() -> None:
    """未启用时 start 返回 False，发布/上传安全返回。"""
    conn = CloudConnector(_settings(enabled=False))
    assert conn.enabled is False
    assert conn.start() is False
    assert conn.publish_state(_state()) is False
    assert conn.upload_bag(Path("x.mcap")) is None


def test_start_and_publish() -> None:
    """启用后 start 聚合链路，publish_state/event/health 落到 fake producer。"""
    fake = FakeProducer()
    conn = CloudConnector(_settings(), producer_factory=lambda s: fake)
    assert conn.start() is True
    assert conn.started is True
    assert conn.publish_state(_state()) is True
    assert conn.publish_event("harsh_braking") is True
    assert conn.publish_health() is True
    topics = [t for t, _, _ in fake.sent]
    assert topics == ["hunter.SIM_001.telemetry", "hunter.SIM_001.event", "hunter.SIM_001.health"]
    conn.stop()
    assert conn.started is False


def test_publish_state_throttles() -> None:
    """telemetry_interval_s 上限时第二次发布被节流（返回 True 但不落 broker）。"""
    fake = FakeProducer()
    conn = CloudConnector(_settings(telemetry_interval_s=1.0), producer_factory=lambda s: fake)
    conn.start()
    first = conn.publish_state(_state())
    second = conn.publish_state(_state())
    assert first is True
    assert second is True  # 节流视为"已按节拍处理"但未发送
    assert len(fake.sent) == 1


def test_publish_event_swallows_exception() -> None:
    """publish_event 内部映射/发送异常时记 WARN 返回 False，绝不冒泡阻断仿真。"""
    conn = CloudConnector(_settings(), producer_factory=lambda s: FakeProducer())
    conn.start()

    def boom(*args: object, **kwargs: object) -> bool:
        raise ValueError("unknown event_type")

    conn.producer.publish_event = boom  # type: ignore[method-assign]
    assert conn.publish_event("not_a_contract_event") is False


def test_upload_bag_swallows_exception() -> None:
    """上传执行（_perform_upload）失败时记 WARN 不冒泡（C-1）。"""
    conn = CloudConnector(_settings(), producer_factory=lambda s: FakeProducer())
    conn.start()

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("presign 500")

    conn.uploader.upload = boom  # type: ignore[method-assign]
    # 直接调用上传线程入口：异常应被收口，不向上抛出
    conn._perform_upload(Path("x.mcap"), "rosbag")
    conn.stop()


def test_upload_bag_runs_in_background() -> None:
    """upload_bag 不阻塞调用线程：任务投递到后台线程，stop() join 后完成（C-2）。"""
    recorded: list[str] = []
    conn = CloudConnector(_settings(), producer_factory=lambda s: FakeProducer())
    conn.start()

    def fake_upload(path: object, data_type: str = "rosbag") -> object:
        recorded.append(str(path))
        return object()

    conn.uploader.upload = fake_upload  # type: ignore[method-assign]
    assert conn.upload_bag(Path("run1.mcap")) is None  # fire-and-forget，立即返回
    conn.stop()  # join 等待后台线程处理完已入队任务
    assert "run1.mcap" in (recorded[0] if recorded else "")


def test_upload_worker_restarts_after_stop() -> None:
    """stop → start 重启后新 worker 应分配全新 Event，不会因旧 Event 已置位而立即退出。"""
    recorded: list[str] = []
    conn = CloudConnector(_settings(), producer_factory=lambda s: FakeProducer())

    def fake_upload(path: object, data_type: str = "rosbag") -> object:
        recorded.append(Path(path).name)
        return object()

    conn.uploader.upload = fake_upload  # type: ignore[method-assign]

    conn.start()
    conn.upload_bag(Path("run1.mcap"))
    conn.stop()

    conn.start()  # 重启
    conn.uploader.upload = fake_upload  # type: ignore[method-assign]
    conn.upload_bag(Path("run2.mcap"))
    conn.stop()

    assert "run1.mcap" in recorded and "run2.mcap" in recorded  # 两轮均落数，无静默失效


# ─── V2.6.0：有界上传队列（P0-4） ──────────────────────────────────────────────────


class _SentinelThread:
    """伪“存活”工作线程：使 _ensure_upload_worker 提前返回，不覆盖注入队列。"""

    @staticmethod
    def is_alive() -> bool:
        return True

    @staticmethod
    def join(timeout: float = 0.0) -> None:
        return None


def _workerless_connector(**overrides: object) -> CloudConnector:
    """构造已启动但不跑真 worker 的连接器（预置哨兵线程，直接操作队列/计数）。"""
    conn = CloudConnector(_settings(upload_queue_max_size=2, **overrides))
    conn._started = True
    conn._upload_thread = _SentinelThread()  # type: ignore[assignment]
    conn._upload_stop = threading.Event()
    return conn


def test_upload_queue_is_bounded_by_setting() -> None:
    """_ensure_upload_worker 按配置创建有界队列。"""
    conn = CloudConnector(_settings(upload_queue_max_size=7))
    conn._ensure_upload_worker()
    try:
        assert conn._upload_q is not None
        assert conn._upload_q.maxsize == 7
    finally:
        conn.stop()


class _FakeFullQueue:
    """丢弃路径专测的队列替身：前 full_times 次 put_nowait 报满，之后成功入队。

    get_nowait 返回预置的最旧任务（无则抛 Empty），用于驱动腾位丢弃分支。
    """

    def __init__(self, oldest: Any = None, full_times: int = 1) -> None:
        self.oldest = oldest
        self._full_times = full_times
        self.put_calls = 0

    def put_nowait(self, job: Any) -> None:
        self.put_calls += 1
        if self.put_calls <= self._full_times:
            raise queue.Full

    def get_nowait(self) -> Any:
        if self.oldest is None:
            raise queue.Empty
        job = self.oldest
        self.oldest = None
        return job


def test_upload_bag_drops_oldest_when_full() -> None:
    """队满时丢最旧腾位重试成功：新任务保留、旧任务被丢弃并计数。"""
    conn = _workerless_connector()
    baseline = counter_value(CLOUD_UPLOAD_DROPPED_TOTAL)
    # 首次 put 报满 → get_nowait 取走最旧 → 重试 put 成功
    fake = _FakeFullQueue(oldest=(Path("old-1.mcap"), "rosbag"), full_times=1)
    conn._upload_q = fake  # type: ignore[assignment]

    conn.upload_bag(Path("new.mcap"))

    assert conn.upload_stats["dropped"] == 1  # 丢旧任务计入
    assert fake.put_calls == 2  # 首次失败 + 腾位后重试成功
    assert counter_value(CLOUD_UPLOAD_DROPPED_TOTAL) >= baseline + 1


def test_upload_bag_drops_new_when_retry_still_full() -> None:
    """无可 eviction 且重试仍满（极端并发）时丢弃新任务本身，不阻塞调用线程。"""
    conn = _workerless_connector()
    # get_nowait 抛 Empty（无可 eviction）→ 重试 put 仍满 → 仅丢新任务一次
    fake = _FakeFullQueue(oldest=None, full_times=99)
    conn._upload_q = fake  # type: ignore[assignment]

    conn.upload_bag(Path("doomed.mcap"))

    assert conn.upload_stats["dropped"] == 1  # 丢的是新任务
    assert fake.put_calls == 2  # 两次尝试均报满


def test_upload_bag_get_race_falls_through_to_worker() -> None:
    """并发消费者腾空队列（get_nowait Empty）：不丢任何任务，重试入队成功。"""
    conn = _workerless_connector()
    # 首次报满 → get_nowait Empty（并发消费者已腾空）→ 重试 put 成功，无丢弃
    fake = _FakeFullQueue(oldest=None, full_times=1)
    conn._upload_q = fake  # type: ignore[assignment]

    conn.upload_bag(Path("keep.mcap"))

    assert conn.upload_stats["dropped"] == 0
    assert fake.put_calls == 2


def test_upload_stats_count_success_and_failed() -> None:
    """_perform_upload 按结果累加 success/failed 计数。"""
    conn = _workerless_connector()

    conn.uploader.upload = lambda path, data_type="rosbag": object()  # type: ignore[method-assign, assignment]
    conn._perform_upload(Path("ok.mcap"), "rosbag")

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("minio down")

    conn.uploader.upload = _boom  # type: ignore[method-assign]
    conn._perform_upload(Path("bad.mcap"), "rosbag")

    stats = conn.upload_stats
    assert stats == {"success": 1, "failed": 1, "dropped": 0}


def test_bounded_queue_end_to_end_backpressure() -> None:
    """worker 慢消费时限流背压：超出容量的旧任务被丢弃，新任务仍能上传。"""
    gate = threading.Event()
    a_started = threading.Event()
    recorded: list[str] = []
    record_lock = threading.Lock()

    conn = CloudConnector(
        _settings(upload_queue_max_size=2), producer_factory=lambda s: FakeProducer()
    )

    def _slow_upload(path: object, data_type: str = "rosbag") -> object:
        if str(path) == "a.mcap":
            a_started.set()
            assert gate.wait(timeout=5.0)  # 首单在途时卡住 worker
        with record_lock:
            recorded.append(Path(path).name)
        return object()

    conn.uploader.upload = _slow_upload  # type: ignore[method-assign]
    conn.start()
    try:
        conn.upload_bag(Path("a.mcap"))  # 被 worker 取走并阻塞在 gate
        assert a_started.wait(timeout=2.0)  # 确认队列已腾位，后续入队时序确定
        conn.upload_bag(Path("b.mcap"))  # 入队
        conn.upload_bag(Path("c.mcap"))  # 入队（满）
        for name in ("d", "e", "f"):
            conn.upload_bag(Path(f"{name}.mcap"))  # 队满 → 依次丢最旧
    finally:
        gate.set()
        conn.stop()

    stats = conn.upload_stats
    assert stats["dropped"] == 3
    assert stats["success"] == 3  # a + e + f
    assert stats["failed"] == 0
    with record_lock:
        assert "a.mcap" in recorded and "f.mcap" in recorded

"""CloudConnector 聚合入口单测（V2.10.0，注入 fake producer/client）。

校验未启用 no-op、启用后 start 聚合链路、publish_telemetry 按
telemetry_proxy_interval_s 节流、upload_bag 透传；V2.6.0 补充有界上传
队列的溢出丢弃与成功/失败计数（P0-4）。不依赖真实 Kafka/httpx。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.common.metrics import CLOUD_UPLOAD_DROPPED_TOTAL, counter_value
from hunter_sim.common.models import CloudSettings


class FakeKafkaProducer:
    """桩生产者：记录 send 调用，兼容 SimDataProducer 接口。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> Any:  # noqa: ANN001, D102
        self.sent.append((topic, key, value))
        return _FakeFuture()

    def flush(self, timeout: float = 3.0) -> None:  # noqa: ANN001, D102
        return None

    def close(self, timeout: float = None) -> None:  # noqa: D102
        return None


class _FakeFuture:
    def get(self, timeout: float = None) -> None:  # noqa: ANN001, D102
        return None


def _settings(**overrides: object) -> CloudSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "sim_id": "sim-001",
        "sensor_data_topic_template": "carla.{sim_id}.sensor_data",
        "sensor_data_dlq_template": "carla.{sim_id}.sensor_data.dlq",
        "telemetry_proxy_interval_s": 0.1,
    }
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


def _factory(fake: FakeKafkaProducer) -> Any:
    """返回兼容 SimDataProducer 的 producer_factory。"""

    def factory(settings: CloudSettings, sim_id: str) -> FakeKafkaProducer:
        return fake

    return factory


def _tel_msg() -> dict[str, Any]:
    return {
        "type": "sim_telemetry",
        "sim_id": "sim-001",
        "payload": {"pose": {"x": 0.0, "y": 0.0}},
    }


def _status_msg() -> dict[str, Any]:
    return {
        "type": "sim_status",
        "sim_id": "sim-001",
        "payload": {"status": "RUNNING", "progress_percent": 50.0},
    }


def test_disabled_noop() -> None:
    """未启用时 start 返回 False，各发布方法安全返回 False。"""
    conn = CloudConnector(_settings(enabled=False))
    assert conn.enabled is False
    assert conn.start("sim-x") is False
    assert conn.publish_telemetry(_tel_msg()) is False
    assert conn.publish_status(_status_msg()) is False
    assert conn.upload_bag(Path("x.mcap")) is None


def test_start_and_publish_telemetry() -> None:
    """启用后 start 聚合链路，publish_telemetry/status/event 落到 fake producer。"""
    fake = FakeKafkaProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(fake))
    assert conn.start("sim-001") is True
    assert conn.started is True
    assert conn.publish_telemetry(_tel_msg()) is True
    assert conn.publish_status(_status_msg()) is True
    assert conn.publish_event(
        "collision", "critical", sim_time=1.0, description="test collision"
    ) is True
    # topic 应均为 carla.sim-001.sensor_data
    for topic, _, _ in fake.sent:
        assert topic == "carla.sim-001.sensor_data"
    assert len(fake.sent) == 3
    conn.stop()
    assert conn.started is False


def test_publish_telemetry_throttles() -> None:
    """telemetry_proxy_interval_s 节流：间隔内第二次发布被跳过（返回 True）。"""
    fake = FakeKafkaProducer()
    conn = CloudConnector(
        _settings(telemetry_proxy_interval_s=1.0), producer_factory=_factory(fake)
    )
    conn.start("sim-001")
    first = conn.publish_telemetry(_tel_msg())
    second = conn.publish_telemetry(_tel_msg())
    assert first is True
    assert second is True  # 节流视为"已按节拍处理"但未发送
    assert len(fake.sent) == 1
    conn.stop()


def test_publish_event_swallows_exception() -> None:
    """publish_event 内部异常时返回 False，绝不冒泡阻断仿真。"""
    fake = FakeKafkaProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(fake))
    conn.start("sim-001")

    def boom(**kwargs: Any) -> bool:
        raise ValueError("unknown event_type")

    if conn.producer is not None:
        conn.producer.publish_event = boom  # type: ignore[method-assign]
    assert conn.publish_event("not_a_real_event") is False
    conn.stop()


def test_upload_bag_swallows_exception() -> None:
    """_perform_upload 失败时记 WARN 不冒泡（C-1）。"""
    fake = FakeKafkaProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(fake))
    conn.start("sim-001")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("presign 500")

    conn.uploader.upload = boom  # type: ignore[method-assign]
    conn._perform_upload(Path("x.mcap"), "rosbag")
    conn.stop()
    assert conn.upload_stats["failed"] == 1


def test_upload_bag_runs_in_background() -> None:
    """upload_bag 不阻塞调用线程：任务投递到后台线程，stop() join 后完成。"""
    recorded: list[str] = []
    fake = FakeKafkaProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(fake))
    conn.start("sim-001")

    def fake_upload(path: Any, data_type: str = "rosbag") -> Any:
        recorded.append(str(path))
        return None  # 返回 None 避免 sensor_file publish 报错

    conn.uploader.upload = fake_upload  # type: ignore[method-assign]
    assert conn.upload_bag(Path("run1.mcap")) is None  # fire-and-forget
    conn.stop()
    assert "run1.mcap" in (recorded[0] if recorded else "")


def test_upload_worker_restarts_after_stop() -> None:
    """stop → start 重启后新 worker 应分配全新 Event，不会立即退出。"""
    recorded: list[str] = []
    fake = FakeKafkaProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(fake))

    def fake_upload(path: Any, data_type: str = "rosbag") -> Any:
        recorded.append(Path(path).name)
        return None

    conn.start("sim-001")
    conn.uploader.upload = fake_upload  # type: ignore[method-assign]  start 后 patch
    conn.upload_bag(Path("run1.mcap"))
    conn.stop()

    conn.start("sim-001")
    conn.uploader.upload = fake_upload  # type: ignore[method-assign]  start 重建 uploader 后再 patch
    conn.upload_bag(Path("run2.mcap"))
    conn.stop()

    assert "run1.mcap" in recorded and "run2.mcap" in recorded


# ─── V2.6.0：有界上传队列（P0-4）──────────────────────────────────────────────


class _SentinelThread:
    """伪"存活"工作线程：使 _ensure_upload_worker 提前返回。"""

    @staticmethod
    def is_alive() -> bool:
        return True

    @staticmethod
    def join(timeout: float = 0.0) -> None:
        return None


def _workerless_connector(**overrides: object) -> CloudConnector:
    """构造已启动但不跑真 worker 的连接器。"""
    conn = CloudConnector(_settings(upload_queue_max_size=2, **overrides))
    conn._started = True
    conn._upload_thread = _SentinelThread()  # type: ignore[assignment]
    conn._upload_stop = threading.Event()
    return conn


def test_upload_queue_is_bounded_by_setting() -> None:
    """_ensure_upload_worker 按配置创建有界队列。"""
    fake = FakeKafkaProducer()
    conn = CloudConnector(_settings(upload_queue_max_size=7), producer_factory=_factory(fake))
    conn._ensure_upload_worker()
    try:
        assert conn._upload_q is not None
        assert conn._upload_q.maxsize == 7
    finally:
        conn.stop()


class _FakeFullQueue:
    """丢弃路径专测的队列替身。"""

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
    fake_q = _FakeFullQueue(oldest=(Path("old-1.mcap"), "rosbag"), full_times=1)
    conn._upload_q = fake_q  # type: ignore[assignment]
    conn.upload_bag(Path("new.mcap"))
    assert conn.upload_stats["dropped"] == 1
    assert fake_q.put_calls == 2
    assert counter_value(CLOUD_UPLOAD_DROPPED_TOTAL) >= baseline + 1


def test_upload_bag_drops_new_when_retry_still_full() -> None:
    """无可 eviction 且重试仍满时丢弃新任务本身。"""
    conn = _workerless_connector()
    fake_q = _FakeFullQueue(oldest=None, full_times=99)
    conn._upload_q = fake_q  # type: ignore[assignment]
    conn.upload_bag(Path("doomed.mcap"))
    assert conn.upload_stats["dropped"] == 1
    assert fake_q.put_calls == 2


def test_upload_stats_count_success_and_failed() -> None:
    """_perform_upload 按结果累加 success/failed 计数。"""
    conn = _workerless_connector()

    class _FakeResult:
        bucket = "b"
        object_key = "k"
        size_bytes = 100
        sha256 = "abc"

    conn.uploader.upload = lambda path, data_type="rosbag": _FakeResult()  # type: ignore[method-assign]
    conn._perform_upload(Path("ok.mcap"), "rosbag")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("minio down")

    conn.uploader.upload = _boom  # type: ignore[method-assign]
    conn._perform_upload(Path("bad.mcap"), "rosbag")

    stats = conn.upload_stats
    assert stats["success"] == 1
    assert stats["failed"] == 1

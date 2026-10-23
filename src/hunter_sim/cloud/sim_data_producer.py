"""SimDataProducer — 向 carla.{sim_id}.sensor_data 发布类型化消息（P1）。

每个仿真实例持有一个独立的 SimDataProducer 实例，key=sim_id，acks=all。

发布路径：
- ``publish_status()``    — sim_status（1Hz + 状态跃迁）
- ``publish_telemetry()`` — sim_telemetry（10Hz）
- ``publish_event()``     — sim_event
- ``publish_metrics()``   — sim_metrics（场景终止后）
- ``publish_sensor_file()`` — sensor_file（bag 上传完成后）
- ``publish_artifact()``  — artifact（eval.json 等产物）

遵循可选依赖隔离规范：kafka 惰性导入，缺失或未启用时整体降级为 no-op，
消息进入有界内存缓冲，``flush()`` 尽力重投，绝不阻断仿真主循环。

替代关系：本类取代 ``TelemetryProducer``（已 @deprecated）。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from typing import Any, Callable, Optional

from hunter_sim.cloud.sim_data_mapper import (
    build_artifact,
    build_sensor_file,
    build_sim_event,
    build_sim_metrics,
    build_sim_status,
    build_sim_telemetry,
    SimStatusPayload,
)
from hunter_sim.common.metrics import (
    SENSOR_FILE_PUBLISHED_TOTAL,
    SIM_DATA_BUFFER_SIZE,
    SIM_DATA_PRODUCER_ERRORS_TOTAL,
    SIM_EVENT_PUBLISHED_TOTAL,
    SIM_METRICS_PUBLISHED_TOTAL,
    SIM_STATUS_PUBLISHED_TOTAL,
)
from hunter_sim.common.models import CloudSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_MAX_BUFFER = 2000

# 生产者工厂注入点（单测用桩替换，避免依赖真实 kafka-python）
ProducerFactory = Callable[[CloudSettings, str], Any]


class SimDataProducer:
    """按实例向 carla.{sim_id}.sensor_data 发布类型化消息。

    key=sim_id，acks=all，有界缓冲，DLQ 转发。

    Args:
        settings: HunterCore 接入配置（读取 sensor_data_topic_template 等字段）。
        sim_id: 当前仿真实例 ID（等于 SimInstance.sim_instance_id）。
        producer_factory: 可选 Kafka 生产者工厂（单测注入）；
            签名 ``(settings, sim_id) -> KafkaProducer``，
            需提供 ``send(topic, key, value)`` / ``flush()`` / ``close()``。
    """

    def __init__(
        self,
        settings: CloudSettings,
        sim_id: str,
        producer_factory: Optional[ProducerFactory] = None,
    ) -> None:
        self._settings = settings
        self._sim_id = sim_id
        self._factory = producer_factory
        self._producer: Any = None
        self._available = False
        self._buffer: deque[tuple[str, bytes, bytes]] = deque(maxlen=_MAX_BUFFER)
        self._lock = threading.Lock()
        self._seq = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """建立生产者；未启用/缺依赖/构建失败时降级为 no-op。

        Returns:
            是否可用（False = 后续发布仅进缓冲，不发往 broker）。
        """
        if not self._settings.enabled:
            logger.info(
                f"SimDataProducer[{self._sim_id}] 未启用，降级为 no-op"
            )
            self._available = False
            return False
        try:
            if self._factory is not None:
                self._producer = self._factory(self._settings, self._sim_id)
            else:
                self._producer = self._build_kafka_producer()
            self._available = True
            logger.info(
                f"SimDataProducer[{self._sim_id}] 已连接：{self._settings.kafka_bootstrap} "
                f"topic={self.sensor_data_topic}"
            )
        except Exception as exc:  # noqa: BLE001  依赖缺失/认证失败均降级，不阻断仿真
            self._producer = None
            self._available = False
            SIM_DATA_PRODUCER_ERRORS_TOTAL.labels(sim_id=self._sim_id, op="connect").inc()
            logger.warning(
                f"SimDataProducer[{self._sim_id}] 初始化失败，降级为 no-op："
                f"{type(exc).__name__}: {exc}"
            )
        return self._available

    def _build_kafka_producer(self) -> Any:
        """惰性构建 kafka-python KafkaProducer（acks=all，SASL_SSL）。"""
        from kafka import KafkaProducer  # noqa: PLC0415  可选依赖惰性导入

        s = self._settings
        kwargs: dict[str, Any] = {
            "bootstrap_servers": s.kafka_bootstrap,
            "security_protocol": s.security_protocol,
            "sasl_mechanism": s.sasl_mechanism,
            "acks": s.sensor_data_acks,          # "all" — 保证可靠性
            "max_in_flight_requests_per_connection": s.sensor_data_max_inflight,
            "linger_ms": 5,
            "retries": 5,
            "max_block_ms": 1000,
            "delivery_timeout_ms": 10000,
        }
        if s.sasl_username is not None:
            kwargs["sasl_plain_username"] = s.sasl_username.get_secret_value()
        if s.sasl_password is not None:
            kwargs["sasl_plain_password"] = s.sasl_password.get_secret_value()
        if s.security_protocol.upper().endswith("SSL") and s.ca_cert_path:
            kwargs["ssl_cafile"] = s.ca_cert_path
        return KafkaProducer(**kwargs)

    def close(self, timeout: float = 3.0) -> None:
        """刷新缓冲并关闭底层生产者。"""
        self.flush()
        producer = self._producer
        if producer is not None:
            try:
                producer.flush(timeout=timeout) if _accepts_timeout(producer) else producer.flush()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"SimDataProducer flush error: {exc}")
            try:
                producer.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"SimDataProducer close error: {exc}")
        self._producer = None
        self._available = False

    # ── 属性 ───────────────────────────────────────────────────────────────

    @property
    def sensor_data_topic(self) -> str:
        """当前实例的 sensor_data Topic 全名。"""
        return self._settings.sensor_data_topic_template.format(sim_id=self._sim_id)

    @property
    def dlq_topic(self) -> str:
        """当前实例的 DLQ Topic 全名。"""
        return self._settings.sensor_data_dlq_template.format(sim_id=self._sim_id)

    @property
    def sim_id(self) -> str:
        """当前实例 sim_id。"""
        return self._sim_id

    @property
    def available(self) -> bool:
        """生产者是否可用（已连接且启用）。"""
        return self._available

    @property
    def buffered_count(self) -> int:
        """当前待重投缓冲消息数。"""
        with self._lock:
            return len(self._buffer)

    # ── 序号 ───────────────────────────────────────────────────────────────

    def next_seq(self) -> int:
        """返回并自增单调序号（线程安全）。"""
        with self._lock:
            self._seq += 1
            return self._seq

    # ── 发布接口 ────────────────────────────────────────────────────────────

    def publish_status(self, message: dict[str, Any]) -> bool:
        """发布 sim_status 消息（由调用方通过 build_sim_status 构造）。"""
        ok = self._send(message)
        if ok:
            SIM_STATUS_PUBLISHED_TOTAL.inc()
        return ok

    def publish_status_from_payload(
        self,
        scene_id: str,
        payload: SimStatusPayload,
    ) -> bool:
        """便捷方法：从 SimStatusPayload 构建并发布 sim_status。"""
        seq = self.next_seq()
        msg = build_sim_status(
            sim_id=self._sim_id,
            scene_id=scene_id,
            payload=payload,
            seq=seq,
        )
        return self.publish_status(msg)

    def publish_telemetry(self, message: dict[str, Any]) -> bool:
        """发布 sim_telemetry 消息（由调用方通过 build_sim_telemetry 构造）。"""
        return self._send(message)

    def publish_event(  # noqa: PLR0913
        self,
        vehicle_id: Optional[str],
        scene_id: str,
        event_type: str,
        event_level: str,
        sim_time: float,
        description: str,
        data: Optional[dict[str, Any]] = None,
    ) -> bool:
        """构建并发布 sim_event 消息。"""
        import time as _time  # noqa: PLC0415

        seq = self.next_seq()
        msg = build_sim_event(
            sim_id=self._sim_id,
            vehicle_id=vehicle_id,
            scene_id=scene_id,
            seq=seq,
            ts=_time.time(),
            event_type=event_type,
            event_level=event_level,
            sim_time=sim_time,
            description=description,
            data=data,
        )
        ok = self._send(msg)
        if ok:
            SIM_EVENT_PUBLISHED_TOTAL.inc()
        return ok

    def publish_metrics(self, message: dict[str, Any]) -> bool:
        """发布 sim_metrics 消息（由调用方通过 build_sim_metrics 构造）。"""
        ok = self._send(message)
        if ok:
            SIM_METRICS_PUBLISHED_TOTAL.inc()
        return ok

    def publish_sensor_file(
        self,
        bucket: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
        sensor_type: str = "rosbag",
        start_time: float = 0.0,
        end_time: float = 0.0,
        frame_count: int = 0,
        vehicle_id: Optional[str] = None,
        scene_id: str = "",
    ) -> bool:
        """构建并发布 sensor_file 消息（bag 上传完成后调用）。"""
        import time as _time  # noqa: PLC0415

        seq = self.next_seq()
        msg = build_sensor_file(
            sim_id=self._sim_id,
            seq=seq,
            ts=_time.time(),
            bucket=bucket,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
            sensor_type=sensor_type,
            start_time=start_time,
            end_time=end_time,
            frame_count=frame_count,
            vehicle_id=vehicle_id,
            scene_id=scene_id,
        )
        ok = self._send(msg)
        if ok:
            SENSOR_FILE_PUBLISHED_TOTAL.inc()
        return ok

    def publish_artifact(
        self,
        name: str,
        bucket: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
        media_type: str,
        vehicle_id: Optional[str] = None,
        scene_id: str = "",
    ) -> bool:
        """构建并发布 artifact 消息（eval.json / events.jsonl / run.log 等）。"""
        import time as _time  # noqa: PLC0415

        seq = self.next_seq()
        msg = build_artifact(
            sim_id=self._sim_id,
            seq=seq,
            ts=_time.time(),
            name=name,
            bucket=bucket,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
            media_type=media_type,
            vehicle_id=vehicle_id,
            scene_id=scene_id,
        )
        return self._send(msg)

    # ── 内部发送 ────────────────────────────────────────────────────────────

    def _send(self, message: dict[str, Any]) -> bool:
        """发送一条 JSON 消息到 sensor_data Topic。

        kafka-python send() 返回 Future：broker 不可达 / SASL 认证失败 / 投递超时
        都是异步错误，通过 errback 捕获并重入有界缓冲。
        """
        topic = self.sensor_data_topic
        key = self._sim_id.encode("utf-8")
        value = json.dumps(message, default=str).encode("utf-8")
        if not self._available or self._producer is None:
            self._buffer_message(topic, key, value)
            return False
        try:
            future = self._producer.send(topic, key=key, value=value)
        except Exception as exc:  # noqa: BLE001  序列化/缓冲满等同步异常
            SIM_DATA_PRODUCER_ERRORS_TOTAL.labels(sim_id=self._sim_id, op="send").inc()
            logger.warning(
                f"SimDataProducer[{self._sim_id}] send 失败（topic={topic}），入缓冲："
                f"{type(exc).__name__}: {exc}"
            )
            self._buffer_message(topic, key, value)
            return False
        add_errback = getattr(future, "add_errback", None)
        if callable(add_errback):
            try:
                add_errback(
                    lambda exc, t=topic, k=key, v=value: self._on_send_error(exc, t, k, v)
                )
            except Exception:  # noqa: BLE001
                pass
        return True

    def _on_send_error(self, exc: BaseException, topic: str, key: bytes, value: bytes) -> None:
        """Kafka 异步投递失败回调：记 WARN + 指标，重入缓冲待 flush 重投。"""
        SIM_DATA_PRODUCER_ERRORS_TOTAL.labels(sim_id=self._sim_id, op="async").inc()
        logger.warning(
            f"SimDataProducer[{self._sim_id}] 异步投递失败（topic={topic}），"
            f"入缓冲重投：{type(exc).__name__}: {exc}"
        )
        self._buffer_message(topic, key, value)

    def _buffer_message(self, topic: str, key: bytes, value: bytes) -> None:
        """把一条消息放入有界重投缓冲（maxlen 满时自动丢弃最旧）。"""
        with self._lock:
            self._buffer.append((topic, key, value))
            SIM_DATA_BUFFER_SIZE.labels(sim_id=self._sim_id).set(len(self._buffer))

    def flush(self) -> int:
        """尽力重投缓冲消息，返回成功重投条数。"""
        if self._producer is None or not self._available:
            return 0
        resent = 0
        while True:
            with self._lock:
                if not self._buffer:
                    break
                topic, key, value = self._buffer.popleft()
                SIM_DATA_BUFFER_SIZE.labels(sim_id=self._sim_id).set(len(self._buffer))
            try:
                self._producer.send(topic, key=key, value=value)
                resent += 1
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._buffer.appendleft((topic, key, value))
                break
        if resent:
            logger.info(f"SimDataProducer[{self._sim_id}] 缓冲重投 {resent} 条")
        return resent

    def send_to_dlq(self, original_topic: str, raw_value: bytes, reason: str = "") -> None:
        """将消息转发到 DLQ（broker 不可达 / Schema 校验失败时调用）。"""
        dlq = self.dlq_topic
        key = self._sim_id.encode("utf-8")
        if not self._available or self._producer is None:
            logger.warning(
                f"SimDataProducer[{self._sim_id}] DLQ 不可用，消息丢弃"
                f"（from={original_topic}, reason={reason}）"
            )
            return
        try:
            self._producer.send(dlq, key=key, value=raw_value)
            logger.info(f"SimDataProducer[{self._sim_id}] 已转发 DLQ（from={original_topic}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SimDataProducer[{self._sim_id}] DLQ 写入失败: {exc}")


def _accepts_timeout(producer: Any) -> bool:
    """探测 flush 是否接受 timeout 参数（kafka-python 支持，桩可能不支持）。"""
    import inspect  # noqa: PLC0415

    flush = getattr(producer, "flush", None)
    if flush is None:
        return False
    try:
        return "timeout" in inspect.signature(flush).parameters
    except (TypeError, ValueError):
        return False

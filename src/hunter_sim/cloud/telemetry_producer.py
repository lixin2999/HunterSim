"""HunterCore Kafka 遥测生产者（V2.2.0）。

以"车端"身份向 Kafka（SASL_SSL / SCRAM-SHA-512）发布
`hunter.{vehicle_id}.telemetry / event / health`，key=vehicle_id 保证单车有序。

遵循 CARLA/可选依赖隔离规范：`kafka` 惰性导入，缺失或未启用时整体降级为 no-op
（消息进有界内存缓冲，`flush()` 尽力重投），绝不阻断仿真主循环。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from typing import Any, Callable, Optional

from hunter_sim.cloud.telemetry_mapper import build_event, build_health, build_telemetry
from hunter_sim.common.models import CloudSettings, VehicleState
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_MAX_BUFFER = 1000

# 生产者工厂注入点（单测用桩替换，避免依赖真实 kafka-python）
ProducerFactory = Callable[[CloudSettings], Any]


class TelemetryProducer:
    """Kafka 遥测生产者（best-effort，不阻断仿真）。

    Args:
        settings: HunterCore 接入配置。
        producer_factory: 可选生产者工厂（默认惰性构建 kafka-python KafkaProducer）；
            单测注入桩对象，需提供 `send(topic, key, value)` / `flush()` / `close()`。
    """

    def __init__(self, settings: CloudSettings, producer_factory: Optional[ProducerFactory] = None) -> None:
        self._settings = settings
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
            是否可用（False 表示后续发布仅进缓冲，不发往 broker）。
        """
        if not self._settings.enabled:
            logger.info("HunterCore 遥测未启用（HUNTER_CORE_ENABLED=false），生产者降级为 no-op")
            self._available = False
            return False
        try:
            if self._factory is not None:
                self._producer = self._factory(self._settings)
            else:
                self._producer = self._build_kafka_producer()
            self._available = True
            logger.info(f"HunterCore 遥测生产者已连接：{self._settings.kafka_bootstrap}")
        except Exception as exc:  # noqa: BLE001  依赖缺失/认证失败均降级，不阻断仿真
            self._producer = None
            self._available = False
            logger.warning(f"HunterCore 遥测生产者初始化失败，降级为 no-op：{type(exc).__name__}: {exc}")
        return self._available

    def _build_kafka_producer(self) -> Any:
        """惰性构建 kafka-python KafkaProducer（SASL_SSL/SCRAM-SHA-512）。"""
        from kafka import KafkaProducer  # noqa: PLC0415  可选依赖惰性导入

        s = self._settings
        kwargs: dict[str, Any] = {
            "bootstrap_servers": s.kafka_bootstrap,
            "security_protocol": s.security_protocol,
            "sasl_mechanism": s.sasl_mechanism,
            "acks": 1,
            "linger_ms": 5,
            "retries": 3,
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
                logger.debug(f"Kafka producer flush error: {exc}")
            try:
                producer.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Kafka producer close error: {exc}")
        self._producer = None
        self._available = False

    @property
    def available(self) -> bool:
        """生产者是否可用（已连接且启用）。"""
        return self._available

    @property
    def buffered_count(self) -> int:
        """当前待重投缓冲消息数。"""
        with self._lock:
            return len(self._buffer)

    # ── 发布 ──────────────────────────────────────────────────────────────
    def next_seq(self) -> int:
        """返回并自增单调序号（线程安全）。"""
        with self._lock:
            self._seq += 1
            return self._seq

    def publish_telemetry(self, state: VehicleState, seq: Optional[int] = None) -> bool:
        """发布一帧遥测（seq 缺省自动递增）。"""
        payload = build_telemetry(
            self._settings.vehicle_id, self._seq if seq is None else seq, state
        )
        return self._send(self._resolve(self._settings.telemetry_topic), payload)

    def publish_event(self, timestamp: float, event_type: str, **kwargs: Any) -> bool:
        """发布一条事件（等级自动映射）。"""
        payload = build_event(self._settings.vehicle_id, timestamp, event_type, **kwargs)
        return self._send(self._resolve(self._settings.event_topic), payload)

    def publish_health(self, timestamp: float, status: str = "auto_driving") -> bool:
        """发布一条健康心跳。"""
        payload = build_health(self._settings.vehicle_id, timestamp, status=status)
        return self._send(self._resolve(self._settings.health_topic), payload)

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _resolve(self, topic_template: str) -> str:
        """把 `{vehicle_id}` 模板替换为实际车辆 ID。"""
        return topic_template.format(vehicle_id=self._settings.vehicle_id)

    def _send(self, topic: str, payload: dict[str, Any]) -> bool:
        """发送一条 JSON 消息；失败进缓冲（不抛出）。"""
        key = self._settings.vehicle_id.encode("utf-8")
        value = json.dumps(payload, default=str).encode("utf-8")
        if not self._available or self._producer is None:
            with self._lock:
                self._buffer.append((topic, key, value))
            return False
        try:
            self._producer.send(topic, key=key, value=value)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Kafka send 失败（topic={topic}），入缓冲：{type(exc).__name__}: {exc}")
            with self._lock:
                self._buffer.append((topic, key, value))
            return False

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
            try:
                self._producer.send(topic, key=key, value=value)
                resent += 1
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._buffer.appendleft((topic, key, value))
                break
        if resent:
            logger.info(f"HunterCore 缓冲重投 {resent} 条")
        return resent


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

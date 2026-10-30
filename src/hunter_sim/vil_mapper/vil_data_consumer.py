"""VIL Kafka 数据接入服务（V2.10.0：消费 carla.{sim_id}.vil_state）。

从 Kafka 实时消费平台转发的实车遥测数据，解析后推入 TelemetryBuffer。
支持多 Topic 订阅、自动重连、反序列化异常处理。

Topics：
- carla.{sim_id}.vil_state：平台转发的实车定位 + 状态（10Hz）
- legacy_telemetry_topic：兼容单机调试的旧 telemetry_clean（可选）
- hunter.{vehicle_id}.command_result：控制指令回读（10Hz）
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from pydantic import ValidationError as PydanticValidationError

from hunter_sim.cloud.kafka_security import build_consumer_security_kwargs
from hunter_sim.common.exceptions import KafkaConnectionError
from hunter_sim.common.metrics import (
    CLOUD_DLQ_PUBLISHED_TOTAL,
    KAFKA_CONSUMED_TOTAL,
    KAFKA_CONSUMER_LAG,
    KAFKA_CONSUMER_REBUILDS_TOTAL,
    KAFKA_CONSUME_ERRORS_TOTAL,
    VIL_MESSAGE_DROPPED_TOTAL,
    VIL_STATE_CONSUMED_TOTAL,
    observe_vil_e2e_lag,
)
from hunter_sim.common.models import (
    DetectedObject,
    KafkaSettings,
    PerceptionResult,
    Transform,
    VehicleState,
    VILSettings,
)
from hunter_sim.common.utils import get_logger
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame

logger = get_logger(__name__)


class VILDataConsumer:
    """VIL Kafka 数据消费者。

    在独立线程中运行 KafkaConsumer 循环，将解析后的遥测帧推入 TelemetryBuffer。
    通过依赖注入的 buffer 实例与同步服务解耦。

    Args:
        settings: Kafka 连接配置。
        vil_settings: VIL 参数配置。
        vehicle_id: 目标实车 ID。
        buffer: 遥测数据缓冲区。
        sim_id: 当前实例的 sim_id（优先于 settings.sim_id，用于渲染 carla.{sim_id}.vil_state Topic）。
        producer_factory: 可选 Kafka 生产者工厂（DLQ 转发用，单测注入）。
    """

    def __init__(
        self,
        settings: KafkaSettings,
        vil_settings: VILSettings,
        vehicle_id: str,
        buffer: TelemetryBuffer,
        sim_id: str = "",
        producer_factory: Any = None,
    ) -> None:
        self._settings = settings
        self._vil_settings = vil_settings
        self._vehicle_id = vehicle_id
        self._sim_id = sim_id or settings.sim_id  # 优先用参数，次先 settings
        self._buffer = buffer
        self._producer_factory = producer_factory
        # V3.1.2：接入安全参数在构造期解析，配置错误立即失败（不留空转线程）
        self._security_kwargs = build_consumer_security_kwargs(settings)
        self._consumer: Any = None
        self._dlq_producer: Any = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._msg_count: int = 0
        self._error_count: int = 0
        # V2.7.0 P1-8：连续轮询错误计数（达阈值触发消费者重建）
        self._consecutive_errors: int = 0
        # V2.7.0 P1-9：乱序/重复防护（按 timestamp + seq 单调）
        self._last_timestamp: Optional[float] = None
        self._last_seq: Optional[int] = None
        logger.info(
            f"VILDataConsumer initialized: vehicle_id={vehicle_id}, sim_id={self._sim_id}, "
            f"bootstrap={settings.bootstrap_servers}, "
            f"protocol={self._security_kwargs['security_protocol']}, "
            f"tls={'on' if 'ssl_cafile' in self._security_kwargs else 'off'}"
        )

    def start(self) -> None:
        """启动 Kafka 消费线程（非阻塞）。"""
        if self._running.is_set():
            logger.warning("VILDataConsumer already running")
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._consume_loop,
            name=f"VILKafkaConsumer-{self._vehicle_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("VILDataConsumer thread started")

    def stop(self, timeout_s: float = 5.0) -> None:
        """停止消费线程并关闭 Kafka 连接。

        Args:
            timeout_s: 等待线程退出超时时间（秒）。
        """
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_s)
        if self._consumer is not None:
            try:
                self._consumer.close(timeout_ms=2000)
            except Exception as exc:
                logger.warning(f"Error closing Kafka consumer: {exc}")
            self._consumer = None
        if self._dlq_producer is not None:
            try:
                self._dlq_producer.flush(timeout=2.0)
                self._dlq_producer.close(timeout=2.0)
            except Exception as exc:  # noqa: BLE001  关闭 DLQ 生产者失败不影响停止
                logger.warning(f"Error closing DLQ producer: {exc}")
            self._dlq_producer = None
        logger.info(
            f"VILDataConsumer stopped: received={self._msg_count}, errors={self._error_count}"
        )

    @property
    def is_running(self) -> bool:
        """消费线程是否在运行。"""
        return self._running.is_set()

    @property
    def stats(self) -> dict[str, int]:
        """返回消费统计。"""
        return {"received": self._msg_count, "errors": self._error_count}

    def _create_consumer(self, topics: list[str]) -> Any:
        """构建（或重建）KafkaConsumer（V2.7.0 P1-8：拆出便于自愈重建）。"""
        from kafka import KafkaConsumer  # noqa: PLC0415  延迟导入

        consumer_kwargs: dict[str, Any] = {
            "bootstrap_servers": self._settings.bootstrap_servers.split(","),
            "group_id": self._settings.group_id,
            "auto_offset_reset": self._settings.auto_offset_reset,
            "enable_auto_commit": self._settings.enable_auto_commit,
            "value_deserializer": lambda v: v,
            "consumer_timeout_ms": 1000,
            # V3.1.2：security_protocol / SASL 凭据 / TLS·mTLS 证书
            **self._security_kwargs,
        }
        return KafkaConsumer(*topics, **consumer_kwargs)

    def _rebuild_consumer(self) -> bool:
        """关闭并重建消费者（P1-8 自愈）；返回是否重建成功。"""
        old = self._consumer
        self._consumer = None
        if old is not None:
            try:
                old.close(timeout_ms=1000)
            except Exception as exc:  # noqa: BLE001  旧连接关闭失败不阻断重建
                logger.warning(f"Closing stale Kafka consumer failed: {exc}")
        KAFKA_CONSUMER_REBUILDS_TOTAL.inc()
        try:
            self._consumer = self._create_consumer(self._build_topic_list())
            logger.warning(
                "Rebuilt VIL Kafka consumer after consecutive errors "
                "(rebuilds total via metric huntersim_kafka_consumer_rebuilds_total)"
            )
            return True
        except Exception as exc:  # noqa: BLE001  重建失败下一轮再试
            logger.error(f"Kafka consumer rebuild failed: {exc}")
            return False

    def _update_lag(self) -> None:
        """尽力估算并上报消费滞后（P1-8）：无 assignment/API 时静默跳过。"""
        consumer = self._consumer
        if consumer is None:
            return
        try:
            assignment = consumer.assignment()
            if not assignment:
                return
            positions = consumer.position(list(assignment))
            end_offsets = consumer.end_offsets(list(assignment))
            lag = sum(
                max(0, int(end_offsets[tp]) - int(positions[tp]))
                for tp in assignment
                if tp in end_offsets and tp in positions
            )
            KAFKA_CONSUMER_LAG.set(lag)
        except Exception as exc:  # noqa: BLE001  滞后估算失败不影响消费
            logger.debug(f"Kafka lag estimation skipped: {exc}")

    def _consume_loop(self) -> None:
        """Kafka 消费主循环（在独立线程中运行，含自愈重建）。"""
        try:
            import kafka  # noqa: F401,PLC0415  探测依赖（具体构造延迟到 _create_consumer）
        except ImportError:
            logger.error("kafka-python not installed, VIL consumer disabled")
            return

        topics = self._build_topic_list()
        if not topics:
            logger.error(
                "No Kafka topic to subscribe (empty SIM_ID and no legacy/command topic); "
                "VIL consumer disabled"
            )
            self._running.clear()
            return
        try:
            self._consumer = self._create_consumer(topics)
            logger.info(f"Kafka consumer connected, topics={topics}")

            rebuild_after = max(1, int(self._settings.rebuild_after_errors))
            self_heal = bool(self._settings.enable_self_heal)

            while self._running.is_set():
                consumer = self._consumer
                if consumer is None:
                    # 上次重建失败：退避后重试，不直接放弃循环
                    time.sleep(1.0)
                    if self._rebuild_consumer():
                        self._consecutive_errors = 0
                    continue
                try:
                    msg_iter = consumer.poll(timeout_ms=200)
                    # 本轮成功：清零连续错误，刷新滞后
                    self._consecutive_errors = 0
                    self._update_lag()
                    for tp, messages in msg_iter.items():
                        for msg in messages:
                            self._handle_message(msg.topic, msg.value)
                except Exception as exc:
                    self._error_count += 1
                    self._consecutive_errors += 1
                    KAFKA_CONSUME_ERRORS_TOTAL.inc()
                    logger.warning(
                        f"Kafka poll error (consecutive={self._consecutive_errors}): {exc}"
                    )
                    if self_heal and self._consecutive_errors >= rebuild_after:
                        logger.error(
                            f"Kafka consumer unhealthy after {self._consecutive_errors} "
                            "consecutive errors; triggering self-heal rebuild"
                        )
                        self._rebuild_consumer()
                        self._consecutive_errors = 0
                    time.sleep(1.0)

        except Exception as exc:
            logger.error(f"VIL consumer fatal error: {exc}")
            raise KafkaConnectionError(self._settings.bootstrap_servers, str(exc)) from exc
        finally:
            self._running.clear()

    def _get_dlq_producer(self) -> Any:
        """惰性创建 DLQ 生产者（P1-8）：未配置 dlq_topic 或创建失败返回 None。"""
        if self._dlq_producer is not None:
            return self._dlq_producer
        if not self._settings.dlq_topic:
            return None
        try:
            if self._producer_factory is not None:
                self._dlq_producer = self._producer_factory()
            else:
                from kafka import KafkaProducer  # noqa: PLC0415  延迟导入

                producer_kwargs: dict[str, Any] = {
                    "bootstrap_servers": self._settings.bootstrap_servers.split(","),
                    # V3.1.2：DLQ 生产者与消费者共用同一套接入安全参数
                    **self._security_kwargs,
                }
                self._dlq_producer = KafkaProducer(**producer_kwargs)
        except Exception as exc:  # noqa: BLE001  DLQ 不可用时降级为仅计数丢弃
            logger.warning(f"DLQ producer unavailable (bad messages will be counted-dropped): {exc}")
            self._dlq_producer = None
        return self._dlq_producer

    def _send_to_dlq(self, source_topic: str, raw: bytes) -> None:
        """将坏消息转发到死信队列（P1-8）；无 DLQ 时仅计数。"""
        producer = self._get_dlq_producer()
        if producer is None:
            return
        try:
            producer.send(self._settings.dlq_topic, value=raw)
            CLOUD_DLQ_PUBLISHED_TOTAL.inc()
            logger.warning(f"Forwarded malformed message from '{source_topic}' to DLQ "
                           f"'{self._settings.dlq_topic}'")
        except Exception as exc:  # noqa: BLE001  DLQ 写入失败不阻断消费
            logger.warning(f"DLQ publish failed (message dropped): {exc}")

    def _build_topic_list(self) -> list[str]:
        """构建订阅 Topic 列表（渲染 carla.{sim_id}.vil_state 模板）。

        sim_id 未配置时**不订阅字面量模板**（否则会静默卡在一个永远无数据的
        Topic 上），只保留显式配置的 legacy Topic 并记告警。

        V3.0.0：``command_topic`` 随无人消费的控制指令回读链路一并移除。
        """
        topic = self._settings.telemetry_topic
        topics: list[str] = []
        if "{sim_id}" in topic:
            if self._sim_id:
                topics.append(topic.format(sim_id=self._sim_id))
            else:
                logger.warning(
                    f"telemetry_topic '{topic}' requires sim_id but SIM_ID is empty; "
                    "vil_state topic will not be subscribed"
                )
        else:
            topics.append(topic)
        # 兼容单机调试的旧 telemetry_clean Topic
        if self._settings.legacy_telemetry_topic:
            topics.append(self._settings.legacy_telemetry_topic)
        return topics

    def _get_primary_telemetry_topic(self) -> str:
        """返回渲染后的主遥测 Topic（用于 _handle_message 判断）。"""
        topic = self._settings.telemetry_topic
        if "{sim_id}" in topic and self._sim_id:
            topic = topic.format(sim_id=self._sim_id)
        return topic

    def _handle_message(self, topic: str, raw: bytes) -> None:
        """解析单条 Kafka 消息，更新缓冲区。

        支持两种遥测格式：
        1. carla.{sim_id}.vil_state（新）：含 pose/twist 字段；
        2. telemetry_clean（旧）：含 position/velocity 字段。

        V2.7.0 P1-8：无法解码/解析的坏消息转发 DLQ；
        V2.7.0 P1-9：遥测按 timestamp+seq 丢弃乱序/重复消息。
        V2.10.0：增加 sim_id 一致性校验。
        """
        self._msg_count += 1
        KAFKA_CONSUMED_TOTAL.inc()
        try:
            payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(f"Failed to decode Kafka message from {topic}: {exc}")
            self._error_count += 1
            self._send_to_dlq(topic, raw)
            return

        # V2.10.0： sim_id 一致性校验（vil_state 格式）
        if self._sim_id and "sim_id" in payload:
            msg_sim_id = str(payload.get("sim_id", ""))
            if msg_sim_id != self._sim_id:
                VIL_MESSAGE_DROPPED_TOTAL.labels(reason="sim_id_mismatch").inc()
                logger.debug(
                    f"Skip vil_state from mismatched sim_id "
                    f"(expected={self._sim_id}, got={msg_sim_id})"
                )
                return

        # 旧格式兼容： vehicle_id 过滤
        remote_vid = payload.get("vehicle_id")
        if remote_vid is not None and str(remote_vid) != self._vehicle_id:
            # 尝试匹配 sim_id（vil_state 格式 vehicle_id 可能为空）
            inner_payload = payload.get("payload", {})
            if not ("pose" in inner_payload or "pose" in payload):
                logger.debug(f"Skip message from non-target vehicle '{remote_vid}'")
                return

        primary_telemetry_topic = self._get_primary_telemetry_topic()
        legacy_topic = self._settings.legacy_telemetry_topic
        is_telemetry = (
            topic == primary_telemetry_topic
            or (legacy_topic and topic == legacy_topic)
        )

        try:
            if is_telemetry:
                if not self._accept_in_order(payload):
                    return
                # 判断格式：新版 vil_state（有 pose 字段）或旧版 telemetry_clean
                inner = payload.get("payload", payload)
                if "pose" in inner:
                    # V2.10.0: carla.{sim_id}.vil_state 格式
                    frame = self._parse_vil_state(payload)
                    VIL_STATE_CONSUMED_TOTAL.inc()
                    # 记录端到端延迟
                    msg_ts = payload.get("timestamp")
                    if msg_ts is not None:
                        try:
                            lag_ms = (time.time() - float(msg_ts)) * 1000.0
                            observe_vil_e2e_lag(lag_ms)
                        except (ValueError, TypeError):
                            pass
                else:
                    # 旧版 telemetry_clean 格式
                    frame = self._parse_telemetry(payload)
                self._buffer.push(frame)
            else:
                # V3.0.0：control 回读走 vil_state 的 control.target_* 段，
                # command_result 回读 Topic 已移除，非遥测 Topic 不再有消费语义。
                logger.debug(f"Ignore message on unsubscribed topic '{topic}'")
                return
        except (KeyError, ValueError, TypeError, PydanticValidationError) as exc:
            logger.warning(f"Parse error on topic {topic}: {exc}")
            self._error_count += 1
            self._send_to_dlq(topic, raw)

    def _accept_in_order(self, payload: dict[str, Any]) -> bool:
        """遥测消息单调性/去重闸门（V2.7.0 P1-9）。

        仅对 telemetry 主题生效（command_result 回写最新帧不参与排序）。
        优先比较 ``seq``（若存在），否则回退到 ``timestamp``；丢弃时计数
        ``huntersim_vil_message_dropped_total`` 并返回 False。未启用防护时直通。
        """
        if not getattr(self._vil_settings, "ordering_guard_enabled", False):
            return True
        ts = payload.get("timestamp")
        ts_val: Optional[float] = None
        if ts is not None:
            try:
                ts_val = float(ts)
            except (TypeError, ValueError):
                ts_val = None
        seq_val: Optional[int] = None
        raw_seq = payload.get("seq")
        if raw_seq is not None:
            try:
                seq_val = int(raw_seq)
            except (TypeError, ValueError):
                seq_val = None

        accepted = True
        reason = ""
        with self._lock:
            if seq_val is not None and self._last_seq is not None and seq_val <= self._last_seq:
                accepted, reason = False, "duplicate_seq"
            elif (
                seq_val is None
                and ts_val is not None
                and self._last_timestamp is not None
                and ts_val < self._last_timestamp
            ):
                accepted, reason = False, "out_of_order"
            if accepted:
                if seq_val is not None:
                    self._last_seq = seq_val
                if ts_val is not None:
                    self._last_timestamp = ts_val
        if not accepted:
            VIL_MESSAGE_DROPPED_TOTAL.labels(reason=reason).inc()
            logger.debug(f"Dropped {reason} telemetry message (seq={seq_val}, ts={ts_val})")
        return accepted

    def reset_ordering_guard(self) -> None:
        """清空乱序/去重闸门状态（会话或场景切换后 seq/timestamp 可能重新计数）。"""
        with self._lock:
            self._last_seq = None
            self._last_timestamp = None

    def _parse_telemetry(self, payload: dict[str, Any]) -> TelemetryFrame:
        """解析 telemetry_clean 格式消息。"""
        ts: float = float(payload.get("timestamp", time.time()))
        pos = payload.get("position", {})
        vel = payload.get("velocity", {})
        state = payload.get("state", {})

        transform = Transform(
            x=float(pos.get("x", 0.0)),
            y=float(pos.get("y", 0.0)),
            z=float(pos.get("z", 0.0)),
            pitch=float(pos.get("pitch", 0.0)),
            yaw=float(pos.get("yaw", 0.0)),
            roll=float(pos.get("roll", 0.0)),
        )
        vehicle_state = VehicleState(
            time_stamp=ts,
            transform=transform,
            velocity=(
                float(vel.get("vx", 0.0)),
                float(vel.get("vy", 0.0)),
                float(vel.get("vz", 0.0)),
            ),
            acceleration=(
                float(state.get("ax", 0.0)),
                float(state.get("ay", 0.0)),
                float(state.get("az", 0.0)),
            ),
            angular_velocity=(
                float(state.get("wx", 0.0)),
                float(state.get("wy", 0.0)),
                float(state.get("wz", 0.0)),
            ),
            steering=float(state.get("steering", 0.0)),
            throttle=float(state.get("throttle", 0.0)),
            brake=float(state.get("brake", 0.0)),
            gear=int(state.get("gear", 1)),
            vehicle_speed=float(vel.get("speed", 0.0)),
        )

        perception: Optional[PerceptionResult] = None
        if "perception" in payload:
            perc_data = payload["perception"]
            # 解析感知目标列表（设计文档 §4.4.3：x/y/length/width 等字段）
            objects: list[DetectedObject] = []
            for idx, obj in enumerate(perc_data.get("objects", [])):
                if not isinstance(obj, dict):
                    continue
                objects.append(
                    DetectedObject(
                        object_id=int(obj.get("id", idx)),
                        object_type=str(obj.get("type", "unknown")),
                        transform=Transform(
                            x=float(obj.get("x", 0.0)),
                            y=float(obj.get("y", 0.0)),
                            z=float(obj.get("z", 0.0)),
                            yaw=float(obj.get("heading", 0.0)),
                        ),
                        size=(
                            float(obj.get("length", 4.0)),
                            float(obj.get("width", 2.0)),
                            float(obj.get("height", 1.5)),
                        ),
                        velocity=(
                            float(obj.get("vx", 0.0)),
                            float(obj.get("vy", 0.0)),
                            0.0,
                        ),
                        confidence=float(obj.get("confidence", 1.0)),
                    )
                )
            perception = PerceptionResult(
                time_stamp=float(perc_data.get("timestamp", ts)),
                objects=objects,
            )

        return TelemetryFrame(
            vehicle_id=self._vehicle_id,
            timestamp=ts,
            receive_time=time.time(),
            vehicle_state=vehicle_state,
            perception=perception,
        )

    def _parse_vil_state(self, payload: dict[str, Any]) -> TelemetryFrame:
        """解析 carla.{sim_id}.vil_state 格式消息（V2.10.0）。

        调用 ``cloud.vil_state_parser.parse_vil_state`` 将 vil_state 信封转换为
        内部 ``TelemetryFrame``。
        """
        from hunter_sim.cloud.vil_state_parser import parse_vil_state  # noqa: PLC0415

        return parse_vil_state(payload, vehicle_id=self._vehicle_id)

"""VIL Kafka 数据接入服务（PROMPT-ENG-002-A）。

从 Kafka 实时消费实车遥测数据，解析后推入 TelemetryBuffer。
支持多 Topic 订阅、自动重连、反序列化异常处理。

Topics：
- telemetry_clean: 车辆定位 + 车辆状态（10Hz）
- hunter.{vehicle_id}.command_result: 控制指令回读（10Hz）
- perception_result: 感知结果（10Hz，可选）
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from pydantic import ValidationError as PydanticValidationError

from hunter_sim.common.exceptions import KafkaConnectionError
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
    """

    def __init__(
        self,
        settings: KafkaSettings,
        vil_settings: VILSettings,
        vehicle_id: str,
        buffer: TelemetryBuffer,
    ) -> None:
        self._settings = settings
        self._vil_settings = vil_settings
        self._vehicle_id = vehicle_id
        self._buffer = buffer
        self._consumer: Any = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._msg_count: int = 0
        self._error_count: int = 0
        logger.info(
            f"VILDataConsumer initialized: vehicle_id={vehicle_id}, "
            f"bootstrap={settings.bootstrap_servers}"
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

    def _consume_loop(self) -> None:
        """Kafka 消费主循环（在独立线程中运行）。"""
        try:
            from kafka import KafkaConsumer  # noqa: PLC0415
        except ImportError:
            logger.error("kafka-python not installed, VIL consumer disabled")
            return

        topics = self._build_topic_list()
        try:
            consumer_kwargs: dict[str, Any] = {
                "bootstrap_servers": self._settings.bootstrap_servers.split(","),
                "group_id": self._settings.group_id,
                "auto_offset_reset": self._settings.auto_offset_reset,
                "enable_auto_commit": self._settings.enable_auto_commit,
                "value_deserializer": lambda v: v,
                "consumer_timeout_ms": 1000,
            }
            if self._settings.username:
                consumer_kwargs["sasl_username"] = self._settings.username.get_secret_value()
                consumer_kwargs["sasl_password"] = (
                    self._settings.password.get_secret_value() if self._settings.password else ""
                )

            self._consumer = KafkaConsumer(*topics, **consumer_kwargs)
            logger.info(f"Kafka consumer connected, topics={topics}")

            while self._running.is_set():
                try:
                    msg_iter = self._consumer.poll(timeout_ms=200)
                    for tp, messages in msg_iter.items():
                        for msg in messages:
                            self._handle_message(msg.topic, msg.value)
                except Exception as exc:
                    self._error_count += 1
                    logger.warning(f"Kafka poll error: {exc}")
                    time.sleep(1.0)

        except Exception as exc:
            logger.error(f"VIL consumer fatal error: {exc}")
            raise KafkaConnectionError(self._settings.bootstrap_servers, str(exc)) from exc
        finally:
            self._running.clear()

    def _build_topic_list(self) -> list[str]:
        """构建订阅 Topic 列表。"""
        topics = [self._settings.telemetry_topic]
        command_topic = self._settings.command_topic.format(vehicle_id=self._vehicle_id)
        topics.append(command_topic)
        return topics

    def _handle_message(self, topic: str, raw: bytes) -> None:
        """解析单条 Kafka 消息，更新缓冲区。"""
        self._msg_count += 1
        try:
            payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(f"Failed to decode Kafka message from {topic}: {exc}")
            self._error_count += 1
            return

        # 只处理目标车辆数据（设计文档 §4.2.2：vehicle_id 过滤）
        remote_vid = payload.get("vehicle_id")
        if remote_vid is not None and str(remote_vid) != self._vehicle_id:
            logger.debug(f"Skip message from non-target vehicle '{remote_vid}'")
            return

        try:
            if topic == self._settings.telemetry_topic:
                frame = self._parse_telemetry(payload)
                self._buffer.push(frame)
            else:
                cmd_frame = self._parse_command_result(payload)
                if cmd_frame is not None:
                    self._buffer.push(cmd_frame)
        except (KeyError, ValueError, PydanticValidationError) as exc:
            logger.warning(f"Parse error on topic {topic}: {exc}")
            self._error_count += 1

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

    def _parse_command_result(self, payload: dict[str, Any]) -> Optional[TelemetryFrame]:
        """解析 command_result 格式消息，更新已有帧的 control_cmd 字段。"""
        latest = self._buffer.get_latest()
        if latest is None:
            return None
        latest.control_cmd = {
            "throttle": float(payload.get("throttle", 0.0)),
            "steer": float(payload.get("steer", 0.0)),
            "brake": float(payload.get("brake", 0.0)),
        }
        return latest

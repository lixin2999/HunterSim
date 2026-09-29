"""模块 4.2：Kafka 遥测数据消费实现（``telemetry_clean`` 主题）。

线程模型：

- Kafka ``KafkaConsumer`` 是阻塞式迭代器，本模块在**独立 daemon 线程**中运行消费
  循环，避免污染 asyncio 事件循环；
- 消费线程与主事件循环通过 :class:`threading.Lock` + 最新帧引用交换数据（无队列，
  始终取最新一帧，与 §4.2.2 ``auto_offset_reset='latest'`` 语义一致）；
- :meth:`poll_latest` 是 async 接口，仅读取缓存，不做阻塞 IO。

降级：若 ``kafka-python`` 未安装（例如单元测试或纯回放场景），惰性导入失败会抛出
:class:`~hunter_sim.core.exceptions.KafkaConnectionError`；上层应注入 Mock
:class:`TelemetrySource` 替代。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from hunter_sim.app.vil.models import VehicleTelemetry, VILConfig
from hunter_sim.core.exceptions import KafkaConnectionError
from hunter_sim.core.logging import logger


class KafkaTelemetryConsumerImpl:
    """满足 :class:`~hunter_sim.app.vil.protocols.TelemetrySource` 契约。"""

    def __init__(self, config: VILConfig) -> None:
        """初始化消费者（**不**连接 Kafka；连接在 :meth:`start` 中惰性完成）。

        Args:
            config: VIL 配置，读取 ``kafka_bootstrap_servers`` / ``kafka_group_id`` /
                ``telemetry_topic`` / ``target_vehicle_id``。
        """
        self._config = config
        self._lock = threading.Lock()
        self._latest: VehicleTelemetry | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._consumer: Any | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动后台消费线程（连接 Kafka）。

        Raises:
            KafkaConnectionError: ``kafka-python`` 未安装或客户端构造失败。
        """
        if self._thread is not None:
            return
        consumer = self._build_consumer()
        self._consumer = consumer
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="huntersim-kafka-telemetry",
            daemon=True,
        )
        self._thread.start()
        logger.bind(component="vil.kvconsumer").info(
            "Kafka 消费线程已启动: topic={} group={} target={}",
            self._config.telemetry_topic,
            self._config.kafka_group_id,
            self._config.target_vehicle_id,
        )

    async def stop(self) -> None:
        """停止后台线程并关闭 Kafka 客户端（幂等）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # consumer.close() 会让消费循环退出；给短暂时间 join。
            thread.join(timeout=2.0)
        consumer = self._consumer
        if consumer is not None:
            try:
                consumer.close()
            except Exception as exc:  # pragma: no cover - 依赖 close 内部行为
                logger.bind(component="vil.kvconsumer").warning("Kafka 关闭失败: {}", exc)
        self._thread = None
        self._consumer = None
        with self._lock:
            self._latest = None

    # ---------- 数据接口 ----------

    async def poll_latest(self) -> VehicleTelemetry | None:
        """返回当前缓存的最新一帧（可能为 ``None``）。"""
        with self._lock:
            return self._latest

    # ---------- 内部实现 ----------

    def _build_consumer(self) -> Any:
        try:
            from kafka import KafkaConsumer
        except ImportError as exc:  # pragma: no cover - 依赖未安装路径
            raise KafkaConnectionError(
                "kafka-python 未安装；请添加依赖或通过 Mock TelemetrySource 注入"
            ) from exc
        try:
            return KafkaConsumer(
                self._config.telemetry_topic,
                bootstrap_servers=self._config.kafka_bootstrap_servers,
                group_id=self._config.kafka_group_id,
                auto_offset_reset="latest",
                enable_auto_commit=True,
                # 消费循环自行反序列化，因此不做 value_deserializer（保留原始 bytes）。
            )
        except Exception as exc:
            raise KafkaConnectionError(
                f"Kafka 消费者构造失败: brokers={self._config.kafka_bootstrap_servers} "
                f"topic={self._config.telemetry_topic} ({exc})"
            ) from exc

    def _run_loop(self) -> None:
        """后台线程主循环：poll → 过滤 vehicle_id → 更新缓存。"""
        consumer = self._consumer
        if consumer is None:  # pragma: no cover - start 保证非空
            return
        # poll 参数：timeout_ms=100 平衡延迟与 CPU；每次返回 {topic: [messages]}。
        while not self._stop_event.is_set():
            try:
                records = consumer.poll(timeout_ms=100)
            except Exception as exc:  # 网络抖动等：记录并继续
                logger.bind(component="vil.kvconsumer").warning("Kafka poll 异常: {}", exc)
                continue
            for messages in records.values():
                for msg in messages:
                    self._handle_message(msg)

    def _handle_message(self, msg: Any) -> None:
        raw = getattr(msg, "value", None)
        if raw is None:
            return
        try:
            payload = json.loads(
                raw.decode("utf-8") if isinstance(raw, bytes) else raw
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.bind(component="vil.kvconsumer").debug("消息解析失败: {}", exc)
            return
        if not isinstance(payload, dict):
            return
        if payload.get("vehicle_id") != self._config.target_vehicle_id:
            return
        try:
            telemetry = VehicleTelemetry.model_validate(payload)
        except ValueError as exc:  # pydantic ValidationError
            logger.bind(component="vil.kvconsumer").debug("遥测字段校验失败: {}", exc)
            return
        with self._lock:
            self._latest = telemetry


__all__ = ["KafkaTelemetryConsumerImpl"]

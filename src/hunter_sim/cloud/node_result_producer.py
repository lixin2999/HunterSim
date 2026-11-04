"""节点级 Kafka 回传通道（V2.0，方案 §2.5.4 / §8.1 / 接入清单 §1.5）。

与实例级 ``carla.{sim_id}.sensor_data`` 并存：本通道把**实例终态结果摘要**按
节点聚合回传到 ``simulation.{node_id}.result``（+ 派生死信 ``.dlq``），由平台
``data-collector-simulation`` 消费组消费，与真实车队 Topic 物理隔离。

设计口径（沿用 SimDataProducer 的韧性纪律）：

- 复用平台数据面凭据（``CloudSettings`` 的 SASL/TLS），Topic 名由 ``SimNodeSettings``
  渲染（``simulation.{node_id}.result``），key=sim_id，acks=all；
- ``enabled=false`` / 凭据缺失 / node_id 为空 / kafka 依赖缺失时整体降级为 no-op，
  ``publish`` 安全返回 False，绝不阻断仿真主循环或结果落库；
- 消息形态遵循方案 §5.4 结果视图（平台响应模型忽略未声明字段，向后兼容）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from hunter_sim.cloud.kafka_security import build_ssl_kwargs
from hunter_sim.common.metrics import NODE_RESULT_PUBLISHED_TOTAL
from hunter_sim.common.models import CloudSettings, SimNodeSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

#: 生产者工厂注入点（单测用桩替换，避免依赖真实 kafka-python）
ProducerFactory = Callable[[CloudSettings], Any]


def build_node_result(
    *,
    node_id: str,
    sim_id: str,
    scene_id: str,
    status: str,
    success: bool,
    message: Optional[str] = None,
    success_criteria_result: Optional[dict[str, Any]] = None,
    metrics: Optional[dict[str, Any]] = None,
    artifacts: Optional[list[dict[str, Any]]] = None,
    provider: str = "carla",
    provider_version: str = "",
    started_at: Optional[float] = None,
    finished_at: Optional[float] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    """构造节点级结果回传消息（对齐方案 §5.4/§7.2 结果扩展）。

    Args:
        node_id: 仿真节点标识（回传归属节点）。
        sim_id: 平台仿真会话 ID（消息 key）。
        scene_id: 场景 ID。
        status: 平台终态词汇（succeeded/failed/canceled）。
        success: 场景是否通过。
        message: 失败/取消原因（成功时一般为 None）。
        success_criteria_result: 判据明细（原始量，平台裁决）。
        metrics: 原始指标摘要（碰撞数/最小距离等，可选）。
        artifacts: 产物元信息列表（name/object_key/size_bytes）。
        provider: 仿真器供应商标识（默认 carla）。
        provider_version: 仿真器版本（可留空）。
        started_at/finished_at: Unix 时间戳（秒）。
        ts: 消息生成时间戳（默认当前时间）。

    Returns:
        可直接 JSON 序列化的消息字典。
    """
    return {
        "type": "simulation_result",
        "node_id": node_id,
        "sim_id": sim_id,
        "scene_id": scene_id,
        "status": status,
        "success": bool(success),
        "message": message,
        "success_criteria_result": success_criteria_result or {},
        "metrics": metrics or {},
        "artifacts": artifacts or [],
        "provider": provider,
        "provider_version": provider_version,
        "started_at": started_at,
        "finished_at": finished_at,
        "timestamp": ts if ts is not None else time.time(),
    }


class NodeResultProducer:
    """向 ``simulation.{node_id}.result`` 发布实例终态结果摘要。

    Args:
        cloud: 平台数据面配置（读取 kafka_bootstrap / SASL / TLS 凭据）。
        node: 节点配置（读取 result_enabled / node_id / Topic 模板）。
        producer_factory: 可选 Kafka 生产者工厂（单测注入）；
            签名 ``(cloud_settings) -> KafkaProducer``，需提供
            ``send(topic, key, value)`` / ``flush()`` / ``close()``。
    """

    def __init__(
        self,
        cloud: CloudSettings,
        node: SimNodeSettings,
        producer_factory: Optional[ProducerFactory] = None,
    ) -> None:
        self._cloud = cloud
        self._node = node
        self._factory = producer_factory
        self._producer: Any = None
        self._available = False

    @property
    def usable(self) -> bool:
        """配置是否足以启用本通道（启用 + 有 node_id + 凭据齐备）。"""
        return bool(
            self._node.result_enabled
            and self._node.enabled
            and self._node.node_id
            and self._cloud.enabled
        )

    @property
    def topic(self) -> str:
        """节点级回传 Topic 全名（node_id 为空时为 ''）。"""
        return self._node.render_result_topic()

    @property
    def available(self) -> bool:
        """生产者是否已连接可用。"""
        return self._available

    def connect(self) -> bool:
        """建立生产者；配置不足或依赖缺失时降级为 no-op。"""
        if not self.usable:
            logger.debug("NodeResultProducer 未启用或配置不足，降级为 no-op")
            self._available = False
            return False
        try:
            self._producer = (
                self._factory(self._cloud)
                if self._factory is not None
                else self._build_kafka_producer()
            )
            self._available = True
            logger.info(
                f"NodeResultProducer 已连接：{self._cloud.kafka_bootstrap} "
                f"topic={self.topic}"
            )
        except Exception as exc:  # noqa: BLE001  依赖缺失/认证失败均降级，不阻断仿真
            self._producer = None
            self._available = False
            logger.warning(
                f"NodeResultProducer 初始化失败，降级为 no-op：{type(exc).__name__}: {exc}"
            )
        return self._available

    def _build_kafka_producer(self) -> Any:
        """惰性构建 kafka-python KafkaProducer（acks=all，SASL_SSL）。"""
        from kafka import KafkaProducer  # noqa: PLC0415  可选依赖惰性导入

        c = self._cloud
        kwargs: dict[str, Any] = {
            "bootstrap_servers": c.kafka_bootstrap,
            "security_protocol": c.security_protocol,
            "sasl_mechanism": c.sasl_mechanism,
            "acks": c.sensor_data_acks,
            "linger_ms": 5,
            "retries": 5,
            "max_block_ms": 1000,
            "delivery_timeout_ms": 10000,
        }
        if c.sasl_username is not None:
            kwargs["sasl_plain_username"] = c.sasl_username.get_secret_value()
        if c.sasl_password is not None:
            kwargs["sasl_plain_password"] = c.sasl_password.get_secret_value()
        kwargs.update(build_ssl_kwargs(c))
        return KafkaProducer(**kwargs)

    def publish(self, message: dict[str, Any]) -> bool:
        """发布一条结果摘要；不可用时安全返回 False（不抛异常）。"""
        topic = self.topic
        if not self._available or self._producer is None or not topic:
            return False
        key = str(message.get("sim_id") or self._node.node_id).encode("utf-8")
        value = json.dumps(message, default=str).encode("utf-8")
        try:
            self._producer.send(topic, key=key, value=value)
            NODE_RESULT_PUBLISHED_TOTAL.labels(node_id=self._node.node_id).inc()
            return True
        except Exception as exc:  # noqa: BLE001  投递失败仅告警，不阻断结果落库
            logger.warning(
                f"NodeResultProducer 发布失败（topic={topic}）："
                f"{type(exc).__name__}: {exc}"
            )
            return False

    def close(self, timeout: float = 3.0) -> None:
        """刷新并关闭底层生产者（幂等）。"""
        producer = self._producer
        if producer is not None:
            try:
                producer.flush(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
            try:
                producer.close()
            except Exception:  # noqa: BLE001
                pass
        self._producer = None
        self._available = False


__all__ = ["NodeResultProducer", "build_node_result"]

"""EdgeLinkRuntime — 真 SIL 边缘链路运行时（V3.9.0）。

仿真端 ↔ 车端 HunterEdge 的 Kafka 数据总线桥接，形成全栈自动驾驶闭环：

- **传感器上行**：``carla.{sim_id}.edge_sensor``，JSON 信封
  ``{stream, sim_ts, wall_ts_ns, seq, meta, payload_b64}``；流 ``rgb``(JPEG)、
  ``depth``(32FC1)、``lidar``(XYZ float32)、``imu``、``ego_state``(速度/横摆/位姿)。
- **控制下行**：``carla.{sim_id}.edge_control``，帧 ``{linear_x, angular_z,
  wall_ts_ns, seq}``（Twist 语义），latest-wins。
- **会话信令**：``stream="session"`` 的 boot（setup）/ stop（cleanup）帧。

与 :class:`~hunter_sim.platform_adapter.vil.VILRuntime` 同构：**不自己 tick 世界**。
``step()`` 挂到 ``SceneRunnerService`` 唯一 tick 循环（``control_driver`` hook），
只在该 tick 取最新控制指令 + 看门狗判定，并馈送 ``ego_state``；传感器上行由
``edge_link_sink`` 回调从编码 worker 线程注入（复用现有编码产物，不重复编码）。

连接参数复用 ``KafkaSettings``（SASL_SSL/SCRAM/证书，``build_consumer_security_kwargs``
在构造期解析，配置错误立即失败）；kafka-python 惰性导入，工厂注入点供单测桩替换。
**Kafka 不可达时 start() 快速失败**（抛 ``KafkaConnectionError``），不静默回退脚本
自车——避免「假 SIL」。看门狗：控制帧断流 > ``watchdog_ms`` → 零速刹停保持，
``status_override`` 报 PAUSED + 原因（对齐车端 safety_guard 语义）。

V3.9.1：``start()`` 先做 Topic 自检/补建。broker 关闭 ``auto.create.topics.enable``
时（HunterCore 的 9093 接入面正是如此），生产者每条 ``send()`` 会阻塞满
``max_block_ms`` 后抛 ``KafkaTimeoutError: Failed to update metadata``，消费者也
永远拿不到分区——表现为「传感器全部丢弃、``control_received=0``、自车被看门狗
刹停，但实例仍跑完」。

Topic 由**本类在 start() 时用 AdminClient 预建**（``sim_id`` 每轮都变，手工
``kafka-topics.sh`` 预建不是常规手段）：账号缺 ``CREATE`` 时快速失败，并在报错里
给出**权限口径**的修法——按 ``carla.*`` 前缀授予
``CREATE/DESCRIBE/READ/WRITE``（单机测试环境可再开 broker
``auto.create.topics.enable`` 作兜底）。而不是静默跑完一场假 SIL。
"""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from hunter_sim.cloud.kafka_limits import (
    MAX_REQUEST_OVERHEAD_BYTES as _MAX_REQUEST_OVERHEAD_BYTES,
)
from hunter_sim.cloud.kafka_security import (
    build_consumer_security_kwargs,
    ssl_failure_hint,
)
from hunter_sim.common.exceptions import KafkaConnectionError
from hunter_sim.common.models import EdgeLinkSettings, HunterSimSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

#: Kafka 生产者超时组合（毫秒）：必须满足 delivery >= linger + request，
#: 否则 kafka-python 初始化抛 KafkaConfigurationError（与 SimDataProducer 同口径）。
_LINGER_MS = 5
_REQUEST_TIMEOUT_MS = 10_000
_DELIVERY_TIMEOUT_MS = 30_000

#: 各流默认上行限频（Hz）；EdgeLinkSettings 同名字段可覆盖。
_STREAM_FPS_FIELDS: dict[str, str] = {
    "rgb": "rgb_fps",
    "depth": "depth_fps",
    "lidar": "lidar_fps",
    "imu": "imu_fps",
    "ego_state": "ego_state_fps",
}

#: 速率滑动窗口（秒）与单流样本上限：**与 FrameHub.fps 同口径**，保证面板
#: 「传感器帧率」与「SIL 馈送速率」语义一致（太短会抖动，太长会掩盖掉帧）。
_FPS_WINDOW_S = 5.0
_MAX_FPS_SAMPLES = 600

#: 工厂注入点（单测用桩替换，避免依赖真实 kafka-python）。
ProducerFactory = Callable[[], Any]
ConsumerFactory = Callable[[list[str]], Any]
AdminFactory = Callable[[], Any]

#: Topic 补建超时（毫秒）与发送失败告警节流窗口（秒）。
_TOPIC_CREATE_TIMEOUT_MS = 10_000
_SEND_ERROR_LOG_INTERVAL_S = 10.0
_SIZE_DROP_LOG_INTERVAL_S = 10.0


@dataclass(frozen=True)
class EdgeControl:
    """车端回传的控制指令（Twist 语义）。

    Attributes:
        linear_x: 纵向速度指令（m/s，负值倒车）。
        angular_z: 横摆角速度指令（rad/s，左转为正）。
        stale: 是否处于看门狗/等待态（True 时 linear_x/angular_z 恒 0，
            调用方应零速刹停保持，不得沿用上一帧指令）。
        fatal: V3.9.2 首帧控制超时（会话已建立但整场无 ``/cmd_vel``），
            调用方应**结束场景**而非零速跑满时长（否则空跑满时长再判 fail）。
        reason: ``fatal=True`` 时的失败原因（透传给场景失败描述）。
    """

    linear_x: float
    angular_z: float
    stale: bool = False
    fatal: bool = False
    reason: str = ""


class EdgeLinkRuntime:
    """单实例真 SIL 边缘链路运行时。

    Args:
        settings: 全局配置（复用 ``kafka`` 连接参数 + ``edge_link`` 链路参数）。
        sim_id: 当前仿真实例 ID（渲染 ``carla.{sim_id}.edge_*`` Topic）。
        runner_provider: 返回当前 ``SceneRunnerService`` 的回调（延迟绑定自车）。
        scene_id: 场景 ID（会话 boot 帧用）。
        map_id: 地图 ID（会话 boot 帧用）。
        edge_config: 平台/场景下发的链路覆盖（enabled/各流 fps/watchdog_ms 等）。
        producer_factory: 可选生产者工厂（单测注入），``() -> producer``。
        consumer_factory: 可选消费者工厂（单测注入），``(topics) -> consumer``。
        admin_factory: 可选 Admin 工厂（单测注入），``() -> admin``；仅用于
            Topic 自检/补建。注入 producer/consumer 工厂时不做自检（连接由
            调用方托管）。
    """

    def __init__(
        self,
        settings: HunterSimSettings,
        sim_id: str,
        runner_provider: Callable[[], Any],
        scene_id: str = "",
        map_id: str = "",
        edge_config: Optional[dict[str, Any]] = None,
        producer_factory: Optional[ProducerFactory] = None,
        consumer_factory: Optional[ConsumerFactory] = None,
        admin_factory: Optional[AdminFactory] = None,
    ) -> None:
        self._settings = settings
        self._sim_id = sim_id
        self._scene_id = scene_id
        self._map_id = map_id
        self._runner_provider = runner_provider
        self._producer_factory = producer_factory
        self._consumer_factory = consumer_factory
        self._admin_factory = admin_factory
        self._edge_settings = self._resolve_edge_settings(edge_config or {})
        # 接入安全参数构造期解析：配置错误立即失败（不留空转线程）
        self._security_kwargs = build_consumer_security_kwargs(settings.kafka)

        self._producer: Any = None
        self._consumer: Any = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.RLock()

        # 控制下行状态（latest-wins）
        self._latest_linear_x = 0.0
        self._latest_angular_z = 0.0
        self._last_control_recv_ts: Optional[float] = None
        self._last_control_wall_ts_ns: Optional[int] = None
        self._last_control_seq: Optional[int] = None
        self._control_count = 0
        self._control_errors = 0
        self._control_age_s = 0.0
        self._control_stamps: deque[float] = deque(maxlen=_MAX_FPS_SAMPLES)

        # 传感器上行状态
        self._seq = 0
        self._last_send_ts: dict[str, float] = {}
        self._sent_counts: dict[str, int] = {}
        self._rate_drops: dict[str, int] = {}
        self._size_drops: dict[str, int] = {}
        self._send_errors = 0
        self._send_error_suppressed = 0
        self._last_send_error_log_ts = 0.0
        self._last_size_drop_log_ts = 0.0
        self._record_drops = 0
        self._send_stamps: dict[str, deque[float]] = {}

        # 会话/看门狗状态
        self._boot_sent = False
        self._stop_sent = False
        self._interrupted = False
        self._waiting_first = True
        self._first_step_ts: Optional[float] = None
        self._first_control_fatal = False
        self._bound_ego: Any = None
        self._steps = 0
        self._start_ts = time.time()

    # ── 配置 ────────────────────────────────────────────────────────────────

    def _resolve_edge_settings(self, overrides: dict[str, Any]) -> EdgeLinkSettings:
        """合并场景/平台覆盖到 EdgeLinkSettings（仅接受已知字段，非法值忽略）。"""
        base = self._settings.edge_link
        # 从类取字段表：Pydantic v2.11 起实例访问 model_fields 已弃用
        known = set(type(base).model_fields)
        update: dict[str, Any] = {}
        for key, value in overrides.items():
            if key in known and value is not None:
                update[key] = value
        if not update:
            return base
        try:
            # 必须走 model_validate：Pydantic v2 的 model_copy(update=...) **不做校验**，
            # 非法值（如 rgb_fps="abc"）会静默写入，直到 _fps_for 转 float 才抛错，
            # 而那是 stats/面板/上报的热路径。校验失败则整体退回全局默认。
            merged = base.model_dump()
            merged.update(update)
            return type(base).model_validate(merged)
        except Exception as exc:  # noqa: BLE001  非法覆盖退回全局默认，不阻断装配
            logger.warning(f"EdgeLink 配置覆盖非法，退回默认：{exc}")
            return base

    @property
    def enabled(self) -> bool:
        """链路是否启用（装配判定用）。"""
        return self._edge_settings.enabled

    @property
    def sensor_topic(self) -> str:
        """传感器上行 Topic 全名。"""
        return self._edge_settings.sensor_topic_template.format(sim_id=self._sim_id)

    @property
    def control_topic(self) -> str:
        """控制下行 Topic 全名。"""
        return self._edge_settings.control_topic_template.format(sim_id=self._sim_id)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """建立生产者 + 消费者并启动控制消费线程。

        Raises:
            KafkaConnectionError: sim_id 缺失 / Kafka 不可达 / 构建失败（快速失败，
                由 executor 捕获并将实例标记 failed，不静默回退脚本自车）。
        """
        with self._lock:
            if self._producer is not None:
                return
            if not self._sim_id:
                raise KafkaConnectionError(
                    self._settings.kafka.bootstrap_servers,
                    "EdgeLink 需要 sim_id 渲染 carla.{sim_id}.edge_* Topic，当前为空",
                )
            bootstrap = self._settings.kafka.bootstrap_servers
            # V3.9.1：Topic 自检/补建（broker auto.create 关闭时必须预创建）。
            # 只用真实连接路径时执行；单测注入生产者/消费者工厂视为连接已托管。
            if self._producer_factory is None and self._consumer_factory is None:
                try:
                    self._ensure_topics()
                except KafkaConnectionError:
                    raise
                except Exception as exc:  # noqa: BLE001  自检失败同样快速失败
                    raise KafkaConnectionError(
                        bootstrap,
                        f"edge Topic 自检失败（SIL_EDGE 快速失败）："
                        f"{type(exc).__name__}: {exc}{ssl_failure_hint(exc)}",
                    ) from exc
            try:
                self._producer = (
                    self._producer_factory()
                    if self._producer_factory is not None
                    else self._build_producer()
                )
            except Exception as exc:  # noqa: BLE001  快速失败：明确报错而非假 SIL
                raise KafkaConnectionError(
                    bootstrap,
                    f"edge_sensor 生产者初始化失败（SIL_EDGE 快速失败，不退回脚本自车）："
                    f"{type(exc).__name__}: {exc}{ssl_failure_hint(exc)}",
                ) from exc
            try:
                self._consumer = (
                    self._consumer_factory([self.control_topic])
                    if self._consumer_factory is not None
                    else self._build_consumer()
                )
            except Exception as exc:  # noqa: BLE001
                self._close_producer_locked()
                raise KafkaConnectionError(
                    bootstrap,
                    f"edge_control 消费者初始化失败（SIL_EDGE 快速失败）："
                    f"{type(exc).__name__}: {exc}{ssl_failure_hint(exc)}",
                ) from exc
            self._running.set()
            self._consumer_thread = threading.Thread(
                target=self._consume_loop,
                name=f"EdgeLinkControl-{self._sim_id}",
                daemon=True,
            )
            self._consumer_thread.start()
        logger.info(
            "EdgeLink runtime started: sim_id=%s sensor=%s control=%s watchdog=%dms",
            self._sim_id,
            self.sensor_topic,
            self.control_topic,
            self._edge_settings.watchdog_ms,
        )

    def stop(self, timeout_s: float = 3.0) -> None:
        """发送会话 stop 帧、停止消费线程并关闭连接（幂等）。"""
        with self._lock:
            if not self._stop_sent and self._producer is not None:
                try:
                    self._send_session("stop")
                except Exception as exc:  # noqa: BLE001  stop 帧失败不阻断回收
                    logger.debug(f"EdgeLink stop frame failed: {exc}")
                self._stop_sent = True
            self._running.clear()
            consumer = self._consumer
            producer = self._producer
            thread = self._consumer_thread
            self._consumer = None
            self._producer = None
            self._consumer_thread = None
            self._bound_ego = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
        if consumer is not None:
            try:
                consumer.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"EdgeLink consumer close failed: {exc}")
        if producer is not None:
            self._flush_close_producer(producer)
        logger.info(
            "EdgeLink runtime stopped: sim_id=%s control_received=%d steps=%d",
            self._sim_id,
            self._control_count,
            self._steps,
        )

    def _build_producer(self) -> Any:
        """惰性构建 kafka-python KafkaProducer（传感器上行，acks=1 低延迟）。"""
        from kafka import KafkaProducer  # noqa: PLC0415  可选依赖惰性导入

        kwargs: dict[str, Any] = {
            "bootstrap_servers": self._settings.kafka.bootstrap_servers.split(","),
            "acks": 1,
            "linger_ms": _LINGER_MS,
            "retries": 3,
            "max_block_ms": 1000,
            "request_timeout_ms": _REQUEST_TIMEOUT_MS,
            "delivery_timeout_ms": _DELIVERY_TIMEOUT_MS,
            # V3.9.2：显式对齐单条记录上限（默认 1 MiB + 协议头预留），否则
            # kafka-python 的默认 1 MiB 会在 send() 同步抛
            # MessageSizeTooLargeError（历史上表现为面板里裸的线程栈）。
            "max_request_size": self._edge_settings.max_record_bytes
            + _MAX_REQUEST_OVERHEAD_BYTES,
            **self._security_kwargs,
        }
        return KafkaProducer(**kwargs)

    def _build_consumer(self) -> Any:
        """惰性构建 kafka-python KafkaConsumer（控制下行，latest 偏移）。"""
        from kafka import KafkaConsumer  # noqa: PLC0415  可选依赖惰性导入

        kwargs: dict[str, Any] = {
            "bootstrap_servers": self._settings.kafka.bootstrap_servers.split(","),
            "group_id": self._edge_settings.control_group_id,
            "auto_offset_reset": "latest",
            "enable_auto_commit": True,
            "value_deserializer": lambda v: v,
            "consumer_timeout_ms": 1000,
            **self._security_kwargs,
        }
        return KafkaConsumer(self.control_topic, **kwargs)

    def _build_admin(self) -> Any:
        """惰性构建 KafkaAdminClient（仅 Topic 自检/补建，V3.9.1）。"""
        from kafka.admin import KafkaAdminClient  # noqa: PLC0415

        return KafkaAdminClient(
            bootstrap_servers=self._settings.kafka.bootstrap_servers.split(","),
            client_id=f"hunter-sim-edge-{self._sim_id}"[:200],
            **self._security_kwargs,
        )

    def _ensure_topics(self) -> None:
        """确保 ``edge_sensor`` / ``edge_control`` 存在（缺则补建）。

        Raises:
            KafkaConnectionError: Topic 缺失且补建被拒（权限/策略），附按
                ``carla.*`` 前缀授权的口径说明。存在性自检本身失败时只告警，
                不阻断启动——避免在 ACL 只给 Write/Read 的现场把可用链路误
                判为故障。
        """
        admin = self._admin_factory() if self._admin_factory is not None else self._build_admin()
        try:
            try:
                existing = set(admin.list_topics())
            except Exception as exc:  # noqa: BLE001  自检不可用：保持既有行为
                logger.warning(
                    "EdgeLink Topic 存在性自检失败，跳过补建（需给 Kafka 主体对 "
                    f"carla.* 授予 DESCRIBE、或开 auto.create）：{type(exc).__name__}: {exc}"
                )
                return
            missing = [
                topic
                for topic in (self.sensor_topic, self.control_topic)
                if topic not in existing
            ]
            if not missing:
                return
            self._create_topics(admin, missing)
        finally:
            close = getattr(admin, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001  关闭失败不影响启动
                    logger.debug(f"EdgeLink admin close failed: {exc}")

    def _create_topics(self, admin: Any, missing: list[str]) -> None:
        """补建缺失 Topic；已存在视为成功，权限/策略拒绝给出人工操作指引。"""
        from kafka.admin import NewTopic  # noqa: PLC0415
        from kafka.errors import (  # noqa: PLC0415
            KafkaError,
            TopicAuthorizationFailedError,
        )

        # 单分区：EdgeLink 以 sim_id 为 key，消息本就会落到同一分区，1 分区即可
        # 保序且最省资源。副本数取 min(3, broker 数)——不能用 -1（kafka-python
        # 的 CreateTopics 只到 v3，协议默认值语义不可靠），单 broker 现场必须为 1。
        replication = self._replication_factor(admin)
        new_topics = [
            NewTopic(
                name=topic,
                num_partitions=1,
                replication_factor=replication,
            )
            for topic in missing
        ]
        try:
            admin.create_topics(new_topics, timeout_ms=_TOPIC_CREATE_TIMEOUT_MS)
        except TopicAuthorizationFailedError as exc:
            raise KafkaConnectionError(
                self._settings.kafka.bootstrap_servers,
                f"edge Topic 不存在且 Kafka 主体无 CREATE 权限（{missing}）："
                f"{type(exc).__name__}: {exc}。修法任选其一：① 给该主体授予 "
                f"carla.* 前缀的 CREATE/DESCRIBE/READ/WRITE（ACL 按前缀生效，"
                f"覆盖每轮变化的 sim_id）；② 单机测试环境可另开 broker "
                f"auto.create.topics.enable=true 作兜底（预建在前，CREATE 仍需授）。"
                f"注意 sim_id 每轮都变，手工 kafka-topics.sh 预建不是常规手段；"
                f"Topic 由 EdgeLinkRuntime.start() 用 AdminClient 预建。",
            ) from exc
        except KafkaError as exc:
            # TopicAlreadyExists 属竞态成功；其余（副本数/策略）仍应阻断启动。
            if type(exc).__name__ == "TopicAlreadyExistsError":
                logger.info(f"EdgeLink Topic 已被并发创建：{missing}")
                return
            raise KafkaConnectionError(
                self._settings.kafka.bootstrap_servers,
                f"edge Topic 补建失败（{missing}）：{type(exc).__name__}: {exc}",
            ) from exc
        logger.info(f"EdgeLink Topic 已创建：{missing}（broker auto.create 关闭时必需）")

    @staticmethod
    def _replication_factor(admin: Any) -> int:
        """按在线 broker 数取副本数（上限 3，下限 1）。"""
        try:
            brokers = admin.describe_cluster().get("brokers") or []
            return max(1, min(3, len(brokers)))
        except Exception as exc:  # noqa: BLE001  探测失败按单副本（最保守）
            logger.debug(f"EdgeLink describe_cluster failed, RF=1: {exc}")
            return 1

    def _close_producer_locked(self) -> None:
        """构建消费者失败时回收已建生产者（调用方持锁）。"""
        producer = self._producer
        self._producer = None
        if producer is not None:
            self._flush_close_producer(producer)

    @staticmethod
    def _flush_close_producer(producer: Any) -> None:
        """尽力 flush + close 生产者（桩可能不支持 timeout 参数）。"""
        try:
            producer.flush()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"EdgeLink producer flush failed: {exc}")
        try:
            producer.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"EdgeLink producer close failed: {exc}")

    # ── 每 tick 调用（control_driver hook）────────────────────────────────────

    def step(self, delta_seconds: float) -> Optional[EdgeControl]:
        """取最新控制指令 + 看门狗判定，并馈送 ego_state（tick 线程调用）。

        Returns:
            当前控制指令；看门狗/等待态返回 ``EdgeControl(0, 0, stale=True)``，
            调用方应零速刹停保持。自车未绑定（场景未就绪）时返回 None。
        """
        with self._lock:
            self._steps += 1
            now = time.time()
            if self._first_step_ts is None:
                self._first_step_ts = now
            self._bind_locked()
            if self._bound_ego is None:
                return None
            if not self._boot_sent:
                self._send_session("boot")
                self._boot_sent = True
            self._publish_ego_state()
            watchdog_s = self._edge_settings.watchdog_ms / 1000.0
            if self._last_control_recv_ts is None:
                # 尚未收到任何控制帧：零速保持，等待车端全栈产出 /cmd_vel
                self._waiting_first = True
                self._interrupted = False
                if self._first_control_timeout(now):
                    return EdgeControl(
                        0.0,
                        0.0,
                        stale=True,
                        fatal=True,
                        reason=(
                            "edge_control_unavailable: 会话已建立 "
                            f"{now - (self._first_step_ts or now):.1f}s 内未收到车端任何 "
                            "/cmd_vel（首帧控制超时，EDGE_LINK_FIRST_CONTROL_TIMEOUT_S）"
                        ),
                    )
                return EdgeControl(0.0, 0.0, stale=True)
            self._waiting_first = False
            age = now - self._last_control_recv_ts
            self._control_age_s = age
            if age > watchdog_s:
                self._interrupted = True
                return EdgeControl(0.0, 0.0, stale=True)
            self._interrupted = False
            return EdgeControl(self._latest_linear_x, self._latest_angular_z, stale=False)

    def _first_control_timeout(self, now: float) -> bool:
        """首帧控制是否已超时（幂等，只告警一次）。

        会话已建立但累计 ``first_control_timeout_s`` 仍未收到任何 ``/cmd_vel``
        时，返回 True 要求调用方结束场景：否则会拉完整个 ``duration_seconds``、
        录下数百 MB~GB mcap、再判 fail，把一次配置缺失拖成十几分钟的无效跑。
        """
        timeout_s = self._edge_settings.first_control_timeout_s
        if timeout_s <= 0.0 or self._first_step_ts is None:
            return False
        waited = now - self._first_step_ts
        if waited < timeout_s:
            return False
        if not self._first_control_fatal:
            self._first_control_fatal = True
            logger.error(
                "edge_control_unavailable: 会话已建立 %.1fs，仍未收到车端任何 /cmd_vel"
                "（首帧控制超时 %.0fs，EDGE_LINK_FIRST_CONTROL_TIMEOUT_S）→ 结束场景并判 fail。"
                "请查车端：sil_bridge 是否在跑且绑定 sim_id=%s、ros2 topic hz /cmd_vel 是否有输出、"
                "mapping cruise 是否已启动（/auto_mission/start_mapping_cruise）",
                waited,
                timeout_s,
                self._sim_id,
            )
        return True

    def status_override(self) -> Optional[tuple[str, Optional[str]]]:
        """看门狗/等待态 → PAUSED + 原因；控制正常流动时 None（运行器自行上报）。"""
        if self._waiting_first:
            return ("PAUSED", "edge_control_waiting")
        if self._interrupted:
            return ("PAUSED", "edge_control_watchdog")
        return None

    # ── 传感器上行（edge_link_sink 回调，编码 worker 线程调用）─────────────────

    def publish_sensor(
        self,
        stream: str,
        sim_ts: float,
        wall_ts_ns: int,
        payload: bytes,
        meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """馈送一帧传感器（复用现有编码产物，限频 + 尺寸保护 + base64 信封）。

        Args:
            stream: 流名（rgb/depth/lidar/imu）。
            sim_ts: CARLA 仿真经过秒。
            wall_ts_ns: 墙钟 epoch 纳秒。
            payload: 已编码字节（JPEG / 32FC1 / XYZ float32 / IMU 打包）。
            meta: 流元数据（尺寸/编码/frame_id 等）。

        Returns:
            是否实际发送（限频/超限/不可用返回 False）。
        """
        if not payload:
            return False
        fps = self._fps_for(stream)
        now = time.time()
        with self._lock:
            if self._producer is None:
                return False
            if _rate_limited(now, self._last_send_ts.get(stream, 0.0), fps):
                self._rate_drops[stream] = self._rate_drops.get(stream, 0) + 1
                return False
            if len(payload) > self._edge_settings.max_payload_bytes:
                self._size_drops[stream] = self._size_drops.get(stream, 0) + 1
                logger.warning(
                    "EdgeLink 帧超尺寸上限丢弃：stream=%s bytes=%d limit=%d",
                    stream,
                    len(payload),
                    self._edge_settings.max_payload_bytes,
                )
                return False
            self._last_send_ts[stream] = now
            self._stamp_send_locked(stream, now)
            seq = self._next_seq_locked()
            self._sent_counts[stream] = self._sent_counts.get(stream, 0) + 1
        envelope = {
            "stream": stream,
            "sim_ts": float(sim_ts),
            "wall_ts_ns": int(wall_ts_ns),
            "seq": seq,
            "meta": dict(meta or {}),
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        }
        return self._send(envelope)

    def _fps_for(self, stream: str) -> float:
        """流的上行限频（Hz）；未知流不限频（返回 0）。"""
        field = _STREAM_FPS_FIELDS.get(stream)
        if field is None:
            return 0.0
        try:
            return float(getattr(self._edge_settings, field, 0.0))
        except (TypeError, ValueError):
            # 兜底：非法值不得让 stats 热路径抛错，退回该字段声明的默认档
            # （不能返回 0，否则变成不限频而撑爆带宽）。
            default = type(self._edge_settings).model_fields[field].default
            return float(default)

    def _publish_ego_state(self) -> None:
        """按 ego_state_fps 馈送自车速度/横摆角速度/位姿（tick 线程，调用方持锁）。"""
        fps = self._edge_settings.ego_state_fps
        now = time.time()
        if _rate_limited(now, self._last_send_ts.get("ego_state", 0.0), fps):
            return
        state = self._read_ego_state()
        if state is None:
            return
        self._last_send_ts["ego_state"] = now
        self._stamp_send_locked("ego_state", now)
        seq = self._next_seq_locked()
        self._sent_counts["ego_state"] = self._sent_counts.get("ego_state", 0) + 1
        payload = json.dumps(state, default=str).encode("utf-8")
        envelope = {
            "stream": "ego_state",
            "sim_ts": state.get("sim_ts", 0.0),
            "wall_ts_ns": int(now * 1e9),
            "seq": seq,
            "meta": {"frame_id": "base_link", "encoding": "json"},
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        }
        self._send(envelope)

    def _read_ego_state(self) -> Optional[dict[str, Any]]:
        """读自车真值（tick 线程 = 信箱 owner，直读安全）；失败降级 None。

        CARLA ``get_angular_velocity`` 返回 deg/s，统一转 rad/s（ROS 口径）。
        """
        ego = self._bound_ego
        if ego is None:
            return None
        try:
            transform = ego.get_transform()
            loc = transform.location
            rot = transform.rotation
            vel = ego.get_velocity()
            ang = ego.get_angular_velocity()
            speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
            return {
                "speed_ms": round(speed, 4),
                "velocity": [round(vel.x, 4), round(vel.y, 4), round(vel.z, 4)],
                "yaw_rate": round(math.radians(float(ang.z)), 6),
                "angular_velocity": [
                    round(math.radians(float(ang.x)), 6),
                    round(math.radians(float(ang.y)), 6),
                    round(math.radians(float(ang.z)), 6),
                ],
                "pose": {
                    "x": round(float(loc.x), 4),
                    "y": round(float(loc.y), 4),
                    "z": round(float(loc.z), 4),
                    "yaw": round(math.radians(float(rot.yaw)), 6),
                    "pitch": round(math.radians(float(rot.pitch)), 6),
                    "roll": round(math.radians(float(rot.roll)), 6),
                },
            }
        except Exception as exc:  # noqa: BLE001  单帧读取失败不阻断场景
            logger.debug(f"EdgeLink ego_state read failed: {exc}")
            return None

    # ── 会话信令 ────────────────────────────────────────────────────────────

    def _send_session(self, phase: str) -> None:
        """发送会话 boot/stop 帧（调用方持锁）。"""
        meta: dict[str, Any] = {
            "phase": phase,
            "sim_id": self._sim_id,
            "scene_id": self._scene_id,
            "map_id": self._map_id,
        }
        if phase == "boot":
            meta["initial_pose"] = self._read_ego_state()
            meta["streams"] = ["rgb", "depth", "lidar", "imu", "ego_state"]
            meta["watchdog_ms"] = self._edge_settings.watchdog_ms
        seq = self._next_seq_locked()
        envelope = {
            "stream": "session",
            "sim_ts": 0.0,
            "wall_ts_ns": int(time.time() * 1e9),
            "seq": seq,
            "meta": meta,
            "payload_b64": "",
        }
        self._send(envelope)
        logger.info("EdgeLink session %s: sim_id=%s scene=%s", phase, self._sim_id, self._scene_id)

    # ── 控制下行消费 ────────────────────────────────────────────────────────

    def _consume_loop(self) -> None:
        """控制消费主循环（独立线程，latest-wins）。"""
        try:
            import kafka  # noqa: F401,PLC0415  探测依赖
        except ImportError:
            logger.error("kafka-python not installed, EdgeLink control consumer disabled")
            self._running.clear()
            return
        while self._running.is_set():
            consumer = self._consumer
            if consumer is None:
                time.sleep(0.2)
                continue
            try:
                batches = consumer.poll(timeout_ms=200)
            except Exception as exc:  # noqa: BLE001  轮询失败退避重试，不退出循环
                logger.warning(f"EdgeLink control poll error: {exc}")
                time.sleep(0.5)
                continue
            if not batches:
                continue
            for messages in batches.values():
                for msg in messages:
                    self._handle_control(msg.value)

    def _handle_control(self, raw: bytes) -> None:
        """解析一帧控制指令并更新最新值（按 seq 去重/丢乱序）。"""
        try:
            frame = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            self._control_errors += 1
            return
        if not isinstance(frame, dict):
            self._control_errors += 1
            return
        try:
            linear_x = float(frame.get("linear_x", 0.0))
            angular_z = float(frame.get("angular_z", 0.0))
        except (TypeError, ValueError):
            self._control_errors += 1
            return
        if not (math.isfinite(linear_x) and math.isfinite(angular_z)):
            self._control_errors += 1
            return
        seq = _optional_int(frame.get("seq"))
        wall_ts_ns = _optional_int(frame.get("wall_ts_ns"))
        with self._lock:
            if seq is not None and self._last_control_seq is not None and seq <= self._last_control_seq:
                return  # 重复/乱序：latest-wins 只接受更新的 seq
            recv_ts = time.time()
            self._latest_linear_x = linear_x
            self._latest_angular_z = angular_z
            self._last_control_recv_ts = recv_ts
            self._last_control_wall_ts_ns = wall_ts_ns
            if seq is not None:
                self._last_control_seq = seq
            self._control_count += 1
            self._control_stamps.append(recv_ts)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _bind_locked(self) -> None:
        """延迟绑定当前运行器自车（场景重载自车替换时刷新引用，调用方持锁）。"""
        runner = self._runner_provider()
        if runner is None:
            return
        ego = getattr(runner, "ego_vehicle", None)
        if ego is not None:
            self._bound_ego = ego

    def _next_seq_locked(self) -> int:
        """单调全局序号（调用方持锁）。"""
        self._seq += 1
        return self._seq

    def _stamp_send_locked(self, stream: str, now: float) -> None:
        """记一次实际上行样本（调用方持锁）；仅用于馈送速率滑窗。"""
        stamps = self._send_stamps.get(stream)
        if stamps is None:
            stamps = deque(maxlen=_MAX_FPS_SAMPLES)
            self._send_stamps[stream] = stamps
        stamps.append(now)

    def _send(self, envelope: dict[str, Any]) -> bool:
        """发送一条 JSON 信封到传感器上行 Topic（best-effort，不阻断仿真）。"""
        producer = self._producer
        if producer is None:
            return False
        key = self._sim_id.encode("utf-8")
        try:
            value = json.dumps(envelope, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self._send_errors += 1
            logger.warning(f"EdgeLink envelope serialize failed: {exc}")
            return False
        # V3.9.2：按**序列化后**记录尺寸判限（而不是只看 base64 前的 payload）。
        # 超出 broker/producer 上限的记录 kafka-python 会在 send() 同步抛
        # MessageSizeTooLargeError，异常栈直接冒到面板且不带 stream/尺寸上下文；
        # 这里提前丢弃并计数，日志给出可执行的处置。
        limit = self._edge_settings.max_record_bytes
        if len(value) > limit:
            stream = str(envelope.get("stream") or "unknown")
            self._record_drops += 1
            self._size_drops[stream] = self._size_drops.get(stream, 0) + 1
            now = time.time()
            if now - self._last_size_drop_log_ts >= _SIZE_DROP_LOG_INTERVAL_S:
                self._last_size_drop_log_ts = now
                logger.warning(
                    "EdgeLink 记录超限丢弃：stream=%s bytes=%d limit=%d"
                    "（EDGE_LINK_MAX_RECORD_BYTES，须 ≤ broker message.max.bytes）"
                    "——可降该流 fps/分辨率，或让运维抬高 broker message.max.bytes "
                    "后同步调大本值",
                    stream,
                    len(value),
                    limit,
                )
            return False
        try:
            producer.send(self.sensor_topic, key=key, value=value)
        except Exception as exc:  # noqa: BLE001  发送失败计数降级，不冒泡阻断
            self._send_errors += 1
            now = time.time()
            # V3.9.1：Topic 缺失时每条 send() 都会抛 KafkaTimeoutError（1Hz×5 流），
            # 不节流会在面板刷出上千条同因告警，掩盖真正的首因。首条必报，其后
            # 每 10s 汇总一次被抑制条数。
            if now - self._last_send_error_log_ts >= _SEND_ERROR_LOG_INTERVAL_S:
                suppressed = self._send_error_suppressed
                self._send_error_suppressed = 0
                self._last_send_error_log_ts = now
                suffix = f"（近 {_SEND_ERROR_LOG_INTERVAL_S:.0f}s 另抑制 {suppressed} 条）" if suppressed else ""
                logger.warning(
                    f"EdgeLink send failed (stream={envelope.get('stream')}): "
                    f"{type(exc).__name__}: {exc}{suffix}"
                )
            else:
                self._send_error_suppressed += 1
            return False
        return True

    # ── 可观测 ──────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """链路诊断（脱敏，供 sim_status/sim_telemetry 的 edge_link 段与 viz 卡片）。"""
        with self._lock:
            now = time.time()
            control_age_ms = (
                round((now - self._last_control_recv_ts) * 1000.0, 1)
                if self._last_control_recv_ts is not None
                else None
            )
            return {
                "enabled": True,
                "sim_id": self._sim_id,
                "sensor_topic": self.sensor_topic,
                "control_topic": self.control_topic,
                "control_source": "edge_link",
                "boot_sent": self._boot_sent,
                "waiting_first_control": self._waiting_first,
                "watchdog_tripped": self._interrupted,
                "watchdog_ms": self._edge_settings.watchdog_ms,
                "control_received": self._control_count,
                "control_errors": self._control_errors,
                "control_age_ms": control_age_ms,
                "control_hz": _rate_hz(self._control_stamps, now),
                "last_linear_x": self._latest_linear_x,
                "last_angular_z": self._latest_angular_z,
                "sensor_sent": dict(self._sent_counts),
                "sensor_hz": {
                    name: _rate_hz(stamps, now)
                    for name, stamps in self._send_stamps.items()
                },
                "sensor_nominal_fps": {
                    name: self._fps_for(name) for name in _STREAM_FPS_FIELDS
                },
                "sensor_rate_dropped": dict(self._rate_drops),
                "sensor_size_dropped": dict(self._size_drops),
                "sensor_record_dropped": self._record_drops,
                "record_limit_bytes": self._edge_settings.max_record_bytes,
                "first_control_timeout_s": self._edge_settings.first_control_timeout_s,
                "send_errors": self._send_errors,
                "steps": self._steps,
                "uptime_s": round(max(0.0, now - self._start_ts), 1),
            }

    def summary(self) -> dict[str, Any]:
        """平台契约用的精简链路段（sim_status/sim_telemetry 的 ``edge_link``）。

        只透出裁决与排障必需项：链路年龄、控制回传频率、看门狗态、控制源、
        各流馈送速率。完整诊断（Topic 名/累计计数/丢弃明细）走 viz 只读端点，
        不进平台契约，避免 10Hz 上行报文膨胀。
        """
        stats = self.stats
        return {
            "enabled": True,
            "control_source": stats["control_source"],
            "session_booted": stats["boot_sent"],
            "waiting_first_control": stats["waiting_first_control"],
            "watchdog_tripped": stats["watchdog_tripped"],
            "watchdog_ms": stats["watchdog_ms"],
            "control_age_ms": stats["control_age_ms"],
            "control_hz": stats["control_hz"],
            "sensor_hz": stats["sensor_hz"],
            "send_errors": stats["send_errors"],
            "uptime_s": stats["uptime_s"],
        }


def _rate_hz(stamps: deque[float], now: float) -> float:
    """最近 ``_FPS_WINDOW_S`` 窗口内的速率（Hz），窗口右端锚 ``now``。

    样本数/跨度算法与 ``FrameHub.fps`` 一致（``(n-1)/span``），但锚点取 ``now``
    而非最后一个样本：链路断流时速率必须衰减到 0，否则卡片会在看门狗已刹停后
    仍显示标称速率，反而掩盖断流（FrameHub 另有 ``age_s`` 判 stale，此处没有）。
    """
    if len(stamps) < 2:
        return 0.0
    cutoff = now - _FPS_WINDOW_S
    window = [ts for ts in stamps if ts >= cutoff]
    if len(window) < 2:
        return 0.0
    span = window[-1] - window[0]
    if span <= 0:
        return 0.0
    return round((len(window) - 1) / span, 2)


def _rate_limited(now: float, last: float, fps: float) -> bool:
    """限频判定（带浮点量级容差）；``fps <= 0`` 表示不限频。

    ``time.time()`` 处于 epoch 量级（~1.7e9），float64 表示误差约 4e-7 s：
    恰好按标称周期到达的帧会算出略小于周期的差值而被误丢，10Hz 静默退化成
    约 5Hz。容差取 ``max(1e-9, |now| * 1e-15)`` 覆盖该量级误差（与车端
    sil_bridge 的 StreamRateLimiter 同口径）。
    """
    if fps <= 0.0:
        return False
    tol = max(1e-9, abs(now) * 1e-15)
    return (now - last) < (1.0 / fps) - tol


def _optional_int(value: Any) -> Optional[int]:
    """安全转 int（None/非法返回 None）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["EdgeControl", "EdgeLinkRuntime"]

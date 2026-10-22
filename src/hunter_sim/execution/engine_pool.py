"""CARLA 引擎库存与地图亲和调度（V2.4.0 执行面分离，V2.8.0 P2-14 地图感知）。

一个 CARLA server 同一时刻只能承载一个场景（``load_world`` 会重置整个世界）。
``EnginePool`` 以端点（host:rpc_port）为粒度维护占用关系，保证：

- 同一端点不会被两个实例同时租用；
- 多引擎部署时，每个实例在自己的端点上并行运行；
- 释放后端点立即可被新实例复用（不再依赖端口递增猜测）。

V2.8.0 P2-14：端点额外记住自己最后一次实际加载的地图（CARLA 释放租约后世界仍驻内存）。
装配前按目标地图做亲和选择：同地图的空闲端点优先复用（省一次 ``load_world``，
避免不必要的地图重置），因此“不同地图也能共存”不再受单地图误伤。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.models import CarlaSettings

# 端点字符串中允许的最多段数：host:rpc[:stream[:tm]]
_MAX_ENDPOINT_PARTS = 4
_MIN_PORT = 1024
_MAX_PORT = 65535


class EngineUnavailable(RuntimeError):
    """没有可用 CARLA 引擎（全部被占用或端点不可用）。"""


@dataclass(frozen=True)
class EngineEndpoint:
    """一个 CARLA 引擎端点。

    Attributes:
        host: CARLA 服务器地址。
        rpc_port: RPC 端口。
        stream_port: 数据流端口（默认 rpc+1）。
        tm_port: Traffic Manager 端口（默认 8000）。
    """

    host: str
    rpc_port: int
    stream_port: int
    tm_port: int = 8000

    @property
    def key(self) -> str:
        """端点唯一键：host:rpc_port。"""
        return f"{self.host}:{self.rpc_port}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 响应字段。"""
        return {
            "key": self.key,
            "host": self.host,
            "rpc_port": self.rpc_port,
            "stream_port": self.stream_port,
            "tm_port": self.tm_port,
        }


@dataclass
class EngineLease:
    """引擎租约：记录哪个实例在哪个端点上运行哪个地图。

    V2.7.0 P1-7：新增 ``last_renew_at`` 心跳时间戳，运行中的实例持续续租；
    心跳长时间未刷即视为僵尸持有者（线程已死但租约仍在）。
    """

    instance_id: str
    endpoint: EngineEndpoint
    map_id: str
    acquired_at: float = field(default_factory=time.time)
    last_renew_at: float = field(default_factory=time.time)

    def renew(self, now: Optional[float] = None) -> None:
        """刷一次心跳时间（默认当前时刻）。"""
        self.last_renew_at = time.time() if now is None else float(now)

    def is_stale(self, ttl_seconds: float, now: Optional[float] = None) -> bool:
        """心跳是否超过 ``ttl_seconds`` 未续（ttl<=0 视为禁用，永不判僵）。"""
        if ttl_seconds <= 0:
            return False
        current = time.time() if now is None else float(now)
        return (current - self.last_renew_at) > ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 响应字段。"""
        return {
            "instance_id": self.instance_id,
            "engine": self.endpoint.to_dict(),
            "map_id": self.map_id,
            "acquired_at": self.acquired_at,
            "last_renew_at": self.last_renew_at,
        }


def parse_engine_endpoints(
    spec: str,
    base: CarlaSettings,
    max_engines: int = 8,
) -> list[EngineEndpoint]:
    """解析 ``EXECUTION_ENGINE_ENDPOINTS``，为空时退化为单引擎。

    格式：``host:rpc[:stream[:tm]],host2:rpc2``。端口均为整数，
    stream 缺省取 ``rpc+1``，tm 缺省取 ``CARLA_TM_PORT`` 或 8000。

    Args:
        spec: 逗号分隔的端点清单。
        base: CARLA 基础配置（单引擎回退与默认 TM 端口）。
        max_engines: 允许的最大端点数。

    Returns:
        端点列表（至少一个）。

    Raises:
        ConfigurationError: 端点格式非法、端口越界或数量超限。
    """
    default_tm = getattr(base, "tm_port", 8000)
    if not spec.strip():
        return [
            EngineEndpoint(
                host=base.host,
                rpc_port=base.rpc_port,
                stream_port=base.stream_port,
                tm_port=default_tm,
            )
        ]

    entries = [entry.strip() for entry in spec.split(",") if entry.strip()]
    if not entries:
        raise ConfigurationError("execution.engine_endpoints", "端点清单为空")
    if len(entries) > max_engines:
        raise ConfigurationError(
            "execution.engine_endpoints",
            f"端点数量 {len(entries)} 超过 EXECUTION_MAX_ENGINES={max_engines}",
        )

    endpoints: list[EngineEndpoint] = []
    seen: set[str] = set()
    for entry in entries:
        parts = entry.split(":")
        if len(parts) < 2 or len(parts) > _MAX_ENDPOINT_PARTS:
            raise ConfigurationError(
                "execution.engine_endpoints",
                f"端点 '{entry}' 格式非法，应为 host:rpc[:stream[:tm]]",
            )
        host = parts[0].strip()
        if not host:
            raise ConfigurationError(
                "execution.engine_endpoints", f"端点 '{entry}' 缺少 host"
            )
        try:
            ports = [int(p) for p in parts[1:]]
        except ValueError as exc:
            raise ConfigurationError(
                "execution.engine_endpoints", f"端点 '{entry}' 端口不是整数"
            ) from exc
        for port in ports:
            if not _MIN_PORT <= port <= _MAX_PORT:
                raise ConfigurationError(
                    "execution.engine_endpoints",
                    f"端点 '{entry}' 端口 {port} 超出范围 [{_MIN_PORT}, {_MAX_PORT}]",
                )
        rpc = ports[0]
        stream = ports[1] if len(ports) > 1 else rpc + 1
        tm = ports[2] if len(ports) > 2 else default_tm
        endpoint = EngineEndpoint(host=host, rpc_port=rpc, stream_port=stream, tm_port=tm)
        if endpoint.key in seen:
            raise ConfigurationError(
                "execution.engine_endpoints", f"端点 '{endpoint.key}' 重复声明"
            )
        seen.add(endpoint.key)
        endpoints.append(endpoint)
    return endpoints


class EnginePool:
    """引擎端点租约池（线程安全）。

    Args:
        endpoints: 已声明的引擎库存（用于容量与快照展示）。
    """

    def __init__(
        self, endpoints: Optional[Iterable[EngineEndpoint]] = None, lease_ttl_seconds: float = 0.0
    ) -> None:
        self._known: dict[str, EngineEndpoint] = {
            ep.key: ep for ep in (endpoints or [])
        }
        self._leases: dict[str, EngineLease] = {}
        self._lock = threading.RLock()
        # 心跳 TTL（秒）：0 表示禁用僵判定（单引擎/未启用心跳）
        self._ttl_seconds = float(lease_ttl_seconds)
        # 端点 → 最后一次成功加载的地图（P2-14）：释放租约后仍保留，用于亲和调度
        self._endpoint_maps: dict[str, str] = {}

    @property
    def capacity(self) -> int:
        """已声明的引擎数量（未声明时按已租用端点数动态计算）。"""
        with self._lock:
            leased = {lease.endpoint.key for lease in self._leases.values()}
            return max(len(self._known), len(leased))

    @property
    def active_count(self) -> int:
        """当前活跃租约数（= 正在运行的场景数）。"""
        with self._lock:
            return len(self._leases)

    def acquire(
        self,
        instance_id: str,
        endpoint: EngineEndpoint,
        map_id: str,
    ) -> EngineLease:
        """为实例租用指定引擎端点。

        Args:
            instance_id: 实例 ID。
            endpoint: 实例绑定的 CARLA 端点。
            map_id: 实例地图。

        Returns:
            租约对象（同一实例重复调用返回既有租约）。

        Raises:
            EngineUnavailable: 端点已被其他实例占用。
        """
        with self._lock:
            existing = self._leases.get(instance_id)
            if existing is not None:
                if existing.endpoint.key != endpoint.key:
                    raise EngineUnavailable(
                        f"Instance '{instance_id}' already holds engine "
                        f"'{existing.endpoint.key}', cannot switch to '{endpoint.key}' at runtime"
                    )
                return existing
            holder = self._holder_of(endpoint.key)
            if holder is not None:
                raise EngineUnavailable(
                    f"CARLA engine '{endpoint.key}' is occupied by instance "
                    f"'{holder.instance_id}' (one engine can host one scene at a time); "
                    "stop/destroy that instance or add engines via EXECUTION_ENGINE_ENDPOINTS"
                )
            lease = EngineLease(instance_id=instance_id, endpoint=endpoint, map_id=map_id)
            self._leases[instance_id] = lease
            self._known.setdefault(endpoint.key, endpoint)
            self._endpoint_maps[endpoint.key] = map_id
            return lease

    def release(self, instance_id: str) -> bool:
        """释放实例的引擎租约，返回是否确实释放。"""
        with self._lock:
            return self._leases.pop(instance_id, None) is not None

    def get(self, instance_id: str) -> Optional[EngineLease]:
        """查询实例当前租约。"""
        with self._lock:
            return self._leases.get(instance_id)

    def renew(self, instance_id: str) -> bool:
        """为实例租约刷一次心跳（P1-7）；无租约时返回 False。

        由健康巡检周期对存活运行器调用；僵尸持有者因无存活运行器而不再被续，
        心跳逐渐过期直至被判为僵尸。
        """
        with self._lock:
            lease = self._leases.get(instance_id)
            if lease is None:
                return False
            lease.renew()
            return True

    def stale_leases(self) -> list[EngineLease]:
        """返回心跳超时的僵尸租约（TTL<=0 时为空）。"""
        with self._lock:
            return [lease for lease in self._leases.values() if lease.is_stale(self._ttl_seconds)]

    # ─── 地图亲和调度（V2.8.0 P2-14） ──────────────────────────────────

    def note_loaded_map(self, endpoint_key: str, map_id: str) -> None:
        """登记端点实际加载的地图（世界加载成功后由运行器调用）。

        ``acquire`` 只能记录“申请时的地图”，而 ``load_world`` 可能失败或回退到
        默认地图；本方法以事实为准覆盖，下次调度才能正确命中亲和。
        """
        if not map_id:
            return
        with self._lock:
            self._endpoint_maps[endpoint_key] = map_id

    def loaded_map_of(self, endpoint_key: str) -> str:
        """端点最后已知加载的地图（未知返回空串）。"""
        with self._lock:
            return self._endpoint_maps.get(endpoint_key, "")

    def is_endpoint_free(self, endpoint_key: str) -> bool:
        """端点当前是否无活跃租约（僵租约视同空闲，已被心跳回收口径覆盖）。"""
        with self._lock:
            return self._holder_of(endpoint_key) is None

    def lease_of(self, endpoint_key: str) -> Optional[EngineLease]:
        """返回占用指定端点的租约（无占用返回 None）。"""
        with self._lock:
            return self._holder_of(endpoint_key)

    def pick_free_endpoint(
        self,
        candidates: Iterable[EngineEndpoint],
        prefer_map_id: str = "",
    ) -> Optional[EngineEndpoint]:
        """在候选端点中挑一个空闲的，同地图优先（P2-14）。

        优先级：已加载目标地图的空闲端点（免 ``load_world``）
        → 从未加载过地图的空闲端点（避免动他人在用的世界）
        → 按声明顺序的首个空闲端点（需切换地图，但仍是空闲引擎）。

        Args:
            candidates: 候选端点库存（声明顺序即优先级顺序）。
            prefer_map_id: 目标地图 ID（空串表示无亲和偏好）。

        Returns:
            选中的端点；候选全部被占用时返回 None。
        """
        with self._lock:
            free = [ep for ep in candidates if self._holder_of(ep.key) is None]
            if not free:
                return None
            if prefer_map_id:
                for endpoint in free:
                    if self._endpoint_maps.get(endpoint.key) == prefer_map_id:
                        return endpoint
            for endpoint in free:
                if not self._endpoint_maps.get(endpoint.key):
                    return endpoint
            return free[0]

    def snapshot(self) -> dict[str, Any]:
        """执行面快照（供 ``GET /execution`` 与排障使用）。"""
        with self._lock:
            leases = {iid: lease.to_dict() for iid, lease in self._leases.items()}
            for iid, lease in self._leases.items():
                leases[iid]["stale"] = lease.is_stale(self._ttl_seconds)
            engines: list[dict[str, Any]] = []
            for key, endpoint in sorted(self._known.items()):
                holder = self._holder_of(key)
                item = endpoint.to_dict()
                item["holder"] = holder.instance_id if holder else None
                item["map_id"] = (
                    holder.map_id if holder else self._endpoint_maps.get(key, "")
                )
                item["acquired_at"] = holder.acquired_at if holder else None
                item["last_renew_at"] = holder.last_renew_at if holder else None
                item["stale"] = bool(holder and holder.is_stale(self._ttl_seconds))
                engines.append(item)
            stale_count = sum(
                1 for lease in self._leases.values() if lease.is_stale(self._ttl_seconds)
            )
        return {
            "engines": engines,
            "capacity": len(engines),
            "active_leases": len(leases),
            "stale_leases": stale_count,
            "lease_ttl_seconds": self._ttl_seconds,
            "map_affinity_enabled": True,
            "leases": list(leases.values()),
        }

    def _holder_of(self, endpoint_key: str) -> Optional[EngineLease]:
        """返回占用指定端点的租约（已持有锁）。"""
        for lease in self._leases.values():
            if lease.endpoint.key == endpoint_key:
                return lease
        return None


__all__ = [
    "EngineEndpoint",
    "EngineLease",
    "EnginePool",
    "EngineUnavailable",
    "parse_engine_endpoints",
]

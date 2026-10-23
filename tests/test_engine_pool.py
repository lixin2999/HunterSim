"""引擎库存与租约池测试（V2.4.0 执行面分离，V2.8.0 P2-14 地图亲和）。

覆盖端点字符串解析、独占租约语义、释放复用、快照内容，
以及端点地图记忆与亲和调度（同地图空闲优先 → 未加载过地图 → 声明顺序）；
不依赖真实 CARLA server。
"""

from __future__ import annotations

import time

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.models import CarlaSettings
from hunter_sim.execution.engine_pool import (
    EngineEndpoint,
    EnginePool,
    EngineUnavailable,
    parse_engine_endpoints,
)


def _base(**overrides: object) -> CarlaSettings:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "rpc_port": 2000,
        "stream_port": 2001,
        "tm_port": 8000,
    }
    values.update(overrides)
    return CarlaSettings(**values)


class TestParseEngineEndpoints:
    def test_empty_spec_falls_back_to_single_engine(self) -> None:
        endpoints = parse_engine_endpoints("", _base())

        assert len(endpoints) == 1
        assert endpoints[0].key == "127.0.0.1:2000"
        assert endpoints[0].stream_port == 2001
        assert endpoints[0].tm_port == 8000

    def test_multiple_hosts_with_defaults(self) -> None:
        endpoints = parse_engine_endpoints("10.0.0.1:2000,10.0.0.2:2100", _base())

        assert [ep.key for ep in endpoints] == ["10.0.0.1:2000", "10.0.0.2:2100"]
        assert endpoints[0].stream_port == 2001
        assert endpoints[1].stream_port == 2101
        assert endpoints[1].tm_port == 8000

    def test_full_form_overrides_stream_and_tm(self) -> None:
        endpoints = parse_engine_endpoints("carla-1:2000:3001:9000", _base())

        assert endpoints[0].stream_port == 3001
        assert endpoints[0].tm_port == 9000

    def test_whitespace_is_tolerated(self) -> None:
        endpoints = parse_engine_endpoints(" 10.0.0.1:2000 , 10.0.0.2:2000 ", _base())
        assert len(endpoints) == 2

    @pytest.mark.parametrize(
        "spec",
        [
            "127.0.0.1",  # 缺端口
            "127.0.0.1:abc",  # 端口非整数
            "127.0.0.1:80",  # 端口越界（<1024）
            "127.0.0.1:70000",  # 端口越界（>65535）
            ":2000",  # 缺 host
            "127.0.0.1:2000:2001:8000:1",  # 段数过多
        ],
    )
    def test_invalid_spec_raises_configuration_error(self, spec: str) -> None:
        with pytest.raises(ConfigurationError):
            parse_engine_endpoints(spec, _base())

    def test_duplicate_endpoint_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="重复"):
            parse_engine_endpoints("127.0.0.1:2000,127.0.0.1:2000", _base())

    def test_exceeding_max_engines_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="EXECUTION_MAX_ENGINES"):
            parse_engine_endpoints(
                "127.0.0.1:2000,127.0.0.1:2100,127.0.0.1:2200", _base(), max_engines=2
            )

    def test_to_dict(self) -> None:
        payload = EngineEndpoint("10.1.1.1", 2000, 2001, 8000).to_dict()
        assert payload == {
            "key": "10.1.1.1:2000",
            "host": "10.1.1.1",
            "rpc_port": 2000,
            "stream_port": 2001,
            "tm_port": 8000,
        }


class TestEnginePool:
    def _pool(self) -> tuple[EnginePool, EngineEndpoint, EngineEndpoint]:
        first = EngineEndpoint("10.0.0.1", 2000, 2001)
        second = EngineEndpoint("10.0.0.2", 2000, 2001)
        return EnginePool([first, second]), first, second

    def test_capacity_and_active_count(self) -> None:
        pool, first, _ = self._pool()
        assert pool.capacity == 2
        assert pool.active_count == 0

        pool.acquire("inst-1", first, "Town03")
        assert pool.active_count == 1

    def test_acquire_returns_lease(self) -> None:
        pool, first, _ = self._pool()
        lease = pool.acquire("inst-1", first, "Town03")

        assert lease.instance_id == "inst-1"
        assert lease.endpoint.key == "10.0.0.1:2000"
        assert lease.map_id == "Town03"
        assert lease.acquired_at > 0
        assert pool.get("inst-1") is lease

    def test_same_instance_returns_existing_lease(self) -> None:
        pool, first, _ = self._pool()
        lease = pool.acquire("inst-1", first, "Town03")
        assert pool.acquire("inst-1", first, "Town03") is lease

    def test_same_instance_cannot_switch_engine(self) -> None:
        pool, first, second = self._pool()
        pool.acquire("inst-1", first, "Town03")

        with pytest.raises(EngineUnavailable, match="cannot switch"):
            pool.acquire("inst-1", second, "Town03")

    def test_engine_exclusive_to_one_instance(self) -> None:
        pool, first, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")

        with pytest.raises(EngineUnavailable, match="inst-1"):
            pool.acquire("inst-2", first, "Town03")

    def test_release_frees_engine_for_reuse(self) -> None:
        pool, first, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")

        assert pool.release("inst-1") is True
        assert pool.release("inst-1") is False
        assert pool.active_count == 0

        lease = pool.acquire("inst-2", first, "Town04")
        assert lease.instance_id == "inst-2"
        assert lease.map_id == "Town04"

    def test_snapshot_reports_holder_and_map(self) -> None:
        pool, first, second = self._pool()
        pool.acquire("inst-1", first, "Town03")

        snapshot = pool.snapshot()
        engines = {item["key"]: item for item in snapshot["engines"]}
        assert snapshot["capacity"] == 2
        assert snapshot["active_leases"] == 1
        assert engines["10.0.0.1:2000"]["holder"] == "inst-1"
        assert engines["10.0.0.1:2000"]["map_id"] == "Town03"
        assert engines["10.0.0.2:2000"]["holder"] is None
        assert snapshot["leases"][0]["instance_id"] == "inst-1"
        assert second.key in engines

    def test_unknown_endpoint_is_registered_on_acquire(self) -> None:
        pool = EnginePool([])
        endpoint = EngineEndpoint("127.0.0.1", 2000, 2001)

        pool.acquire("inst-1", endpoint, "Town03")

        assert pool.capacity == 1
        assert pool.snapshot()["engines"][0]["key"] == "127.0.0.1:2000"


class TestMapAffinity:
    """地图感知调度（V2.8.0 P2-14）。"""

    def _pool(self) -> tuple[EnginePool, EngineEndpoint, EngineEndpoint, EngineEndpoint]:
        first = EngineEndpoint("10.0.0.1", 2000, 2001)
        second = EngineEndpoint("10.0.0.2", 2000, 2001)
        third = EngineEndpoint("10.0.0.3", 2000, 2001)
        return EnginePool([first, second, third]), first, second, third

    def test_note_and_read_loaded_map(self) -> None:
        pool, first, _, _ = self._pool()

        assert pool.loaded_map_of(first.key) == ""
        pool.note_loaded_map(first.key, "Town03")
        assert pool.loaded_map_of(first.key) == "Town03"

        # 空 map_id 不覆盖已有事实（load_world 回退时不得抹除记忆）
        pool.note_loaded_map(first.key, "")
        assert pool.loaded_map_of(first.key) == "Town03"

        # 未知端点不报错，返回空串
        assert pool.loaded_map_of("10.9.9.9:2000") == ""

    def test_acquire_records_requested_map(self) -> None:
        pool, first, _, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")

        assert pool.loaded_map_of(first.key) == "Town03"

    def test_loaded_map_fact_overrides_acquire_claim(self) -> None:
        """实际加载成功的世界为准：避免 acquire 时的“申请地图”误导亲和。"""
        pool, first, _, _ = self._pool()
        pool.acquire("inst-1", first, "Town05")
        pool.note_loaded_map(first.key, "Town03")

        assert pool.loaded_map_of(first.key) == "Town03"

    def test_same_map_prefers_endpoint_already_holding_it(self) -> None:
        pool, first, second, _ = self._pool()
        pool.acquire("inst-1", second, "Town03")
        pool.note_loaded_map(second.key, "Town03")
        pool.release("inst-1")

        picked = pool.pick_free_endpoint([first, second], prefer_map_id="Town03")

        # 声明顺序上 first 在前，但 second 已加载目标地图，应优先复用（免 load_world）
        assert picked is second

    def test_free_endpoint_without_map_beats_map_switch(self) -> None:
        pool, first, second, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")
        pool.note_loaded_map(first.key, "Town03")
        pool.release("inst-1")

        picked = pool.pick_free_endpoint([first, second], prefer_map_id="Town04")

        # 目标地图未命中：宁可动“从未加载过地图”的空闲引擎，不去切换他人在用的世界
        assert picked is second

    def test_falls_back_to_first_free_when_all_hold_other_maps(self) -> None:
        pool, first, second, _ = self._pool()
        pool.note_loaded_map(first.key, "Town01")
        pool.note_loaded_map(second.key, "Town02")

        assert pool.pick_free_endpoint([first, second], prefer_map_id="Town09") is first
        # 无亲和偏好时同样按声明顺序
        assert pool.pick_free_endpoint([first, second]) is first

    def test_returns_none_when_all_candidates_occupied(self) -> None:
        pool, first, second, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")
        pool.acquire("inst-2", second, "Town04")

        assert pool.pick_free_endpoint([first, second], prefer_map_id="Town03") is None

    def test_occupied_endpoint_with_matching_map_is_not_picked(self) -> None:
        """亲和仅限“空闲”端点：同地图但已被占用的引擎不得抢用。"""
        pool, first, second, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")
        pool.note_loaded_map(first.key, "Town03")

        assert pool.pick_free_endpoint([first, second], prefer_map_id="Town03") is second

    def test_is_endpoint_free_and_lease_of(self) -> None:
        pool, first, _, _ = self._pool()
        assert pool.is_endpoint_free(first.key) is True

        lease = pool.acquire("inst-1", first, "Town03")
        assert pool.is_endpoint_free(first.key) is False
        assert pool.lease_of(first.key) is lease
        assert pool.lease_of("10.9.9.9:2000") is None

        pool.release("inst-1")
        assert pool.is_endpoint_free(first.key) is True
        assert pool.lease_of(first.key) is None

    def test_snapshot_exposes_affinity_and_free_endpoint_map(self) -> None:
        pool, first, second, _ = self._pool()
        pool.acquire("inst-1", first, "Town03")
        pool.note_loaded_map(second.key, "Town04")
        pool.release("inst-1")

        engines = {item["key"]: item for item in pool.snapshot()["engines"]}
        assert pool.snapshot()["map_affinity_enabled"] is True
        # 空闲端点仍保留最后已知地图（供亲和决策与排障）
        assert engines[second.key]["holder"] is None
        assert engines[second.key]["map_id"] == "Town04"
        assert engines[first.key]["map_id"] == "Town03"


# ─── V2.9.0：租约僵化判定（强制回收前置条件） ──────────────────────────────


class TestLeaseStaleness:
    def test_ttl_zero_disables_staleness(self) -> None:
        endpoint = EngineEndpoint("10.0.0.1", 2000, 2001)
        pool = EnginePool([endpoint])
        lease = pool.acquire("inst-1", endpoint, "Town03")
        lease.renew(now=0.0)  # 极旧心跳

        assert pool.lease_ttl_seconds == 0.0
        assert pool.is_stale("inst-1") is False
        assert pool.stale_leases() == []

    def test_is_stale_after_ttl_expiry_and_renew_recovers(self) -> None:
        endpoint = EngineEndpoint("10.0.0.1", 2000, 2001)
        pool = EnginePool([endpoint], lease_ttl_seconds=30.0)
        lease = pool.acquire("inst-1", endpoint, "Town03")

        assert pool.lease_ttl_seconds == 30.0
        assert pool.is_stale("inst-1") is False  # 刚领取即新鲜

        lease.renew(now=time.time() - 60.0)
        assert pool.is_stale("inst-1") is True
        assert [item.instance_id for item in pool.stale_leases()] == ["inst-1"]

        assert pool.renew("inst-1") is True  # 存活持有者续租后恢复
        assert pool.is_stale("inst-1") is False

    def test_is_stale_unknown_instance(self) -> None:
        pool = EnginePool(lease_ttl_seconds=30.0)

        assert pool.is_stale("ghost") is False
        assert pool.renew("ghost") is False

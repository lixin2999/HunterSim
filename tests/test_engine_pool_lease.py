"""EnginePool 租约 / 地图亲和调度单测（V3.0.0 执行面收敛后补齐）。

覆盖：端点解析（缺省回退、越界/重复/格式非法）、租约领取与端点互斥、
心跳续租与僵尸判定、地图亲和选择优先级、执行面快照。
"""

from __future__ import annotations

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.models import CarlaSettings
from hunter_sim.execution.engine_pool import (
    EngineEndpoint,
    EngineLease,
    EnginePool,
    EngineUnavailable,
    parse_engine_endpoints,
)

_TTL = 30.0


def _endpoint(rpc_port: int = 2000, host: str = "127.0.0.1") -> EngineEndpoint:
    return EngineEndpoint(host=host, rpc_port=rpc_port, stream_port=rpc_port + 1, tm_port=8000)


class TestParseEngineEndpoints:
    def test_empty_spec_falls_back_to_single_engine(self) -> None:
        endpoints = parse_engine_endpoints("", CarlaSettings())

        assert len(endpoints) == 1
        assert endpoints[0].key == "127.0.0.1:2000"
        assert endpoints[0].stream_port == 2001
        assert endpoints[0].tm_port == 8000

    def test_full_and_short_forms(self) -> None:
        base = CarlaSettings(tm_port=9000)
        endpoints = parse_engine_endpoints("10.0.0.1:2000:2001:8000, 10.0.0.2:3000", base)

        assert [ep.key for ep in endpoints] == ["10.0.0.1:2000", "10.0.0.2:3000"]
        assert endpoints[0].tm_port == 8000
        # 缺省 stream=rpc+1、tm 取 CARLA_TM_PORT
        assert endpoints[1].stream_port == 3001
        assert endpoints[1].tm_port == 9000

    def test_too_many_engines_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            parse_engine_endpoints("h1:2000,h2:2001", CarlaSettings(), max_engines=1)

    @pytest.mark.parametrize(
        "spec",
        [
            "127.0.0.1",  # 缺 rpc 端口
            "127.0.0.1:2000:2001:8000:9",  # 段数超限
            ":2000",  # 缺 host
            "127.0.0.1:abc",  # 端口非整数
            "127.0.0.1:80",  # 端口越界
        ],
    )
    def test_invalid_entries_raise(self, spec: str) -> None:
        with pytest.raises(ConfigurationError):
            parse_engine_endpoints(spec, CarlaSettings())

    def test_duplicate_declaration_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            parse_engine_endpoints("127.0.0.1:2000,127.0.0.1:2000", CarlaSettings())


class TestLease:
    def test_acquire_release_and_query(self) -> None:
        pool = EnginePool([_endpoint()])
        lease = pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")

        assert lease.instance_id == "inst-1"
        assert pool.active_count == 1
        assert pool.get("inst-1") is lease
        assert pool.lease_of("127.0.0.1:2000") is lease
        assert pool.is_endpoint_free("127.0.0.1:2000") is False
        assert pool.loaded_map_of("127.0.0.1:2000") == "/Game/Maps/Town01"

        assert pool.release("inst-1") is True
        assert pool.release("inst-1") is False
        assert pool.active_count == 0
        assert pool.get("inst-1") is None
        assert pool.is_endpoint_free("127.0.0.1:2000") is True

    def test_same_instance_same_endpoint_is_idempotent(self) -> None:
        pool = EnginePool()
        first = pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")
        again = pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")

        assert again is first
        assert pool.active_count == 1

    def test_same_instance_cannot_switch_endpoint(self) -> None:
        pool = EnginePool()
        pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")

        with pytest.raises(EngineUnavailable):
            pool.acquire("inst-1", _endpoint(rpc_port=3000), "/Game/Maps/Town01")

    def test_endpoint_exclusivity(self) -> None:
        pool = EnginePool()
        pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")

        with pytest.raises(EngineUnavailable):
            pool.acquire("inst-2", _endpoint(), "/Game/Maps/Town02")

    def test_capacity_prefers_declared_inventory(self) -> None:
        declared = EnginePool([_endpoint(2000), _endpoint(3000)])
        assert declared.capacity == 2

        dynamic = EnginePool()
        assert dynamic.capacity == 0
        dynamic.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")
        assert dynamic.capacity == 1


class TestHeartbeat:
    def test_renew_tracks_existing_lease(self) -> None:
        pool = EnginePool(lease_ttl_seconds=_TTL)
        pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")

        assert pool.renew("inst-1") is True
        assert pool.renew("missing") is False
        assert pool.stale_leases() == []
        assert pool.is_stale("inst-1") is False
        assert pool.is_stale("missing") is False
        assert pool.lease_ttl_seconds == _TTL

    def test_stale_detection(self) -> None:
        pool = EnginePool(lease_ttl_seconds=_TTL)
        lease = pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")
        lease.renew(now=1000.0)

        assert lease.is_stale(_TTL, now=1000.0 + _TTL - 1) is False
        assert lease.is_stale(_TTL, now=1000.0 + _TTL + 1) is True
        assert lease.is_stale(0.0, now=10_000.0) is False  # TTL<=0 永不判僵

        assert [item.instance_id for item in pool.stale_leases()] == ["inst-1"]
        assert pool.is_stale("inst-1") is True


class TestMapAffinity:
    def test_note_loaded_map_overrides_requested_map(self) -> None:
        pool = EnginePool()
        pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")
        pool.note_loaded_map("127.0.0.1:2000", "/Game/Maps/Town03")
        pool.note_loaded_map("127.0.0.1:2000", "")  # 空地图不覆盖

        assert pool.loaded_map_of("127.0.0.1:2000") == "/Game/Maps/Town03"
        assert pool.loaded_map_of("unknown:1") == ""

    def test_pick_prefers_loaded_map(self) -> None:
        pool = EnginePool()
        first, second = _endpoint(2000), _endpoint(3000)
        pool.note_loaded_map(first.key, "/Game/Maps/Town01")
        pool.note_loaded_map(second.key, "/Game/Maps/Town03")

        assert pool.pick_free_endpoint([first, second], "/Game/Maps/Town03") is second

    def test_pick_prefers_never_loaded_endpoint(self) -> None:
        pool = EnginePool()
        first, second = _endpoint(2000), _endpoint(3000)
        pool.note_loaded_map(first.key, "/Game/Maps/Town01")

        assert pool.pick_free_endpoint([first, second], "/Game/Maps/Town05") is second

    def test_pick_falls_back_to_first_free(self) -> None:
        pool = EnginePool()
        first, second = _endpoint(2000), _endpoint(3000)
        pool.note_loaded_map(first.key, "/Game/Maps/Town01")
        pool.note_loaded_map(second.key, "/Game/Maps/Town03")

        assert pool.pick_free_endpoint([first, second], "/Game/Maps/Town05") is first

    def test_pick_skips_occupied_and_returns_none(self) -> None:
        pool = EnginePool()
        first, second = _endpoint(2000), _endpoint(3000)
        pool.acquire("inst-1", first, "/Game/Maps/Town01")
        pool.acquire("inst-2", second, "/Game/Maps/Town03")

        assert pool.pick_free_endpoint([first, second], "") is None


class TestSnapshot:
    def test_snapshot_reports_engines_and_leases(self) -> None:
        pool = EnginePool([_endpoint()], lease_ttl_seconds=_TTL)
        lease = pool.acquire("inst-1", _endpoint(), "/Game/Maps/Town01")
        lease.renew(now=1000.0)

        payload = pool.snapshot()

        assert payload["capacity"] == 1
        assert payload["active_leases"] == 1
        assert payload["lease_ttl_seconds"] == _TTL
        assert payload["map_affinity_enabled"] is True
        assert payload["stale_leases"] == 1
        engine = payload["engines"][0]
        assert engine["key"] == "127.0.0.1:2000"
        assert engine["holder"] == "inst-1"
        assert engine["map_id"] == "/Game/Maps/Town01"
        assert engine["stale"] is True
        assert payload["leases"][0]["instance_id"] == "inst-1"

    def test_snapshot_without_leases_marks_engine_free(self) -> None:
        pool = EnginePool([_endpoint()])

        payload = pool.snapshot()

        engine = payload["engines"][0]
        assert engine["holder"] is None
        assert engine["map_id"] == ""
        assert engine["acquired_at"] is None
        assert engine["last_renew_at"] is None
        assert engine["stale"] is False
        assert payload["active_leases"] == 0


class TestLeaseDataclasses:
    def test_endpoint_serialization(self) -> None:
        payload = _endpoint().to_dict()

        assert payload == {
            "key": "127.0.0.1:2000",
            "host": "127.0.0.1",
            "rpc_port": 2000,
            "stream_port": 2001,
            "tm_port": 8000,
        }

    def test_lease_renew_defaults_to_now(self) -> None:
        lease = EngineLease(instance_id="inst-1", endpoint=_endpoint(), map_id="m")
        before = lease.last_renew_at
        lease.renew()

        assert lease.last_renew_at >= before
        assert lease.to_dict()["map_id"] == "m"

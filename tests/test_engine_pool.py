"""引擎库存与租约池测试（V2.4.0 执行面分离）。

覆盖端点字符串解析、独占租约语义、释放复用与快照内容；
不依赖真实 CARLA server。
"""

from __future__ import annotations

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

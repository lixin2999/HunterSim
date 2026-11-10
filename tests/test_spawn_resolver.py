"""出生点替换与参与者相对偏移测试（HunterCore V1.25.0 契约）。"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from hunter_sim.scene_runner.scene_config import SpawnPoint
from hunter_sim.scene_runner.spawn_resolver import (
    MAX_SPAWN_ATTEMPTS,
    candidate_spawns,
    is_carla_town,
    is_placeholder_origin,
    relative_offset_from_ego,
    resolve_actor_spawn,
    resolve_ego_spawn_candidates,
    scene_key_of,
    stable_candidate_index,
    transform_to_spawn_point,
)
from tests.mocks.carla_mocks import MockLocation, MockRotation, MockTransform


class _Map:
    """最小地图桩：出生点 + 路面判断。"""

    def __init__(self, on_road: bool | None = True) -> None:
        self._on_road = on_road
        self._spawn_points = [
            MockTransform(MockLocation(float(index * 20.0), 0.0, 0.0)) for index in range(20)
        ]

    def get_spawn_points(self) -> list[MockTransform]:
        return list(self._spawn_points)

    def get_waypoint(self, location: Any, project_to_road: bool = True) -> Any:
        if self._on_road is None:
            raise RuntimeError("no waypoint")
        return object() if self._on_road else None


class _World:
    def __init__(self, on_road: bool | None = True) -> None:
        self._map = _Map(on_road)

    def get_map(self) -> _Map:
        return self._map


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn_point_on_road 需要 carla.Location；测试环境无 SDK 时注入桩。"""
    module = types.ModuleType("carla")
    module.Location = MockLocation  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", module)


class TestHelpers:
    def test_placeholder_origin(self) -> None:
        assert is_placeholder_origin(SpawnPoint(x=0.0, y=0.0)) is True
        assert is_placeholder_origin(SpawnPoint(x=0.05, y=-0.05, yaw_deg=0.05)) is True
        assert is_placeholder_origin(SpawnPoint(x=1.0, y=0.0)) is False

    def test_map_type_detection(self) -> None:
        assert is_carla_town("carla_town") is True
        assert is_carla_town(" CARLA_TOWN ") is True
        assert is_carla_town("hd_map") is False
        assert is_carla_town("") is False

    def test_scene_key_is_stable(self) -> None:
        assert scene_key_of("scene-1", "v1") == "scene-1::v1"
        assert scene_key_of("scene-1") == "scene-1"
        assert scene_key_of("scene-1", "v1") == scene_key_of("scene-1", "v1")

    def test_transform_to_spawn_point(self) -> None:
        transform = MockTransform(MockLocation(3.0, -4.0, 0.5), MockRotation(yaw=90.0))
        spawn = transform_to_spawn_point(transform)
        assert (spawn.x, spawn.y, spawn.z) == (3.0, -4.0, 0.5)
        assert spawn.yaw_deg == pytest.approx(90.0)

    def test_candidate_spawns_drops_world_origin(self) -> None:
        world = _World()
        world._map._spawn_points.insert(
            0, MockTransform(MockLocation(0.0, 0.0, 0.0))
        )
        points = candidate_spawns(world)
        # 原始 20 点含 1 个世界原点，再插入 1 个后仅剩 19 个有效点
        assert len(points) == 19
        assert all(point.location.x != 0.0 for point in points)


class TestEgoSpawn:
    def test_non_carla_town_never_replaces(self) -> None:
        world = _World()
        spawn = SpawnPoint(x=0.0, y=0.0)
        candidates = resolve_ego_spawn_candidates(
            world, spawn, map_type="hd_map", scene_key="scene-1"
        )
        assert candidates == [spawn]

    def test_carla_town_origin_is_replaced(self) -> None:
        world = _World()
        spawn = SpawnPoint(x=0.0, y=0.0)
        candidates = resolve_ego_spawn_candidates(
            world, spawn, map_type="carla_town", scene_key="scene-1"
        )
        assert candidates
        assert is_placeholder_origin(candidates[0]) is False
        assert len(candidates) <= MAX_SPAWN_ATTEMPTS

    def test_carla_town_off_road_is_replaced(self) -> None:
        world = _World(on_road=False)
        spawn = SpawnPoint(x=500.0, y=500.0, yaw_deg=30.0)
        candidates = resolve_ego_spawn_candidates(
            world, spawn, map_type="carla_town", scene_key="scene-offroad"
        )
        assert is_placeholder_origin(candidates[0]) is False
        assert candidates[0].x != pytest.approx(500.0)

    def test_carla_town_valid_point_is_kept_first(self) -> None:
        world = _World(on_road=True)
        spawn = SpawnPoint(x=15.0, y=8.0, yaw_deg=45.0)
        candidates = resolve_ego_spawn_candidates(
            world, spawn, map_type="carla_town", scene_key="scene-valid"
        )
        assert candidates[0] == spawn
        # 仍保留地图候选点用于 spawn 失败重试
        assert len(candidates) > 1

    def test_replacement_is_reproducible(self) -> None:
        world = _World()
        spawn = SpawnPoint(x=0.0, y=0.0)
        first = resolve_ego_spawn_candidates(
            world, spawn, map_type="carla_town", scene_key="scene-42"
        )
        second = resolve_ego_spawn_candidates(
            world, spawn, map_type="carla_town", scene_key="scene-42"
        )
        assert first == second

    def test_stable_index_wraps_and_shifts(self) -> None:
        count = 20
        base = stable_candidate_index("scene-42", count)
        assert 0 <= base < count
        assert stable_candidate_index("scene-42", count) == base
        assert stable_candidate_index("scene-42", count, variant_index=1) == (base + 1) % count
        assert stable_candidate_index("scene-42", 0) == 0


class TestActorSpawn:
    def test_relative_semantics_adds_ego_base_when_yaw_zero(self) -> None:
        ego = SpawnPoint(x=100.0, y=20.0, z=0.5, yaw_deg=0.0)
        offset = SpawnPoint(x=5.0, y=-3.0, z=0.0, yaw_deg=10.0)
        result = resolve_actor_spawn(ego, offset, "relative_to_ego")
        assert result.x == pytest.approx(105.0)
        assert result.y == pytest.approx(17.0)
        assert result.z == pytest.approx(0.5)
        assert result.yaw_deg == pytest.approx(10.0)

    def test_relative_offset_is_rotated_by_ego_yaw(self) -> None:
        """平台偏移是车体系（x 前向/y 左向），自车 yaw=90 时必须旋转叠加。"""
        ego = SpawnPoint(x=392.47, y=212.90, z=0.30, yaw_deg=90.0)
        offset = SpawnPoint(x=18.47, y=0.0, z=0.0, yaw_deg=0.0)
        result = resolve_actor_spawn(ego, offset, "relative_to_ego")
        # yaw=90：车体前向 = 世界 +y，因此 x 偏移落到世界 y 上
        assert result.x == pytest.approx(392.47, abs=1e-6)
        assert result.y == pytest.approx(212.90 + 18.47, abs=1e-6)
        assert result.yaw_deg == pytest.approx(90.0)

    def test_relative_left_offset_goes_to_world_minus_x(self) -> None:
        """y 为正表示自车左侧；yaw=90 时左侧 = 世界 -x。"""
        ego = SpawnPoint(x=0.0, y=0.0, yaw_deg=90.0)
        result = resolve_actor_spawn(
            ego, SpawnPoint(x=0.0, y=4.0), "relative_to_ego"
        )
        assert result.x == pytest.approx(-4.0)
        assert result.y == pytest.approx(0.0)

    def test_relative_yaw_normalized(self) -> None:
        ego = SpawnPoint(x=0.0, y=0.0, yaw_deg=170.0)
        offset = SpawnPoint(x=1.0, y=0.0, yaw_deg=30.0)
        result = resolve_actor_spawn(ego, offset, "relative_to_ego")
        assert result.yaw_deg == pytest.approx(-160.0)

    def test_absolute_semantics_returns_offset(self) -> None:
        ego = SpawnPoint(x=100.0, y=20.0, yaw_deg=90.0)
        offset = SpawnPoint(x=5.0, y=-3.0, yaw_deg=10.0)
        assert resolve_actor_spawn(ego, offset, "absolute") == offset
        assert resolve_actor_spawn(ego, offset, "") == offset

    def test_relative_offset_round_trip(self) -> None:
        """``relative_offset_from_ego`` 是叠加的逆运算（日志/面板方位用）。"""
        ego = SpawnPoint(x=392.47, y=212.90, yaw_deg=90.0)
        offset = SpawnPoint(x=18.47, y=-3.0)
        world = resolve_actor_spawn(ego, offset, "relative_to_ego")
        ahead, left = relative_offset_from_ego(ego, world)
        assert ahead == pytest.approx(18.47, abs=1e-6)
        assert left == pytest.approx(-3.0, abs=1e-6)

    def test_relative_offset_reports_behind(self) -> None:
        ego = SpawnPoint(x=10.0, y=10.0, yaw_deg=0.0)
        ahead, left = relative_offset_from_ego(
            ego, SpawnPoint(x=10.0 - 5.0, y=10.0 + 2.0)
        )
        assert ahead == pytest.approx(-5.0)
        assert left == pytest.approx(2.0)

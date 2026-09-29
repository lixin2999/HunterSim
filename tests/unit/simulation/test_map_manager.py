"""simulation.map_manager / simulation.maps 单元测试（mock 连接，无需真实 CARLA）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter_sim.core.exceptions import CarlaSimulationError, ConfigurationError
from hunter_sim.simulation.map_manager import MapManagerImpl
from hunter_sim.simulation.maps import (
    BUILTIN_MAP_REGISTRY,
    BuiltInMap,
    MapCategory,
    MapSource,
    OpenDriveOptions,
)
from hunter_sim.simulation.protocols import MapManager


def _connection() -> MagicMock:
    """构造满足 CarlaConnectionManager 契约的 mock。"""
    conn = MagicMock(name="connection")
    conn.load_world = AsyncMock(return_value=MagicMock(name="world"))
    conn.get_client.return_value = MagicMock(name="client")
    conn.get_world.return_value = MagicMock(name="reloaded_world")
    return conn


def _manager() -> MapManagerImpl:
    return MapManagerImpl(_connection())


def test_satisfies_protocol() -> None:
    assert isinstance(_manager(), MapManager)


def test_coordinate_frame_is_left_handed_metric() -> None:
    frame = _manager().coordinate_frame
    assert frame.handedness == "left"
    assert frame.x_axis == "east"
    assert frame.y_axis == "north"
    assert frame.z_axis == "up"
    assert frame.unit == "meter"


def test_registry_covers_all_builtin_maps() -> None:
    assert len(BUILTIN_MAP_REGISTRY) == len(list(BuiltInMap)) == 8
    infos = _manager().list_builtin_maps()
    assert len(infos) == 8
    assert all(info.is_builtin for info in infos)


def test_registry_metadata_matches_spec() -> None:
    mgr = _manager()
    assert mgr.get_map_info("Town03").applicable_use == "综合测试"
    assert mgr.get_map_info("Town04").category is MapCategory.HIGHWAY
    assert mgr.get_map_info("Town07").description == "乡村道路"
    assert mgr.get_map_info("Town10").applicable_use == "城市密集场景"


def test_list_maps_by_category() -> None:
    mgr = _manager()
    highways = {info.name for info in mgr.list_maps_by_category(MapCategory.HIGHWAY)}
    assert highways == {"Town04", "Town06"}
    assert mgr.list_maps_by_category(MapCategory.RURAL) == [mgr.get_map_info("Town07")]


def test_is_known_map() -> None:
    mgr = _manager()
    assert mgr.is_known_map("Town01") is True
    assert mgr.is_known_map("Town99") is False


def test_get_map_info_unknown_raises() -> None:
    with pytest.raises(ConfigurationError):
        _manager().get_map_info("NotAMap")


async def test_load_map_success_updates_current() -> None:
    conn = _connection()
    mgr = MapManagerImpl(conn)
    assert mgr.current_map is None
    world = await mgr.load_map("Town05")
    conn.load_world.assert_awaited_once_with("Town05")
    assert world is conn.load_world.return_value
    assert mgr.current_map is not None
    assert mgr.current_map.name == "Town05"
    assert mgr.current_map.source is MapSource.BUILTIN


async def test_load_map_invalid_raises() -> None:
    mgr = _manager()
    with pytest.raises(ConfigurationError):
        await mgr.load_map("TownXX")


def test_opendrive_options_validation() -> None:
    with pytest.raises(ValueError):
        OpenDriveOptions(vertex_distance=0)
    with pytest.raises(ValueError):
        OpenDriveOptions(tolerance=-0.1)
    with pytest.raises(ValueError):
        OpenDriveOptions(wall_height=-1)


async def test_load_opendrive_from_file(tmp_path: Path) -> None:
    xodr = tmp_path / "custom_map.xodr"
    xodr.write_text("<OpenDRIVE><road/></OpenDRIVE>", encoding="utf-8")
    conn = _connection()
    mgr = MapManagerImpl(conn)
    world = await mgr.load_opendrive(xodr)

    client = conn.get_client.return_value
    client.generate_opendrive_world.assert_called_once()
    # 生成后应刷新世界缓存
    conn.get_world.assert_called_once_with(reload=True)
    assert world is conn.get_world.return_value
    assert mgr.current_map is not None
    assert mgr.current_map.source is MapSource.OPENDRIVE
    assert mgr.current_map.opendrive_path == xodr


async def test_load_opendrive_with_options(tmp_path: Path) -> None:
    xodr = tmp_path / "m.xodr"
    xodr.write_text("<OpenDRIVE/>", encoding="utf-8")
    conn = _connection()
    mgr = MapManagerImpl(conn)
    await mgr.load_opendrive(xodr, options=OpenDriveOptions(vertex_distance=1.5))
    args = conn.get_client.return_value.generate_opendrive_world.call_args[0]
    assert args[0] == "<OpenDRIVE/>"


async def test_load_opendrive_missing_file_raises(tmp_path: Path) -> None:
    mgr = _manager()
    with pytest.raises(ConfigurationError):
        await mgr.load_opendrive(tmp_path / "nope.xodr")


async def test_load_opendrive_bad_suffix_raises(tmp_path: Path) -> None:
    bad = tmp_path / "map.txt"
    bad.write_text("not opendrive", encoding="utf-8")
    mgr = _manager()
    with pytest.raises(ConfigurationError):
        await mgr.load_opendrive(bad)


async def test_load_opendrive_xml_string() -> None:
    conn = _connection()
    mgr = MapManagerImpl(conn)
    await mgr.load_opendrive_xml("<OpenDRIVE><road id='1'/></OpenDRIVE>", name="runtime_map")
    conn.get_client.return_value.generate_opendrive_world.assert_called_once()
    assert mgr.current_map is not None
    assert mgr.current_map.name == "runtime_map"
    assert mgr.current_map.source is MapSource.OPENDRIVE


async def test_load_opendrive_generation_failure_maps_error() -> None:
    conn = _connection()
    conn.get_client.return_value.generate_opendrive_world.side_effect = RuntimeError("boom")
    mgr = MapManagerImpl(conn)
    with pytest.raises(CarlaSimulationError):
        await mgr.load_opendrive_xml("<OpenDRIVE/>")

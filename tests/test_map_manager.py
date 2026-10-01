"""地图管理模块测试（PROMPT-ENG-001-B）。

使用 MockCarlaClient 隔离 CARLA 依赖，覆盖内置/自定义地图加载、
OpenDRIVE 导入、生成点查询与地面高度计算。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError, ConfigurationError
from hunter_sim.engine.map_manager import BUILTIN_MAPS, MapInfo, MapManager
from tests.mocks.carla_mocks import MockCarlaClient


class _WaypointMap:
    def get_waypoint_z(self, loc: Any) -> float:
        return 1.75


class _GroundWorld:
    def __init__(self) -> None:
        self._map = _WaypointMap()

    def get_map(self) -> _WaypointMap:
        return self._map


@pytest.fixture
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入最小 carla 模块，供 _make_carla_location 使用。"""
    mod = types.ModuleType("carla")

    class _Loc:
        def __init__(self, x: float = 0, y: float = 0, z: float = 0) -> None:
            self.x, self.y, self.z = x, y, z

    mod.Location = _Loc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", mod)


class TestMapRegistration:
    def test_builtin_maps_registered(self) -> None:
        mgr = MapManager(MockCarlaClient())
        ids = {m.map_id for m in mgr.list_available_maps()}
        assert BUILTIN_MAPS.issubset(ids)
        assert mgr.is_map_available("Town03") is True
        assert mgr.is_map_available("Mars") is False

    def test_map_info_display_name_autofill(self) -> None:
        info = MapInfo(map_id="Town01")
        assert info.display_name == "Town01"


class TestLoadMap:
    def test_load_builtin(self) -> None:
        client = MockCarlaClient()
        mgr = MapManager(client)
        world = mgr.load_map("Town05")
        assert mgr.current_map_id == "Town05"
        assert mgr.current_world is world
        assert world.get_map().name == "Town05"

    def test_load_unregistered_raises(self) -> None:
        mgr = MapManager(MockCarlaClient())
        with pytest.raises(ConfigurationError):
            mgr.load_map("Nowhere")

    def test_load_error_wrapped(self) -> None:
        client = MockCarlaClient()

        def _boom(_name: str) -> Any:
            raise RuntimeError("RPC timeout")

        client.load_world = _boom  # type: ignore[assignment]
        mgr = MapManager(client)
        with pytest.raises(CarlaSimulationError):
            mgr.load_map("Town01")

    def test_preload_success_and_failure(self) -> None:
        client = MockCarlaClient()
        # Town02 预加载成功，BadMap 触发 warning 分支
        mgr = MapManager(client, preload_maps=["Town02", "BadMap"])
        assert mgr.current_map_id == "Town02"


class TestOpenDriveImport:
    def test_import_missing_file(self, tmp_path: Path) -> None:
        mgr = MapManager(MockCarlaClient())
        with pytest.raises(ConfigurationError):
            mgr.import_opendrive_map("m1", tmp_path / "nope.xodr")

    def test_import_bad_extension(self, tmp_path: Path) -> None:
        p = tmp_path / "map.txt"
        p.write_text("x")
        mgr = MapManager(MockCarlaClient())
        with pytest.raises(ConfigurationError):
            mgr.import_opendrive_map("m1", p)

    def test_import_and_load_custom(self, tmp_path: Path) -> None:
        p = tmp_path / "city.xodr"
        p.write_text("<OpenDRIVE/>", encoding="utf-8")
        client = MockCarlaClient()
        mgr = MapManager(client, custom_map_dir=tmp_path)
        info = mgr.import_opendrive_map("city", p, recommended_quality="epic")
        assert info.is_builtin is False
        assert mgr.is_map_available("city")
        world = mgr.load_map("city")
        assert world.get_map().name == "custom"

    def test_auto_load_custom_reference(self, tmp_path: Path) -> None:
        """附录 B：未注册的 "custom/{name}" 引用自动从 custom_map_dir 导入并加载。"""
        p = tmp_path / "garage.xodr"
        p.write_text("<OpenDRIVE/>", encoding="utf-8")
        mgr = MapManager(MockCarlaClient(), custom_map_dir=tmp_path)
        assert not mgr.is_map_available("custom/garage")
        world = mgr.load_map("custom/garage")
        assert world.get_map().name == "custom"
        assert mgr.current_map_id == "custom/garage"
        assert mgr.is_map_available("custom/garage")

    def test_custom_reference_missing_file(self, tmp_path: Path) -> None:
        """custom/ 引用对应文件不存在时仍报 ConfigurationError。"""
        mgr = MapManager(MockCarlaClient(), custom_map_dir=tmp_path)
        with pytest.raises(ConfigurationError):
            mgr.load_map("custom/nonexistent")

    @pytest.mark.parametrize(
        "bad_id",
        ["custom/../secret", "custom/../../evil", "custom/a/b", "custom/evil\n", "custom/..."],
    )
    def test_custom_name_traversal_rejected(self, tmp_path: Path, bad_id: str) -> None:
        """审查项 E：custom/{name} 含路径分隔符/非法字符 → fullmatch 拒绝，防路径穿越。"""
        mgr = MapManager(MockCarlaClient(), custom_map_dir=tmp_path)
        with pytest.raises(ConfigurationError):
            mgr.load_map(bad_id)

    def test_read_xodr_none(self) -> None:
        with pytest.raises(ConfigurationError):
            MapManager._read_xodr_file(None)


class TestSpawnAndGround:
    def test_get_spawn_points(self) -> None:
        mgr = MapManager(MockCarlaClient())
        world = mgr.load_map("Town03")
        pts = mgr.get_spawn_points(world)
        assert len(pts) == 50

    def test_get_spawn_points_error(self) -> None:
        mgr = MapManager(MockCarlaClient())

        class _Bad:
            def get_map(self) -> Any:
                raise RuntimeError("no map")

        with pytest.raises(CarlaSimulationError):
            mgr.get_spawn_points(_Bad())

    def test_get_ground_z_success(self, fake_carla: None) -> None:
        mgr = MapManager(MockCarlaClient())
        assert mgr.get_ground_z(_GroundWorld(), 1.0, 2.0) == 1.75

    def test_get_ground_z_fallback_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 无 carla 模块 → _make_carla_location 抛错 → 返回 0.0
        monkeypatch.setitem(sys.modules, "carla", None)
        mgr = MapManager(MockCarlaClient())
        assert mgr.get_ground_z(_GroundWorld(), 1.0, 2.0) == 0.0

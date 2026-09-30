"""CARLA 服务端配置单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hunter_sim.common.models import QualityLevel
from hunter_sim.engine.carla_server_config import (
    CarlaServerConfig,
    check_carla_installed,
)


class TestProfiles:
    def test_quality_profile_lookup(self) -> None:
        cfg = CarlaServerConfig(quality=QualityLevel.EPIC)
        assert cfg.get_quality_profile()["gpu_memory_gb"] == 10.0

    def test_max_concurrent(self) -> None:
        assert CarlaServerConfig(quality=QualityLevel.LOW).max_concurrent_instances() == 8
        assert CarlaServerConfig(quality=QualityLevel.MEDIUM).max_concurrent_instances() == 4
        assert CarlaServerConfig(quality=QualityLevel.EPIC).max_concurrent_instances() == 2


class TestValidation:
    def test_gpu_id_bounds(self) -> None:
        with pytest.raises(ValidationError):
            CarlaServerConfig(gpu_id=99)

    def test_delta_bounds(self) -> None:
        with pytest.raises(ValidationError):
            CarlaServerConfig(fixed_delta_seconds=0.0)


class TestBuildArgs:
    def test_base_and_offscreen(self) -> None:
        args = CarlaServerConfig(rpc_port=2000, world_map="Town05", offscreen=True).build_carla_exe_args()
        assert "-carla-rpc-port=2000" in args
        assert any("DefaultMap=Town05" in a for a in args)
        assert "-RenderOffScreen" in args

    def test_no_rendering_and_gpu(self) -> None:
        args = CarlaServerConfig(no_rendering=True, gpu_id=1).build_carla_exe_args()
        assert "-nullrhi" in args
        assert "-graphicsadapter=1" in args

    def test_auto_gpu_no_adapter_flag(self) -> None:
        args = CarlaServerConfig(gpu_id=-1).build_carla_exe_args()
        assert not any(a.startswith("-graphicsadapter") for a in args)

    def test_epic_quality_flag(self) -> None:
        args = CarlaServerConfig(quality=QualityLevel.EPIC).build_carla_exe_args()
        assert "-epic" in args

    def test_low_medium_quality_flag(self) -> None:
        assert "-quality" in CarlaServerConfig(quality=QualityLevel.LOW).build_carla_exe_args()

    def test_extra_args_appended(self) -> None:
        args = CarlaServerConfig(extra_args=["-fov=90", "-x"]).build_carla_exe_args()
        assert args[-2:] == ["-fov=90", "-x"]


class TestCommandStrings:
    def test_cmd_string(self) -> None:
        cfg = CarlaServerConfig(carla_root=Path(r"C:\CARLA"))
        s = cfg.to_cmd_string()
        assert s.startswith('start "" ')
        assert "CarlaUE4.exe" in s

    def test_powershell_string(self) -> None:
        s = CarlaServerConfig().to_powershell_string()
        assert "Start-Process" in s
        assert "-ArgumentList" in s


class TestCheckInstalled:
    def test_missing_returns_false(self, tmp_path: Path) -> None:
        assert check_carla_installed(tmp_path) is False

    def test_present_returns_true(self, tmp_path: Path) -> None:
        (tmp_path / "CarlaUE4.exe").write_text("", encoding="utf-8")
        assert check_carla_installed(tmp_path) is True

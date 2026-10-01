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

    def test_streaming_port_included(self) -> None:
        # 设计文档 §3.1.1：启动参数包含数据流端口
        args = CarlaServerConfig(stream_port=2001).build_carla_exe_args()
        assert "-carla-streaming-port=2001" in args

    def test_nosound_fps_default(self) -> None:
        # 默认非 benchmark：含 -fps=50；benchmark 与同步定步互斥，默认关闭
        args = CarlaServerConfig().build_carla_exe_args()
        assert "-nosound" in args
        assert "-fps=50" in args
        assert "-benchmark" not in args

    def test_benchmark_excludes_fps(self) -> None:
        # 显式启用 benchmark 时不再附加 -fps（审查项 M：互斥语义）
        args = CarlaServerConfig(benchmark=True).build_carla_exe_args()
        assert "-benchmark" in args
        assert not any(a.startswith("-fps=") for a in args)

    def test_no_rendering_and_gpu(self) -> None:
        args = CarlaServerConfig(no_rendering=True, gpu_id=1).build_carla_exe_args()
        assert "-nullrhi" in args
        # CARLA 0.9.16 选用显卡的正确参数为 -graphicsadapter（审查项 M）
        assert "-graphicsadapter=1" in args
        assert not any(a.startswith("-gpu=") for a in args)

    def test_auto_gpu_no_adapter_flag(self) -> None:
        args = CarlaServerConfig(gpu_id=-1).build_carla_exe_args()
        assert not any(a.startswith("-graphicsadapter=") for a in args)

    def test_epic_quality_flag(self) -> None:
        args = CarlaServerConfig(quality=QualityLevel.EPIC).build_carla_exe_args()
        assert "-quality-level=Epic" in args

    def test_low_medium_quality_flag(self) -> None:
        assert "-quality-level=Low" in CarlaServerConfig(quality=QualityLevel.LOW).build_carla_exe_args()
        assert "-quality-level=Medium" in CarlaServerConfig(quality=QualityLevel.MEDIUM).build_carla_exe_args()

    def test_extra_args_appended(self) -> None:
        args = CarlaServerConfig(extra_args=["-fov=90", "-x"]).build_carla_exe_args()
        assert args[-2:] == ["-fov=90", "-x"]


class TestSimulationModeSettings:
    def test_sync_settings_match_doc(self) -> None:
        # 设计文档 §3.1.2：同步模式固定步长 20ms + 子步 0.01s × 4
        s = CarlaServerConfig().build_sync_settings()
        assert s["synchronous_mode"] is True
        assert s["fixed_delta_seconds"] == 0.02
        assert s["substepping"] is True
        assert s["max_substep_delta_time"] == 0.01
        assert s["max_substeps"] == 4

    def test_async_settings_match_doc(self) -> None:
        # 设计文档 §3.1.2：异步模式可变步长
        s = CarlaServerConfig().build_async_settings()
        assert s["synchronous_mode"] is False
        assert s["fixed_delta_seconds"] is None


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

"""core.config 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hunter_sim.core.config import SensorConfig, load_scenario_config
from hunter_sim.core.exceptions import ConfigurationError


def test_load_default_scenario(default_scenario_path: Path) -> None:
    cfg = load_scenario_config(default_scenario_path)
    assert cfg.scenario.map == "Town04"
    assert cfg.scenario.tick_rate == 20
    assert cfg.vehicle.blueprint == "vehicle.tesla.model3"
    assert len(cfg.sensors) == 4
    assert {s.id for s in cfg.sensors} == {"rgb_front", "lidar_top", "imu", "gnss"}


def test_output_formats(default_scenario_path: Path) -> None:
    cfg = load_scenario_config(default_scenario_path)
    assert cfg.output.formats.camera == "png"
    assert cfg.output.formats.lidar == "hdf5"


def test_full_suite_sensors_valid(sensors_suite_path: Path) -> None:
    raw = yaml.safe_load(sensors_suite_path.read_text(encoding="utf-8"))
    sensors = [SensorConfig.model_validate(item) for item in raw["sensors"]]
    assert len(sensors) >= 7
    camera = next(s for s in sensors if s.id == "rgb_front")
    assert camera.width == 1920 and camera.fov == 90


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigurationError):
        load_scenario_config("no_such_file.yaml")


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    p = tmp_path / "cfg.toml"
    p.write_text("[scenario]\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_scenario_config(p)


def test_duplicate_sensor_ids_raises(tmp_path: Path) -> None:
    data = {
        "scenario": {"name": "x", "map": "Town01", "duration_seconds": 10, "tick_rate": 20},
        "vehicle": {"blueprint": "vehicle.a"},
        "sensors": [
            {"id": "a", "type": "other.imu", "position": [0, 0, 0], "rotation": [0, 0, 0]},
            {"id": "a", "type": "other.gnss", "position": [0, 0, 0], "rotation": [0, 0, 0]},
        ],
    }
    p = tmp_path / "dup.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_scenario_config(p)


def test_unknown_field_forbidden(tmp_path: Path) -> None:
    data = {
        "scenario": {"name": "x", "map": "Town01", "duration_seconds": 10, "tick_rate": 20},
        "vehicle": {"blueprint": "vehicle.a"},
        "sensors": [
            {"id": "a", "type": "other.imu", "position": [0, 0, 0], "rotation": [0, 0, 0]},
        ],
        "unexpected_section": 1,
    }
    p = tmp_path / "extra.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_scenario_config(p)


def test_invalid_color_rejected(tmp_path: Path) -> None:
    data = {
        "scenario": {"name": "x", "map": "Town01", "duration_seconds": 10, "tick_rate": 20},
        "vehicle": {"blueprint": "vehicle.a", "color": "300,0,0"},
        "sensors": [
            {"id": "a", "type": "other.imu", "position": [0, 0, 0], "rotation": [0, 0, 0]},
        ],
    }
    p = tmp_path / "badcolor.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_scenario_config(p)

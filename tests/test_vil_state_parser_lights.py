"""vil_state_parser 灯光与舵角单位测试（V3.0.0）。

覆盖计划 §6.1：
- 顶层 ``lights`` 段解析（缺省键不写入）；
- ``chassis.lights`` 回退路径；
- 未知键被过滤；
- 字符串/数字写法归一化为 bool；
- ``steering_unit="deg"`` 换算为弧度（顶层与 chassis 内均支持）；
- 缺字段时默认值不破坏旧报文。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from hunter_sim.cloud.vil_state_parser import parse_vil_state


def _message(**overrides: Any) -> dict[str, Any]:
    """构造最小 vil_state 报文（扁平格式，方案 §5.3）。"""
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "sim_id": "sim-test-001",
        "source_timestamp": 1724035200.0,
        "pose": {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": 0.0},
        "twist": {"linear": [5.0, 0.0, 0.0], "angular": [0.0, 0.0, 0.0]},
        "chassis": {"velocity": 5.0, "steering_angle": 0.0},
    }
    payload.update(overrides)
    return payload


# ── lights 段 ─────────────────────────────────────────────────────────────────


class TestLightsSection:
    def test_top_level_lights_parsed(self) -> None:
        frame = parse_vil_state(
            _message(
                lights={
                    "left_turn": True,
                    "right_turn": False,
                    "low_beam": True,
                    "hazard": False,
                }
            )
        )
        assert frame.vehicle_state.lights == {
            "left_turn": True,
            "right_turn": False,
            "low_beam": True,
            "hazard": False,
        }

    def test_missing_lights_defaults_to_empty(self) -> None:
        """未上报灯光 → 空字典（下游据此跳过灯光同步）。"""
        frame = parse_vil_state(_message())
        assert frame.vehicle_state.lights == {}

    def test_partial_lights_only_reports_present_keys(self) -> None:
        frame = parse_vil_state(_message(lights={"left_turn": True}))
        assert frame.vehicle_state.lights == {"left_turn": True}
        assert "right_turn" not in frame.vehicle_state.lights

    def test_unknown_keys_filtered(self) -> None:
        frame = parse_vil_state(_message(lights={"left_turn": True, "fog": True}))
        assert frame.vehicle_state.lights == {"left_turn": True}

    def test_chassis_lights_fallback(self) -> None:
        """``chassis.lights`` 为兼容写法，顶层缺失时生效。"""
        frame = parse_vil_state(
            _message(chassis={"velocity": 5.0, "lights": {"right_turn": True}})
        )
        assert frame.vehicle_state.lights == {"right_turn": True}

    def test_top_level_wins_over_chassis(self) -> None:
        frame = parse_vil_state(
            _message(
                lights={"left_turn": True},
                chassis={"velocity": 5.0, "lights": {"right_turn": True}},
            )
        )
        assert frame.vehicle_state.lights == {"left_turn": True}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (True, True),
            (False, False),
            (1, True),
            (0, False),
            ("true", True),
            ("TRUE", True),
            ("on", True),
            ("off", False),
            ("false", False),
            ("", False),
        ],
    )
    def test_bool_normalisation(self, raw: Any, expected: bool) -> None:
        frame = parse_vil_state(_message(lights={"low_beam": raw}))
        assert frame.vehicle_state.lights["low_beam"] is expected

    def test_non_dict_lights_ignored(self) -> None:
        frame = parse_vil_state(_message(lights=[1, 2, 3]))
        assert frame.vehicle_state.lights == {}

    def test_empty_lights_dict_returns_empty(self) -> None:
        frame = parse_vil_state(_message(lights={}))
        assert frame.vehicle_state.lights == {}

    def test_empty_lights_dict_falls_back_to_chassis(self) -> None:
        """``lights: {}`` 视为未上报，回退 ``chassis.lights``（不得把灯光置空）。"""
        frame = parse_vil_state(
            _message(
                lights={},
                chassis={"velocity": 5.0, "lights": {"hazard": True}},
            )
        )
        assert frame.vehicle_state.lights == {"hazard": True}


# ── 舵角单位 ──────────────────────────────────────────────────────────────────


class TestSteeringUnit:
    def test_radian_is_default(self) -> None:
        frame = parse_vil_state(
            _message(chassis={"velocity": 5.0, "steering_angle": 0.5})
        )
        assert frame.vehicle_state.steering == pytest.approx(0.5)

    def test_degrees_converted_at_top_level(self) -> None:
        frame = parse_vil_state(
            _message(
                steering_unit="deg",
                chassis={"velocity": 5.0, "steering_angle": 30.0},
            )
        )
        assert frame.vehicle_state.steering == pytest.approx(math.radians(30.0))

    def test_degrees_converted_inside_chassis(self) -> None:
        frame = parse_vil_state(
            _message(
                chassis={
                    "velocity": 5.0,
                    "steering_angle": -45.0,
                    "steering_unit": "deg",
                }
            )
        )
        assert frame.vehicle_state.steering == pytest.approx(math.radians(-45.0))

    def test_degrees_variant_case_insensitive(self) -> None:
        frame = parse_vil_state(
            _message(
                steering_unit="DEG",
                chassis={"velocity": 5.0, "steering_angle": 90.0},
            )
        )
        assert frame.vehicle_state.steering == pytest.approx(math.pi / 2.0)

    def test_explicit_rad_unit_untouched(self) -> None:
        frame = parse_vil_state(
            _message(
                steering_unit="rad",
                chassis={"velocity": 5.0, "steering_angle": 0.25},
            )
        )
        assert frame.vehicle_state.steering == pytest.approx(0.25)

    def test_legacy_steer_key_still_supported(self) -> None:
        """旧 telemetry_clean 的 ``chassis.steer`` 保持兼容。"""
        frame = parse_vil_state(_message(chassis={"speed_m_s": 5.0, "steer": 0.2}))
        assert frame.vehicle_state.steering == pytest.approx(0.2)

    def test_missing_steering_defaults_zero(self) -> None:
        frame = parse_vil_state(_message(chassis={"velocity": 5.0}))
        assert frame.vehicle_state.steering == 0.0

    def test_brake_still_parsed_alongside_lights(self) -> None:
        frame = parse_vil_state(
            _message(
                lights={"left_turn": True},
                chassis={"velocity": 5.0, "brake": 0.6},
            )
        )
        assert frame.vehicle_state.brake == pytest.approx(0.6)
        assert frame.vehicle_state.lights == {"left_turn": True}

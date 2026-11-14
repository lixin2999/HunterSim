"""V3.9.0 真 SIL 控制接管专项单测（B2）。

覆盖：
- cmd_vel（Twist）→ 油门/刹车/转向换算：P + 巡航前馈速度环、稳态死区、零速保持；
- 自行车模型转向：符号约定（ROS 左转为正 → CARLA steer 取反）、限幅、低速分母兜底；
- 倒车负值：挡位滞回、换挡前停稳、倒车反馈量按挡位方向取符号；
- 看门狗：stale/控制源异常 → 立即零速刹停（不走低通）、恢复后继续、NaN/Inf 兜底；
- 控制源标记：script / edge_link 与诊断快照字段；
- edge_link_sink：复用已编码产物（不重复编码）、overview 不馈送、异常不阻断仿真；
- executor：extra_params.edge_link 覆盖解析、只读视图降级。

用桩件隔离 CARLA/Kafka，仅验证控制语义与数据出口。
"""

from __future__ import annotations

import json
import math
import types
from pathlib import Path
from typing import Any, Optional

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.common.models import HunterSimSettings
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor, _RunContext, _edge_link_override
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.scene_runner.builtin_scenario import (
    _EDGE_REVERSE_SWITCH_MS,
    _EDGE_STEER_MIN_SPEED_MS,
    _EGO_BRAKE_HOLD,
    _EGO_LANE_MAX_STEER_RAD,
    _EGO_LANE_WHEELBASE_M,
    _EGO_SPEED_FEEDFORWARD,
    BuiltinScenario,
    _safe_float,
)
from hunter_sim.scene_runner.event_detector import EventDetector
from hunter_sim.scene_runner.scene_config import SceneConfig, SpawnPoint
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager
from hunter_sim.sensor_sim.frame_hub import (
    STREAM_DEPTH,
    STREAM_IMU,
    STREAM_LIDAR,
    STREAM_OVERVIEW,
    STREAM_RGB,
    FrameHub,
)
from tests.mocks.carla_mocks import (
    MockLocation,
    MockRotation,
    MockTransform,
    MockVector3D,
    MockVehicle,
)

_BASE_CONFIG: dict[str, Any] = {
    "scene_id": "scene-v390-edge",
    "map_id": "Town03",
    "duration_seconds": 30.0,
    "ego_vehicle": {
        "vehicle_blueprint": "hunter_se",
        "spawn_point": {"x": 0.0, "y": 0.0},
        "autopilot": False,
    },
    "traffic_participants": [],
}


# ─── 桩件 ────────────────────────────────────────────────────────────────────


def _make_scenario(
    overrides: Optional[dict[str, Any]] = None,
    *,
    control_driver: Any = None,
    edge_link_sink: Any = None,
    frame_hub: Any = None,
) -> BuiltinScenario:
    config = SceneConfig(**{**_BASE_CONFIG, **(overrides or {})})
    return BuiltinScenario(
        config,
        SceneStateManager(config.scene_id),
        EventDetector(config.scene_events),
        transform_factory=lambda _world, sp: MockTransform(
            MockLocation(sp.x, sp.y, sp.z or 0.5)
        ),
        frame_hub=frame_hub,
        control_driver=control_driver,
        edge_link_sink=edge_link_sink,
    )


def _ego(speed: float = 0.0, yaw: float = 0.0) -> MockVehicle:
    """自车桩：速度沿 +X（yaw=0 时前向投影即 speed）。"""
    ego = MockVehicle("vehicle.hunter_se")
    ego._velocity = MockVector3D(speed, 0.0, 0.0)  # noqa: SLF001
    ego._transform = MockTransform(  # noqa: SLF001
        MockLocation(0.0, 0.0, 0.5), MockRotation(yaw=yaw)
    )
    return ego


def _attached(scenario: BuiltinScenario, ego: MockVehicle) -> BuiltinScenario:
    scenario._ego = ego  # noqa: SLF001
    scenario._ego_spawn = SpawnPoint(  # noqa: SLF001
        x=0.0, y=0.0, z=0.5, yaw_deg=0.0
    )
    return scenario


def _cmd(
    linear_x: float = 0.0,
    angular_z: float = 0.0,
    stale: bool = False,
    *,
    fatal: bool = False,
    reason: str = "",
) -> Any:
    return types.SimpleNamespace(
        linear_x=linear_x, angular_z=angular_z, stale=stale, fatal=fatal, reason=reason
    )


class _Sink:
    """edge_link_sink 桩：记录 (stream, sim_ts, wall_ts_ns, payload, meta)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, int, bytes, dict[str, Any]]] = []

    def __call__(
        self,
        stream: str,
        sim_ts: float,
        wall_ts_ns: int,
        payload: bytes,
        meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        self.calls.append((stream, sim_ts, wall_ts_ns, payload, dict(meta or {})))
        return True

    def only(self) -> tuple[str, float, int, bytes, dict[str, Any]]:
        assert len(self.calls) == 1, f"expected 1 feed, got {len(self.calls)}"
        return self.calls[0]


def _image_data(width: int = 4, height: int = 2, ts: float = 100.0) -> Any:
    return types.SimpleNamespace(
        data=bytes(width * height * 4), width=width, height=height, timestamp=ts
    )


def _lidar_data(points: int = 3, ts: float = 100.0) -> Any:
    return types.SimpleNamespace(data=bytes(points * 16), timestamp=ts)


def _imu_data(ts: float = 100.0) -> Any:
    return types.SimpleNamespace(
        accelerometer=(1.0, 2.0, 3.0), gyroscope=(0.1, 0.2, 0.3), timestamp=ts
    )


# ─── 数值兜底 ────────────────────────────────────────────────────────────────


class TestSafeFloat:
    def test_normal_value_passes_through(self) -> None:
        assert _safe_float(2.5) == pytest.approx(2.5)

    def test_non_finite_falls_back_to_zero(self) -> None:
        assert _safe_float(float("nan")) == 0.0
        assert _safe_float(float("inf")) == 0.0
        assert _safe_float(float("-inf")) == 0.0

    def test_invalid_input_falls_back_to_zero(self) -> None:
        assert _safe_float(None) == 0.0
        assert _safe_float("abc") == 0.0
        assert _safe_float("1.5") == pytest.approx(1.5)


# ─── Twist → 油门/刹车/转向 ──────────────────────────────────────────────────


class TestTwistConversion:
    def test_below_target_gives_throttle_above_feedforward(self) -> None:
        scenario = _make_scenario()
        throttle, brake, steer = scenario._edge_twist_to_control(3.0, 0.0, 1.0)  # noqa: SLF001
        assert throttle > _EGO_SPEED_FEEDFORWARD
        assert brake == pytest.approx(0.0)
        assert steer == pytest.approx(0.0)

    def test_steady_state_keeps_cruise_feedforward(self) -> None:
        scenario = _make_scenario()
        throttle, brake, _ = scenario._edge_twist_to_control(3.0, 0.0, 3.0)  # noqa: SLF001
        assert throttle == pytest.approx(_EGO_SPEED_FEEDFORWARD)
        assert brake == pytest.approx(0.0)

    def test_overspeed_brakes_without_throttle(self) -> None:
        scenario = _make_scenario()
        throttle, brake, _ = scenario._edge_twist_to_control(1.0, 0.0, 3.0)  # noqa: SLF001
        assert throttle == pytest.approx(0.0)
        assert brake > 0.5

    def test_zero_command_holds_brake_when_stopped(self) -> None:
        scenario = _make_scenario()
        throttle, brake, _ = scenario._edge_twist_to_control(0.0, 0.0, 0.0)  # noqa: SLF001
        assert throttle == pytest.approx(0.0)
        assert brake == pytest.approx(_EGO_BRAKE_HOLD)

    def test_throttle_and_brake_are_clamped_to_unit(self) -> None:
        scenario = _make_scenario()
        throttle, _, _ = scenario._edge_twist_to_control(50.0, 0.0, 0.0)  # noqa: SLF001
        assert throttle == pytest.approx(1.0)
        _, brake, _ = scenario._edge_twist_to_control(0.0, 0.0, 50.0)  # noqa: SLF001
        assert brake == pytest.approx(1.0)

    def test_ros_left_turn_maps_to_negative_carla_steer(self) -> None:
        """ROS ω>0 为左转；CARLA steer>0 朝 +Y（右），故必须取反号。"""
        scenario = _make_scenario()
        _, _, steer = scenario._edge_twist_to_control(2.0, 0.5, 2.0)  # noqa: SLF001
        assert steer < 0.0

    def test_ros_right_turn_maps_to_positive_carla_steer(self) -> None:
        scenario = _make_scenario()
        _, _, steer = scenario._edge_twist_to_control(2.0, -0.5, 2.0)  # noqa: SLF001
        assert steer > 0.0

    def test_zero_yaw_rate_gives_zero_steer(self) -> None:
        scenario = _make_scenario()
        _, _, steer = scenario._edge_twist_to_control(2.0, 0.0, 2.0)  # noqa: SLF001
        assert steer == pytest.approx(0.0)

    def test_steer_matches_bicycle_model(self) -> None:
        scenario = _make_scenario()
        speed, yaw_rate = 2.0, 0.3
        _, _, steer = scenario._edge_twist_to_control(speed, yaw_rate, speed)  # noqa: SLF001
        expected = -math.atan(_EGO_LANE_WHEELBASE_M * yaw_rate / speed) / _EGO_LANE_MAX_STEER_RAD
        assert steer == pytest.approx(expected)

    def test_large_yaw_rate_clamped_to_unit_steer(self) -> None:
        scenario = _make_scenario()
        _, _, steer = scenario._edge_twist_to_control(1.0, 50.0, 1.0)  # noqa: SLF001
        assert -1.0 <= steer <= 1.0
        assert abs(steer) == pytest.approx(1.0)

    def test_low_speed_denominator_floor_avoids_divergence(self) -> None:
        """静止时 ω/v 会发散，分母兜底到 _EDGE_STEER_MIN_SPEED_MS。"""
        scenario = _make_scenario()
        _, _, steer = scenario._edge_twist_to_control(0.0, 0.5, 0.0)  # noqa: SLF001
        raw = math.atan(_EGO_LANE_WHEELBASE_M * 0.5 / _EDGE_STEER_MIN_SPEED_MS)
        expected = -max(-_EGO_LANE_MAX_STEER_RAD, min(_EGO_LANE_MAX_STEER_RAD, raw))
        assert steer == pytest.approx(expected / _EGO_LANE_MAX_STEER_RAD)
        assert math.isfinite(steer)


# ─── 倒车挡 ──────────────────────────────────────────────────────────────────


class TestReverseGear:
    def test_negative_command_engages_reverse_when_stopped(self) -> None:
        scenario = _make_scenario()
        scenario._edge_twist_to_control(-1.0, 0.0, 0.0)  # noqa: SLF001
        assert scenario._edge_reverse is True  # noqa: SLF001

    def test_reverse_feedback_uses_direction_aligned_sign(self) -> None:
        """倒车 1m/s + 指令 -1m/s → 稳态前馈，不得被误判为超速而刹停。"""
        scenario = _make_scenario()
        scenario._edge_reverse = True  # noqa: SLF001
        throttle, brake, _ = scenario._edge_twist_to_control(-1.0, 0.0, -1.0)  # noqa: SLF001
        assert throttle == pytest.approx(_EGO_SPEED_FEEDFORWARD)
        assert brake == pytest.approx(0.0)

    def test_reverse_accelerates_when_slower_than_command(self) -> None:
        scenario = _make_scenario()
        scenario._edge_reverse = True  # noqa: SLF001
        throttle, brake, _ = scenario._edge_twist_to_control(-2.0, 0.0, -0.5)  # noqa: SLF001
        assert throttle > _EGO_SPEED_FEEDFORWARD
        assert brake == pytest.approx(0.0)

    def test_gear_shift_waits_until_stopped(self) -> None:
        """行进中换挡无效：先满刹停稳，挡位保持不变。"""
        scenario = _make_scenario()
        result = scenario._edge_twist_to_control(-1.0, 0.0, 3.0)  # noqa: SLF001
        assert result == (0.0, 1.0, 0.0)
        assert scenario._edge_reverse is False  # noqa: SLF001

    def test_reverse_hysteresis_holds_near_zero(self) -> None:
        scenario = _make_scenario()
        scenario._edge_reverse = True  # noqa: SLF001
        scenario._edge_twist_to_control(_EDGE_REVERSE_SWITCH_MS / 2, 0.0, 0.0)  # noqa: SLF001
        assert scenario._edge_reverse is True  # noqa: SLF001

    def test_forward_command_disengages_reverse_when_stopped(self) -> None:
        scenario = _make_scenario()
        scenario._edge_reverse = True  # noqa: SLF001
        scenario._edge_twist_to_control(1.0, 0.0, 0.0)  # noqa: SLF001
        assert scenario._edge_reverse is False  # noqa: SLF001

    def test_reverse_flag_is_delivered_to_vehicle_control(self) -> None:
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(control_driver=lambda _dt: _cmd(linear_x=-1.0)), ego
        )
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert ego._control.reverse is True  # noqa: SLF001


# ─── 看门狗 ──────────────────────────────────────────────────────────────────


class TestWatchdog:
    def test_stale_brakes_immediately_without_ema_lag(self) -> None:
        ego = _ego(speed=3.0)
        scenario = _attached(
            _make_scenario(control_driver=lambda _dt: _cmd(stale=True)), ego
        )
        # 预置低通状态为巡航油门：刹停必须立即生效，不得被 EMA 拖慢
        scenario._ego_ctrl_throttle = 0.8  # noqa: SLF001
        scenario._ego_ctrl_brake = 0.0  # noqa: SLF001
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert ego._control.throttle == pytest.approx(0.0)  # noqa: SLF001
        assert ego._control.brake == pytest.approx(_EGO_BRAKE_HOLD)  # noqa: SLF001
        assert scenario._edge_watchdog_stale is True  # noqa: SLF001
        assert scenario._edge_stale_frames == 1  # noqa: SLF001

    def test_stale_zeroes_steer(self) -> None:
        ego = _ego(speed=2.0)
        scenario = _attached(
            _make_scenario(control_driver=lambda _dt: _cmd(stale=True)), ego
        )
        scenario._ego_ctrl_steer = 0.6  # noqa: SLF001
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert ego._control.steer == pytest.approx(0.0)  # noqa: SLF001

    def test_recovery_resumes_command(self) -> None:
        commands = iter([_cmd(stale=True)] + [_cmd(linear_x=3.0)] * 30)
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(control_driver=lambda _dt: next(commands)), ego
        )
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert scenario._edge_watchdog_stale is True  # noqa: SLF001
        assert ego._control.brake == pytest.approx(_EGO_BRAKE_HOLD)  # noqa: SLF001
        # 恢复首帧：油门开始上升，制动按低通释放（不阶跃，与确定性控制同口径）
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert scenario._edge_watchdog_stale is False  # noqa: SLF001
        assert ego._control.throttle > 0.0  # noqa: SLF001
        assert ego._control.brake < _EGO_BRAKE_HOLD  # noqa: SLF001
        # 持续恢复后制动完全释放，油门回到加速区
        for _ in range(29):
            scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert ego._control.brake == pytest.approx(0.0, abs=1e-6)  # noqa: SLF001
        assert ego._control.throttle > _EGO_SPEED_FEEDFORWARD  # noqa: SLF001

    def test_driver_exception_triggers_watchdog_stop(self) -> None:
        """控制源抛异常等同断流：必须刹停，不得沿用上一帧指令滑行。"""

        def _boom(_dt: float) -> Any:
            raise RuntimeError("kafka consumer down")

        ego = _ego(speed=2.0)
        scenario = _attached(_make_scenario(control_driver=_boom), ego)
        scenario._ego_ctrl_throttle = 0.9  # noqa: SLF001
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert ego._control.throttle == pytest.approx(0.0)  # noqa: SLF001
        assert ego._control.brake == pytest.approx(_EGO_BRAKE_HOLD)  # noqa: SLF001
        assert scenario._edge_watchdog_stale is True  # noqa: SLF001

    def test_none_control_is_noop_before_ego_bound(self) -> None:
        ego = _ego()
        scenario = _attached(_make_scenario(control_driver=lambda _dt: None), ego)
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert ego._control is None  # noqa: SLF001
        assert scenario._edge_control_frames == 0  # noqa: SLF001

    def test_fatal_first_control_timeout_fails_scene(self) -> None:
        """V3.9.2：首帧控制超时 → 抛 CarlaSimulationError 结束场景。

        继续零速跑只会白录 mcap 再判 fail（实机一轮拉了满时长 + 1.87 GB）。
        必须走场景失败路径，让 executor 立即 _fail_instance。
        """
        reason = (
            "edge_control_unavailable: 会话已建立 30.1s 内未收到车端任何 "
            "/cmd_vel（首帧控制超时，EDGE_LINK_FIRST_CONTROL_TIMEOUT_S）"
        )
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(
                control_driver=lambda _dt: _cmd(stale=True, fatal=True, reason=reason)
            ),
            ego,
        )
        with pytest.raises(CarlaSimulationError) as excinfo:
            scenario._apply_edge_control(0.02)  # noqa: SLF001
        text = str(excinfo.value)
        assert "edge_control" in text
        assert "edge_control_unavailable" in text
        # 不得先落一帧假控制（评估引擎靠 0 帧判定断流）
        assert scenario._edge_control_frames == 0  # noqa: SLF001

    def test_fatal_without_reason_uses_default_text(self) -> None:
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(control_driver=lambda _dt: _cmd(fatal=True)), ego
        )
        with pytest.raises(CarlaSimulationError) as excinfo:
            scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert "/cmd_vel" in str(excinfo.value)

    def test_nan_command_sanitized_to_zero(self) -> None:
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(
                control_driver=lambda _dt: _cmd(
                    linear_x=float("nan"), angular_z=float("inf")
                )
            ),
            ego,
        )
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert scenario._edge_last_linear_x == 0.0  # noqa: SLF001
        assert scenario._edge_last_angular_z == 0.0  # noqa: SLF001
        assert ego._control.throttle == pytest.approx(0.0)  # noqa: SLF001
        assert ego._control.brake > 0.0  # noqa: SLF001
        assert math.isfinite(ego._control.steer)  # noqa: SLF001

    def test_stale_clears_physical_stuck_latch(self) -> None:
        """刹停期间清空卡滞判定，避免控制恢复后被旧 latch 冻住。"""
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(control_driver=lambda _dt: _cmd(stale=True)), ego
        )
        scenario._ego_phys_stuck_since_s = 12.0  # noqa: SLF001
        scenario._ego_phys_stuck_warned = True  # noqa: SLF001
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert scenario._ego_phys_stuck_since_s == 0.0  # noqa: SLF001
        assert scenario._ego_phys_stuck_warned is False  # noqa: SLF001

    def test_no_ego_is_noop(self) -> None:
        scenario = _make_scenario(control_driver=lambda _dt: _cmd(linear_x=3.0))
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        assert scenario._edge_control_frames == 0  # noqa: SLF001


# ─── 控制源标记与 tick 让位 ──────────────────────────────────────────────────


class TestControlSource:
    def test_default_source_is_script(self) -> None:
        assert _make_scenario().control_source == "script"

    def test_control_driver_marks_edge_link(self) -> None:
        scenario = _make_scenario(control_driver=lambda _dt: _cmd())
        assert scenario.control_source == "edge_link"

    def test_diagnostics_snapshot_fields(self) -> None:
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(
                control_driver=lambda _dt: _cmd(linear_x=1.5, angular_z=-0.2)
            ),
            ego,
        )
        scenario._apply_edge_control(0.02)  # noqa: SLF001
        diag = scenario.edge_control_diagnostics
        assert diag["source"] == "edge_link"
        assert diag["control_frames"] == 1
        assert diag["stale_frames"] == 0
        assert diag["linear_x"] == pytest.approx(1.5)
        assert diag["angular_z"] == pytest.approx(-0.2)
        assert diag["watchdog_stale"] is False
        assert diag["reverse"] is False
        assert set(diag) >= {
            "source", "watchdog_stale", "control_frames", "stale_frames",
            "linear_x", "angular_z", "reverse", "throttle", "brake", "steer",
        }

    def test_script_diagnostics_report_script_source(self) -> None:
        assert _make_scenario().edge_control_diagnostics["source"] == "script"

    def test_tick_bypasses_script_control(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """control_driver 非空时脚本确定性控制完全让位（评估裁决的才是被测系统）。"""
        ego = _ego(speed=0.0)
        scenario = _attached(
            _make_scenario(
                {"extra_params": {"disable_scene_event_triggers": True}},
                control_driver=lambda _dt: _cmd(linear_x=2.0),
            ),
            ego,
        )
        called: list[str] = []
        monkeypatch.setattr(
            scenario, "_apply_ego_manual_speed", lambda _dt: called.append("script")
        )
        monkeypatch.setattr(scenario, "_advance_scripted_walkers", lambda _dt: None)
        monkeypatch.setattr(scenario, "_update_traveled_distance", lambda _dt: None)
        monkeypatch.setattr(scenario, "_warn_ego_speed_stuck", lambda: None)
        monkeypatch.setattr(scenario, "_reapply_post_spawn_controls_once", lambda: None)

        scenario.tick(None, 0.0, 0.02)

        assert called == []
        assert scenario._edge_control_frames == 1  # noqa: SLF001
        assert ego._control is not None  # noqa: SLF001

    def test_tick_uses_script_control_without_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = _attached(
            _make_scenario({"extra_params": {"disable_scene_event_triggers": True}}),
            _ego(),
        )
        called: list[str] = []
        monkeypatch.setattr(
            scenario, "_apply_ego_manual_speed", lambda _dt: called.append("script")
        )
        monkeypatch.setattr(scenario, "_advance_scripted_walkers", lambda _dt: None)
        monkeypatch.setattr(scenario, "_update_traveled_distance", lambda _dt: None)
        monkeypatch.setattr(scenario, "_warn_ego_speed_stuck", lambda: None)
        monkeypatch.setattr(scenario, "_reapply_post_spawn_controls_once", lambda: None)

        scenario.tick(None, 0.0, 0.02)

        assert called == ["script"]


# ─── 传感器上行馈送 ──────────────────────────────────────────────────────────


class TestEdgeLinkSink:
    def test_rgb_feed_reuses_encoded_payload(self) -> None:
        hub = FrameHub()
        sink = _Sink()
        scenario = _make_scenario(frame_hub=hub, edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_RGB, _image_data())  # noqa: SLF001
        stream, sim_ts, wall_ns, payload, meta = sink.only()
        assert stream == STREAM_RGB
        frame = hub.latest(STREAM_RGB)
        assert frame is not None
        # 同一份编码产物：不得为上行重复编码
        assert payload == frame.payload
        assert meta["width"] == 4
        assert meta["height"] == 2
        assert meta["frame_id"] == "camera_front"
        assert sim_ts == pytest.approx(100.0)
        assert wall_ns > 0

    def test_depth_feed_is_32fc1_and_matches_hub(self) -> None:
        hub = FrameHub()
        sink = _Sink()
        scenario = _make_scenario(frame_hub=hub, edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_DEPTH, _image_data())  # noqa: SLF001
        stream, _, _, payload, meta = sink.only()
        assert stream == STREAM_DEPTH
        assert meta["encoding"] == "32FC1"
        assert meta["frame_id"] == "camera_depth"
        assert len(payload) == 4 * 2 * 4
        frame = hub.latest(STREAM_DEPTH)
        assert frame is not None
        assert payload == frame.payload

    def test_depth_computed_when_only_sink_attached(self) -> None:
        """无 FrameHub / 无录制器时也必须算米制深度并馈送（旧门条件会漏算）。"""
        sink = _Sink()
        scenario = _make_scenario(edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_DEPTH, _image_data())  # noqa: SLF001
        _, _, _, payload, meta = sink.only()
        assert meta["encoding"] == "32FC1"
        assert len(payload) == 4 * 2 * 4

    def test_lidar_feed_payload_and_point_count(self) -> None:
        hub = FrameHub()
        sink = _Sink()
        scenario = _make_scenario(frame_hub=hub, edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_LIDAR, _lidar_data(points=3))  # noqa: SLF001
        stream, _, _, payload, meta = sink.only()
        assert stream == STREAM_LIDAR
        assert meta["points"] == 3
        assert meta["point_bytes"] == 16
        assert meta["frame_id"] == "lidar_top"
        assert len(payload) == 48
        frame = hub.latest(STREAM_LIDAR)
        assert frame is not None
        assert payload == frame.payload

    def test_imu_feed_json_accel_gyro(self) -> None:
        sink = _Sink()
        scenario = _make_scenario(edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001
        stream, _, _, payload, meta = sink.only()
        assert stream == STREAM_IMU
        body = json.loads(payload.decode("utf-8"))
        assert body["accel"] == [1.0, 2.0, 3.0]
        assert body["gyro"] == [0.1, 0.2, 0.3]
        assert meta["encoding"] == "json"
        assert meta["frame_id"] == "imu_link"

    def test_overview_is_not_fed(self) -> None:
        """环视流仅可视化，不得占用上行带宽。"""
        sink = _Sink()
        scenario = _make_scenario(edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_OVERVIEW, _image_data())  # noqa: SLF001
        assert sink.calls == []

    def test_sink_exception_does_not_break_dispatch(self) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("kafka producer down")

        hub = FrameHub()
        scenario = _make_scenario(frame_hub=hub, edge_link_sink=_boom)
        scenario._dispatch_sensor_data(STREAM_RGB, _image_data())  # noqa: SLF001
        assert hub.latest(STREAM_RGB) is not None

    def test_empty_payload_not_fed(self) -> None:
        sink = _Sink()
        scenario = _make_scenario(edge_link_sink=sink)
        scenario._dispatch_sensor_data(STREAM_LIDAR, _lidar_data(points=0))  # noqa: SLF001
        assert sink.calls == []

    def test_without_sink_dispatch_is_unchanged(self) -> None:
        hub = FrameHub()
        scenario = _make_scenario(frame_hub=hub)
        scenario._dispatch_sensor_data(STREAM_RGB, _image_data())  # noqa: SLF001
        assert hub.latest(STREAM_RGB) is not None


# ─── executor：覆盖解析与只读视图 ────────────────────────────────────────────


class TestEdgeLinkOverride:
    def test_absent_returns_none(self) -> None:
        assert _edge_link_override(SceneConfig(**_BASE_CONFIG)) is None

    def test_bool_true_enables(self) -> None:
        config = SceneConfig(**{**_BASE_CONFIG, "extra_params": {"edge_link": True}})
        assert _edge_link_override(config) == {"enabled": True}

    def test_bool_false_disables(self) -> None:
        config = SceneConfig(**{**_BASE_CONFIG, "extra_params": {"edge_link": False}})
        assert _edge_link_override(config) == {"enabled": False}

    def test_dict_passes_through_per_field(self) -> None:
        raw = {"enabled": True, "watchdog_ms": 800, "rgb_fps": 5.0}
        config = SceneConfig(**{**_BASE_CONFIG, "extra_params": {"edge_link": raw}})
        assert _edge_link_override(config) == raw

    def test_string_truthy_and_falsy(self) -> None:
        for text in ("1", "true", "YES", "on"):
            config = SceneConfig(
                **{**_BASE_CONFIG, "extra_params": {"edge_link": text}}
            )
            assert _edge_link_override(config) == {"enabled": True}
        config = SceneConfig(**{**_BASE_CONFIG, "extra_params": {"edge_link": "off"}})
        assert _edge_link_override(config) == {"enabled": False}

    def test_blank_string_returns_none(self) -> None:
        config = SceneConfig(**{**_BASE_CONFIG, "extra_params": {"edge_link": "  "}})
        assert _edge_link_override(config) is None


class _StubPlane:
    """最小执行平面桩（只实现执行器构造/收尾实际调用的方法）。"""

    def __init__(self, runner: Any = None) -> None:
        self.runner = runner
        self.released: list[str] = []

    def ensure_runner(
        self,
        instance: Any,
        *,
        on_state_change: Any = None,
        on_event: Any = None,
        runner_hooks: Optional[dict[str, Any]] = None,
    ) -> Any:
        if self.runner is not None:
            self.runner.hooks = runner_hooks or {}
        return self.runner

    def get_runner(self, instance_id: str) -> Any:
        return self.runner

    def release(self, instance_id: str) -> None:
        self.released.append(instance_id)

    def release_all(self) -> None:
        return None

    def producer_for_sim_id(self, sim_id: str) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> Any:
    instance_store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    yield instance_store
    instance_store.close()


@pytest.fixture
def executor(
    test_settings: HunterSimSettings, tmp_path: Path, store: InstanceStore
) -> Any:
    instance_executor = SimulationExecutor(
        test_settings,
        AdapterSettings(
            db_path=str(tmp_path / "adapter.db"),
            artifacts_dir=str(tmp_path / "artifacts"),
        ),
        store,
        _StubPlane(),
    )
    yield instance_executor
    instance_executor.stop()


def _record(store: InstanceStore) -> InstanceRecord:
    record = InstanceRecord(
        instance_id="inst-edge-1",
        sim_id="sim-edge-42",
        scene_id="scene-v390-edge",
        scene_name="真 SIL 控制接管",
        mode="sil",
        scene_config={
            "map": {
                "map_id": "Town03",
                "spawn_point": {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
            },
            "duration": 30.0,
            "ego_vehicle": {"model": "hunter_se"},
            "actors": [],
        },
        total_time_s=30.0,
        actors_count=1,
    )
    store.insert(record)
    return record


class TestExecutorViews:
    def test_edge_link_status_without_context(self, executor: Any) -> None:
        assert executor.edge_link_status("missing") == {
            "enabled": False,
            "control_source": "script",
        }

    def test_control_diagnostics_without_context(self, executor: Any) -> None:
        assert executor.control_diagnostics("missing") == {"source": "script"}

    def test_edge_link_status_merges_stats_and_control(
        self, executor: Any, store: InstanceStore
    ) -> None:
        record = _record(store)
        link: Any = types.SimpleNamespace(
            stats={"enabled": True, "control_source": "edge_link", "control_received": 7}
        )
        runner: Any = types.SimpleNamespace(
            control_diagnostics=lambda: {"source": "edge_link", "watchdog_stale": False}
        )
        context = _RunContext(
            record=record, scene=SceneConfig(**_BASE_CONFIG), runner=runner
        )
        context.edge_link = link
        executor._contexts[record.instance_id] = context  # noqa: SLF001
        status = executor.edge_link_status(record.instance_id)
        assert status["enabled"] is True
        assert status["control_received"] == 7
        assert status["control"]["source"] == "edge_link"
        assert status["control"]["watchdog_stale"] is False

    def test_control_diagnostics_passthrough(
        self, executor: Any, store: InstanceStore
    ) -> None:
        record = _record(store)
        runner: Any = types.SimpleNamespace(
            control_diagnostics=lambda: {"source": "edge_link", "stale_frames": 3}
        )
        context = _RunContext(
            record=record, scene=SceneConfig(**_BASE_CONFIG), runner=runner
        )
        executor._contexts[record.instance_id] = context  # noqa: SLF001
        assert executor.control_diagnostics(record.instance_id) == {
            "source": "edge_link",
            "stale_frames": 3,
        }

    def test_control_diagnostics_degrades_on_runner_error(
        self, executor: Any, store: InstanceStore
    ) -> None:
        record = _record(store)

        def _boom() -> dict[str, Any]:
            raise RuntimeError("runner released")

        runner: Any = types.SimpleNamespace(control_diagnostics=_boom)
        context = _RunContext(
            record=record, scene=SceneConfig(**_BASE_CONFIG), runner=runner
        )
        executor._contexts[record.instance_id] = context  # noqa: SLF001
        assert executor.control_diagnostics(record.instance_id) == {"source": "script"}

    def test_edge_link_status_degrades_on_stats_error(
        self, executor: Any, store: InstanceStore
    ) -> None:
        record = _record(store)

        class _BrokenLink:
            @property
            def stats(self) -> dict[str, Any]:
                raise RuntimeError("link closed")

        context = _RunContext(
            record=record, scene=SceneConfig(**_BASE_CONFIG), runner=None
        )
        context.edge_link = _BrokenLink()  # type: ignore[assignment]
        executor._contexts[record.instance_id] = context  # noqa: SLF001
        assert executor.edge_link_status(record.instance_id) == {
            "enabled": False,
            "control_source": "script",
        }

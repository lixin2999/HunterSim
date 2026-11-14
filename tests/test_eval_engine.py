"""评估指标引擎单元测试（PROMPT-TEST-001 / PROMPT-ENG-007-A）。"""

from __future__ import annotations

from typing import Any

import pytest

from hunter_sim.common.models import EvalGrade
from hunter_sim.eval_service.evaluation_engine import (
    _EVAL_SPEED_RAMPUP_S,
    _EVAL_STOP_FRACTION,
    BatchEvaluator,
    ComfortMetrics,
    determine_grade,
    EfficiencyMetrics,
    EvaluationEngine,
    SafetyMetrics,
    SceneEvaluationResult,
)


class TestDetermineGrade:
    """等级判定函数测试。"""

    def test_grade_s(self) -> None:
        assert determine_grade(0.96, 0.0) == EvalGrade.S

    def test_grade_a(self) -> None:
        assert determine_grade(0.86, 0.015) == EvalGrade.A

    def test_grade_b(self) -> None:
        assert determine_grade(0.72, 0.04) == EvalGrade.B

    def test_grade_c(self) -> None:
        assert determine_grade(0.51, 0.09) == EvalGrade.C

    def test_grade_d_low_pass_rate(self) -> None:
        assert determine_grade(0.4, 0.2) == EvalGrade.D

    def test_grade_d_high_collision_rate(self) -> None:
        assert determine_grade(0.99, 0.11) == EvalGrade.D


class TestEvaluationEngine:
    """评估引擎测试。"""

    def test_safe_scene_passes(self) -> None:
        engine = EvaluationEngine()
        states = [
            {"vehicle_speed": 3.0, "timestamp": i * 0.02, "acceleration": (0.5, 0.0, 0.0), "steering": 0.1}
            for i in range(100)
        ]
        result = engine.evaluate_scene(
            scene_id="test_001",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[10.0, 8.0, 12.0],
            min_distances=[5.0, 4.5],
            scene_duration_s=2.0,
            completed=True,
        )
        assert result.passed is True
        assert result.safety.collision_count == 0
        assert result.safety.min_ttc_s == 8.0

    def test_collision_scene_fails(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_002",
            vehicle_states=[],
            events=[{"event_type": "collision"}],
            collision_events=[{"timestamp": 1.0}],
            ttc_samples=[0.5],
            min_distances=[0.5],
            scene_duration_s=10.0,
            completed=True,
        )
        assert result.passed is False
        assert result.safety.collision_count == 1

    def test_min_safe_distance_criterion_fails_on_near_miss(self) -> None:
        """V3.8.4：近距擦碰（0.05m < 平台 4.25m）即使无碰撞也判失败。"""
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="near_miss",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[0.05],
            speed_limit_ms=3.0,
            scene_duration_s=10.0,
            completed=True,
            min_safe_distance_m=4.25,
        )
        assert result.passed is False
        assert result.success_criteria["min_safe_distance_ok"] is False
        assert result.success_criteria["min_safe_distance_required"] == 4.25

    def test_min_safe_distance_criterion_passes_when_clear(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="clear",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[6.0],
            speed_limit_ms=3.0,
            scene_duration_s=10.0,
            completed=True,
            min_safe_distance_m=4.25,
        )
        assert result.passed is True
        assert result.success_criteria["min_safe_distance_ok"] is True

    def test_speed_deviation_criterion_fails_when_over(self) -> None:
        """V3.8.4：实测速度超期望初速+偏差上限判失败。"""
        engine = EvaluationEngine()
        states = [
            {"vehicle_speed": 5.0, "timestamp": i * 0.02, "acceleration": (0.0, 0.0, 0.0), "steering": 0.0}
            for i in range(50)
        ]
        result = engine.evaluate_scene(
            scene_id="over_speed",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            speed_limit_ms=3.0,
            scene_duration_s=1.0,
            completed=True,
            max_speed_deviation_ms=1.15,
        )
        assert result.passed is False
        assert result.success_criteria["max_speed_deviation_ok"] is False

    def test_speed_deviation_criterion_passes_within_limit(self) -> None:
        engine = EvaluationEngine()
        states = [
            {"vehicle_speed": 3.5, "timestamp": i * 0.02, "acceleration": (0.0, 0.0, 0.0), "steering": 0.0}
            for i in range(50)
        ]
        result = engine.evaluate_scene(
            scene_id="within_limit",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            speed_limit_ms=3.0,
            scene_duration_s=1.0,
            completed=True,
            max_speed_deviation_ms=1.15,
        )
        assert result.passed is True
        assert result.success_criteria["max_speed_deviation_ok"] is True

    def test_emergency_brake_counted(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_003",
            vehicle_states=[],
            events=[{"event_type": "emergency_brake"}] * 3,
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            scene_duration_s=5.0,
            completed=True,
        )
        assert result.safety.emergency_brake_count == 3

    def test_incomplete_scene_fails(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_004",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            scene_duration_s=10.0,
            completed=False,
        )
        assert result.passed is False

    def test_takeover_scene_fails(self) -> None:
        """人工接管场景判定失败（文档 §9.2.4 接管率统计）。"""
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_005",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            scene_duration_s=10.0,
            completed=True,
            takeover=True,
        )
        assert result.passed is False
        assert result.takeover is True

    def test_ttc_below_target_fails(self) -> None:
        """最小 TTC < 3.0s 未达安全目标（文档 §9.2.1）。"""
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_006",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[2.5],
            min_distances=[5.0],
            scene_duration_s=10.0,
            completed=True,
        )
        assert result.passed is False
        assert result.safety.meets_targets() is False

    def test_success_criteria_and_max_speed(self) -> None:
        """成功准则与最大速度字段（文档 §9.4）。"""
        engine = EvaluationEngine()
        states = [
            {"vehicle_speed": 3.0 + (i % 10) * 0.1, "timestamp": i * 0.02}
            for i in range(100)
        ]
        result = engine.evaluate_scene(
            scene_id="test_007",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[10.0],
            min_distances=[5.1],
            speed_limit_ms=10.0,
            scene_duration_s=2.0,
            completed=True,
        )
        assert result.efficiency.max_speed_ms == pytest.approx(3.9)
        assert result.success_criteria["no_collision"] is True
        assert result.success_criteria["min_safe_distance"] == 5.1
        assert result.success_criteria["max_speed_deviation"] == 0.0


class TestMetricsTargets:
    """指标目标值判定测试（文档 §9.2）。"""

    def test_safety_meets_targets_default_inf_ok(self) -> None:
        # 无采样数据（inf）时视为满足 TTC/车距要求
        assert SafetyMetrics().meets_targets() is True

    def test_safety_fails_on_emergency_brakes(self) -> None:
        # 紧急制动 < 2 次/场景
        assert SafetyMetrics(emergency_brake_count=2).meets_targets() is False

    def test_safety_fails_on_lane_invasion(self) -> None:
        assert SafetyMetrics(lane_invasion_count=1).meets_targets() is False

    def test_comfort_meets_targets(self) -> None:
        assert ComfortMetrics(avg_acceleration_ms2=0.8).meets_targets() is True
        # 平均加速度 < 1.0 m/s²
        assert ComfortMetrics(avg_acceleration_ms2=1.2).meets_targets() is False
        # 转向平滑度 < 0.3 rad/s
        assert ComfortMetrics(steering_smoothness_rad_s=0.4).meets_targets() is False


class TestBatchEvaluator:
    """批量评估测试。"""

    def _make_result(
        self,
        scene_id: str,
        passed: bool,
        collisions: int = 0,
        takeover: bool = False,
        completed: bool = True,
    ) -> SceneEvaluationResult:
        safety = SafetyMetrics(collision_count=collisions)
        return SceneEvaluationResult(
            scene_id=scene_id,
            grade=EvalGrade.S if passed else EvalGrade.D,
            safety=safety,
            efficiency=EfficiencyMetrics(completion_rate=1.0 if completed else 0.0),
            comfort=ComfortMetrics(),
            passed=passed,
            takeover=takeover,
        )

    def test_all_pass_grade_s(self) -> None:
        ev = BatchEvaluator()
        for i in range(20):
            ev.add_result(self._make_result(f"scene_{i}", passed=True))
        summary = ev.summary()
        assert summary["pass_rate"] == 1.0
        assert summary["overall_grade"] == "S"

    def test_some_fail_lower_grade(self) -> None:
        ev = BatchEvaluator()
        for i in range(10):
            ev.add_result(self._make_result(f"s{i}", passed=(i < 7)))
        summary = ev.summary()
        assert summary["pass_rate"] == 0.7
        assert summary["overall_grade"] in ("B", "C")

    def test_to_json(self) -> None:
        ev = BatchEvaluator()
        ev.add_result(self._make_result("s1", passed=True))
        js = ev.to_json()
        assert "overall_grade" in js
        assert '"total_scenes": 1' in js

    def test_task_metrics_summary(self) -> None:
        """任务指标汇总（文档 §9.2.4）。"""
        ev = BatchEvaluator()
        ev.add_result(self._make_result("s1", passed=True))
        ev.add_result(self._make_result("s2", passed=False, collisions=1))
        ev.add_result(self._make_result("s3", passed=False, takeover=True))
        ev.add_result(self._make_result("s4", passed=True, completed=False))
        summary = ev.summary()
        assert summary["pass_rate"] == 0.5
        assert summary["collision_rate"] == 0.25
        assert summary["takeover_rate"] == 0.25
        assert summary["target_achievement_rate"] == 0.75
        assert summary["failed_scenes"] == ["s2", "s3"]

    def test_metric_averages(self) -> None:
        """各指标均值汇总（文档 §9.5）。"""
        ev = BatchEvaluator()
        ev.add_result(SceneEvaluationResult(
            scene_id="s1", grade=EvalGrade.S, safety=SafetyMetrics(),
            efficiency=EfficiencyMetrics(avg_speed_ms=4.0),
            comfort=ComfortMetrics(avg_acceleration_ms2=1.0), passed=True,
        ))
        ev.add_result(SceneEvaluationResult(
            scene_id="s2", grade=EvalGrade.S, safety=SafetyMetrics(),
            efficiency=EfficiencyMetrics(avg_speed_ms=2.0),
            comfort=ComfortMetrics(avg_acceleration_ms2=3.0), passed=True,
        ))
        avg = ev.summary()["metric_averages"]
        assert avg["avg_speed_ms"] == pytest.approx(3.0)
        assert avg["avg_acceleration_ms2"] == pytest.approx(2.0)


class TestFrameDtDerivation:
    """审查项 R：帧步长从数据推导 + time_stamp 规范字段兼容。"""

    def test_waiting_time_uses_derived_dt(self) -> None:
        # 10Hz 帧（time_stamp 规范字段）：等待时长 = 帧数 × 推导步长 0.1s
        states = [
            {"vehicle_speed": 0.1, "time_stamp": i * 0.1} for i in range(10)
        ]
        eff = EvaluationEngine._compute_efficiency(states, duration_s=1.0, completed=True)
        assert eff.waiting_time_s == pytest.approx(10 * 0.1)

    def test_waiting_time_falls_back_without_timestamps(self) -> None:
        # 无时间戳时回退 50Hz 默认步长
        states = [{"vehicle_speed": 0.1} for _ in range(10)]
        eff = EvaluationEngine._compute_efficiency(states, duration_s=1.0, completed=True)
        assert eff.waiting_time_s == pytest.approx(10 * 0.02)

    def test_total_distance_reads_time_stamp_field(self) -> None:
        # 旧实现只读 timestamp，规范字段 time_stamp 会得 0（审查项 R 核心缺陷）
        states = [
            {"vehicle_speed": 2.0, "time_stamp": i * 0.05} for i in range(10)
        ]
        eff = EvaluationEngine._compute_efficiency(states, duration_s=0.5, completed=True)
        assert eff.total_distance_m == pytest.approx(2.0 * 0.05 * 9)

    def test_jerk_uses_derived_dt(self) -> None:
        # 10Hz：加速度每帧 +0.1 → jerk = 0.1/0.1 = 1.0 m/s³（硬编码 0.02 会得 5.0）
        states = [
            {
                "vehicle_speed": 1.0,
                "time_stamp": i * 0.1,
                "acceleration": (0.1 * i, 0.0, 0.0),
                "steering": 0.0,
            }
            for i in range(5)
        ]
        com = EvaluationEngine._compute_comfort(states)
        assert com.avg_jerk_ms3 == pytest.approx(1.0)


# ─── publish_results（V2.10.0 平台接入）────────────────────────────────────


class _FakeSimDataProducer:
    """记录所有发布调用的桩 SimDataProducer。"""

    def __init__(self) -> None:
        self.published_metrics: list[dict] = []
        self.published_artifacts: list[dict] = []
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def publish_metrics(self, message: dict) -> bool:
        self.published_metrics.append(message)
        return True

    def publish_artifact(self, **kwargs: object) -> bool:
        self.published_artifacts.append(kwargs)
        return True


class TestPublishResults:
    """V2.10.0：EvaluationEngine.publish_results 将结果发布到 sim_metrics + artifact。"""

    def _make_result(self) -> SceneEvaluationResult:
        safety = SafetyMetrics(
            collision_count=0, min_ttc_s=5.0, min_distance_m=3.0,
            emergency_brake_count=0, lane_invasion_count=0, red_light_count=0,
        )
        efficiency = EfficiencyMetrics(
            avg_speed_ms=8.0, max_speed_ms=12.0, total_distance_m=100.0,
            scene_duration_s=15.0, completion_rate=1.0,
        )
        comfort = ComfortMetrics(
            avg_acceleration_ms2=0.5, max_acceleration_ms2=1.0,
            avg_jerk_ms3=1.0, steering_smoothness_rad_s=0.1,
        )
        return SceneEvaluationResult(
            scene_id="scene-test",
            timestamp="2026-10-23T00:00:00Z",
            passed=True,
            grade=EvalGrade.A,
            safety=safety,
            efficiency=efficiency,
            comfort=comfort,
            success_criteria={"collision_free": True},
            events=[],
            takeover=False,
        )

    def test_publish_results_calls_metrics_and_artifact(self) -> None:
        fake = _FakeSimDataProducer()
        result = self._make_result()
        EvaluationEngine.publish_results(
            sim_data_producer=fake,
            sim_id="sim-test",
            vehicle_id="HUNTER-001",
            scene_id="scene-test",
            result=result,
        )
        assert len(fake.published_metrics) == 1
        assert len(fake.published_artifacts) == 1
        art = fake.published_artifacts[0]
        assert art["name"] == "eval.json"
        assert "sim-test" in art["object_key"]

    def test_publish_results_none_noop(self) -> None:
        """None producer 安全返回，不抛异常。"""
        result = self._make_result()
        EvaluationEngine.publish_results(
            sim_data_producer=None,
            sim_id="sim-test",
            vehicle_id="",
            scene_id="scene-test",
            result=result,
        )
        # 无异常即通过


class TestV388BilateralSpeedDeviation:
    """V3.8.8：速度偏差判据双边化，修复长时间停驶的假通过。

    原判据只算 max_speed − limit（超速侧），自车冻结 87% 时长时 max_speed ≤
    limit → deviation=0 → passed=True（实机 SCN-2085）。下发偏差上限且给出
    期望速度时叠加欠速侧：排除起步窗口后取均值与期望比较；无采样/未给
    期望则跳过（向后兼容、不误伤短场景与正常短暂停驶）。
    """

    @staticmethod
    def _states(segments: list[tuple[float, float, float]]) -> list[dict[str, object]]:
        """按 (起始s, 时长s, 速度m/s) 分段生成 10Hz 状态帧。"""
        states: list[dict[str, object]] = []
        for start_s, dur_s, speed in segments:
            n = int(dur_s * 10)
            for i in range(n):
                states.append(
                    {
                        "vehicle_speed": speed,
                        "timestamp": start_s + i * 0.1,
                        "acceleration": (0.0, 0.0, 0.0),
                        "steering": 0.0,
                    }
                )
        return states

    def _evaluate(self, states: list[dict[str, object]], **kwargs: Any) -> SceneEvaluationResult:
        return EvaluationEngine().evaluate_scene(
            scene_id="v388",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            speed_limit_ms=3.0,
            scene_duration_s=220.0,
            completed=True,
            **kwargs,
        )

    def test_underspeed_frozen_ego_fails_deviation(self) -> None:
        """复刻实机：前 12s 巡航 3.0，之后冻结 208s → passed=False、欠速明细留痕。"""
        states = self._states([(0.0, 12.0, 3.0), (12.0, 208.0, 0.0)])
        result = self._evaluate(
            states,
            max_speed_deviation_ms=1.15,
            desired_speed_ms=3.0,
        )
        assert result.passed is False
        sc = result.success_criteria
        assert sc["max_speed_deviation_ok"] is False
        assert sc["overspeed_deviation_ms"] == pytest.approx(0.0)
        # ramp 后均值 ≈ (7s×3.0 + 208s×0) / 215 ≈ 0.10 → 欠速偏差 ≈ 2.9
        assert sc["underspeed_deviation_ms"] == pytest.approx(2.90, abs=0.15)

    def test_normal_drive_not_penalized(self) -> None:
        """短暂停驶 6s（含巡航 3.0）：欠速偏差在上限内→仍通过。"""
        states = self._states([(0.0, 60.0, 3.0), (60.0, 6.0, 0.0), (66.0, 54.0, 3.0)])
        result = self._evaluate(
            states,
            max_speed_deviation_ms=1.15,
            desired_speed_ms=3.0,
        )
        assert result.passed is True
        assert result.success_criteria["max_speed_deviation_ok"] is True

    def test_no_desired_keeps_legacy_overspeed_only(self) -> None:
        """未传期望速度：退回单边超速判据（向后兼容旧调用）。"""
        states = self._states([(0.0, 30.0, 0.0)])  # 全程停驶
        result = self._evaluate(states, max_speed_deviation_ms=1.15)
        assert result.passed is True
        assert result.success_criteria["underspeed_deviation_ms"] == 0.0

    def test_short_scene_skips_underspeed(self) -> None:
        """ramp 窗口内无采样（短场景）：跳过欠速检查，不把好场景误判失败。"""
        states = self._states([(0.0, 2.0, 0.5)])
        result = self._evaluate(
            states,
            max_speed_deviation_ms=1.15,
            desired_speed_ms=3.0,
        )
        assert result.passed is True
        assert result.success_criteria["underspeed_deviation_ms"] == 0.0

    def test_post_ramp_avg_speed_excludes_rampup(self) -> None:
        """_post_ramp_avg_speed 只取起步窗口后的采样。"""
        states = self._states(
            [(0.0, _EVAL_SPEED_RAMPUP_S, 0.0), (_EVAL_SPEED_RAMPUP_S, 1.0, 2.0)]
        )
        avg = EvaluationEngine._post_ramp_avg_speed(states, desired_speed_ms=3.0)  # noqa: SLF001
        assert avg == pytest.approx(2.0)
        # 全部落在 ramp 窗口内 → None（跳过检查而非当 0 误判）
        early = self._states([(0.0, 2.0, 0.0)])
        assert EvaluationEngine._post_ramp_avg_speed(early, 3.0) is None  # noqa: SLF001


class TestV389UnderspeedFallbackWithoutCriterion:
    """V3.8.9：平台未下发 max_speed_deviation 时的欠速内置兜底。

    V3.8.8 把欠速检查门控在 ``max_speed_deviation_ms is not None`` 上，实机
    SCN-2085-...-1a1e-animal 的 success_criteria 未带该判据 → 检查被跳过 → 卡滞 218s
    仍 passed=True。V3.8.9 只要给出期望速度就用内置兜底拦「全程几乎没起步」，
    但不约束超速侧、不误杀「先行驶后刹停避让」。
    """

    @staticmethod
    def _states(segments: list[tuple[float, float, float]]) -> list[dict[str, object]]:
        """按 (起始s, 时长s, 速度m/s) 分段生成 10Hz 状态帧。"""
        states: list[dict[str, object]] = []
        for start_s, dur_s, speed in segments:
            for i in range(int(dur_s * 10)):
                states.append(
                    {
                        "vehicle_speed": speed,
                        "timestamp": start_s + i * 0.1,
                        "acceleration": (0.0, 0.0, 0.0),
                        "steering": 0.0,
                    }
                )
        return states

    def _evaluate(self, states: list[dict[str, object]], **kwargs: Any) -> SceneEvaluationResult:
        # 注意：不传 max_speed_deviation_ms，走 V3.8.9 内置兜底分支
        return EvaluationEngine().evaluate_scene(
            scene_id="v389",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            speed_limit_ms=3.0,
            scene_duration_s=220.0,
            completed=True,
            **kwargs,
        )

    def test_wedged_ego_no_criterion_now_fails(self) -> None:
        """复刻实机：全程 0.05 m/s 爬行、无平台偏差判据 → 兜底拦下 passed=False。"""
        states = self._states([(0.0, 220.0, 0.05)])
        result = self._evaluate(states, desired_speed_ms=3.0)
        assert result.passed is False
        sc = result.success_criteria
        assert sc["max_speed_deviation_ok"] is False
        assert sc["underspeed_deviation_ms"] == pytest.approx(2.95, abs=0.1)

    def test_drive_then_avoidance_stop_not_flagged(self) -> None:
        """合法「先巡航 3.0、后刹停避让」：最高速≈期望 → 不被兜底误杀。"""
        states = self._states([(0.0, 100.0, 3.0), (100.0, 120.0, 0.0)])
        result = self._evaluate(states, desired_speed_ms=3.0)
        assert result.passed is True
        assert result.success_criteria["max_speed_deviation_ok"] is True

    def test_normal_cruise_no_criterion_passes(self) -> None:
        """全程正常巡航：ramp 后均值≈期望 → 通过。"""
        states = self._states([(0.0, 220.0, 3.0)])
        result = self._evaluate(states, desired_speed_ms=3.0)
        assert result.passed is True

    def test_short_scene_no_criterion_skips(self) -> None:
        """短场景（全落在 ramp 窗口）：无采样→不判（空采样≠欠速）。"""
        states = self._states([(0.0, 2.0, 0.1)])
        result = self._evaluate(states, desired_speed_ms=3.0)
        assert result.passed is True

    def test_no_desired_no_criterion_legacy_pass(self) -> None:
        """既无期望速度又无判据：保持旧行为（偏差侧全放行）。"""
        states = self._states([(0.0, 220.0, 0.0)])
        result = self._evaluate(states)
        assert result.passed is True
        assert result.success_criteria["underspeed_deviation_ms"] == 0.0

    def test_ego_never_moved_threshold_and_none_guard(self) -> None:
        """_ego_never_moved 单元：双低才判、任一高即不判、None/无期望不判。"""
        never = EvaluationEngine._ego_never_moved  # noqa: SLF001
        thr = 3.0 * _EVAL_STOP_FRACTION
        assert never(max_speed_ms=thr - 0.1, avg_post_ramp=thr - 0.1, desired_speed_ms=3.0) is True
        # 最高速达阈（先行驶后刹停）→ 不判
        assert never(max_speed_ms=thr + 0.1, avg_post_ramp=0.0, desired_speed_ms=3.0) is False
        # 均值达阈（正常巡航）→ 不判
        assert never(max_speed_ms=0.0, avg_post_ramp=thr + 0.1, desired_speed_ms=3.0) is False
        # 无采样 / 无期望 / 期望≤0 → 不判
        assert never(max_speed_ms=0.0, avg_post_ramp=None, desired_speed_ms=3.0) is False
        assert never(max_speed_ms=0.0, avg_post_ramp=0.0, desired_speed_ms=None) is False
        assert never(max_speed_ms=0.0, avg_post_ramp=0.0, desired_speed_ms=0.0) is False

    def test_lax_platform_limit_still_blocks_never_moved(self) -> None:
        """V3.9.1：平台给了宽松偏差上限也不得放行「整场没起步」。

        实机 SCN-2056 自车 0 m/s 跑完 57s 却 passed=True——平台判据阈值一旦
        宽松，旧逻辑就只看 deviation <= limit。停驶判定必须与阈值无关。
        """
        states = self._states([(0.0, 57.0, 0.0)])
        result = self._evaluate(
            states, desired_speed_ms=3.0, max_speed_deviation_ms=100.0
        )
        assert result.passed is False
        assert result.success_criteria["max_speed_deviation_ok"] is False

    def test_edge_control_zero_frames_fails(self) -> None:
        """V3.9.1：SIL_EDGE 整场 0 控制帧 → 车端未参与，评估判 fail。"""
        states = self._states([(0.0, 220.0, 3.0)])
        result = self._evaluate(states, desired_speed_ms=3.0, edge_control_frames=0)
        assert result.passed is False
        assert result.success_criteria["edge_control_ok"] is False
        assert result.success_criteria["edge_control_frames"] == 0

    def test_edge_control_frames_present_passes(self) -> None:
        states = self._states([(0.0, 220.0, 3.0)])
        result = self._evaluate(states, desired_speed_ms=3.0, edge_control_frames=42)
        assert result.passed is True
        assert result.success_criteria["edge_control_ok"] is True

    def test_edge_control_absent_legacy_ignored(self) -> None:
        """未装配边缘链路（None）时不参与裁决，保持旧行为。"""
        states = self._states([(0.0, 220.0, 3.0)])
        result = self._evaluate(states, desired_speed_ms=3.0)
        assert result.passed is True
        assert result.success_criteria["edge_control_frames"] is None
        assert result.success_criteria["edge_control_ok"] is True

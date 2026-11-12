"""仿真评估指标计算引擎（PROMPT-ENG-007-A）。

实现安全、效率、舒适性、任务四类评估指标的计算，
以及评估等级判定（S/A/B/C/D）和报告生成。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hunter_sim.common.models import EvalGrade
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# ─── 评估目标阈值（设计文档 §9.2 指标表） ────────────────────────────
MIN_TTC_TARGET_S = 3.0           # 最小 TTC > 3.0s
MIN_DISTANCE_TARGET_M = 3.0      # 最小车距 > 3.0m
EMERGENCY_BRAKE_LIMIT = 2        # 紧急制动 < 2 次/场景
AVG_ACCEL_TARGET_MS2 = 1.0       # 平均加速度 < 1.0 m/s²
MAX_ACCEL_TARGET_MS2 = 2.0       # 最大加速度 < 2.0 m/s²
AVG_JERK_TARGET_MS3 = 2.0        # 平均加加速度 < 2.0 m/s³
STEERING_SMOOTHNESS_TARGET = 0.3  # 转向平滑度 < 0.3 rad/s

# 默认帧步长（秒，50Hz）：仅在无法从数据帧时间戳推导时作为回退（审查项 R）
DEFAULT_FRAME_DT_S = 0.02

#: V3.8.8：欠速侧偏差评估的起步窗口排除时长（秒）：场景开始前 N 秒为加速/起步
#: 阶段，不计入平均速度，避免正常起步被误判欠速；无 ramp 后采样时跳过欠速检查。
_EVAL_SPEED_RAMPUP_S = 5.0

#: V3.8.9：平台未下发 ``max_speed_deviation`` 判据时的「停驶/楔死」兜底阈值系数。
#: 仅当自车全程最高速 **与** 起步窗口后均值同时 < 期望速度 × 该系数，才判定自车
#: 几乎没起步（区别于「先加速到期望、后刹停避让」——后者最高速≈期望，不触发），
#: 修复「卡滞 214s 仍 passed=True」的默认放行假通过不再依赖平台是否发该可选判据。
_EVAL_STOP_FRACTION = 0.5

# 评估等级标准
_EVAL_THRESHOLDS: list[tuple[EvalGrade, float, float]] = [
    (EvalGrade.S, 0.95, 0.00),   # 通过率 >= 95%, 碰撞率 0%
    (EvalGrade.A, 0.85, 0.02),
    (EvalGrade.B, 0.70, 0.05),
    (EvalGrade.C, 0.50, 0.10),
]


def determine_grade(pass_rate: float, collision_rate: float) -> EvalGrade:
    """根据通过率和碰撞率确定评估等级。

    碰撞率按“不超过上限”判定（S 级要求碰撞率恰好为 0）。
    """
    for grade, min_pass, max_coll in _EVAL_THRESHOLDS:
        if pass_rate >= min_pass and collision_rate <= max_coll:
            return grade
    return EvalGrade.D


@dataclass
class SafetyMetrics:
    """安全指标计算结果。

    Attributes:
        collision_count: 碰撞总次数。
        min_ttc_s: 最小碰撞时间（TTC，秒）。
        min_distance_m: 最小车距（米）。
        emergency_brake_count: 紧急制动次数。
        lane_invasion_count: 车道偏离次数。
        red_light_count: 闯红灯次数。
    """

    collision_count: int = 0
    min_ttc_s: float = float("inf")
    min_distance_m: float = float("inf")
    emergency_brake_count: int = 0
    lane_invasion_count: int = 0
    red_light_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "collision_count": self.collision_count,
            "min_ttc_s": self.min_ttc_s if self.min_ttc_s != float("inf") else None,
            "min_distance_m": self.min_distance_m if self.min_distance_m != float("inf") else None,
            "emergency_brake_count": self.emergency_brake_count,
            "lane_invasion_count": self.lane_invasion_count,
            "red_light_count": self.red_light_count,
        }

    def meets_targets(self) -> bool:
        """安全指标是否全部达到文档 §9.2.1 目标值。"""
        return (
            self.collision_count == 0
            and (self.min_ttc_s == float("inf") or self.min_ttc_s > MIN_TTC_TARGET_S)
            and (self.min_distance_m == float("inf") or self.min_distance_m > MIN_DISTANCE_TARGET_M)
            and self.emergency_brake_count < EMERGENCY_BRAKE_LIMIT
            and self.lane_invasion_count == 0
            and self.red_light_count == 0
        )


@dataclass
class EfficiencyMetrics:
    """效率指标计算结果（文档 §9.2.2）。

    Attributes:
        avg_speed_ms: 平均速度（m/s）。
        max_speed_ms: 最大速度（m/s）。
        total_distance_m: 总行驶距离（米）。
        scene_duration_s: 场景实际运行时长（秒，即场景完成时间）。
        waiting_time_s: 等待（停止）时间（秒）。
        completion_rate: 场景完成率 (0~1)。
    """

    avg_speed_ms: float = 0.0
    max_speed_ms: float = 0.0
    total_distance_m: float = 0.0
    scene_duration_s: float = 0.0
    waiting_time_s: float = 0.0
    completion_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_speed_ms": round(self.avg_speed_ms, 3),
            "max_speed_ms": round(self.max_speed_ms, 3),
            "total_distance_m": round(self.total_distance_m, 2),
            "scene_duration_s": round(self.scene_duration_s, 2),
            "waiting_time_s": round(self.waiting_time_s, 2),
            "completion_rate": round(self.completion_rate, 3),
        }


@dataclass
class ComfortMetrics:
    """舒适性指标计算结果。

    Attributes:
        avg_acceleration_ms2: 平均加速度绝对值（m/s²）。
        max_acceleration_ms2: 最大加速度（m/s²）。
        avg_jerk_ms3: 平均加加速度（m/s³）。
        max_jerk_ms3: 最大加加速度（m/s³）。
        steering_smoothness_rad_s: 转向平滑度（平均角速度，rad/s）。
    """

    avg_acceleration_ms2: float = 0.0
    max_acceleration_ms2: float = 0.0
    avg_jerk_ms3: float = 0.0
    max_jerk_ms3: float = 0.0
    steering_smoothness_rad_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_acceleration_ms2": round(self.avg_acceleration_ms2, 3),
            "max_acceleration_ms2": round(self.max_acceleration_ms2, 3),
            "avg_jerk_ms3": round(self.avg_jerk_ms3, 3),
            "max_jerk_ms3": round(self.max_jerk_ms3, 3),
            "steering_smoothness_rad_s": round(self.steering_smoothness_rad_s, 4),
        }

    def meets_targets(self) -> bool:
        """舒适性指标是否全部达到文档 §9.2.3 目标值。"""
        return (
            self.avg_acceleration_ms2 < AVG_ACCEL_TARGET_MS2
            and self.max_acceleration_ms2 < MAX_ACCEL_TARGET_MS2
            and self.avg_jerk_ms3 < AVG_JERK_TARGET_MS3
            and self.steering_smoothness_rad_s < STEERING_SMOOTHNESS_TARGET
        )


@dataclass
class SceneEvaluationResult:
    """单场景完整评估结果。

    Attributes:
        scene_id: 场景 ID。
        grade: 评估等级。
        safety: 安全指标。
        efficiency: 效率指标。
        comfort: 舒适性指标。
        passed: 是否通过（基于文档 §9.2 成功准则）。
        timestamp: 评估时间。
        events: 事件列表。
        takeover: 是否发生人工接管（文档 §9.2.4 接管率统计）。
        success_criteria: 成功准则明细（文档 §9.4）。
    """

    scene_id: str
    grade: EvalGrade
    safety: SafetyMetrics
    efficiency: EfficiencyMetrics
    comfort: ComfortMetrics
    passed: bool
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    events: list[dict[str, Any]] = field(default_factory=list)
    takeover: bool = False
    success_criteria: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "grade": self.grade.value,
            "passed": self.passed,
            "timestamp": self.timestamp,
            "takeover": self.takeover,
            "success_criteria": self.success_criteria,
            "safety": self.safety.to_dict(),
            "efficiency": self.efficiency.to_dict(),
            "comfort": self.comfort.to_dict(),
            "events": self.events,
        }


class EvaluationEngine:
    """评估指标计算主引擎。

    输入仿真运行数据（轨迹、事件列表），输出 SceneEvaluationResult。
    """

    def evaluate_scene(
        self,
        scene_id: str,
        vehicle_states: list[dict[str, Any]],
        events: list[dict[str, Any]],
        collision_events: list[dict[str, Any]],
        ttc_samples: list[float],
        min_distances: list[float],
        speed_limit_ms: float = 10.0,
        scene_duration_s: float = 60.0,
        completed: bool = True,
        takeover: bool = False,
        min_safe_distance_m: float | None = None,
        max_speed_deviation_ms: float | None = None,
        desired_speed_ms: float | None = None,
    ) -> SceneEvaluationResult:
        """计算单场景的完整评估结果。

        Args:
            scene_id: 场景 ID。
            vehicle_states: 车辆状态帧列表（含 velocity/acceleration 字段）。
            events: 检测到的事件列表。
            collision_events: 碰撞事件列表。
            ttc_samples: TTC 采样值列表。
            min_distances: 最小距离采样列表。
            speed_limit_ms: 限速值。
            scene_duration_s: 场景设定运行时长。
            completed: 场景是否正常完成。
            takeover: 是否发生人工接管（文档 §9.2.4）。
            min_safe_distance_m: 平台成功判据的最小安全距离阈值（米，V3.8.4）；
                None 表示不下发该判据，退回文档默认目标。
            max_speed_deviation_ms: 平台成功判据的速度偏差上限（m/s，V3.8.4）；
                None 表示不下发该判据。
            desired_speed_ms: 场景期望巡航速度（m/s，V3.8.8/V3.8.9）。与
                max_speed_deviation_ms 同时下发时偏差判据双边（超速/欠速取大）；
                未下发 max_speed_deviation_ms 但给出期望速度时，V3.8.9 启用内置
                兜底：自车全程几乎没起步（最高速与 ramp 后均值均 < 期望×系数）
                则判 fail，修复「停驶 87% 时长却 passed=True」不再依赖平台可选判据。

        Returns:
            SceneEvaluationResult。
        """
        # 安全指标
        safety = self._compute_safety(collision_events, events, ttc_samples, min_distances)

        # 效率指标
        efficiency = self._compute_efficiency(vehicle_states, scene_duration_s, completed)

        # 舒适性指标
        comfort = self._compute_comfort(vehicle_states)

        # 成功准则（文档 §9.4 success_criteria）
        overspeed_deviation = max(0.0, efficiency.max_speed_ms - speed_limit_ms)
        # V3.8.8/V3.8.9：欠速侧偏差——原判据只抓超速（max_speed − limit），自车长时间
        # 停驶/爬行时 max_speed ≤ limit → deviation=0 → 假通过（实机 SCN-2085 冻结 214s
        # 仍 passed=True）。V3.8.9 起欠速偏差的计算不再门控平台是否下发 max_speed_deviation：
        # 只要给出期望速度就计算并留痕；是否据此判失败见下方 speed_deviation_ok 分支。
        underspeed_deviation = 0.0
        avg_post_ramp: float | None = None
        if desired_speed_ms:
            avg_post_ramp = self._post_ramp_avg_speed(vehicle_states, desired_speed_ms)
            if avg_post_ramp is not None:
                underspeed_deviation = max(0.0, desired_speed_ms - avg_post_ramp)
        # 双边偏差供报告展示与平台判据裁决用
        max_speed_deviation = max(overspeed_deviation, underspeed_deviation)
        min_distance_ok = (
            min_safe_distance_m is None
            or safety.min_distance_m == float("inf")
            or safety.min_distance_m >= min_safe_distance_m
        )
        if max_speed_deviation_ms is not None:
            # 平台显式下发偏差上限：双边按其阈值裁决（V3.8.8 语义，向后兼容）。
            speed_deviation_ok = max_speed_deviation <= max_speed_deviation_ms
        else:
            # V3.8.9：平台未下发偏差上限时的内置兜底——只拦「全程几乎没起步」的停驶/楔死
            # 假通过，不约束超速侧（保持不发该判据的旧场景向后兼容）；合法「先行驶后刹停
            # 避让」的最高速≈期望，不会被误判。
            speed_deviation_ok = not self._ego_never_moved(
                efficiency.max_speed_ms, avg_post_ramp, desired_speed_ms
            )
        success_criteria = {
            "no_collision": safety.collision_count == 0,
            "max_speed_deviation": round(max_speed_deviation, 3),
            "min_safe_distance": (
                safety.min_distance_m
                if safety.min_distance_m != float("inf")
                else None
            ),
            # V3.8.4：平台判据阈值与逐项达标布尔（供平台裁决与报告展示）。
            "min_safe_distance_required": min_safe_distance_m,
            "max_speed_deviation_limit": max_speed_deviation_ms,
            # V3.8.8：过/欠速明细（超速侧/欠速侧各自偏差，便于报告与排障）
            "overspeed_deviation_ms": round(overspeed_deviation, 3),
            "underspeed_deviation_ms": round(underspeed_deviation, 3),
            "min_safe_distance_ok": min_distance_ok,
            "max_speed_deviation_ok": speed_deviation_ok,
        }

        # 任务指标：基于成功准则判定通过/失败（文档 §9.3 步骤 5）
        # V3.8.4：叠加平台下发的 min_safe_distance / max_speed_deviation 判据，
        # 避免「近距擦碰/超速却 passed=True」的假通过。
        passed = (
            completed
            and not takeover
            and safety.meets_targets()
            and min_distance_ok
            and speed_deviation_ok
        )

        # 等级判定（单场景用 pass/fail，批量用 pass_rate）
        grade = EvalGrade.S if passed else EvalGrade.D

        return SceneEvaluationResult(
            scene_id=scene_id,
            grade=grade,
            safety=safety,
            efficiency=efficiency,
            comfort=comfort,
            passed=passed,
            events=events,
            takeover=takeover,
            success_criteria=success_criteria,
        )

    @staticmethod
    def publish_results(
        sim_data_producer: Any,
        sim_id: str,
        vehicle_id: str,
        scene_id: str,
        result: SceneEvaluationResult,
        eval_bucket: str = "hunter-raw-data",
    ) -> None:
        """将评估结果发布到平台（ sim_metrics + artifact）。

        应在场景终止评估完成后调用。无 ``sim_data_producer`` 时安全返回。

        Args:
            sim_data_producer: ``SimDataProducer`` 实例。
            sim_id: 仿真实例 ID。
            vehicle_id: 孪生车 ID。
            scene_id: 场景 ID。
            result: 单场景评估结果。
            eval_bucket: 产物文件存储 Bucket。
        """
        if sim_data_producer is None:
            return
        try:
            import json as _json
            import time as _time

            from hunter_sim.cloud.sim_data_mapper import build_sim_metrics  # noqa: PLC0415

            seq_metrics = sim_data_producer.next_seq()
            metrics_msg = build_sim_metrics(
                sim_id=sim_id,
                vehicle_id=vehicle_id or None,
                scene_id=scene_id,
                seq=seq_metrics,
                ts=_time.time(),
                grade=result.grade.value,
                passed=result.passed,
                safety=result.safety.to_dict(),
                efficiency=result.efficiency.to_dict(),
                comfort=result.comfort.to_dict(),
                task={
                    "completion_rate": result.efficiency.completion_rate,
                    "scene_duration_s": result.efficiency.scene_duration_s,
                },
                success_criteria_result=result.success_criteria,
                takeover_required=result.takeover,
            )
            sim_data_producer.publish_metrics(metrics_msg)

            # 发布 artifact：将评估结果作为 eval.json 登记到平台
            result_dict = result.to_dict()
            json_bytes = _json.dumps(result_dict, default=str).encode("utf-8")
            size_bytes = len(json_bytes)
            import hashlib
            sha256 = hashlib.sha256(json_bytes).hexdigest()
            object_key = f"scenarios/simulations/{sim_id}/{scene_id}/eval.json"
            sim_data_producer.publish_artifact(
                name="eval.json",
                bucket=eval_bucket,
                object_key=object_key,
                size_bytes=size_bytes,
                sha256=sha256,
                media_type="application/json",
                vehicle_id=vehicle_id or None,
                scene_id=scene_id,
            )
        except Exception as exc:  # noqa: BLE001  发布失败不阻断主流程
            logger.warning(f"publish_results failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _frame_time_s(frame: dict[str, Any]) -> float:
        """读取帧时间戳（秒）：兼容规范字段 time_stamp 与遥测/录制字段 timestamp（审查项 R）。"""
        value = frame.get("time_stamp")
        if value is None:
            value = frame.get("timestamp")
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _derive_frame_dt(cls, states: list[dict[str, Any]]) -> float:
        """从相邻帧时间戳推导帧步长（取中位数，对丢帧/异常值鲁棒）。

        推导失败（帧数不足或时间戳无效/非单调）时回退 DEFAULT_FRAME_DT_S。
        """
        times = [cls._frame_time_s(s) for s in states]
        diffs = sorted(b - a for a, b in zip(times, times[1:]) if 0.0 < b - a < 1.0)
        if not diffs:
            return DEFAULT_FRAME_DT_S
        return diffs[len(diffs) // 2]

    @staticmethod
    def _compute_safety(
        collisions: list[dict[str, Any]],
        events: list[dict[str, Any]],
        ttc_samples: list[float],
        min_distances: list[float],
    ) -> SafetyMetrics:
        lane_inv = sum(1 for e in events if e.get("event_type") == "lane_invasion")
        red_light = sum(1 for e in events if e.get("event_type") == "red_light")
        emergency_brake = sum(1 for e in events if e.get("event_type") == "emergency_brake")
        return SafetyMetrics(
            collision_count=len(collisions),
            min_ttc_s=min(ttc_samples) if ttc_samples else float("inf"),
            min_distance_m=min(min_distances) if min_distances else float("inf"),
            emergency_brake_count=emergency_brake,
            lane_invasion_count=lane_inv,
            red_light_count=red_light,
        )

    @staticmethod
    def _post_ramp_avg_speed(
        states: list[dict[str, Any]], desired_speed_ms: float
    ) -> float | None:
        """起步窗口后的平均速度（V3.8.8 欠速判据用）；无采样返 None 跳过检查。

        只取帧时间 ≥ ``_EVAL_SPEED_RAMPUP_S`` 的 ``vehicle_speed``，排除起步/加速
        阶段；时间戳缺失或全部落在 ramp 窗口内（短场景）时返 None，避免把
        「没测到」当成「欠速」（与 V3.8.4 空采样教训同口径）。
        """
        speeds = [
            float(s.get("vehicle_speed", 0.0))
            for s in states
            if EvaluationEngine._frame_time_s(s) >= _EVAL_SPEED_RAMPUP_S
        ]
        if not speeds:
            return None
        return sum(speeds) / len(speeds)

    @staticmethod
    def _ego_never_moved(
        max_speed_ms: float,
        avg_post_ramp: float | None,
        desired_speed_ms: float | None,
    ) -> bool:
        """判定自车是否「全程几乎没起步」（V3.8.9 欠速兜底，区别于先行驶后刹停）。

        仅当最高速 **与** 起步窗口后均值同时 < 期望×``_EVAL_STOP_FRACTION`` 才判停驶/楔死：
        先加速到期望再刹停避让的场景最高速≈期望，不触发（避免误杀合法避让）；
        无期望速度或 ramp 窗口内无采样（``avg_post_ramp is None``）时不判（空采样≠欠速）。
        """
        if not desired_speed_ms or desired_speed_ms <= 0.0:
            return False
        if avg_post_ramp is None:
            return False
        threshold = desired_speed_ms * _EVAL_STOP_FRACTION
        return max_speed_ms < threshold and avg_post_ramp < threshold

    @staticmethod
    def _compute_efficiency(
        states: list[dict[str, Any]],
        duration_s: float,
        completed: bool,
    ) -> EfficiencyMetrics:
        speeds = [s.get("vehicle_speed", 0.0) for s in states]
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.0
        max_speed = max(speeds) if speeds else 0.0
        total_dist = 0.0
        for i in range(1, len(states)):
            # 审查项 R：时间字段兼容 time_stamp（规范）/timestamp（录制），不再只读单一字段
            dt = EvaluationEngine._frame_time_s(states[i]) - EvaluationEngine._frame_time_s(states[i - 1])
            if 0.0 < dt < 0.1:
                total_dist += states[i].get("vehicle_speed", 0.0) * dt
        # 审查项 R：帧步长由数据帧时间戳推导，不再硬编码 50Hz
        frame_dt = EvaluationEngine._derive_frame_dt(states)
        waiting = sum(1 for s in speeds if s < 0.2) * frame_dt
        return EfficiencyMetrics(
            avg_speed_ms=avg_speed,
            max_speed_ms=max_speed,
            total_distance_m=total_dist,
            scene_duration_s=duration_s,
            waiting_time_s=waiting,
            completion_rate=1.0 if completed else 0.0,
        )

    @staticmethod
    def _compute_comfort(states: list[dict[str, Any]]) -> ComfortMetrics:
        # 审查项 R：帧步长从数据推导（回退 50Hz），不再在三处硬编码 0.02
        dt = EvaluationEngine._derive_frame_dt(states) or DEFAULT_FRAME_DT_S
        accels = []
        yaws = []
        prev_speeds: list[float] = []
        for s in states:
            ax, ay, _ = s.get("acceleration", (0.0, 0.0, 0.0))
            accels.append(math.sqrt(ax ** 2 + ay ** 2))
            yaws.append(s.get("steering", 0.0))
            prev_speeds.append(s.get("vehicle_speed", 0.0))

        avg_acc = sum(accels) / len(accels) if accels else 0.0
        max_acc = max(accels) if accels else 0.0

        # Jerk 估算（加速度差分）
        jerks: list[float] = []
        for i in range(1, len(accels)):
            jerks.append(abs(accels[i] - accels[i - 1]) / dt)
        avg_jerk = sum(jerks) / len(jerks) if jerks else 0.0
        max_jerk = max(jerks) if jerks else 0.0

        # 转向平滑度
        steer_rates: list[float] = []
        for i in range(1, len(yaws)):
            steer_rates.append(abs(yaws[i] - yaws[i - 1]) / dt)
        steer_smooth = sum(steer_rates) / len(steer_rates) if steer_rates else 0.0

        return ComfortMetrics(
            avg_acceleration_ms2=avg_acc,
            max_acceleration_ms2=max_acc,
            avg_jerk_ms3=avg_jerk,
            max_jerk_ms3=max_jerk,
            steering_smoothness_rad_s=steer_smooth,
        )


class BatchEvaluator:
    """批量场景评估器。

    管理多个场景评估结果，计算总体通过率和等级。
    """

    def __init__(self) -> None:
        self._results: list[SceneEvaluationResult] = []

    def add_result(self, result: SceneEvaluationResult) -> None:
        """添加单场景评估结果。"""
        self._results.append(result)

    def results(self) -> list[SceneEvaluationResult]:
        """返回全部场景评估结果。"""
        return list(self._results)

    def overall_grade(self) -> EvalGrade:
        """计算批量评估总体等级。"""
        if not self._results:
            return EvalGrade.D
        pass_rate = sum(1 for r in self._results if r.passed) / len(self._results)
        collision_rate = sum(1 for r in self._results if r.safety.collision_count > 0) / len(self._results)
        return determine_grade(pass_rate, collision_rate)

    def summary(self) -> dict[str, Any]:
        """返回批量评估摘要（含文档 §9.2.4 任务指标与 §9.5 汇总项）。"""
        total = len(self._results)
        if total == 0:
            return {"total": 0}
        passed = sum(1 for r in self._results if r.passed)
        return {
            "total_scenes": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total,
            # 任务指标（文档 §9.2.4）
            "collision_rate": sum(1 for r in self._results if r.safety.collision_count > 0) / total,
            "takeover_rate": sum(1 for r in self._results if r.takeover) / total,
            "target_achievement_rate": sum(
                1 for r in self._results if r.efficiency.completion_rate >= 1.0
            ) / total,
            "overall_grade": self.overall_grade().value,
            "avg_collision_count": sum(r.safety.collision_count for r in self._results) / total,
            # 各指标均值（文档 §9.5 汇总报告）
            "metric_averages": {
                "avg_speed_ms": round(sum(r.efficiency.avg_speed_ms for r in self._results) / total, 3),
                "avg_distance_m": round(sum(r.efficiency.total_distance_m for r in self._results) / total, 2),
                "avg_acceleration_ms2": round(sum(r.comfort.avg_acceleration_ms2 for r in self._results) / total, 3),
                "avg_jerk_ms3": round(sum(r.comfort.avg_jerk_ms3 for r in self._results) / total, 3),
            },
            # 失败场景列表（文档 §9.5 汇总报告）
            "failed_scenes": [r.scene_id for r in self._results if not r.passed],
            "results": [r.to_dict() for r in self._results],
        }

    def to_json(self, indent: int = 2) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.summary(), indent=indent, default=str)

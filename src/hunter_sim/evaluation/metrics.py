"""模块 4.1：评估指标计算引擎（纯 numpy，无外部重依赖）。

覆盖四类指标：轨迹跟踪误差（ADE/FDE）、驾驶舒适度（加速度/加加速度）、
安全合规（碰撞率/路线完成率）、传感器覆盖率。输入均为数组或 ``core`` 契约对象。
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from hunter_sim.core.contracts import VehicleState
from hunter_sim.core.exceptions import ValidationError
from hunter_sim.evaluation.models import (
    ComfortMetrics,
    CoverageMetrics,
    SafetyMetrics,
    TrajectoryMetrics,
)


class MetricsEngineImpl:
    """满足 :class:`~hunter_sim.evaluation.protocols.MetricsEngine` 契约。"""

    def trajectory_error(
        self,
        predicted: NDArray[np.floating],
        reference: NDArray[np.floating],
    ) -> TrajectoryMetrics:
        """计算预测轨迹相对参考轨迹的 ADE / FDE / 最大误差（取前 2 列 x,y）。"""
        if predicted.shape != reference.shape:
            raise ValidationError(
                f"轨迹形状不一致: predicted={predicted.shape} reference={reference.shape}"
            )
        if predicted.ndim != 2 or predicted.shape[1] < 2:
            raise ValidationError(f"轨迹需为非空 (N, >=2) 数组，实际 shape={predicted.shape}")
        if predicted.shape[0] == 0:
            raise ValidationError("轨迹不能为空")
        diff = predicted[:, :2].astype(np.float64) - reference[:, :2].astype(np.float64)
        errors = np.linalg.norm(diff, axis=1)
        return TrajectoryMetrics(
            ade=float(errors.mean()),
            fde=float(errors[-1]),
            max_error=float(errors.max()),
        )

    def comfort(
        self,
        states: list[VehicleState],
        *,
        dt: float,
        a_max: float = 4.0,
        j_max: float = 10.0,
    ) -> ComfortMetrics:
        """由车辆状态序列计算舒适度：水平加速度模的 RMS/峰值与加加速度、综合得分。"""
        if dt <= 0:
            raise ValidationError(f"dt 必须为正数: {dt}")
        if len(states) < 2:
            raise ValidationError("至少需要两个车辆状态")
        accel = np.array(
            [float(np.hypot(s.acceleration_x, s.acceleration_y)) for s in states],
            dtype=np.float64,
        )
        jerk = np.abs(np.diff(accel)) / dt
        a_exceed = float(np.mean(accel > a_max))
        j_exceed = float(np.mean(jerk > j_max)) if jerk.size else 0.0
        score = float(np.clip(1.0 - max(a_exceed, j_exceed), 0.0, 1.0))
        return ComfortMetrics(
            rms_accel=float(np.sqrt(np.mean(accel**2))),
            max_accel=float(accel.max()),
            max_jerk=float(jerk.max()) if jerk.size else 0.0,
            comfort_score=score,
        )

    def safety(
        self,
        *,
        collision_count: int,
        lane_invasion_count: int,
        total_ticks: int,
        tick_rate: float,
        route_completion: float = 1.0,
    ) -> SafetyMetrics:
        """由事件计数与运行时长计算安全指标。"""
        if tick_rate <= 0:
            raise ValidationError(f"tick_rate 必须为正数: {tick_rate}")
        if collision_count < 0 or lane_invasion_count < 0 or total_ticks < 0:
            raise ValidationError("计数参数不能为负")
        duration = total_ticks / tick_rate
        collision_rate = collision_count / duration if duration > 0 else 0.0
        return SafetyMetrics(
            collision_count=collision_count,
            lane_invasion_count=lane_invasion_count,
            collision_rate=float(collision_rate),
            route_completion=float(np.clip(route_completion, 0.0, 1.0)),
        )

    def coverage(
        self,
        *,
        total_ticks: int,
        per_sensor_counts: dict[str, int],
    ) -> CoverageMetrics:
        """各传感器实际帧数相对总 tick 的覆盖率与均值。"""
        if total_ticks < 0:
            raise ValidationError(f"total_ticks 不能为负: {total_ticks}")
        if total_ticks == 0:
            per_sensor = {sid: 0.0 for sid in per_sensor_counts}
            overall = 0.0
        else:
            per_sensor = {
                sid: float(min(1.0, count / total_ticks))
                for sid, count in per_sensor_counts.items()
            }
            overall = float(np.mean(list(per_sensor.values()))) if per_sensor else 0.0
        return CoverageMetrics(
            total_ticks=total_ticks,
            per_sensor=per_sensor,
            overall=overall,
        )


__all__ = ["MetricsEngineImpl"]

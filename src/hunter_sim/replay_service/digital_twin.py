"""数字孪生与场景重建服务（PROMPT-ENG-006-B）。

支持三种孪生模式：
- 实时孪生（VIL 模式，实车当前状态在仿真中实时映射）
- 回放孪生（实车历史轨迹在仿真中复现）
- 对比孪生（实车轨迹与仿真轨迹同屏对比）
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from hunter_sim.common.utils import euclidean_distance_2d, get_logger, rmse

logger = get_logger(__name__)


class TwinMode(str, Enum):
    """数字孪生模式。"""

    REALTIME = "realtime"     # VIL 实时孪生
    REPLAY = "replay"         # 历史回放孪生
    COMPARISON = "comparison"  # 对比孪生（实车 vs 仿真轨迹）


class TrajectoryComparator:
    """实车与仿真轨迹对比引擎。

    计算位置 RMSE、航向误差、速度误差三类指标。
    """

    def compare(
        self,
        real_trajectory: list[dict[str, Any]],
        sim_trajectory: list[dict[str, Any]],
        time_tolerance_s: float = 0.1,
    ) -> dict[str, float]:
        """执行轨迹对比，返回指标字典。

        Args:
            real_trajectory: 实车轨迹帧列表。
            sim_trajectory: 仿真轨迹帧列表。
            time_tolerance_s: 时间匹配容差（秒）。

        Returns:
            包含 position_rmse_m / heading_rmse_deg / speed_rmse_ms 的字典。
        """
        # 按时间戳对齐帧
        sim_by_ts = {round(f["timestamp"], 3): f for f in sim_trajectory}
        pos_errors: list[float] = []
        heading_errors: list[float] = []
        speed_errors: list[float] = []

        for real_frame in real_trajectory:
            matched = self._find_nearest_frame(sim_by_ts, real_frame["timestamp"], time_tolerance_s)
            if matched is None:
                continue
            rx = real_frame["position"]["x"]
            ry = real_frame["position"]["y"]
            sx = matched["position"]["x"]
            sy = matched["position"]["y"]
            pos_errors.append(euclidean_distance_2d(rx, ry, sx, sy))

            rh = real_frame.get("rotation", {}).get("yaw", 0.0)
            sh = matched.get("rotation", {}).get("yaw", 0.0)
            heading_errors.append(abs(rh - sh))

            rs = real_frame.get("velocity", {}).get("speed", 0.0)
            ss = matched.get("velocity", {}).get("speed", 0.0)
            speed_errors.append(abs(rs - ss))

        return {
            "position_rmse_m": rmse(pos_errors),
            "heading_rmse_deg": rmse(heading_errors),
            "speed_rmse_ms": rmse(speed_errors),
            "matched_frames": len(pos_errors),
            "total_real_frames": len(real_trajectory),
        }

    @staticmethod
    def _find_nearest_frame(
        sim_by_ts: dict[float, dict[str, Any]],
        target_ts: float,
        tolerance: float,
    ) -> Optional[dict[str, Any]]:
        """在容差范围内找时间最接近的仿真帧。"""
        best_ts: Optional[float] = None
        min_diff = float("inf")
        for ts in sim_by_ts:
            diff = abs(ts - target_ts)
            if diff < min_diff and diff <= tolerance:
                min_diff = diff
                best_ts = ts
        return sim_by_ts[best_ts] if best_ts is not None else None


class SceneReconstructor:
    """从实车历史数据自动重建仿真场景（设计文档 §8.6 场景自动重建）。

    流程：
    1. 提取实车运行时间段的轨迹和环境数据（调用方通过 DataLoader 完成）
    2. 识别运行区域对应的仿真地图（map_id 由调用方传入）
    3. 从感知结果提取周边交通参与者轨迹
    4. 将感知目标轨迹转化为仿真参与者行为脚本
    5. 生成可在 CARLA 中重新执行的场景配置（SceneConfig 兼容字典）
    6. 场景可用于 SIL 算法回归测试（mode=sil）
    """

    def reconstruct(
        self,
        trajectory: list[dict[str, Any]],
        perception_frames: list[dict[str, Any]],
        map_id: str,
    ) -> dict[str, Any]:
        """执行场景重建，返回 SceneConfig 兼容的字典。

        Args:
            trajectory: 自车历史轨迹。
            perception_frames: 每帧的感知结果（含目标列表）。
            map_id: 目标地图 ID。

        Returns:
            场景配置字典（供 SceneConfig 模型加载）。
        """
        participants: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for frame in perception_frames:
            for obj in frame.get("objects", []):
                oid = obj.get("id", 0)
                if oid in seen_ids:
                    continue
                seen_ids.add(oid)
                obj_type = str(obj.get("type", ""))
                # 参与者类型映射（文档 §7.2：车辆/行人/非机动车）
                if obj_type in ("bicycle", "cyclist"):
                    actor_type = "cyclist"
                    blueprint = "vehicle.diamondback.century"
                elif obj_type == "vehicle":
                    actor_type = "vehicle"
                    blueprint = "vehicle.tesla.model3"
                else:
                    actor_type = "walker"
                    blueprint = "walker.pedestrian.0001"
                participants.append({
                    "participant_id": f"reconstructed_{oid}",
                    "actor_type": actor_type,
                    "blueprint": blueprint,
                    "spawn_point": {
                        "x": obj.get("x", 0.0),
                        "y": obj.get("y", 0.0),
                        "yaw_deg": obj.get("heading", obj.get("yaw", 0.0)),
                    },
                    "behavior": "constant_speed",
                    "speed_ms": obj.get("speed", obj.get("vx", 5.0)),
                })

        ego_start = trajectory[0]["position"] if trajectory else {"x": 0.0, "y": 0.0}
        config_dict: dict[str, Any] = {
            "scene_id": "reconstructed_scene",
            "scene_name": f"Reconstructed from {len(trajectory)} frames",
            "map_id": map_id,
            "mode": "sil",
            "ego_vehicle": {
                "spawn_point": {
                    "x": ego_start["x"],
                    "y": ego_start["y"],
                    "yaw_deg": trajectory[0].get("rotation", {}).get("yaw", 0.0) if trajectory else 0.0,
                },
            },
            "traffic_participants": participants[:50],  # 限制最大数量
            "duration_seconds": (
                trajectory[-1]["timestamp"] - trajectory[0]["timestamp"]
                if len(trajectory) >= 2
                else 60.0
            ),
        }
        logger.info(
            f"Scene reconstructed: {len(trajectory)} ego frames, "
            f"{len(participants)} participants"
        )
        return config_dict

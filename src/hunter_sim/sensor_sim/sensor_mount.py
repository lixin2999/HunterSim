"""传感器安装位置管理（V3.4.0：按实际蓝图外形边界换算）。

挂载点不再写死车身尺寸，而是：

1. 优先使用平台下发 ``sensor_mount_reference.envelope_m``（``status=resolved``
   且 ``blueprint`` 与实际 actor 一致时）；
2. 否则用当前 spawn 出来的车辆包围盒（``bounding_box`` 属性 / ``get_bounding_box()``
   方法，兼容不同 CARLA 版本）本地实测（当前部署常态）；
3. 两者都不可用时显式报错，绝不回落到写死米数。

安装位统一表达为 ``mount_loc = anchor(envelope_m) + offset_m``，其中
``offset_m`` 是传感器本体相对车身锚点的安装余量，来自
``configs/sensor_configs.json`` 的 ``mounts`` 段（支持运行时修改并生效）。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from hunter_sim.common.exceptions import ConfigurationError, SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

#: 传感器配置环境变量（运维可指向自定义文件）
SENSOR_CONFIG_ENV = "HUNTER_SIM_SENSOR_CONFIGS"

#: 平台 envelope_m 必需字段（缺任何一个都视为不可用，不能把缺失当 0）
_ENVELOPE_FIELDS = (
    "front_edge_x_m",
    "rear_edge_x_m",
    "side_edge_y_m",
    "roof_z_m",
    "bottom_z_m",
)

#: 配置文件 senser 键 → 内部传感器类型
_MOUNT_KEY_TO_TYPE: dict[str, SensorType] = {
    "lidar": SensorType.LIDAR,
    "rgb_camera": SensorType.RGB_CAMERA,
    "camera_front_rgb": SensorType.RGB_CAMERA,
    "depth_camera": SensorType.DEPTH_CAMERA,
    "camera_front_depth": SensorType.DEPTH_CAMERA,
    "imu": SensorType.IMU,
    "gnss": SensorType.GNSS,
    "collision": SensorType.COLLISION,
    "lane_invasion": SensorType.LANE_INVASION,
    "obstacle": SensorType.OBSTACLE,
}

#: 无配置文件时的内建默认（锚点 + 传感器安装余量，不含车身尺寸）
_DEFAULT_MOUNTS: dict[str, dict[str, Any]] = {
    "lidar": {
        "mount_id": "lidar_top",
        "anchor": "roof",
        "offset_m": {"x": 0.0, "y": 0.0, "z": 0.15},
        "rot_deg": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    },
    "rgb_camera": {
        "mount_id": "camera_front_rgb",
        "anchor": "front_edge",
        "offset_m": {"x": -0.45, "y": 0.0, "z": -0.10},
        "rot_deg": {"pitch": -8.0, "yaw": 0.0, "roll": 0.0},
    },
    "depth_camera": {
        "mount_id": "camera_front_depth",
        "anchor": "front_edge",
        "offset_m": {"x": -0.45, "y": 0.0, "z": -0.10},
        "rot_deg": {"pitch": -8.0, "yaw": 0.0, "roll": 0.0},
    },
    "imu": {
        "mount_id": "imu_cg",
        "anchor": "center",
        "offset_m": {"x": 0.0, "y": 0.0, "z": 0.0},
        "rot_deg": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    },
    "gnss": {
        "mount_id": "gnss_roof",
        "anchor": "roof",
        "offset_m": {"x": 0.0, "y": 0.0, "z": 0.02},
        "rot_deg": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    },
    "collision": {
        "mount_id": "collision_front",
        "anchor": "front_edge",
        "offset_m": {"x": 0.0, "y": 0.0, "z": 0.10},
        "rot_deg": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    },
    "lane_invasion": {
        "mount_id": "lane_invasion_center",
        "anchor": "center",
        "offset_m": {"x": 0.0, "y": 0.0, "z": 0.10},
        "rot_deg": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    },
    "obstacle": {
        "mount_id": "obstacle_front",
        "anchor": "front_edge",
        "offset_m": {"x": 0.0, "y": 0.0, "z": 0.10},
        "rot_deg": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    },
}


@dataclass(frozen=True)
class SensorEnvelope:
    """车辆实际外形边界（米，actor 局部坐标）。

    Attributes:
        front_edge_x_m: 车头前缘 X（向前为正）。
        rear_edge_x_m: 车尾后缘 X（通常为负）。
        side_edge_y_m: 侧缘绝对值（平台不臆断 Y 轴左右朝向）。
        roof_z_m: 车顶 Z。
        bottom_z_m: 车底 Z。
        center_x_m: 包围盒中心 X（相对 actor 原点）。
        center_y_m: 包围盒中心 Y。
    """

    front_edge_x_m: float
    rear_edge_x_m: float
    side_edge_y_m: float
    roof_z_m: float
    bottom_z_m: float
    center_x_m: float = 0.0
    center_y_m: float = 0.0

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SensorEnvelope":
        """从平台 ``envelope_m`` / 本地实测字典构造（字段缺失则报错）。"""
        missing = [field for field in _ENVELOPE_FIELDS if field not in data]
        if missing:
            raise ValueError(f"envelope_m 缺少字段: {', '.join(missing)}")
        values = {field: _as_float(data.get(field)) for field in _ENVELOPE_FIELDS}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("envelope_m 含非有限数值")
        return cls(
            **values,
            center_x_m=_as_float(data.get("center_x_m"), 0.0),
            center_y_m=_as_float(data.get("center_y_m"), 0.0),
        )

    @property
    def height_m(self) -> float:
        """车高（车顶 - 车底）。"""
        return max(0.0, self.roof_z_m - self.bottom_z_m)

    @property
    def center_z_m(self) -> float:
        """车身高度中点。"""
        return (self.roof_z_m + self.bottom_z_m) / 2.0

    def to_mapping(self) -> dict[str, float]:
        """转为平台 ``envelope_m`` 同名字典（日志/自检用）。"""
        return {
            "front_edge_x_m": self.front_edge_x_m,
            "rear_edge_x_m": self.rear_edge_x_m,
            "side_edge_y_m": self.side_edge_y_m,
            "roof_z_m": self.roof_z_m,
            "bottom_z_m": self.bottom_z_m,
            "half_length_m": (self.front_edge_x_m - self.rear_edge_x_m) / 2.0,
            "half_width_m": self.side_edge_y_m,
            "half_height_m": self.height_m / 2.0,
        }

    def anchor_location(self, anchor: str) -> tuple[float, float, float]:
        """按锚点名取车辆局部坐标（X 向前、Y 右、Z 向上）。"""
        name = (anchor or "center").strip().lower().replace("-", "_")
        side_z = self.center_z_m + self.height_m * 0.25
        anchors: dict[str, tuple[float, float, float]] = {
            "front_edge": (self.front_edge_x_m, 0.0, self.roof_z_m),
            "rear_edge": (self.rear_edge_x_m, 0.0, self.roof_z_m),
            "front_center": (self.front_edge_x_m, 0.0, self.center_z_m),
            "rear_center": (self.rear_edge_x_m, 0.0, self.center_z_m),
            "side_left": (0.0, -self.side_edge_y_m, side_z),
            "side_right": (0.0, self.side_edge_y_m, side_z),
            "side": (0.0, self.side_edge_y_m, side_z),
            "roof": (0.0, 0.0, self.roof_z_m),
            "bottom": (0.0, 0.0, self.bottom_z_m),
            "center": (self.center_x_m, self.center_y_m, self.center_z_m),
        }
        location = anchors.get(name)
        if location is None:
            raise ValueError(f"未知安装锚点 '{anchor}'（可用: {', '.join(sorted(anchors))}）")
        return location


class SensorMountSpec:
    """单个传感器安装规格（已换算为车辆局部 x/y/z）。

    Attributes:
        mount_id: 安装位置标识。
        sensor_type: 传感器类型。
        x: 纵向位置（米，向前为正，相对 actor 变换原点）。
        y: 横向位置（米，向右为正）。
        z: 垂直位置（米，向上为正）。
        pitch_deg: 传感器俯仰角（度，负值向下）。
        yaw_deg: 传感器偏航角（度）。
        roll_deg: 传感器横滚角（度）。
        anchor: 来源锚点（``front_edge``/``roof`` 等，空表示显式坐标）。
    """

    def __init__(
        self,
        mount_id: str,
        sensor_type: SensorType,
        x: float,
        y: float,
        z: float,
        pitch_deg: float = 0.0,
        yaw_deg: float = 0.0,
        roll_deg: float = 0.0,
        anchor: str = "",
    ) -> None:
        self.mount_id = mount_id
        self.sensor_type = sensor_type
        self.x = x
        self.y = y
        self.z = z
        self.pitch_deg = pitch_deg
        self.yaw_deg = yaw_deg
        self.roll_deg = roll_deg
        self.anchor = anchor

    def to_carla_transform(self) -> Any:
        """转换为 carla.Transform（相对车身坐标系）。"""
        import carla  # noqa: PLC0415
        return carla.Transform(
            carla.Location(x=self.x, y=self.y, z=self.z),
            carla.Rotation(pitch=self.pitch_deg, yaw=self.yaw_deg, roll=self.roll_deg),
        )


# ─── 外形边界解析 ─────────────────────────────────────────────────────────────


def local_envelope(vehicle: Any) -> dict[str, float]:
    """从实际 actor 的蓝图包围盒推导 ``envelope_m``（与平台同一套算术）。

    ``BoundingBox.location`` 是相对 actor 变换原点的盒心偏移，``extent`` 是
    半尺寸（已含后视镜/天线等外扩件）。

    Raises:
        SensorSimulationError: 拿不到包围盒或尺寸非法（绝不回落到写死米数）。
    """
    return _local_envelope(vehicle).to_mapping()


def _read_bounding_box(vehicle: Any) -> Any:
    """读取 Actor 包围盒，兼容 CARLA 不同版本的 API 形态。

    CARLA Python API 的标准形态是 ``actor.bounding_box`` 属性；部分封装/回调
    版本提供 ``get_bounding_box()`` 方法。两者都尝试，避免因 API 形态差异
    导致全部传感器被跳过。

    Raises:
        SensorSimulationError: 两种形态都拿不到包围盒。
    """
    errors: list[str] = []
    for name in ("bounding_box", "get_bounding_box"):
        try:
            candidate = getattr(vehicle, name, None)
        except Exception as exc:  # noqa: BLE001  属性访问可能因 Actor 失效抛错
            errors.append(f"{name} 访问失败: {exc}")
            continue
        if candidate is None:
            continue
        try:
            bounding_box = candidate() if callable(candidate) else candidate
        except Exception as exc:  # noqa: BLE001  CARLA 调用异常统一转仿真异常
            errors.append(f"{name} 读取失败: {exc}")
            continue
        if bounding_box is None:
            errors.append(f"{name} 返回 None")
            continue
        if (
            getattr(bounding_box, "location", None) is None
            or getattr(bounding_box, "extent", None) is None
        ):
            errors.append(f"{name} 缺少 location/extent")
            continue
        return bounding_box

    available = sorted(
        name
        for name in dir(vehicle)
        if "bound" in name.lower() or "box" in name.lower()
    )
    detail = "; ".join(errors) if errors else "未找到 bounding_box / get_bounding_box"
    raise SensorSimulationError(
        "mount",
        "unknown",
        "车辆 Actor 无法读取包围盒，不能按实际外形挂载传感器"
        f"（type_id={getattr(vehicle, 'type_id', '-') or '-'}，"
        f"可用属性={available or '无'}）: {detail}",
    )


def _local_envelope(vehicle: Any) -> SensorEnvelope:
    bounding_box = _read_bounding_box(vehicle)
    location = getattr(bounding_box, "location", None)
    extent = getattr(bounding_box, "extent", None)
    if location is None or extent is None:
        raise SensorSimulationError(
            "mount", "unknown", "bounding_box 缺少 location/extent，无法换算挂载点"
        )
    center_x = float(getattr(location, "x", 0.0) or 0.0)
    center_y = float(getattr(location, "y", 0.0) or 0.0)
    center_z = float(getattr(location, "z", 0.0) or 0.0)
    extent_x = float(getattr(extent, "x", 0.0) or 0.0)
    extent_y = float(getattr(extent, "y", 0.0) or 0.0)
    extent_z = float(getattr(extent, "z", 0.0) or 0.0)
    if extent_x <= 0.0 or extent_y <= 0.0 or extent_z <= 0.0:
        raise SensorSimulationError(
            "mount",
            "unknown",
            f"bounding_box extent 非法: ({extent_x}, {extent_y}, {extent_z})",
        )
    return SensorEnvelope(
        front_edge_x_m=center_x + extent_x,
        rear_edge_x_m=center_x - extent_x,
        side_edge_y_m=abs(center_y) + extent_y,
        roof_z_m=center_z + extent_z,
        bottom_z_m=center_z - extent_z,
        center_x_m=center_x,
        center_y_m=center_y,
    )


def resolve_envelope(
    vehicle: Any, sensor_mount_reference: Optional[dict[str, Any]] = None
) -> SensorEnvelope:
    """解析挂载依据：优先平台 ``resolved``，否则本地实测；并留一行来源日志。"""
    reference = sensor_mount_reference or {}
    status = str(reference.get("status") or "unavailable")
    expected_blueprint = str(reference.get("blueprint") or "")
    actual_blueprint = str(getattr(vehicle, "type_id", "") or "")
    envelope: Optional[SensorEnvelope] = None

    if status == "resolved":
        raw_envelope = reference.get("envelope_m")
        if isinstance(raw_envelope, dict) and raw_envelope:
            try:
                envelope = SensorEnvelope.from_mapping(raw_envelope)
            except ValueError as exc:
                logger.warning(f"mount_ref_invalid status=resolved 但 envelope_m 不可用: {exc}")
        if envelope is not None and expected_blueprint and actual_blueprint:
            if expected_blueprint != actual_blueprint:
                logger.warning(
                    "mount_ref_blueprint_mismatch plat_bp=%s actual_bp=%s -> 以本地实测为准",
                    expected_blueprint,
                    actual_blueprint,
                )
                envelope = None

    if envelope is not None:
        _log_mount_source(
            "platform", status, expected_blueprint, actual_blueprint, envelope
        )
        return envelope

    envelope = _local_envelope(vehicle)
    _log_mount_source("local", status, expected_blueprint, actual_blueprint, envelope)
    return envelope


def _log_mount_source(
    source: str,
    status: str,
    expected_blueprint: str,
    actual_blueprint: str,
    envelope: SensorEnvelope,
) -> None:
    """实例创建时打一行挂载依据（运维据此判断平台/本地来源）。"""
    logger.info(
        "mount source=%s status=%s plat_bp=%s actual_bp=%s roof_z=%.2f front_x=%.2f "
        "rear_x=%.2f side_y=%.2f bottom_z=%.2f",
        source,
        status,
        expected_blueprint or "-",
        actual_blueprint or "-",
        envelope.roof_z_m,
        envelope.front_edge_x_m,
        envelope.rear_edge_x_m,
        envelope.side_edge_y_m,
        envelope.bottom_z_m,
    )


# ─── 配置加载与换算 ───────────────────────────────────────────────────────────


def load_mount_config(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """读取 ``configs/sensor_configs.json`` 的 ``mounts`` 段。

    查找顺序：显式 ``path`` → ``HUNTER_SIM_SENSOR_CONFIGS`` → 仓库 ``configs/``
    → 当前工作目录 ``configs/``。全部缺失时使用内建默认（仅锚点+余量），并告警。
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    env_path = os.environ.get(SENSOR_CONFIG_ENV)
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).resolve().parents[3] / "configs" / "sensor_configs.json")
    candidates.append(Path.cwd() / "configs" / "sensor_configs.json")

    config_path = next((item for item in candidates if item.is_file()), None)
    if config_path is None:
        logger.warning(
            "sensor_configs.json 未找到（%s），使用内建锚点+余量默认值",
            ", ".join(str(item) for item in candidates),
        )
        return _copy_default_mounts()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "load_sensor_configs", f"读取 {config_path} 失败: {exc}"
        ) from exc
    mounts = data.get("mounts") if isinstance(data, dict) else None
    if not isinstance(mounts, dict) or not mounts:
        raise ConfigurationError(
            "load_sensor_configs", f"{config_path} 缺少有效的 mounts 段"
        )
    merged = _copy_default_mounts()
    merged.update({str(key): value for key, value in mounts.items() if isinstance(value, dict)})
    return merged


def _copy_default_mounts() -> dict[str, dict[str, Any]]:
    """深拷贝内建默认 mounts（避免调用方原地修改）。"""
    return cast(dict[str, dict[str, Any]], json.loads(json.dumps(_DEFAULT_MOUNTS)))


def compute_local_transform(
    envelope: SensorEnvelope | dict[str, Any], mount_spec: dict[str, Any]
) -> Any:
    """按「锚点 + 安装余量」计算单个传感器的 carla.Transform。"""
    model = (
        envelope if isinstance(envelope, SensorEnvelope) else SensorEnvelope.from_mapping(envelope)
    )
    spec = _mount_spec_from_config("mount", mount_spec, model)
    return spec.to_carla_transform()


def resolve_mount_transforms(
    scene_config: dict[str, Any],
    vehicle: Any,
    mounts_cfg: list[dict[str, Any]] | dict[str, dict[str, Any]],
) -> list[Any]:
    """挂载点统一入口：优先平台依据，其次本地实测。

    Args:
        scene_config: 完整场景配置或 ``sensor_mount_reference`` 本身。
        vehicle: 已 spawn 的车辆 Actor。
        mounts_cfg: ``sensor_configs.json`` 的 ``mounts`` 段（映射或列表）。

    Returns:
        ``carla.Transform`` 列表（与 ``mounts_cfg`` 顺序一致）。
    """
    reference = (
        scene_config.get("sensor_mount_reference")
        if isinstance(scene_config.get("sensor_mount_reference"), dict)
        else scene_config
    )
    envelope = resolve_envelope(vehicle, reference if isinstance(reference, dict) else None)
    specs = (
        [value for value in mounts_cfg.values() if isinstance(value, dict)]
        if isinstance(mounts_cfg, dict)
        else [value for value in mounts_cfg if isinstance(value, dict)]
    )
    return [compute_local_transform(envelope, spec) for spec in specs]


def _mount_spec_from_config(
    key: str, section: dict[str, Any], envelope: SensorEnvelope
) -> SensorMountSpec:
    """把一条 mounts 配置换算为 SensorMountSpec（锚点优先，兼容显式 x/y/z）。"""
    sensor_type = _MOUNT_KEY_TO_TYPE.get(key)
    if sensor_type is None:
        raise ConfigurationError("mount_config", f"未知传感器安装键 '{key}'")
    mount_id = str(section.get("mount_id") or key)
    rotation = _as_mapping(section.get("rot_deg"))
    pitch = _as_float(rotation.get("pitch"), _as_float(section.get("pitch")))
    yaw = _as_float(rotation.get("yaw"), _as_float(section.get("yaw")))
    roll = _as_float(rotation.get("roll"), _as_float(section.get("roll")))

    anchor = str(section.get("anchor") or "").strip()
    if anchor:
        offset = _as_mapping(section.get("offset_m"))
        anchor_x, anchor_y, anchor_z = envelope.anchor_location(anchor)
        return SensorMountSpec(
            mount_id=mount_id,
            sensor_type=sensor_type,
            x=anchor_x + _as_float(offset.get("x")),
            y=anchor_y + _as_float(offset.get("y")),
            z=anchor_z + _as_float(offset.get("z")),
            pitch_deg=pitch,
            yaw_deg=yaw,
            roll_deg=roll,
            anchor=anchor,
        )

    # 显式局部坐标：尊重运维配置（不是代码写死的车身尺寸）
    return SensorMountSpec(
        mount_id=mount_id,
        sensor_type=sensor_type,
        x=_as_float(section.get("x")),
        y=_as_float(section.get("y")),
        z=_as_float(section.get("z")),
        pitch_deg=pitch,
        yaw_deg=yaw,
        roll_deg=roll,
    )


def _build_mount_specs(
    mounts_config: dict[str, Any], envelope: SensorEnvelope
) -> list[SensorMountSpec]:
    """构造全部安装规格；未知键跳过并告警，避免一个笔误阻断全部传感器。"""
    specs: list[SensorMountSpec] = []
    for key, section in mounts_config.items():
        if not isinstance(section, dict):
            logger.warning(f"mount_config_invalid key={key}: 配置项不是对象")
            continue
        try:
            specs.append(_mount_spec_from_config(str(key), section, envelope))
        except ConfigurationError as exc:
            logger.warning(f"mount_config_skipped key={key}: {exc}")
    return specs


def _as_mapping(value: Any) -> dict[str, Any]:
    """安全转字典。"""
    return value if isinstance(value, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    """安全转浮点。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SensorMountManager:
    """传感器安装位置管理器（按实际蓝图外形边界换算）。

    Args:
        vehicle_actor: 已 spawn 的 CARLA Vehicle Actor。
        custom_mounts: 显式安装规格（测试/特殊车辆覆盖；给出时不做 envelope 换算）。
        sensor_mount_reference: 平台下发的 ``sensor_mount_reference``。
        mounts_config: 可选的自定义 mounts 配置（缺省读 ``sensor_configs.json``）。
    """

    def __init__(
        self,
        vehicle_actor: Any,
        custom_mounts: Optional[list[SensorMountSpec]] = None,
        sensor_mount_reference: Optional[dict[str, Any]] = None,
        mounts_config: Optional[dict[str, Any]] = None,
    ) -> None:
        self._vehicle = vehicle_actor
        self._sensor_mount_reference = sensor_mount_reference
        self._mounts_config = mounts_config
        self._envelope: Optional[SensorEnvelope] = None
        self._mounts: list[SensorMountSpec] = []
        self._attached: dict[str, Any] = {}  # mount_id -> sensor actor
        self._mounts_ready = False
        if custom_mounts is not None:
            self._mounts = list(custom_mounts)
            self._mounts_ready = True
        else:
            self._ensure_mounts()

    @property
    def envelope(self) -> Optional[SensorEnvelope]:
        """本次实例使用的车辆外形边界（自定义 mounts 时为 None）。"""
        return self._envelope

    def _ensure_mounts(self) -> None:
        """解析外形边界并换算全部安装位（实例创建时只算一次）。"""
        if self._mounts_ready:
            return
        self._envelope = resolve_envelope(self._vehicle, self._sensor_mount_reference)
        raw_config = self._mounts_config
        if raw_config is None:
            mounts = load_mount_config()
        elif isinstance(raw_config.get("mounts"), dict):
            mounts = raw_config["mounts"]
        else:
            mounts = raw_config
        self._mounts = _build_mount_specs(mounts, self._envelope)
        self._mounts_ready = True
        logger.info(
            "mount_specs_ready count=%d mounts=%s",
            len(self._mounts),
            ",".join(spec.mount_id for spec in self._mounts),
        )

    def get_mount(self, sensor_type: SensorType) -> Optional[SensorMountSpec]:
        """按传感器类型查找第一个安装位置。"""
        self._ensure_mounts()
        for spec in self._mounts:
            if spec.sensor_type == sensor_type:
                return spec
        return None

    def attach_sensor(
        self,
        sensor_type: SensorType,
        blueprint: Any,
        world: Any,
    ) -> Any:
        """在车辆上挂载指定类型的传感器。

        Args:
            sensor_type: 传感器类型。
            blueprint: 已配置的 ActorBlueprint。
            world: CARLA World 对象。

        Returns:
            生成的传感器 Actor。

        Raises:
            SensorSimulationError: 找不到安装位置或生成失败。
        """
        mount = self.get_mount(sensor_type)
        if mount is None:
            raise SensorSimulationError(
                sensor_type.value, "unknown", f"No mount position for {sensor_type.value}"
            )

        try:
            tf = mount.to_carla_transform()
            sensor_actor = world.spawn_actor(blueprint, tf, attach_to=self._vehicle)
            self._attached[mount.mount_id] = sensor_actor
            logger.info(
                "Sensor '%s' attached at mount '%s' (x=%.2f y=%.2f z=%.2f pitch=%.1f)",
                sensor_type.value,
                mount.mount_id,
                mount.x,
                mount.y,
                mount.z,
                mount.pitch_deg,
            )
            return sensor_actor
        except Exception as exc:  # noqa: BLE001  CARLA spawn 异常统一转仿真异常
            raise SensorSimulationError(
                sensor_type.value, mount.mount_id, str(exc)
            ) from exc

    def detach_sensor(self, sensor_type: SensorType) -> None:
        """卸载指定类型的传感器（销毁 Actor）。"""
        for spec in self._mounts:
            if spec.sensor_type == sensor_type and spec.mount_id in self._attached:
                actor = self._attached.pop(spec.mount_id)
                try:
                    actor.stop()
                    actor.destroy()
                    logger.info(f"Sensor '{sensor_type.value}' detached from '{spec.mount_id}'")
                except Exception as exc:  # noqa: BLE001  外部可能已销毁
                    logger.warning(f"Sensor detach error: {exc}")

    def detach_all(self) -> None:
        """卸载所有已挂载传感器（场景销毁时调用）。"""
        for mount_id, actor in list(self._attached.items()):
            try:
                actor.stop()
                actor.destroy()
            except Exception as exc:  # noqa: BLE001  外部可能已销毁
                logger.warning(f"detach_all error for '{mount_id}': {exc}")
        self._attached.clear()
        logger.info("All sensors detached")

    @property
    def attached_count(self) -> int:
        """当前已挂载传感器数量。"""
        return len(self._attached)


__all__ = [
    "SENSOR_CONFIG_ENV",
    "SensorEnvelope",
    "SensorMountManager",
    "SensorMountSpec",
    "compute_local_transform",
    "load_mount_config",
    "local_envelope",
    "resolve_envelope",
    "resolve_mount_transforms",
]

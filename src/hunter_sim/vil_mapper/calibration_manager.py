"""VIL 标定参数管理（PROMPT-ENG-002-B）。

管理实车启动时的初始标定参数，支持持久化保存和加载。
标定参数决定了实车 odom 坐标系与 CARLA 地图坐标系之间的变换关系。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.coordinate_converter import CalibrationParams

logger = get_logger(__name__)

_DEFAULT_CALIB_FILE = Path("data/calibration/vil_calibration.json")


class CalibrationManager:
    """VIL 标定参数持久化管理器。

    Args:
        calib_file_path: 标定文件存储路径。
    """

    def __init__(self, calib_file_path: Path = _DEFAULT_CALIB_FILE) -> None:
        self._path: Path = calib_file_path
        self._current: Optional[CalibrationParams] = None

    def load(self) -> CalibrationParams:
        """从文件加载标定参数。

        文件不存在时返回默认参数（原点出发，航向对齐 X 轴），并写入文件。

        Returns:
            CalibrationParams 标定参数。
        """
        if self._current is not None:
            return self._current

        if not self._path.exists():
            logger.warning(f"Calibration file not found: {self._path}, using defaults")
            self._current = CalibrationParams()
            self.save(self._current)
            return self._current

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._current = CalibrationParams(**raw)
            logger.info(
                f"Calibration loaded from {self._path}: "
                f"x0={self._current.x0:.2f}, y0={self._current.y0:.2f}, "
                f"yaw0={self._current.yaw0:.4f}rad"
            )
            return self._current
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ConfigurationError(
                "CalibrationManager.load",
                f"Failed to parse calibration file {self._path}: {exc}",
            ) from exc

    def save(self, params: CalibrationParams) -> None:
        """保存标定参数到文件。

        Args:
            params: 要保存的标定参数。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            params.model_dump_json(indent=2),
            encoding="utf-8",
        )
        self._current = params
        logger.info(f"Calibration saved to {self._path}")

    def update(self, params: CalibrationParams) -> None:
        """更新内存中的标定参数（不写文件）。

        Args:
            params: 新标定参数。
        """
        self._current = params
        logger.info("Calibration params updated in memory")

    def reset_to_default(self) -> CalibrationParams:
        """重置为默认标定参数（原点，0 航向）。

        Returns:
            默认 CalibrationParams。
        """
        default = CalibrationParams()
        self._current = default
        logger.info("Calibration reset to defaults (x0=0, y0=0, yaw0=0)")
        return default

    @property
    def current(self) -> Optional[CalibrationParams]:
        """当前标定参数（未加载时为 None）。"""
        return self._current

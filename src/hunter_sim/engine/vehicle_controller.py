"""车辆控制器模块：VIL 和 SIL 双模式统一接口（PROMPT-ENG-001-C）。

本模块作为 hunter_se_vehicle.py 的补充，提供控制器工厂方法，
便于服务层根据仿真模式选择合适的控制器实现。
"""

from __future__ import annotations

from typing import Optional, Protocol

from hunter_sim.common.models import SimMode
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.hunter_se_vehicle import (
    CarlaVehicleActorProtocol,
    HunterSEParameters,
    HunterSEVehicleController,
    HunterSESILController,
    VehicleControllerProtocol,
)

logger = get_logger(__name__)


class VehicleControllerFactory:
    """车辆控制器工厂：根据仿真模式创建对应控制器实例。

    Args:
        params: HUNTER SE 物理参数，默认使用标准规格。
    """

    def __init__(self, params: Optional[HunterSEParameters] = None) -> None:
        self._params: HunterSEParameters = params or HunterSEParameters()

    def create(
        self,
        mode: SimMode,
        vehicle_actor: CarlaVehicleActorProtocol,
    ) -> object:
        """根据模式创建对应控制器。

        Args:
            mode: 仿真运行模式。
            vehicle_actor: CARLA Vehicle Actor。

        Returns:
            VIL 模式返回 HunterSEVehicleController，
            SIL 模式返回 HunterSESILController。

        Raises:
            ValueError: 不支持的仿真模式。
        """
        if mode == SimMode.VIL:
            logger.info("Creating VIL controller (direct pose control mode)")
            return HunterSEVehicleController(vehicle_actor, self._params)
        elif mode == SimMode.SIL:
            logger.info("Creating SIL controller (physics-driven mode)")
            return HunterSESILController(vehicle_actor, self._params)
        else:
            raise ValueError(f"Unsupported mode for vehicle controller: {mode}")

"""CARLA 管理面适配器（HunterCore ↔ HunterSim 控制面）。

HunterCore 通过本适配器（默认 ``127.0.0.1:8090``）创建/下发/查询仿真实例，
适配器把平台 ``scene_config`` 映射为内部 ``SceneConfig``，复用执行面
（``ExecutionPlane``）、场景运行器与 VIL 映射链路，串行执行单个 CARLA 世界。
"""

from __future__ import annotations

from hunter_sim.platform_adapter.config import AdapterSettings

__all__ = ["AdapterSettings"]

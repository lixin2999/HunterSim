"""模块 1.3：场景管理器实现。

负责地图加载、天气/时间设置、交通流（NPC 车辆与行人）生成，以及场景运行
生命周期状态机（IDLE→INITIALIZING→RUNNING→PAUSED→COMPLETED/FAILED/ABORTED）
与事件钩子（on_start / on_tick / on_end）。

依赖注入：连接管理器、车辆控制器、事件总线均以 Protocol 注入，便于单测。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any

import carla

from hunter_sim.core.config import ScenarioConfig
from hunter_sim.core.contracts import VehicleState
from hunter_sim.core.events import (
    ScenarioEndedEvent,
    ScenarioStartedEvent,
    SimulationTickEvent,
)
from hunter_sim.core.exceptions import ConfigurationError, SimulationError
from hunter_sim.core.logging import logger
from hunter_sim.core.protocols import EventBus
from hunter_sim.simulation.models import Location, Rotation, Transform
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager,
    ScenarioState,
    VehicleController,
)
from hunter_sim.simulation.weather import WeatherParameters, resolve_preset

_VALID_HOOKS = frozenset({"on_start", "on_tick", "on_end"})

# 合法状态转移表。
_TRANSITIONS: dict[ScenarioState, frozenset[ScenarioState]] = {
    ScenarioState.IDLE: frozenset({ScenarioState.INITIALIZING}),
    ScenarioState.INITIALIZING: frozenset(
        {ScenarioState.RUNNING, ScenarioState.FAILED, ScenarioState.ABORTED}
    ),
    ScenarioState.RUNNING: frozenset(
        {
            ScenarioState.PAUSED,
            ScenarioState.COMPLETED,
            ScenarioState.FAILED,
            ScenarioState.ABORTED,
            ScenarioState.IDLE,
        }
    ),
    ScenarioState.PAUSED: frozenset(
        {ScenarioState.RUNNING, ScenarioState.COMPLETED, ScenarioState.ABORTED, ScenarioState.IDLE}
    ),
    ScenarioState.COMPLETED: frozenset({ScenarioState.IDLE}),
    ScenarioState.FAILED: frozenset({ScenarioState.IDLE}),
    ScenarioState.ABORTED: frozenset({ScenarioState.IDLE}),
}


class ScenarioManagerImpl:
    """满足 :class:`~hunter_sim.simulation.protocols.ScenarioManager` 契约。"""

    def __init__(
        self,
        config: ScenarioConfig,
        connection: CarlaConnectionManager,
        vehicle: VehicleController,
        event_bus: EventBus,
        *,
        rng: random.Random | None = None,
    ) -> None:
        """初始化场景管理器。

        Args:
            config: 场景配置（地图 / 车辆 / 传感器 / 天气 / 交通 / 输出）。
            connection: CARLA 连接管理器。
            vehicle: 主车控制器。
            event_bus: 事件总线，用于发布 tick / 起止事件。
            rng: 随机源（可注入以便测试确定性）。
        """
        self._config = config
        self._connection = connection
        self._vehicle = vehicle
        self._bus = event_bus
        self._rng = rng or random.Random()
        self._state = ScenarioState.IDLE
        self._hooks: dict[str, list[Callable[[], None]]] = {name: [] for name in _VALID_HOOKS}
        self._npc_actors: list[Any] = []
        self._tick = 0
        self._sequence = 0

    @property
    def state(self) -> ScenarioState:
        """当前场景状态机状态。"""
        return self._state

    @property
    def tick_count(self) -> int:
        """已推进的帧数。"""
        return self._tick

    def _transition(self, new_state: ScenarioState) -> None:
        if new_state not in _TRANSITIONS[self._state]:
            raise SimulationError(f"非法状态转移: {self._state.value} -> {new_state.value}")
        self._state = new_state

    def _next_sequence(self) -> int:
        seq = self._sequence
        self._sequence += 1
        return seq

    def register_hook(self, name: str, hook: Callable[[], None]) -> None:
        """注册事件钩子。

        Args:
            name: ``on_start`` / ``on_tick`` / ``on_end`` 之一。
            hook: 无参回调。

        Raises:
            ConfigurationError: 钩子名非法。
        """
        if name not in _VALID_HOOKS:
            raise ConfigurationError(f"未知钩子: {name}，可选 {sorted(_VALID_HOOKS)}")
        self._hooks[name].append(hook)

    def _run_hooks(self, name: str) -> None:
        for hook in self._hooks[name]:
            try:
                hook()
            except Exception as exc:  # 钩子异常不得中断场景
                logger.bind(component="scenario").error("钩子 {} 执行失败: {}", name, exc)

    @staticmethod
    def _carla_to_transform(tf: Any) -> Transform:
        return Transform(
            location=Location(
                x=float(tf.location.x), y=float(tf.location.y), z=float(tf.location.z)
            ),
            rotation=Rotation(
                pitch=float(tf.rotation.pitch),
                yaw=float(tf.rotation.yaw),
                roll=float(tf.rotation.roll),
            ),
        )

    async def configure(self) -> None:
        """加载地图、应用天气与步进设置、生成主车与交通流。

        Raises:
            SimulationError: 当前状态不允许配置。
            ConfigurationError: 生成点不足或配置非法。
        """
        self._transition(ScenarioState.INITIALIZING)
        map_name = self._config.scenario.map
        await self._connection.load_world(map_name)
        world = self._connection.get_world()

        await asyncio.to_thread(self._apply_weather, world)
        await asyncio.to_thread(self._apply_tick_settings, world)

        spawn_points = world.get_map().get_spawn_points()
        index = self._config.vehicle.spawn_point_index
        if not spawn_points or index >= len(spawn_points):
            raise ConfigurationError(f"生成点不足: 需要索引 {index}，可用 {len(spawn_points)} 个")
        ego_transform = self._carla_to_transform(spawn_points[index])
        await self._vehicle.spawn(self._config.vehicle.blueprint, ego_transform, autopilot=False)

        await asyncio.to_thread(self._spawn_traffic, world)
        logger.bind(component="scenario").info(
            "场景配置完成: map={} ego={}", map_name, ego_transform.location
        )

    def _resolve_weather(self) -> WeatherParameters:
        """将配置的天气解析为引擎层参数（§3.4）。

        指定 ``preset`` 时以预设注册表为准（未知预设抛
        :class:`~hunter_sim.core.exceptions.ConfigurationError`）；
        否则由显式数值字段构建。
        """
        weather = self._config.weather
        if weather.preset:
            return resolve_preset(weather.preset)
        return WeatherParameters(
            cloudiness=weather.cloudiness,
            precipitation=weather.precipitation,
            precipitation_deposits=weather.precipitation_deposits,
            wind_intensity=weather.wind_intensity,
            sun_azimuth_angle=weather.sun_azimuth_angle,
            sun_altitude_angle=weather.sun_altitude_angle,
        )

    def _apply_weather(self, world: Any) -> None:
        params = self._resolve_weather()
        world.set_weather(
            carla.WeatherParameters(
                cloudiness=params.cloudiness,
                precipitation=params.precipitation,
                precipitation_deposits=params.precipitation_deposits,
                wind_intensity=params.wind_intensity,
                sun_azimuth_angle=params.sun_azimuth_angle,
                sun_altitude_angle=params.sun_altitude_angle,
            )
        )
        logger.bind(component="scenario").debug(
            "已应用天气: preset={} cloudiness={} precipitation={} sun_altitude={}",
            self._config.weather.preset,
            params.cloudiness,
            params.precipitation,
            params.sun_altitude_angle,
        )

    def _apply_tick_settings(self, world: Any) -> None:
        """根据 ``simulation_mode`` 配置 CARLA 世界步进（``WorldSettings``）。

        - 同步模式（VIL 实时）：启用 ``synchronous_mode``，固定步长由客户端 ``world.tick()`` 驱动；
          按配置启用物理子步以保证稳定性。
        - 异步模式（回放 / SIL）：关闭 ``synchronous_mode``，``fixed_delta_seconds`` 置 ``None``
          表示可变步长（按真实时间推进）。
        """
        mode = self._config.scenario.simulation_mode
        settings = world.get_settings()
        settings.synchronous_mode = mode.is_synchronous
        if mode.is_synchronous:
            # 未显式指定步长时由 tick_rate 推算（1/tick_rate），与实车控制频率对齐。
            settings.fixed_delta_seconds = (
                mode.fixed_delta_seconds
                if mode.fixed_delta_seconds is not None
                else 1.0 / self._config.scenario.tick_rate
            )
            settings.substepping = mode.substepping
            settings.max_substep_delta_time = mode.max_substep_delta_time
            settings.max_substeps = mode.max_substeps
        else:
            # 异步模式：可变步长，子步设置对推进无实质影响，保留默认。
            settings.fixed_delta_seconds = None
        world.apply_settings(settings)
        logger.bind(component="scenario").debug(
            "已应用步进设置: mode={} fixed_delta={} substepping={}",
            mode.mode,
            settings.fixed_delta_seconds,
            settings.substepping,
        )

    def _spawn_traffic(self, world: Any) -> None:
        traffic = self._config.traffic
        tm_port = traffic.traffic_manager_port
        self._spawn_npc_vehicles(world, traffic.npc_vehicles, tm_port)
        self._spawn_walkers(world, traffic.npc_walkers)

    def _spawn_npc_vehicles(self, world: Any, count: int, tm_port: int) -> None:
        if count <= 0:
            return
        blueprints = list(world.get_blueprint_library().filter("vehicle.*"))
        points = list(world.get_map().get_spawn_points())
        if not blueprints or not points:
            logger.bind(component="scenario").warning("缺少车辆蓝图或生成点，跳过 NPC 车辆")
            return
        for _ in range(count):
            bp = self._rng.choice(blueprints)
            tf = self._rng.choice(points)
            actor = world.try_spawn_actor(bp, tf)
            if actor is not None:
                actor.set_autopilot(True, tm_port)
                self._npc_actors.append(actor)

    def _spawn_walkers(self, world: Any, count: int) -> None:
        if count <= 0:
            return
        blueprints = list(world.get_blueprint_library().filter("walker.*"))
        points = list(world.get_map().get_spawn_points())
        if not blueprints or not points:
            logger.bind(component="scenario").warning("缺少行人蓝图或生成点，跳过 NPC 行人")
            return
        for _ in range(count):
            bp = self._rng.choice(blueprints)
            tf = self._rng.choice(points)
            actor = world.try_spawn_actor(bp, tf)
            if actor is not None:
                self._npc_actors.append(actor)

    async def start(self) -> None:
        """启动场景运行。"""
        if self._state == ScenarioState.RUNNING:
            return
        self._transition(ScenarioState.RUNNING)
        world = self._connection.get_world()
        self._bus.publish(
            ScenarioStartedEvent(
                sequence=self._next_sequence(),
                timestamp=float(world.get_snapshot().timestamp),
                scenario_name=self._config.scenario.name,
                map_name=self._config.scenario.map,
                run_id=self._config.scenario.name,
            )
        )
        self._run_hooks("on_start")
        logger.bind(component="scenario").info("场景已启动: {}", self._config.scenario.name)

    async def pause(self) -> None:
        """暂停场景。"""
        self._transition(ScenarioState.PAUSED)

    async def resume(self) -> None:
        """从暂停恢复运行。"""
        self._transition(ScenarioState.RUNNING)

    async def step(self) -> VehicleState:
        """推进一帧：tick 世界、发布 tick 事件、触发 on_tick 钩子并返回主车状态。

        Raises:
            SimulationError: 场景未处于运行态。
        """
        if self._state != ScenarioState.RUNNING:
            raise SimulationError(f"step() 需在 RUNNING 状态调用，当前为 {self._state.value}")
        world = self._connection.get_world()
        await asyncio.to_thread(world.tick)
        self._tick += 1
        timestamp = float(world.get_snapshot().timestamp)
        self._bus.publish(
            SimulationTickEvent(
                sequence=self._next_sequence(),
                timestamp=timestamp,
                tick=self._tick,
                delta_seconds=1.0 / self._config.scenario.tick_rate,
            )
        )
        self._run_hooks("on_tick")
        return self._vehicle.get_state()

    def _cleanup_actors(self) -> None:
        for actor in self._npc_actors:
            try:
                actor.destroy()
            except Exception as exc:  # 单个 NPC 销毁失败不影响整体清理
                logger.bind(component="scenario").warning("NPC 销毁失败: {}", exc)
        self._npc_actors.clear()

    async def stop(self, *, status: str = "completed") -> None:
        """结束场景：清理 NPC 与主车、发布结束事件、触发 on_end 钩子。

        Args:
            status: 结束状态，``completed`` / ``failed`` / ``aborted``。
        """
        target = {
            "completed": ScenarioState.COMPLETED,
            "failed": ScenarioState.FAILED,
            "aborted": ScenarioState.ABORTED,
        }.get(status)
        if target is None:
            raise ConfigurationError(f"非法结束状态: {status}")

        self._cleanup_actors()
        await self._vehicle.destroy()
        if self._state != target:
            self._transition(target)

        timestamp = float(self._connection.get_world().get_snapshot().timestamp)
        self._bus.publish(
            ScenarioEndedEvent(
                sequence=self._next_sequence(),
                timestamp=timestamp,
                scenario_name=self._config.scenario.name,
                run_id=self._config.scenario.name,
                status=status,
                total_frames=self._tick,
            )
        )
        self._run_hooks("on_end")
        logger.bind(component="scenario").info(
            "场景已停止: status={} frames={}", status, self._tick
        )

    async def reset(self) -> None:
        """清理并回到可重新配置的空闲状态。"""
        self._cleanup_actors()
        await self._vehicle.destroy()
        self._tick = 0
        self._state = ScenarioState.IDLE


__all__ = ["ScenarioManagerImpl"]

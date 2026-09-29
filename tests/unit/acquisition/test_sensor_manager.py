"""模块 2.1 传感器管理的单元测试（CARLA 世界/演员全部 mock）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hunter_sim.acquisition.buffer import BufferRegistryImpl
from hunter_sim.acquisition.sensor_manager import SensorManagerImpl
from hunter_sim.core.config import SensorConfig
from hunter_sim.core.exceptions import SensorSimulationError


def make_cfg(sensor_id: str, sensor_type: str) -> SensorConfig:
    return SensorConfig(
        id=sensor_id,
        type=sensor_type,
        position=(0.0, 1.0, 2.0),
        rotation=(0.0, 0.0, 0.0),
        tick_rate=10.0,
        width=8,
        height=8,
        fov=90.0,
        channels=16,
        range=50.0,
        points_per_second=1000000,
        rotation_frequency=10.0,
    )


@pytest.fixture()
def world_setup() -> tuple[MagicMock, list[MagicMock]]:
    """构造 mock CARLA world：蓝图 find 返回支持属性的蓝图，spawn 追加独立传感器演员。"""
    world = MagicMock()

    def _find(alias: str) -> MagicMock:
        bp = MagicMock(name=f"bp:{alias}")
        bp.has_attribute.return_value = True
        return bp

    world.get_blueprint_library.return_value.find.side_effect = _find

    sensors: list[MagicMock] = []

    def _spawn(bp: MagicMock, transform: object, attach_to: object = None) -> MagicMock:
        sensor = MagicMock(name="sensor")
        sensors.append(sensor)
        return sensor

    world.spawn_actor.side_effect = _spawn
    return world, sensors


async def test_initialize_spawns_all(world_setup: tuple[MagicMock, list[MagicMock]]) -> None:
    registry = BufferRegistryImpl(asyncio.get_running_loop())
    mgr = SensorManagerImpl(registry, asyncio.get_running_loop())
    world, sensors = world_setup
    await mgr.initialize(
        [make_cfg("cam", "camera.rgb"), make_cfg("lidar", "lidar.ray_cast")],
        vehicle=MagicMock(),
        world=world,
    )
    assert mgr.sensor_ids == ["cam", "lidar"]
    assert len(sensors) == 2
    world.get_blueprint_library.return_value.find.assert_any_call("sensor.camera.rgb")
    sensors[0].listen.assert_called_once()


async def test_initialize_unsupported_type(
    world_setup: tuple[MagicMock, list[MagicMock]],
) -> None:
    registry = BufferRegistryImpl(asyncio.get_running_loop())
    mgr = SensorManagerImpl(registry, asyncio.get_running_loop())
    world, _ = world_setup
    with pytest.raises(SensorSimulationError):
        await mgr.initialize([make_cfg("x", "bogus.type")], vehicle=MagicMock(), world=world)


async def test_gate_blocks_until_started(
    world_setup: tuple[MagicMock, list[MagicMock]],
) -> None:
    loop = asyncio.get_running_loop()
    registry = BufferRegistryImpl(loop)
    mgr = SensorManagerImpl(registry, loop)
    world, sensors = world_setup
    await mgr.initialize([make_cfg("cam", "camera.rgb")], vehicle=MagicMock(), world=world)
    callback = sensors[0].listen.call_args[0][0]
    measurement = SimpleNamespace(timestamp=1.0)

    # 未启动：数据被门控丢弃，不创建缓冲
    callback(measurement)
    await asyncio.sleep(0)
    assert registry.get("cam") is None

    # 启动后：投递到缓冲并触发用户回调
    received: list[object] = []
    mgr.register_callback("cam", received.append)
    await mgr.start_all()
    callback(measurement)
    buf = registry.get("cam")
    assert buf is not None
    got = await buf.get(timeout=1.0)
    assert got is measurement
    assert received == [measurement]


async def test_stop_gates_again(world_setup: tuple[MagicMock, list[MagicMock]]) -> None:
    loop = asyncio.get_running_loop()
    registry = BufferRegistryImpl(loop)
    mgr = SensorManagerImpl(registry, loop)
    world, sensors = world_setup
    await mgr.initialize([make_cfg("cam", "camera.rgb")], vehicle=MagicMock(), world=world)
    callback = sensors[0].listen.call_args[0][0]
    await mgr.start_all()
    callback(SimpleNamespace(timestamp=1.0))
    buf = registry.get("cam")
    assert buf is not None
    await buf.get(timeout=1.0)

    await mgr.stop_all()
    before = buf.history_size()
    callback(SimpleNamespace(timestamp=2.0))
    await asyncio.sleep(0)
    assert buf.history_size() == before  # 停止后不再写入环形缓冲


async def test_start_unknown_sensor(world_setup: tuple[MagicMock, list[MagicMock]]) -> None:
    loop = asyncio.get_running_loop()
    mgr = SensorManagerImpl(BufferRegistryImpl(loop), loop)
    world, _ = world_setup
    await mgr.initialize([make_cfg("cam", "camera.rgb")], vehicle=MagicMock(), world=world)
    with pytest.raises(SensorSimulationError):
        await mgr.start_sensor("ghost")


async def test_callback_exception_isolated(
    world_setup: tuple[MagicMock, list[MagicMock]],
) -> None:
    loop = asyncio.get_running_loop()
    registry = BufferRegistryImpl(loop)
    mgr = SensorManagerImpl(registry, loop)
    world, sensors = world_setup
    await mgr.initialize([make_cfg("cam", "camera.rgb")], vehicle=MagicMock(), world=world)

    def boom(_measurement: object) -> None:
        raise RuntimeError("callback failure")

    mgr.register_callback("cam", boom)
    await mgr.start_all()
    callback = sensors[0].listen.call_args[0][0]
    measurement = SimpleNamespace(timestamp=1.0)
    callback(measurement)  # 用户回调异常不应向外抛出
    buf = registry.get("cam")
    assert buf is not None
    assert (await buf.get(timeout=1.0)) is measurement


async def test_destroy_all_stops_and_clears(
    world_setup: tuple[MagicMock, list[MagicMock]],
) -> None:
    loop = asyncio.get_running_loop()
    mgr = SensorManagerImpl(BufferRegistryImpl(loop), loop)
    world, sensors = world_setup
    await mgr.initialize([make_cfg("cam", "camera.rgb")], vehicle=MagicMock(), world=world)
    await mgr.destroy_all()
    assert mgr.sensor_ids == []
    assert sensors[0].stop.called
    assert sensors[0].destroy.called


async def test_destroy_all_best_effort(world_setup: tuple[MagicMock, list[MagicMock]]) -> None:
    loop = asyncio.get_running_loop()
    mgr = SensorManagerImpl(BufferRegistryImpl(loop), loop)
    world, sensors = world_setup
    await mgr.initialize([make_cfg("cam", "camera.rgb")], vehicle=MagicMock(), world=world)
    sensors[0].stop.side_effect = RuntimeError("stop failed")
    await mgr.destroy_all()  # 停止失败仍应尽力销毁且不抛出
    assert mgr.sensor_ids == []
    assert sensors[0].destroy.called

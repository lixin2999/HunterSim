"""模块 5.3：命令行入口（惰性依赖 ``typer``）。

核心采集逻辑 :func:`collect` 为纯异步函数，不依赖 CLI 库，可单独测试；仅在
:func:`_build_app` 内惰性导入 ``typer``。模块通过 PEP 562 ``__getattr__`` 暴露
``app``，使控制台脚本 ``huntersim=hunter_sim.app.cli:app`` 在未安装可选依赖 ``cli``
时导入本模块不致失败——仅在实际访问 ``app``（即运行 CLI）时才要求 ``typer``。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hunter_sim.app.bootstrap import build_container
from hunter_sim.app.protocols import Orchestrator
from hunter_sim.app.vil.protocols import VILEngine
from hunter_sim.core.config import ConnectionConfig, load_scenario_config
from hunter_sim.core.logging import configure_logging, logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期导入
    from hunter_sim.app.models import RunResult
    from hunter_sim.core.config import ScenarioConfig

# 避免顶层导入重型依赖；仅使用 Protocol 类型作为容器键。
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager as _ConnectionProtocolType,
)
from hunter_sim.simulation.protocols import ScenarioManager as _ScenarioProtocolType


async def collect(
    config: ScenarioConfig,
    *,
    run_id: str | None = None,
    max_ticks: int | None = None,
    connection_config: ConnectionConfig | None = None,
) -> RunResult:
    """装配容器并执行一次端到端采集运行，返回聚合结果。

    Args:
        config: 已校验的场景配置。
        run_id: 运行 ID；``None`` 时自动生成。
        max_ticks: 最大步进 tick 数；``None`` 时按场景时长推算。
        connection_config: CARLA 连接参数。

    Returns:
        编排器产出的 :class:`~hunter_sim.app.models.RunResult`。
    """
    identifier = run_id or uuid4().hex
    with logger.contextualize(run_id=identifier):
        container = build_container(config, run_id=identifier, connection_config=connection_config)
        interface: type[Any] = Orchestrator
        orchestrator: Orchestrator = container.resolve(interface)
        return await orchestrator.run(max_ticks=max_ticks)


async def run_vil(
    config: ScenarioConfig,
    *,
    run_id: str | None = None,
    max_ticks: int | None = None,
    connection_config: ConnectionConfig | None = None,
) -> int:
    """以 VIL 实车在环模式装配容器并驱动主循环，返回完成 tick 数。

    Args:
        config: 需预先将 ``vil.enabled=True`` 且提供 calibration。
        run_id: 运行 ID。
        max_ticks: 最大 tick；``None`` 时按 ``duration_seconds * tick_rate`` 推算。
        connection_config: CARLA 连接参数。

    Returns:
        VIL 引擎实际完成的 tick 数。

    Raises:
        hunter_sim.core.exceptions.ConfigurationError: ``vil.enabled=False``
            或未提供标定。
    """
    from hunter_sim.core.exceptions import ConfigurationError

    if not config.vil.enabled:
        raise ConfigurationError("vil.enabled=False，无法启动 VIL 模式")
    identifier = run_id or uuid4().hex
    target_ticks = max_ticks if max_ticks is not None else round(
        config.scenario.duration_seconds * config.scenario.tick_rate
    )
    with logger.contextualize(run_id=identifier):
        container = build_container(
            config, run_id=identifier, connection_config=connection_config
        )
        # 先启动场景（与采集编排器一致的初始化路径）。
        # Protocol 作为容器键时使用 type[Any] 传递，避免 mypy 报非具象类型。
        scenario_key: type[Any] = _ScenarioProtocolType
        connection_key: type[Any] = _ConnectionProtocolType
        engine_key: type[Any] = VILEngine
        scenario = container.resolve(scenario_key)
        connection = container.resolve(connection_key)
        await connection.connect()
        await scenario.configure()
        await scenario.start()
        engine: VILEngine = container.resolve(engine_key)
        await engine.start()
        try:
            return await engine.run(max_ticks=target_ticks)
        finally:
            await engine.stop()
            await scenario.stop()
            await connection.disconnect()


def _build_app() -> Any:
    """构建并返回 ``typer`` 应用（惰性导入以隔离可选依赖）。"""
    import typer

    app = typer.Typer(
        name="huntersim",
        help="HunterSim：基于 CARLA 的自动驾驶数据采集 / 处理 / 评估流水线。",
        add_completion=False,
        no_args_is_help=True,
    )

    @app.command()
    def run(
        config: str = typer.Option(..., "--config", help="场景配置文件路径 (.yaml/.json)"),
        max_ticks: int = typer.Option(
            None, "--max-ticks", min=1, help="覆盖按场景时长推算的最大 tick 数"
        ),
        host: str = typer.Option("localhost", "--host", help="CARLA 服务器地址"),
        port: int = typer.Option(2000, "--port", min=1, max=65535, help="CARLA 端口"),
        level: str = typer.Option("INFO", "--log-level", help="日志级别"),
    ) -> None:
        """执行一次采集运行并打印结果摘要。"""
        configure_logging(level)
        scenario_config = load_scenario_config(config)
        conn = ConnectionConfig(host=host, port=port)
        result = asyncio.run(collect(scenario_config, max_ticks=max_ticks, connection_config=conn))
        typer.echo(
            f"运行 {result.run_id} 完成：scenario={result.scenario_name} "
            f"ticks={result.ticks} frames={result.written_frames} "
            f"status={result.metadata.status}"
        )

    @app.command()
    def vil_run(
        config: str = typer.Option(..., "--config", help="VIL 场景配置文件路径"),
        vehicle_id: str = typer.Option(
            None, "--vehicle-id", help="覆盖配置中的 target_vehicle_id"
        ),
        max_ticks: int = typer.Option(
            None, "--max-ticks", min=1, help="最大 tick 数"
        ),
        host: str = typer.Option("localhost", "--host", help="CARLA 服务器地址"),
        port: int = typer.Option(2000, "--port", min=1, max=65535, help="CARLA 端口"),
        level: str = typer.Option("INFO", "--log-level", help="日志级别"),
    ) -> None:
        """启动 VIL 实车在环模式（需 ``carla-vil`` extras 与 Kafka 服务可用）。"""
        configure_logging(level)
        scenario_config = load_scenario_config(config)
        if vehicle_id:
            scenario_config.vil.target_vehicle_id = vehicle_id
        conn = ConnectionConfig(host=host, port=port)
        ticks = asyncio.run(
            run_vil(scenario_config, max_ticks=max_ticks, connection_config=conn)
        )
        typer.echo(f"VIL 运行完成：ticks={ticks}")

    @app.command()
    def validate(
        config: str = typer.Option(..., "--config", help="场景配置文件路径 (.yaml/.json)"),
    ) -> None:
        """校验场景配置是否合法（不连接 CARLA）。"""
        scenario_config = load_scenario_config(config)
        typer.echo(
            f"配置有效：scenario={scenario_config.scenario.name} "
            f"map={scenario_config.scenario.map} "
            f"sensors={len(scenario_config.sensors)}"
        )

    return app


def __getattr__(name: str) -> Any:
    """PEP 562：访问 ``app`` 时惰性构建 typer 应用。"""
    if name == "app":
        built = _build_app()
        globals()["app"] = built
        return built
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = ["collect", "run_vil"]

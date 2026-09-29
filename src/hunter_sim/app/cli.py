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
from hunter_sim.core.config import ConnectionConfig, load_scenario_config
from hunter_sim.core.logging import configure_logging, logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期导入
    from hunter_sim.app.models import RunResult
    from hunter_sim.core.config import ScenarioConfig


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


def _build_app() -> Any:
    """构建并返回 ``typer`` 应用（惰性导入以隔离可选依赖）。"""
    import typer

    app = typer.Typer(
        name="huntersim",
        help="HunterSim：基于 CARLA 的自动驾驶数据采集 / 处理 / 评估流水线。",
        add_completion=False,
        no_args_is_help=True,
    )

    @app.command()  # type: ignore[untyped-decorator]
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

    @app.command()  # type: ignore[untyped-decorator]
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


__all__ = ["collect"]

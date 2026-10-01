"""ScenarioRunner 适配器（PROMPT-ENG-003-B）。

封装 CARLA 官方 ScenarioRunner 的 Python 原子场景执行方式，
将 SceneConfig 转换后的参数传递给 ScenarioRunner 框架。

ScenarioRunner 0.9.16 安装路径：CARLA 内置或独立 pip 包。
"""

from __future__ import annotations

from typing import Any, Optional

from hunter_sim.common.exceptions import CarlaSimulationError, ConfigurationError
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.scene_config import SceneConfig
from hunter_sim.scene_runner.scene_converter import SceneConfigConverter

logger = get_logger(__name__)


class ScenarioRunnerAdapter:
    """CARLA ScenarioRunner 适配器。

    负责将 HunterSim 的 SceneConfig 转换为 ScenarioRunner 可执行的格式，
    并提供启动/停止控制接口。

    Args:
        carla_host: CARLA 服务器地址。
        carla_port: CARLA RPC 端口。
        scenario_runner_path: ScenarioRunner 模块路径（可选，默认使用内置）。
    """

    def __init__(
        self,
        carla_host: str = "127.0.0.1",
        carla_port: int = 2000,
        scenario_runner_path: Optional[str] = None,
    ) -> None:
        self._host = carla_host
        self._port = carla_port
        self._sr_path = scenario_runner_path
        self._converter = SceneConfigConverter()
        self._manager: Any = None
        self._running = False
        logger.debug(f"ScenarioRunnerAdapter: host={carla_host}:{carla_port}")

    def prepare(self, config: SceneConfig) -> dict[str, Any]:
        """将 SceneConfig 转换为 ScenarioRunner 配置字典。

        Args:
            config: 已验证的场景配置。

        Returns:
            ScenarioRunner 配置字典。
        """
        return self._converter.to_scenario_runner_dict(config)

    def start(self, config: SceneConfig) -> None:
        """启动 ScenarioRunner 执行场景。

        需要 CARLA 同步模式已配置。实际 ScenarioManager 由 ScenarioRunner 框架管理。

        Args:
            config: 场景配置。

        Raises:
            ConfigurationError: ScenarioRunner 不可用。
            CarlaSimulationError: 场景启动失败。
        """
        if self._running:
            logger.warning("ScenarioRunner already running, stop() first")
            return

        sr_config = self.prepare(config)

        try:
            # 延迟导入 ScenarioRunner，避免模块级别依赖
            self._try_import_scenario_manager()
            if self._manager is not None:
                self._manager.load_scenario(sr_config)
                self._manager.run_scenario()
                self._running = True
                logger.info(f"ScenarioRunner started: {config.scene_id}")
            else:
                raise ConfigurationError(
                    "ScenarioRunnerAdapter.start",
                    "scenario_manager not available after import",
                )
        except ImportError as exc:
            raise ConfigurationError(
                "ScenarioRunnerAdapter",
                "ScenarioRunner module not found. Install via: "
                "pip install git+https://github.com/carla-simulator/scenario_runner.git@0.9.16",
            ) from exc
        except Exception as exc:
            raise CarlaSimulationError(
                f"scenario_runner_start({config.scene_id})",
                str(exc),
            ) from exc

    def stop(self) -> None:
        """停止 ScenarioRunner 执行。"""
        if not self._running:
            return
        try:
            if self._manager is not None:
                self._manager.stop_scenario()
                self._manager.cleanup()
        except Exception as exc:
            logger.warning(f"ScenarioRunner stop error: {exc}")
        finally:
            self._running = False
            logger.info("ScenarioRunner stopped")

    def _try_import_scenario_manager(self) -> None:
        """尝试导入 ScenarioRunner ScenarioManager。"""
        try:
            from scenario_runner import ScenarioManager  # type: ignore[import-not-found]  # noqa: PLC0415
            self._manager = ScenarioManager(timeout=30.0, debug_mode=False)
        except ImportError:
            self._manager = None
            logger.warning("ScenarioRunner not importable, will use custom_script mode")

    @property
    def is_available(self) -> bool:
        """ScenarioRunner 是否可用。"""
        try:
            import scenario_runner  # noqa: F401, PLC0415
            return True
        except ImportError:
            return False

    @property
    def is_running(self) -> bool:
        """当前 ScenarioRunner 是否正在运行。"""
        return self._running

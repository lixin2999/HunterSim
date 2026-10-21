"""ScenarioRunner 子进程启动器（V2.2.0）。

封装 CARLA 官方 `scenario_runner.py` 的命令行拉起方式，供桌面客户端"场景运行"
面板一键启动/停止 OpenSCENARIO（.xosc）或内置原子场景。与既有
`scene_runner.scenario_adapter.ScenarioRunnerAdapter`（Python 进程内原子场景）并存。

无 Qt 依赖，命令行构建与进程生命周期可独立单测（`popen_factory` 注入桩）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from hunter_sim.common.models import CarlaSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

PopenFactory = Callable[[list[str]], Any]


class ScenarioRunnerLauncher:
    """scenario_runner.py 子进程管理器。

    Args:
        carla: CARLA 连接配置（host/rpc_port/tm_port/timeout）。
        scenario_runner_path: `scenario_runner.py` 路径（None 时按当前目录/PATH 查找）。
        python_executable: 运行 scenario_runner 的 Python 解释器（默认当前解释器）。
        popen_factory: 可选 `Popen` 工厂（单测注入桩，签名 `(cmd: list[str]) -> Popen`）。
    """

    def __init__(
        self,
        carla: CarlaSettings,
        scenario_runner_path: Optional[str] = None,
        python_executable: Optional[str] = None,
        popen_factory: Optional[PopenFactory] = None,
    ) -> None:
        self._carla = carla
        self._sr_path = scenario_runner_path
        self._python = python_executable or sys.executable
        self._popen_factory = popen_factory
        self._process: Any = None

    def build_command(
        self,
        openscenario: Optional[Path] = None,
        scenario: Optional[str] = None,
        sync: bool = True,
        additional_args: Optional[list[str]] = None,
    ) -> list[str]:
        """构建 scenario_runner.py 命令行参数列表。

        Args:
            openscenario: .xosc 文件路径（与 scenario 二选一）。
            scenario: 内置原子场景名（如 `FollowLeadingVehicle`）。
            sync: 是否以同步模式运行（VIL/SIL 定步推进）。
            additional_args: 追加的原生参数。

        Returns:
            命令 token 列表（[python, scenario_runner.py, ...]）。

        Raises:
            ValueError: 既未给 openscenario 也未给 scenario。
        """
        if openscenario is None and scenario is None:
            raise ValueError("必须提供 openscenario 或 scenario 之一")
        cmd: list[str] = [self._python, self._sr_path or "scenario_runner.py"]
        cmd += [
            f"--host={self._carla.host}",
            f"--port={self._carla.rpc_port}",
            f"--trafficManagerPort={self._carla.tm_port}",
            f"--timeout={self._carla.timeout_seconds}",
        ]
        if sync:
            cmd.append("--sync")
        if openscenario is not None:
            cmd.append(f"--openscenario={Path(openscenario).as_posix()}")
        else:
            cmd.append(f"--scenario={scenario}")
        cmd.append("--waitForEgo")
        if additional_args:
            cmd.extend(additional_args)
        return cmd

    def start(
        self,
        openscenario: Optional[Path] = None,
        scenario: Optional[str] = None,
        sync: bool = True,
    ) -> bool:
        """拉起 scenario_runner 子进程；已在运行则直接返回 True。

        Returns:
            是否成功启动（或已在运行）。
        """
        if self.is_running:
            logger.warning("ScenarioRunner 已在运行，先 stop() 再启动")
            return True
        cmd = self.build_command(openscenario=openscenario, scenario=scenario, sync=sync)
        logger.info(f"启动 ScenarioRunner: {' '.join(cmd)}")
        try:
            if self._popen_factory is not None:
                self._process = self._popen_factory(cmd)
            else:
                self._process = subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"ScenarioRunner 启动失败：{type(exc).__name__}: {exc}")
            self._process = None
            return False

    def stop(self, timeout: float = 5.0) -> None:
        """优雅终止子进程（terminate → 超时 kill）。"""
        proc = self._process
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    logger.warning("ScenarioRunner 未在超时内退出，已强制 kill")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"ScenarioRunner stop error: {exc}")
        finally:
            self._process = None
            logger.info("ScenarioRunner 已停止")

    def read_output(self, max_lines: int = 50) -> list[str]:
        """读取子进程 stdout 已有行（供日志面板展示）。

        仅消费当前已缓冲的行；`readline` 读到空（EOF/暂无数据）即停，最多 `max_lines`。
        真正的非阻塞由调用方（SimWorker 线程周期调用）保证。
        """
        proc = self._process
        if proc is None or getattr(proc, "stdout", None) is None:
            return []
        lines: list[str] = []
        try:
            for _ in range(max_lines):
                line = proc.stdout.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            pass
        return lines

    @property
    def is_running(self) -> bool:
        """子进程是否存活。"""
        proc = self._process
        if proc is None:
            return False
        return proc.poll() is None

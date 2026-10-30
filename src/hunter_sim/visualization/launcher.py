"""一键启动编排（``huntersim up``）。

把现场手工步骤固化为一条命令：预检（由调用方传入 ``doctor`` 结果）→ 拉起控制面
（:8090）→ 拉起只读探针面（:8080，可选）→ 打开可视化客户端页面 → 守护子进程
并在任意子进程退出 / Ctrl+C 时统一回收。

设计要点：

- **依赖可注入**：``popen`` / ``opener`` / ``probe`` / ``sleeper`` / ``emit`` 全部可
  替换，单测无需真的起进程或开浏览器；
- **幂等复用**：若目标端口上的服务已就绪（``/health`` 可访问），跳过重复启动，
  避免“第二次 up 把第一次的实例顶掉”；
- **失败不静默**：必需服务（控制面）未就绪即返回非零并回收已拉起的进程；可选
  服务（探针面）失败只提示，不影响仿真与客户端。
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

#: 就绪轮询间隔（秒）
_READY_POLL_INTERVAL_S = 0.5
#: 守护循环轮询间隔（秒）
_GUARD_POLL_INTERVAL_S = 1.0
#: 停止子进程时等待优雅退出的时长（秒）
_STOP_TIMEOUT_S = 10.0
#: 控制面就绪超时（秒）：含实例库初始化与日志装配
MANAGER_READY_TIMEOUT_S = 60.0
#: 探针面就绪超时（秒）
PROBES_READY_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class ServiceSpec:
    """一个被托管的进程。

    Attributes:
        name: 展示名（manager / probes）。
        host: 监听地址。
        port: 监听端口。
        argv: 启动命令行。
        health_path: 就绪探测路径。
        required: 未就绪是否导致 ``up`` 失败。
    """

    name: str
    host: str
    port: int
    argv: tuple[str, ...]
    health_path: str = "/health"
    required: bool = True

    @property
    def url(self) -> str:
        """服务基础地址（通配监听回落 127.0.0.1）。"""
        host = (self.host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    def health_url(self) -> str:
        """就绪探测地址。"""
        return f"{self.url}{self.health_path}"


def build_service_specs(
    settings: HunterSimSettings,
    *,
    python: str = "",
    log_level: str = "info",
    include_probes: bool = True,
) -> list[ServiceSpec]:
    """按配置构造服务启动清单（控制面必启，探针面可选）。

    Args:
        settings: 全局配置（取 ``adapter`` / ``probe`` 的监听地址）。
        python: 解释器路径（默认当前解释器）。
        log_level: uvicorn 日志级别。
        include_probes: 是否一并拉起只读探针面。

    Returns:
        有序启动清单（控制面在前）。
    """
    interpreter = python or sys.executable
    adapter = settings.adapter
    probe = settings.probe
    specs = [
        ServiceSpec(
            name="carla-manager",
            host=adapter.host,
            port=adapter.port,
            argv=(
                interpreter,
                "-m",
                "hunter_sim.cli.main",
                "carla-manager",
                "--host",
                adapter.host,
                "--port",
                str(adapter.port),
                "--log-level",
                log_level,
            ),
        )
    ]
    if include_probes:
        specs.append(
            ServiceSpec(
                name="probes",
                host=probe.host,
                port=probe.port,
                argv=(
                    interpreter,
                    "-m",
                    "hunter_sim.cli.main",
                    "probes",
                    "--host",
                    probe.host,
                    "--port",
                    str(probe.port),
                    "--log-level",
                    log_level,
                ),
                required=False,
            )
        )
    return specs


def http_ok(url: str, timeout: float = 1.5) -> bool:
    """HTTP 是否返回 2xx（就绪探测；任何异常都视为未就绪）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310  仅本机回环
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _print_line(text: str) -> None:
    """默认输出：立即刷新（现场常把 up 重定向到文件，块缓冲会吞掉启动进度）。"""
    print(text, flush=True)


class Launcher:
    """子进程编排器（依赖全部可注入，便于单测）。

    Args:
        popen: 创建子进程的工厂（默认 ``subprocess.Popen``）。
        opener: 打开浏览器的函数（默认 ``webbrowser.open``）。
        probe: 就绪探测函数（默认 ``http_ok``）。
        sleeper: 等待函数（默认 ``time.sleep``）。
        emit: 输出函数（默认逐行刷新的 ``print``）。
    """

    def __init__(
        self,
        *,
        popen: Callable[[Sequence[str]], Any] = subprocess.Popen,
        opener: Callable[[str], Any] = webbrowser.open,
        probe: Callable[[str], bool] = http_ok,
        sleeper: Callable[[float], None] = time.sleep,
        emit: Callable[[str], None] = _print_line,
    ) -> None:
        self._popen = popen
        self._opener = opener
        self._probe = probe
        self._sleeper = sleeper
        self._emit = emit

    # ── 主流程 ──────────────────────────────────────────────────────────────

    def run(
        self,
        specs: Sequence[ServiceSpec],
        viz_url: str,
        *,
        open_browser: bool = True,
        ready_timeout_s: float = MANAGER_READY_TIMEOUT_S,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> int:
        """拉起服务、打开页面并守护运行。

        Args:
            specs: 启动清单（顺序执行，逐项等待就绪）。
            viz_url: 可视化客户端地址。
            open_browser: 是否自动打开浏览器。
            ready_timeout_s: 单个服务的就绪超时（秒）。
            on_ready: 全部服务就绪后的回调（如 ``--demo`` 创建冒烟场景）；
                抛异常只提示，不阻断守护。

        Returns:
            进程退出码：0 表示被 Ctrl+C 正常中断；否则为子进程退出码或 1。
        """
        started: list[tuple[ServiceSpec, Any]] = []
        try:
            for spec in specs:
                if self._probe(spec.health_url()):
                    self._emit(f"[up] {spec.name} 已在运行（{spec.url}），复用现有进程")
                    continue
                process = self._popen(list(spec.argv))
                started.append((spec, process))
                self._emit(f"[up] 启动 {spec.name} -> {spec.url}")
                if self._wait_ready(spec, process, ready_timeout_s):
                    self._emit(f"[up] {spec.name} 就绪（{spec.health_url()}）")
                    continue
                if spec.required:
                    self._emit(
                        f"[up] {spec.name} 未在 {ready_timeout_s:.0f}s 内就绪，"
                        f"请查看该进程日志：{' '.join(spec.argv)}"
                    )
                    return 1
                self._emit(f"[up] {spec.name} 未就绪（非必需，继续启动可视化客户端）")

            if on_ready is not None:
                self._fire_ready(on_ready)

            if open_browser:
                self._open(viz_url)
            else:
                self._emit(f"[up] 可视化客户端地址：{viz_url}")
            return self._guard(started)
        except KeyboardInterrupt:
            self._emit("[up] 收到中断信号，正在停止仿真端…")
            return 0
        finally:
            self._stop(started)

    # ── 内部步骤 ────────────────────────────────────────────────────────────

    def _wait_ready(self, spec: ServiceSpec, process: Any, timeout_s: float) -> bool:
        """轮询就绪（子进程提前退出即判定失败）。"""
        attempts = max(1, int(max(0.0, timeout_s) / _READY_POLL_INTERVAL_S))
        for _ in range(attempts):
            if self._probe(spec.health_url()):
                return True
            if process.poll() is not None:
                return False
            self._sleeper(_READY_POLL_INTERVAL_S)
        return self._probe(spec.health_url())

    def _fire_ready(self, on_ready: Callable[[], None]) -> None:
        """执行就绪回调（Demo 等附加动作失败不得影响守护）。"""
        try:
            on_ready()
        except Exception as exc:  # noqa: BLE001  up 的主职责是守护进程
            self._emit(f"[up] 就绪后附加动作失败（{exc}），服务仍在运行")

    def _open(self, url: str) -> None:
        """打开浏览器（失败只提示，不影响服务运行）。"""
        try:
            opened = self._opener(url)
        except Exception as exc:  # noqa: BLE001  无桌面环境时不得中断 up
            self._emit(f"[up] 打开浏览器失败（{exc}），请手动访问：{url}")
            return
        self._emit(f"[up] 可视化客户端：{url}" + ("" if opened else "（请手动打开）"))

    def _guard(self, started: Sequence[tuple[ServiceSpec, Any]]) -> int:
        """守护循环：任一同管子进程退出即返回其退出码。"""
        if not started:
            self._emit("[up] 服务均已在运行，未接管任何子进程；按 Ctrl+C 结束等待")
        while True:
            for spec, process in started:
                code = process.poll()
                if code is not None:
                    self._emit(f"[up] {spec.name} 已退出（code={code}）")
                    return int(code or 0)
            self._sleeper(_GUARD_POLL_INTERVAL_S)

    def _stop(self, started: Sequence[tuple[ServiceSpec, Any]]) -> None:
        """回收子进程（先 terminate，超时再 kill；幂等）。"""
        for spec, process in reversed(list(started)):
            try:
                if process.poll() is not None:
                    continue
                process.terminate()
                try:
                    process.wait(timeout=_STOP_TIMEOUT_S)
                except Exception:  # noqa: BLE001  超时/无 wait 能力时强杀
                    killer: Optional[Callable[[], Any]] = getattr(process, "kill", None)
                    if callable(killer):
                        killer()
                self._emit(f"[up] 已停止 {spec.name}")
            except Exception as exc:  # noqa: BLE001  回收失败不掩盖主流程退出码
                logger.warning(f"Stop {spec.name} failed: {exc}")


__all__ = [
    "MANAGER_READY_TIMEOUT_S",
    "PROBES_READY_TIMEOUT_S",
    "Launcher",
    "ServiceSpec",
    "build_service_specs",
    "http_ok",
]

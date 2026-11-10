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
  服务（探针面）失败只提示，不影响仿真与客户端；
- **崩溃自愈**：子进程非零退出（典型是 CARLA 原生 fail-fast 带走 ``carla-manager``，
  退出码 ``0xC0000409``）时按退避自动重启，**不牵连其余服务**，重启后继续等待平台
  下发场景；``--no-restart`` 可关闭，正常退出（code=0）与 Ctrl+C 不触发重启。
"""

from __future__ import annotations

import socket
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
#: 子进程异常退出后的重启退避（秒），逐次变长；末位重复使用，避免重启风暴
_RESTART_BACKOFF_S: tuple[float, ...] = (3.0, 5.0, 10.0, 30.0)
#: 连续未稳定情况下的自动重启上限，超过则停止守护交由现场处理
_RESTART_MAX_ATTEMPTS = 4
#: 子进程连续存活多久算“已稳定”（秒）：稳定后再崩则重新计一次重启额度
SERVICE_STABLE_SECONDS = 30.0


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
        tls: 是否以 TLS/mTLS 提供服务（影响探测方式：TLS 用 TCP 端口探测）。
    """

    name: str
    host: str
    port: int
    argv: tuple[str, ...]
    health_path: str = "/health"
    required: bool = True
    tls: bool = False

    @property
    def url(self) -> str:
        """服务基础地址（通配监听回落 127.0.0.1）。"""
        host = (self.host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{host}:{self.port}"

    @property
    def probe_host(self) -> str:
        """探测用实际主机（通配地址回落 127.0.0.1）。"""
        host = (self.host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            return "127.0.0.1"
        return host

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
            tls=adapter.tls_enabled,
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


def tcp_port_ok(host: str, port: int, timeout: float = 1.5) -> bool:
    """TCP 端口是否可连通（TLS/mTLS 场景下的本地就绪探测）。

    当服务端强制 mTLS 时，HTTP 健康检查需携带客户端证书，不适合做本机就绪
    判断；TCP 端口可连接即表明 uvicorn 已绑定 socket 并完成启动。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _print_line(text: str) -> None:
    """默认输出：立即刷新（现场常把 up 重定向到文件，块缓冲会吞掉启动进度）。"""
    print(text, flush=True)


def _stable_seconds() -> float:
    """“已稳定”判定的秒数下限（不小于一个守护轮询周期，避免注入短间隔时误判）。"""
    return max(float(SERVICE_STABLE_SECONDS), _GUARD_POLL_INTERVAL_S)


class Launcher:
    """子进程编排器（依赖全部可注入，便于单测）。

    Args:
        popen: 创建子进程的工厂（默认 ``subprocess.Popen``）。
        opener: 打开浏览器的函数（默认 ``webbrowser.open``）。
        probe: 就绪探测函数（默认 ``http_ok``）。
        sleeper: 等待函数（默认 ``time.sleep``）。
        emit: 输出函数（默认逐行刷新的 ``print``）。
        restart_on_failure: 子进程异常退出（非零码）时是否自动重启（V3.5.4）。
    """

    def __init__(
        self,
        *,
        popen: Callable[[Sequence[str]], Any] = subprocess.Popen,
        opener: Callable[[str], Any] = webbrowser.open,
        probe: Callable[[str], bool] = http_ok,
        sleeper: Callable[[float], None] = time.sleep,
        emit: Callable[[str], None] = _print_line,
        restart_on_failure: bool = True,
    ) -> None:
        self._popen = popen
        self._opener = opener
        self._probe = probe
        self._sleeper = sleeper
        self._emit = emit
        self._restart_on_failure = bool(restart_on_failure)
        # Ctrl+C / 正常回收时置位：守护不得再拉起新进程
        self._stopping = False

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
                if self._probe_spec(spec):
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
            return self._guard(started, ready_timeout_s)
        except KeyboardInterrupt:
            self._stopping = True
            self._emit("[up] 收到中断信号，正在停止仿真端…")
            return 0
        finally:
            self._stop(started)

    # ── 内部步骤 ────────────────────────────────────────────────────────────

    def _probe_spec(self, spec: ServiceSpec) -> bool:
        """按 spec 的 TLS 标志选择探测方式：TLS 用 TCP 端口，非 TLS 用 HTTP。"""
        if spec.tls:
            return tcp_port_ok(spec.probe_host, spec.port)
        return self._probe(spec.health_url())

    def _wait_ready(self, spec: ServiceSpec, process: Any, timeout_s: float) -> bool:
        """轮询就绪（子进程提前退出即判定失败）。"""
        attempts = max(1, int(max(0.0, timeout_s) / _READY_POLL_INTERVAL_S))
        for _ in range(attempts):
            if self._probe_spec(spec):
                return True
            if process.poll() is not None:
                return False
            self._sleeper(_READY_POLL_INTERVAL_S)
        return self._probe_spec(spec)

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

    def _guard(self, started: list[tuple[ServiceSpec, Any]], ready_timeout_s: float) -> int:
        """守护循环：异常退出自动重启，正常退出/主动关停则结束守护。

        CARLA 侧的原生 fail-fast（退出码 3221226505 = ``0xC0000409``）不是 Python
        异常，管理面自身的 ``try/except`` 与看门狗都接不住，只能在本层自愈：
        “场景跑完、进程就没了”不等于仿真端下线，必须自动拉起后继续等下一个场景。
        """
        if not started:
            self._emit("[up] 服务均已在运行，未接管任何子进程；按 Ctrl+C 结束等待")
        attempts = 0
        alive_seconds: dict[int, float] = {}
        while True:
            for index in range(len(started)):
                spec, process = started[index]
                code = process.poll()
                if code is None:
                    alive_seconds[index] = (
                        alive_seconds.get(index, 0.0) + _GUARD_POLL_INTERVAL_S
                    )
                    continue
                if alive_seconds.get(index, 0.0) >= _stable_seconds():
                    # 已经稳定跑过一轮，本次崩溃重新占用新的重启额度
                    attempts = 0
                alive_seconds[index] = 0.0
                if code == 0 or self._stopping or not self._restart_on_failure:
                    self._emit(f"[up] {spec.name} 已退出（code={code}）")
                    return int(code or 0)
                if attempts >= _RESTART_MAX_ATTEMPTS:
                    self._emit(
                        f"[up] {spec.name} 连续 {attempts} 次重启后仍未稳定（code={code}），停止守护；"
                        "请先确认 CARLA 引擎（CarlaUE4.exe / :2000）与端口占用，再重跑 huntersim up"
                    )
                    return int(code)
                attempts += 1
                backoff = _RESTART_BACKOFF_S[min(attempts - 1, len(_RESTART_BACKOFF_S) - 1)]
                self._emit(
                    f"[up] {spec.name} 异常退出（code={code}），"
                    f"{backoff:.0f}s 后自动重启（第 {attempts}/{_RESTART_MAX_ATTEMPTS} 次），"
                    "其余服务保持运行"
                )
                self._sleeper(backoff)
                if self._stopping:
                    return 0
                if self._relaunch(spec, index, started, ready_timeout_s):
                    self._emit(
                        f"[up] {spec.name} 已重启并就绪（{spec.url}），继续等待平台下发场景"
                    )
            self._sleeper(_GUARD_POLL_INTERVAL_S)

    def _relaunch(
        self,
        spec: ServiceSpec,
        index: int,
        started: list[tuple[ServiceSpec, Any]],
        ready_timeout_s: float,
    ) -> bool:
        """重新拉起一个已退出的子进程，返回是否已就绪（不就绪则由守护循环重试）。"""
        try:
            process = self._popen(list(spec.argv))
        except Exception as exc:  # noqa: BLE001  拉起失败只影响本次重启尝试
            self._emit(f"[up] {spec.name} 重启失败（{exc}），稍后按退避重试")
            return False
        started[index] = (spec, process)
        if self._wait_ready(spec, process, ready_timeout_s):
            return True
        self._emit(f"[up] {spec.name} 重启后未在 {ready_timeout_s:.0f}s 内就绪，继续重试")
        return False

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
    "tcp_port_ok",
]

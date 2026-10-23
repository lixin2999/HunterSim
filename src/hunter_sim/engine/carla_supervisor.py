"""CARLA 引擎进程托管与自愈（V2.8.0 P2-15）。

本地/单机部署时，CARLA（CarlaUE4）由运维手工启动：进程崩溃后仿真停摆，
只能靠人盯日志重启。本模块让 sim-service 直接托管回环地址上的 CARLA 进程：

- 启动前拉起（未运行则自动启动并等待 RPC 端口就绪）；
- 运行期看门狗轮询，进程异常退出后按退避自动重启；
- 时间窗口内的重启次数封顶，超限转 ``exhausted`` 并停止盲试（避免抖动风暴）；
- 全部事件（start/exit/restart/ready_timeout/exhausted/stop）打点
  ``huntersim_carla_supervisor_events_total`` 并可回调写入审计。

边界与取舍：
- 仅托管 ``supervisor_binary_path`` 指向的本地可执行文件，且端点必须是回环地址
  （远程/容器内引擎由 systemd / K8s liveness / docker restart 负责，不越权接管）；
- 重启会重置世界：该端点上正在运行的场景必然失败，需重新装配（由
  ``ensure_scene_runner`` 的按需装配完成），本模块不伪造“场景无缝续跑”。
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess  # noqa: S404  托管本地 CARLA 进程是本模块的职责
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from hunter_sim.common.metrics import CARLA_SUPERVISOR_EVENTS_TOTAL
from hunter_sim.common.models import CarlaSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 视为本机的端点地址（其余地址一律不接管，避免误杀远程引擎）
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# 重启封顶的时间窗口（秒）：窗口内累计达上限即转 exhausted
_RESTART_WINDOW_SECONDS = 300.0
# 看门狗轮询间隔（秒）
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
# 就绪探测的单次 socket 超时（秒）
_PROBE_TIMEOUT_SECONDS = 1.0
# 内存中保留的最近事件数（供 /execution 排障展示）
_MAX_RECENT_EVENTS = 50

# 事件名（同时作为指标 label 与审计 action 后缀）
EVENT_START = "start"
EVENT_EXIT = "exit"
EVENT_RESTART = "restart"
EVENT_READY_TIMEOUT = "ready_timeout"
EVENT_EXHAUSTED = "exhausted"
EVENT_STOP = "stop"
EVENT_GIVEUP = "giveup"


def _is_loopback(host: str) -> bool:
    """端点是否指向本机（127.0.0.0/8 或常见别名）。"""
    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")


@dataclass(frozen=True)
class ManagedTarget:
    """一个被托管的 CARLA 端点（从引擎端点对象投影，避免依赖执行面模块）。"""

    key: str
    host: str
    rpc_port: int
    stream_port: int

    @classmethod
    def from_endpoint(cls, endpoint: Any) -> "ManagedTarget":
        """由 ``EngineEndpoint``（或具备同名属性的对象/字典）构造托管目标。"""
        if isinstance(endpoint, dict):
            host = str(endpoint.get("host", "127.0.0.1"))
            rpc = int(endpoint.get("rpc_port", 2000))
            stream = int(endpoint.get("stream_port", rpc + 1))
        else:
            host = str(getattr(endpoint, "host", "127.0.0.1"))
            rpc = int(getattr(endpoint, "rpc_port", 2000))
            stream = int(getattr(endpoint, "stream_port", rpc + 1))
        return cls(key=f"{host}:{rpc}", host=host, rpc_port=rpc, stream_port=stream)


@dataclass
class EndpointRuntime:
    """单个端点的托管运行时状态（看门狗线程独占写入，读侧加锁快照）。"""

    target: ManagedTarget
    state: str = "idle"
    pid: Optional[int] = None
    started_at: float = 0.0
    restarts: deque[float] = field(default_factory=deque)
    total_restarts: int = 0
    last_exit_code: Optional[int] = None
    last_event_at: float = 0.0
    next_attempt_at: float = 0.0
    process: Optional[Any] = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API/日志字段（不含进程句柄）。"""
        return {
            "endpoint": self.target.key,
            "state": self.state,
            "pid": self.pid,
            "started_at": self.started_at,
            "restarts_in_window": len(self.restarts),
            "total_restarts": self.total_restarts,
            "last_exit_code": self.last_exit_code,
            "last_event_at": self.last_event_at,
            "next_attempt_at": self.next_attempt_at,
        }


ProcessEventSink = Callable[[str, str, str], None]
PopenFactory = Callable[[Sequence[str]], Any]


class CarlaProcessSupervisor:
    """本地 CARLA 进程看门狗（线程安全）。

    Args:
        settings: CARLA 配置（取 supervisor_* 字段）。
        endpoints: 候选引擎端点（非回环与无二进制者自动忽略）。
        event_sink: 事件回调 ``(event, endpoint_key, detail)``，用于写审计。
        popen_factory: 进程启动工厂（测试注入点；默认 ``subprocess.Popen``）。
        probe: RPC 就绪探测函数 ``(host, port) -> bool``（测试注入点）。
        poll_interval_seconds: 看门狗轮询间隔。
    """

    def __init__(
        self,
        settings: CarlaSettings,
        endpoints: Sequence[Any],
        event_sink: Optional[ProcessEventSink] = None,
        popen_factory: Optional[PopenFactory] = None,
        probe: Optional[Callable[[str, int], bool]] = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._cfg = settings
        self._event_sink = event_sink
        self._popen_factory: PopenFactory = popen_factory or self._default_popen
        self._probe = probe or _port_open
        self._poll_interval = max(0.2, float(poll_interval_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recent: list[dict[str, Any]] = []
        self._runtime: dict[str, EndpointRuntime] = {}
        for endpoint in endpoints:
            target = ManagedTarget.from_endpoint(endpoint)
            if not _is_loopback(target.host):
                logger.info(
                    f"CARLA supervisor skips remote endpoint '{target.key}' "
                    "(only loopback engines are process-managed)"
                )
                continue
            self._runtime[target.key] = EndpointRuntime(target=target)

    # ─── 生命周期 ─────────────────────────────────────────────────────────

    @property
    def managed_endpoints(self) -> list[str]:
        """被托管的端点 key 列表（回环端点，按声明顺序）。"""
        with self._lock:
            return list(self._runtime)

    @property
    def enabled(self) -> bool:
        """是否具备自愈能力（开关打开且二进制已配置）。"""
        return bool(self._cfg.supervisor_enabled and self._cfg.supervisor_binary_path)

    def start(self) -> None:
        """启动看门狗线程（幂等）；未启用或未配二进制时只观测不拉起。"""
        with self._lock:
            if self._thread is not None or not self._runtime:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._watchdog, name="carla-supervisor", daemon=True
            )
            self._thread.start()
        logger.info(
            f"CARLA process supervisor started for {len(self._runtime)} endpoint(s): "
            f"{', '.join(self.managed_endpoints)}"
            + ("" if self.enabled else " (CARLA_SUPERVISOR_BINARY_PATH 未配置，仅观测)")
        )

    def stop(self) -> None:
        """停止看门狗并终止托管进程（服务关停路径）。"""
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=max(1.0, self._cfg.supervisor_graceful_shutdown_seconds))
        for runtime in list(self._snapshot_runtimes()):
            self._terminate(runtime, reason=EVENT_STOP)

    def ensure_started(self) -> None:
        """立即执行一轮“未运行则拉起”（服务启动时调用，避免等首个轮询周期）。"""
        for runtime in self._snapshot_runtimes():
            with self._lock:
                alive = runtime.process is not None and runtime.process.poll() is None
            if not alive:
                self._attempt_start(runtime, restart=False)

    def restart(self, endpoint_key: str) -> bool:
        """管理员手动重启指定端点的 CARLA 进程（不计入自动重启封顶窗口）。

        Returns:
            是否成功定位端点并完成重启动作（进程退出 + 重新拉起）。
        """
        runtime = self._runtime.get(endpoint_key)
        if runtime is None:
            return False
        self._terminate(runtime, reason=EVENT_RESTART)
        ok = self._attempt_start(runtime, restart=True, manual=True)
        return ok

    def snapshot(self) -> dict[str, Any]:
        """托管状态快照（供 ``GET /execution`` 与排障）。"""
        with self._lock:
            endpoints = [runtime.to_dict() for runtime in self._runtime.values()]
            recent = list(self._recent)
        return {
            "enabled": bool(self._cfg.supervisor_enabled),
            "self_heal_ready": self.enabled,
            "binary_path": self._cfg.supervisor_binary_path,
            "max_restarts": self._cfg.supervisor_max_restarts,
            "restart_window_seconds": _RESTART_WINDOW_SECONDS,
            "restart_backoff_seconds": self._cfg.supervisor_restart_backoff_seconds,
            "ready_timeout_seconds": self._cfg.supervisor_ready_timeout_seconds,
            "poll_interval_seconds": self._poll_interval,
            "endpoints": endpoints,
            "recent_events": recent,
        }

    # ─── 内部实现 ─────────────────────────────────────────────────────────

    def _watchdog(self) -> None:
        """看门狗主循环：检测退出 → 退避重启 → 封顶转 exhausted。"""
        while not self._stop.is_set():
            for runtime in self._snapshot_runtimes():
                try:
                    self._check_once(runtime)
                except Exception as exc:  # noqa: BLE001  看门狗绝不能因单端点异常退出
                    logger.error(f"CARLA supervisor check failed for {runtime.target.key}: {exc}")
            self._stop.wait(self._poll_interval)

    def _check_once(self, runtime: EndpointRuntime) -> None:
        with self._lock:
            process = runtime.process
            alive = process is not None and process.poll() is None
        if alive:
            return
        if process is None:
            if runtime.state in ("idle", "backoff") and time.time() >= runtime.next_attempt_at:
                self._attempt_start(runtime, restart=runtime.total_restarts > 0)
            return
        code = _exit_code(process)
        recent = len(runtime.restarts)
        self._emit(
            runtime,
            EVENT_EXIT,
            f"exit_code={code}, restarts_in_window={recent}",
            level="error",
        )
        with self._lock:
            runtime.last_exit_code = code
            runtime.process = None
            runtime.pid = None
            runtime.state = "crashed"
        if not self._cfg.supervisor_enabled:
            with self._lock:
                runtime.state = "disabled"
            return
        if not self.enabled:
            self._emit(runtime, EVENT_GIVEUP, "CARLA_SUPERVISOR_BINARY_PATH not configured", level="error")
            with self._lock:
                runtime.state = "unconfigured"
            return
        self._prune_window(runtime)
        if len(runtime.restarts) >= max(0, self._cfg.supervisor_max_restarts):
            self._emit(runtime, EVENT_EXHAUSTED, f"max_restarts={self._cfg.supervisor_max_restarts}", level="error")
            with self._lock:
                runtime.state = "exhausted"
            return
        self._attempt_start(runtime, restart=True)

    def _attempt_start(
        self, runtime: EndpointRuntime, restart: bool, manual: bool = False
    ) -> bool:
        """拉起进程并等待 RPC 就绪，返回是否成功。"""
        if not self.enabled:
            with self._lock:
                runtime.state = "unconfigured"
                runtime.next_attempt_at = time.time() + self._cfg.supervisor_restart_backoff_seconds
            return False
        cmd = self._build_command(runtime.target)
        try:
            process = self._popen_factory(cmd)
        except Exception as exc:  # noqa: BLE001  启动失败按退避重试
            self._emit(runtime, EVENT_RESTART if restart else EVENT_START, f"launch failed: {exc}", level="error")
            with self._lock:
                runtime.state = "backoff"
                runtime.next_attempt_at = time.time() + self._cfg.supervisor_restart_backoff_seconds
            return False

        now = time.time()
        with self._lock:
            runtime.process = process
            runtime.pid = _pid_of(process)
            runtime.started_at = now
            runtime.state = "starting"
            if restart and not manual:
                runtime.total_restarts += 1
                runtime.restarts.append(now)
                CARLA_SUPERVISOR_EVENTS_TOTAL.labels(event=EVENT_RESTART).inc()

        ready = self._wait_ready(runtime, process)
        if not ready:
            self._emit(
                runtime,
                EVENT_READY_TIMEOUT,
                f"rpc {runtime.target.key} not ready within "
                f"{self._cfg.supervisor_ready_timeout_seconds:.0f}s",
                level="error",
            )
            self._terminate(runtime, reason=EVENT_READY_TIMEOUT)
            with self._lock:
                runtime.state = "backoff"
                runtime.next_attempt_at = time.time() + self._cfg.supervisor_restart_backoff_seconds
            return False

        with self._lock:
            runtime.state = "running"
        CARLA_SUPERVISOR_EVENTS_TOTAL.labels(event=EVENT_RESTART if restart else EVENT_START).inc()
        self._emit(
            runtime,
            EVENT_RESTART if restart else EVENT_START,
            f"pid={runtime.pid}, cmd={' '.join(cmd)}",
            level="warning" if restart else "info",
        )
        return True

    def _wait_ready(self, runtime: EndpointRuntime, process: Any) -> bool:
        """等待 RPC 端口可连通（期间进程若先退出则判失败）。"""
        deadline = time.time() + float(self._cfg.supervisor_ready_timeout_seconds)
        while time.time() < deadline:
            # V2.9.0：服务关停/手动重启时立即返回，避免 stop 后仍等待就绪而遗留孤儿进程
            if self._stop.is_set():
                return False
            if process is not None and process.poll() is not None:
                return False
            try:
                if self._probe(runtime.target.host, runtime.target.rpc_port):
                    return True
            except Exception as exc:  # noqa: BLE001  探测异常视为未就绪
                logger.debug(f"CARLA readiness probe error for {runtime.target.key}: {exc}")
            self._stop.wait(min(1.0, self._poll_interval))
        return False

    def _terminate(self, runtime: EndpointRuntime, reason: str) -> None:
        """终止进程（先优雅后强杀）。"""
        with self._lock:
            process = runtime.process
            runtime.process = None
            runtime.pid = None
        if process is None:
            return
        grace = float(self._cfg.supervisor_graceful_shutdown_seconds)
        try:
            if process.poll() is None:
                process.terminate()
                _wait(process, grace)
            if process.poll() is None:
                process.kill()
                _wait(process, 2.0)
        except Exception as exc:  # noqa: BLE001  终止失败仅记录，交由看门狗下轮处理
            logger.error(f"Failed to terminate CARLA process for {runtime.target.key}: {exc}")
        CARLA_SUPERVISOR_EVENTS_TOTAL.labels(event=reason if reason in (EVENT_STOP, EVENT_RESTART) else EVENT_STOP).inc()
        self._emit(runtime, reason, f"terminated, exit_code={_exit_code(process)}")

    def _build_command(self, target: ManagedTarget) -> list[str]:
        """拼接 CarlaUE4 启动命令（端口与自定义参数）。"""
        binary = self._cfg.supervisor_binary_path
        if not binary:
            return []
        cmd = [
            binary,
            f"-carla-rpc-port={target.rpc_port}",
            f"-streaming-port={target.stream_port}",
        ]
        extra = self._cfg.supervisor_extra_args.strip()
        if extra:
            cmd.extend(shlex.split(extra, posix=(os.name != "nt")))
        return cmd

    def _prune_window(self, runtime: EndpointRuntime) -> None:
        """丢弃超出重启窗口的历史记录，使封顶按窗口滚动而非累计总量。"""
        cutoff = time.time() - _RESTART_WINDOW_SECONDS
        with self._lock:
            while runtime.restarts and runtime.restarts[0] < cutoff:
                runtime.restarts.popleft()

    def _snapshot_runtimes(self) -> list[EndpointRuntime]:
        with self._lock:
            return list(self._runtime.values())

    def _emit(self, runtime: EndpointRuntime, event: str, detail: str, level: str = "info") -> None:
        """记录事件：日志 + 内存环形列表 + 审计回调（best-effort）。"""
        now = time.time()
        with self._lock:
            runtime.last_event_at = now
            self._recent.append(
                {"event": event, "endpoint": runtime.target.key, "detail": detail, "at": now}
            )
            self._recent = self._recent[-_MAX_RECENT_EVENTS:]
        message = f"CARLA supervisor [{event}] {runtime.target.key}: {detail}"
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)
        sink = self._event_sink
        if sink is not None:
            try:
                sink(event, runtime.target.key, detail)
            except Exception as exc:  # noqa: BLE001  审计失败不影响自愈主流程
                logger.warning(f"CARLA supervisor audit sink failed: {exc}")

    @staticmethod
    def _default_popen(cmd: Sequence[str]) -> Any:
        """默认进程工厂：分离输出，Windows 下独立进程组避免随控制台退出。"""
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return subprocess.Popen(  # noqa: S603  命令来自受控配置 CARLA_SUPERVISOR_BINARY_PATH
            list(cmd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )


def _pid_of(process: Any) -> Optional[int]:
    """读取进程 PID（测试桩可能没有 pid 属性）。"""
    pid = getattr(process, "pid", None)
    return int(pid) if isinstance(pid, int) else None


def _exit_code(process: Any) -> Optional[int]:
    """读取退出码（尚未退出或桩对象无返回码时为 None）。"""
    code = getattr(process, "returncode", None)
    return int(code) if isinstance(code, int) else None


def _wait(process: Any, seconds: float) -> None:
    """等待进程退出（不支持 wait 的桩对象直接返回）。"""
    waiter = getattr(process, "wait", None)
    if not callable(waiter):
        return
    try:
        waiter(timeout=seconds)
    except Exception:  # noqa: BLE001  超时/不支持均交由调用方判定
        return


def _port_open(host: str, port: int) -> bool:
    """TCP 连通即视为 CARLA RPC 就绪。"""
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


__all__ = ["CarlaProcessSupervisor", "ManagedTarget", "EndpointRuntime"]

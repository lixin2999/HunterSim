"""HunterSim 公共工具函数。

提供 RingBuffer、日志初始化、数学辅助工具等跨模块公用组件。

V2.7.0 P1-11：``configure_logging`` 统一日志出口——级别/JSON 结构化/文件轮转
全部由配置驱动，request_id 通过 structlog contextvars 贯穿全部模块日志。
"""

from __future__ import annotations

import logging
import logging.handlers
import math
import os
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Generic, Optional, TypeVar

import structlog

T = TypeVar("T")


# ─── 日志工厂 ─────────────────────────────────────────────────────────────────

# 全局默认日志级别（由应用入口按 HUNTER_SIM_LOG_LEVEL 注入）
_DEFAULT_LEVEL: int = logging.INFO

# configure_logging 已执行标记：模块 logger 不再自行挂 handler（P1-11）
_LOG_CONFIGURED: bool = False


class _RequestIdFilter(logging.Filter):
    """把 structlog contextvars 里的 request_id 注入 stdlib LogRecord（P1-11）。

    中间件通过 ``bind_request_id`` 绑定；无请求上下文时填 ``-``，保证
    文本格式的 ``%(request_id)s`` 字段永不存在缺字段异常。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            rid = structlog.contextvars.get_contextvars().get("request_id")
            record.request_id = rid if isinstance(rid, str) and rid else "-"
        return True


def _make_text_formatter() -> logging.Formatter:
    """可读文本格式：[时间][级别][模块名][线程名][rid] 消息。

    V3.6.0：格式内建 **线程名**。CARLA 客户端非线程安全，现场排查原生崩溃时必须
    从日志判定「这条调用出自哪个线程」；旧格式只有模块名，tick 线程与 HTTP 线程
    的输出长得一模一样，无法定位跨线程并发。前端日志面板（LogBus）本就携带线程名。
    """
    return logging.Formatter(
        fmt=(
            "[%(asctime)s][%(levelname)s][%(name)s][%(threadName)s][%(request_id)s] "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )


#: 保留轮询类访问日志的环境变量名（设为 1/true 时不再降噪）
ACCESS_LOG_POLLING_ENV = "HUNTER_SIM_LOG_ACCESS_POLLING"

#: 视为「面板轮询」的路径前缀（GET 且状态码 <400 时降噪）
_POLLING_PATH_PREFIXES: tuple[str, ...] = (
    "/viz/",
    "/api/v1/instances",
    "/api/v1/scenes",
    "/api/v1/execution",
    "/health",
    "/metrics",
    "/ws",
)


class _AccessLogNoiseFilter(logging.Filter):
    """轮询访问日志降噪（V3.6.0）。

    两个可视化标签页×7 路定时轮询在崩溃现场日志里能占比 60%以上，把真正有用的
    业务/崩溃痕迹淹没。**错误一律保留**（非 2xx/3xx、非 GET 全部输出），
    只把成功轮询降为计数；每分钟输出一次汇总，避免「降噪」变成「丢信息」。
    """

    _LINE_RE = re.compile(r'"(?P<method>[A-Z]+)\s+(?P<path>[^"]*?)\s+HTTP/[\d.]+"\s+(?P<status>\d{3})')

    def __init__(self, summary_interval_s: float = 60.0) -> None:
        super().__init__()
        self._summary_interval_s = max(1.0, float(summary_interval_s))
        self._suppressed = 0
        self._next_summary_at = time.monotonic() + self._summary_interval_s

    def filter(self, record: logging.LogRecord) -> bool:
        if os.environ.get(ACCESS_LOG_POLLING_ENV, "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return True
        if record.levelno >= logging.WARNING:
            return True
        match = self._LINE_RE.search(record.getMessage())
        if match is None:
            return True
        status = int(match.group("status"))
        path = match.group("path")
        question = path.find("?")
        clean = path[:question] if question >= 0 else path
        if (
            match.group("method") == "GET"
            and status < 400
            and clean.startswith(_POLLING_PATH_PREFIXES)
        ):
            self._suppressed += 1
            self._maybe_summarize()
            return False
        return True

    def _maybe_summarize(self) -> None:
        """周期性输出抑制条数（汇总行本身走 root，不经本过滤器）。"""
        now = time.monotonic()
        if now < self._next_summary_at:
            return
        suppressed, self._suppressed = self._suppressed, 0
        self._next_summary_at = now + self._summary_interval_s
        if suppressed:
            logging.getLogger("uvicorn.access").info(
                f"access log 降噪：过去 {self._summary_interval_s:.0f}s 抑制 "
                f"{suppressed} 条面板轮询请求（错误与非轮询请求均保留；"
                f"设 {ACCESS_LOG_POLLING_ENV}=1 可全部输出）"
            )


def bind_request_id(request_id: str) -> None:
    """绑定当前请求的 request_id（structlog contextvars，P1-11）。"""
    structlog.contextvars.bind_contextvars(request_id=request_id)


def clear_request_id() -> None:
    """清除当前上下文的 request_id 绑定（请求结束时调用）。"""
    structlog.contextvars.unbind_contextvars("request_id")


def configure_logging(
    log_level: str = "INFO",
    json_logs: bool = False,
    log_file: str = "",
    log_max_bytes: int = 10_485_760,
    log_backup_count: int = 5,
) -> int:
    """统一日志装配（P1-11，应用入口调用，幂等）。

    - 级别：``HUNTER_SIM_LOG_LEVEL`` 同步全部模块 logger；
    - 出口：摘除各模块 logger 自有 handler，统一挂到 ``hunter_sim`` 父 logger，
      避免多次调用时重复输出；
    - ``json_logs``：为真时 stdlib 与 structlog 两套日志均经
      ``ProcessorFormatter`` 渲染为 JSON（含 request_id/时间戳/级别）；
    - ``log_file``：非空时额外接入 ``RotatingFileHandler``（按字节轮转，
      容器部署可保持 stdout 另选日志驱动接管）。

    Args:
        log_level: DEBUG/INFO/WARNING/ERROR/CRITICAL。
        json_logs: 是否以 JSON 渲染。
        log_file: 轮转日志文件路径（空表示仅 stdout）。
        log_max_bytes: 单文件最大字节数。
        log_backup_count: 保留历史文件数。

    Returns:
        实际生效的 logging 整型级别。
    """
    global _LOG_CONFIGURED  # noqa: PLW0603  进程级配置，全局唯一
    level = set_default_log_level(log_level)
    parent = logging.getLogger("hunter_sim")

    # 幂等：先清空父 logger 已有出口，再摘除模块级 handler
    for handler in list(parent.handlers):
        parent.removeHandler(handler)
    for name in list(logging.root.manager.loggerDict):
        if name == "hunter_sim" or name.startswith("hunter_sim."):
            module_logger = logging.getLogger(name)
            for handler in list(module_logger.handlers):
                module_logger.removeHandler(handler)

    if json_logs:
        timestamper = structlog.processors.TimeStamper(fmt="iso")
        pre_chain: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.ExtraAdder(),
            timestamper,
        ]
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_log_level,
                timestamper,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.make_filtering_bound_logger(level),
            cache_logger_on_first_use=True,
        )
        formatter: logging.Formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=pre_chain,
        )
    else:
        formatter = _make_text_formatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(_RequestIdFilter())
    parent.addHandler(stdout_handler)

    if log_file:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_max_bytes,
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_RequestIdFilter())
        parent.addHandler(file_handler)

    parent.setLevel(level)
    _install_third_party_logging(level)
    _install_access_log_policy(level)
    _LOG_CONFIGURED = True
    return level


#: 需纳入统一出口的第三方 logger 树（V3.6.0）
_THIRD_PARTY_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _install_third_party_logging(level: int) -> None:
    """把 uvicorn 原生日志接进统一出口（V3.6.0，幂等）。

    uvicorn 默认用自带 dictConfig 输出 ``INFO:     Uvicorn running on ...``、
    ``INFO:     127.0.0.1:53124 - "GET ..." 200 OK`` 这类**无时间戳、无线程名**的
    行，与业务日志混排后既不能按时间轴定位崩溃瞬间，也无法套用访问日志降噪规则。
    这里摘除 uvicorn logger 自有 handler，改挂 ``hunter_sim`` 父 logger 的同一组
    出口：``uvicorn`` 作为汇聚点（``propagate=False`` 防重复），``uvicorn.error`` /
    ``uvicorn.access`` 保留向父级传播。须配合 ``uvicorn.run(..., log_config=None)``
    使用——否则 uvicorn 会在启动时重新装回它自己的 handler。
    """
    parent = logging.getLogger("hunter_sim")
    for name in _THIRD_PARTY_LOGGERS:
        third = logging.getLogger(name)
        for handler in list(third.handlers):
            third.removeHandler(handler)
        third.setLevel(level)
        if name == "uvicorn":
            for handler in parent.handlers:
                third.addHandler(handler)
            third.propagate = False
        else:
            third.propagate = True


def _install_access_log_policy(level: int) -> None:
    """接入访问日志降噪过滤器（V3.6.0，幂等）。

    uvicorn 的 access logger 不在 ``hunter_sim`` 子树下，需单独处理；DEBUG 级
    别不降噪（排障时要看到完整请求序列）。
    """
    access = logging.getLogger("uvicorn.access")
    for existing in access.filters:
        if isinstance(existing, _AccessLogNoiseFilter):
            return
    if level <= logging.DEBUG:
        return
    access.addFilter(_AccessLogNoiseFilter())


def set_default_log_level(level: str | int) -> int:
    """设置全局默认日志级别，并同步已创建的 hunter_sim 模块 logger。

    ``get_logger`` 默认取此级别，避免“配置了 HUNTER_SIM_LOG_LEVEL 但不生效”。

    Args:
        level: 级别名（DEBUG/INFO/WARNING/ERROR/CRITICAL）或 logging 整型级别。

    Returns:
        实际生效的 logging 整型级别。
    """
    global _DEFAULT_LEVEL  # noqa: PLW0603  进程级配置，全局唯一
    resolved = getattr(logging, level.upper(), logging.INFO) if isinstance(level, str) else int(level)
    _DEFAULT_LEVEL = resolved
    logging.getLogger().setLevel(resolved)
    # 已创建的模块 logger 都显式设过级别，需一并刷新
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("hunter_sim"):
            logging.getLogger(name).setLevel(resolved)
    return resolved


def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    """获取统一格式的 logging.Logger 实例。

    日志格式：[时间][级别][模块名][rid] 消息；``configure_logging`` 已执行时
    不再挂模块级 handler，统一由 ``hunter_sim`` 父 logger 出口（P1-11）。

    Args:
        name: 模块/类名称，作为日志记录器名称。
        level: 最低日志级别字符串；None 表示使用全局默认级别。

    Returns:
        配置好的 Logger 实例。
    """
    logger = logging.getLogger(name)
    if not logger.handlers and not _LOG_CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_make_text_formatter())
        handler.addFilter(_RequestIdFilter())
        logger.addHandler(handler)
    if level is not None:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    else:
        logger.setLevel(_DEFAULT_LEVEL)
    return logger


def get_struct_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取 structlog JSON 结构化日志记录器（生产环境使用）。

    ``configure_logging`` 已全局装配时直接复用全局配置（P1-11），
    否则按保守 JSON 管线局部配置（保持旧行为）。

    Args:
        name: 记录器名称。

    Returns:
        BoundLogger 实例。
    """
    if not _LOG_CONFIGURED:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        )
    bound_logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return bound_logger


# ─── RingBuffer ───────────────────────────────────────────────────────────────


class RingBuffer(Generic[T]):
    """线程安全的固定大小环形缓冲区。

    只保留最新 N 帧数据，超出时自动覆盖最旧数据。
    用于 VIL 遥测数据缓冲和传感器数据处理。

    Args:
        max_size: 缓冲区最大容量（帧数）。
    """

    def __init__(self, max_size: int = 10) -> None:
        if max_size < 1:
            raise ValueError("RingBuffer max_size must be >= 1")
        self._max_size: int = max_size
        self._buffer: deque[T] = deque(maxlen=max_size)
        self._lock: threading.Lock = threading.Lock()

    def push(self, item: T) -> None:
        """压入新数据，超容量时覆盖最旧项。"""
        with self._lock:
            self._buffer.append(item)

    def latest(self) -> Optional[T]:
        """返回最新一帧数据，缓冲区为空时返回 None。"""
        with self._lock:
            return self._buffer[-1] if self._buffer else None

    def get_range(self, last_n: int) -> list[T]:
        """返回最近 last_n 帧数据（从旧到新）。

        Args:
            last_n: 取回帧数，超出缓冲区大小时返回全部。

        Returns:
            数据列表，缓冲区为空时返回空列表。
        """
        with self._lock:
            items = list(self._buffer)
            return items[-last_n:] if last_n < len(items) else items

    @property
    def size(self) -> int:
        """当前缓冲区数据量。"""
        with self._lock:
            return len(self._buffer)

    @property
    def is_empty(self) -> bool:
        """缓冲区是否为空。"""
        with self._lock:
            return len(self._buffer) == 0

    def clear(self) -> None:
        """清空缓冲区。"""
        with self._lock:
            self._buffer.clear()


# ─── 数学/几何辅助 ────────────────────────────────────────────────────────────


def deg_to_rad(degrees: float) -> float:
    """角度转弧度。"""
    return math.radians(degrees)


def rad_to_deg(radians: float) -> float:
    """弧度转角度。"""
    return math.degrees(radians)


def normalize_angle_rad(angle: float) -> float:
    """将弧度归一化到 [-pi, pi] 范围内。

    Args:
        angle: 任意弧度值。

    Returns:
        归一化后的弧度值。
    """
    return (angle + math.pi) % (2 * math.pi) - math.pi


def lerp(a: float, b: float, t: float) -> float:
    """线性插值。

    Args:
        a: 起始值。
        b: 终止值。
        t: 插值因子，范围 [0, 1]。

    Returns:
        插值结果。
    """
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    """Hermite 平滑插值（S-curve）。

    Args:
        t: 输入值，范围 [0, 1]。

    Returns:
        平滑输出值。
    """
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def euclidean_distance_2d(x1: float, y1: float, x2: float, y2: float) -> float:
    """2D 欧几里得距离。"""
    return math.hypot(x2 - x1, y2 - y1)


def rmse(values: list[float]) -> float:
    """计算均方根误差（RMSE）。

    Args:
        values: 误差值列表。

    Returns:
        RMSE 值，空列表时返回 0.0。
    """
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


# ─── presigned 上传辅助 ────────────────────────────────────────────────────────

#: SigV2 presigned URL 中作为「已签名头」写入查询串、需在直传时原样回送的常见 HTTP 头。
#: presigner 将这些头纳入 canonical request，客户端不发或发不同值会导致 MinIO/S3
#: 重算签名与实际请求不符而返回 `SignatureDoesNotMatch`。子资源参数（uploadId/
#: partNumber/acl 等）**不在此列**，绝不回送为头。
_PRESIGNED_SIGNED_HEADER_PARAMS: tuple[str, ...] = (
    "content-type",
    "content-encoding",
    "content-disposition",
)


def presigned_upload_headers(url: str) -> dict[str, str]:
    """从 presigned 上传 URL 提取需随 PUT 回送的已签名头（V3.8.14）。

    SigV2 presigned URL 会把签名时纳入的 HTTP 头（如实机观测到的 `content-type`）以
    查询参数形式附加；直传请求必须原样发送同名头，否则 MinIO/S3 重算 canonical request
    与实际请求不符，返回 `SignatureDoesNotMatch`。返回 ``{头名: 值}``（小写头名），无则空 dict。

    Args:
        url: presigned 上传地址（含查询串）。

    Returns:
        需回送的请求头字典；解析失败或无签名头时返回空字典。
    """
    from urllib.parse import parse_qs, urlsplit  # noqa: PLC0415

    try:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    except Exception:  # noqa: BLE001  任何解析异常不影响主流程
        return {}
    headers: dict[str, str] = {}
    for param in _PRESIGNED_SIGNED_HEADER_PARAMS:
        for key, values in query.items():
            if key.lower() == param and values and values[0]:
                headers[param] = values[0]
                break
    return headers


def response_detail(response: Any, max_chars: int = 800) -> str:
    """从 HTTP 响应里取截断文本（供 4xx/5xx 定位），读取失败返回空串（V3.8.16 上提）。

    V3.8.9 在产物链路验证：服务端 422 响应体携带字段级校验错误（如
    ``field required``），不带响应体则 4xx 变黑盒无法排障。原为 artifacts 模块
    私有 helper，bag 链路（``cloud/bag_uploader.py``）同类 4xx 仍丢弃响应体
    致实机 complete 422 无法定位，故上提为共享工具供两条链路复用。

    兼容 mock（无 ``text``/``content`` 属性时 ``getattr`` 返回 None → 空串），
    不抛异常；优先 ``text``（已解码）后退 ``content``（字节）。

    Args:
        response: httpx Response 或同形对象。
        max_chars: 截断长度，超出补省略号。

    Returns:
        折叠空白后的截断文本；无文本/读取异常时为空串。
    """
    raw: Any = None
    for attr in ("text", "content"):
        try:
            raw = getattr(response, attr, None)
        except Exception:  # noqa: BLE001  任何读取异常都不影响主流程
            raw = None
        if raw:
            break
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text

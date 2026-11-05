"""仿真节点控制面客户端（HunterCore ``node`` 模式注册/心跳）。

平台侧链路（契约见 HunterCore ``scene-service`` 的 ``sim-node`` 路由）：

1. ``PUT /api/v1/scene/sim-nodes/{node_id}`` 注册能力（maps/capacity/manager_port）；
   目标 IP **不由节点上报**，由平台从入站请求头自学习；
2. ``POST /api/v1/scene/sim-nodes/{node_id}/heartbeat`` 每 ≤30s 续租并刷新来源地址
   —— 这是 IP 漂移的唯一自愈点；
3. 未注册/租约过期 → 平台返回 404 + ``code=3001``，客户端回到步骤 1 重新注册。

本模块只做「上行控制面」：仿真执行、VIL 数据面与平台回调仍由既有
``platform_adapter`` 管理面（:8090）承担。``enabled=false`` 时零副作用。
"""

from __future__ import annotations

import socket
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import quote, urlsplit

from hunter_sim.common.models import SimNodeSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

#: 平台节点控制面前缀（网关前缀 /api/v1/scene 下）
NODE_PATH = "/api/v1/scene/sim-nodes"
#: 平台在线租约默认值（秒）；响应会回显实际值
DEFAULT_LEASE_SECONDS = 90
#: 平台契约上限（心跳上报实例数 / maps 数）
MAX_RUNNING_INSTANCES = 32
MAX_MAPS = 64

ClientFactory = Callable[[], Any]
RunningIdProvider = Callable[[], list[str]]


@dataclass(frozen=True)
class _Outcome:
    """一次控制面调用结果（成功与否 + 平台错误语义）。"""

    ok: bool
    status_code: int
    code: Optional[int]
    message: str
    data: dict[str, Any]


class SimNodeClient:
    """注册/心跳客户端（后台线程，失败不阻断仿真）。

    Args:
        settings: 节点控制面配置。
        base_url: 平台基础地址（``settings.base_url`` 为空时由调用方传网关地址）。
        manager_port: 本机 CARLA 管理 API 端口（``settings.manager_port`` 为空时使用）。
        running_instance_ids: 活动实例 ID 提供者（心跳上报；缺省返回空列表）。
        client_factory: httpx 客户端工厂（单测注入桩）。
    """

    def __init__(
        self,
        settings: SimNodeSettings,
        *,
        base_url: str,
        manager_port: int = 8090,
        running_instance_ids: Optional[RunningIdProvider] = None,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        self._settings = settings
        self._base_url = _normalize_base_url(base_url)
        self._default_manager_port = int(manager_port)
        self._running_instance_ids = running_instance_ids or (lambda: [])
        self._client_factory = client_factory or _default_client
        self._client: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._registered = False
        self._needs_register = True
        self._last_ok_monotonic = 0.0
        self._last_ok_at = 0.0
        self._last_error = ""
        self._endpoint_ip = ""
        self._endpoint_source = ""
        self._mismatch_checked_endpoint = ""
        self._lease_seconds = DEFAULT_LEASE_SECONDS
        self._running_count = 0

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """启动后台注册/心跳线程；配置不完整时记录原因并返回 False。"""
        if not self._settings.enabled:
            logger.info("Sim node integration disabled (HUNTER_CORE_SIM_NODE_ENABLED=false)")
            return False
        problem = self._configuration_problem()
        if problem:
            logger.error(f"Sim node integration not started: {problem}")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="HunterSimNodeHeartbeat", daemon=True
            )
            self._thread.start()
        logger.info(
            f"Sim node heartbeat started: node_id={self._settings.node_id}, "
            f"base_url={self._base_url}, manager_port={self._manager_port()}, "
            f"interval={self._settings.heartbeat_interval_seconds}s"
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """停止心跳线程并关闭 HTTP 客户端（幂等）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001  关停不因连接池异常失败
                logger.debug(f"Sim node HTTP client close failed: {exc}")
        logger.info(f"Sim node heartbeat stopped: node_id={self._settings.node_id}")

    def _run(self) -> None:
        """后台循环：立即注册，随后按周期心跳；3001/404 回到注册分支。"""
        interval = float(self._settings.heartbeat_interval_seconds)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001  任何异常都不得杀死心跳线程
                logger.warning(f"Sim node heartbeat tick failed: {type(exc).__name__}: {exc}")
            if self._stop.wait(interval):
                break

    # ── 单步动作（单测直接调用）──────────────────────────────────────────────

    def tick(self) -> bool:
        """执行一次「注册或心跳」（返回本次调用是否成功）。"""
        if self._needs_register or not self._registered:
            return self.register()
        return self.heartbeat()

    def register(self) -> bool:
        """注册/更新节点能力（PUT，幂等 upsert）。"""
        if not self._ready():
            return False
        outcome = self._call(
            "PUT",
            f"{NODE_PATH}/{quote(self._settings.node_id, safe='')}",
            self._register_payload(),
        )
        if outcome.ok:
            self._apply_node_data(outcome.data, event="registered")
            with self._lock:
                self._registered = True
                self._needs_register = False
            return True
        self._record_failure("register", outcome)
        with self._lock:
            self._registered = False
            self._needs_register = True
        return False

    def heartbeat(self) -> bool:
        """心跳续租并上报活动实例；租约过期时标记下一轮重新注册。"""
        if not self._ready():
            return False
        outcome = self._call(
            "POST",
            f"{NODE_PATH}/{quote(self._settings.node_id, safe='')}/heartbeat",
            {"running_instance_ids": self._running_ids()},
        )
        if outcome.ok:
            self._apply_node_data(outcome.data, event="heartbeat")
            with self._lock:
                self._registered = True
                self._needs_register = False
            return True
        if outcome.status_code == 404 or outcome.code == 3001:
            logger.info(
                f"sim_node_lease_expired: node_id={self._settings.node_id}, "
                f"status={outcome.status_code}, message={outcome.message} "
                "(re-register on next tick)"
            )
            with self._lock:
                self._registered = False
                self._needs_register = True
        else:
            self._record_failure("heartbeat", outcome)
        return False

    def snapshot(self) -> dict[str, Any]:
        """本节点注册状态快照（``/health`` 展示；线程安全）。"""
        with self._lock:
            now = time.monotonic()
            lease = max(1, self._lease_seconds)
            online = (
                self._registered
                and self._last_ok_monotonic > 0.0
                and now - self._last_ok_monotonic <= lease
            )
            return {
                "enabled": bool(self._settings.enabled),
                "node_id": self._settings.node_id,
                "base_url": self._base_url,
                "online": online,
                "endpoint_ip": self._endpoint_ip or None,
                "endpoint_source": self._endpoint_source or None,
                "manager_port": self._manager_port(),
                "protocol": self._settings.protocol,
                "lease_seconds": lease,
                "running_count": self._running_count,
                "last_error": self._last_error or None,
                "last_ok_at": _iso(self._last_ok_at),
                "heartbeat_interval_seconds": self._settings.heartbeat_interval_seconds,
            }

    # ── V2.0 三步接入 step1→配置：安装管理员转交的接入包 ──────────────────

    @staticmethod
    def install_bundle(
        bundle_zip: Union[str, Path],
        dest_dir: Optional[Union[str, Path]] = None,
        *,
        extract: bool = True,
    ) -> Optional[Path]:
        """安装**管理员转交**的节点接入包 ZIP（接入清单 §1.5 三步接入 step1）。

        V1.24.2 起 ``GET /api/v1/scene/sim-nodes/{node_id}/bundle`` 挂 ``scene:execute``
        （角色 admin/operator），且包内含该节点 Kafka 客户端**私钥**（敏感物），
        节点服务令牌（默认 ``data-uploader``）**无权下载**（缺身份头→ 1001、角色不足→ 1002）。
        故接入包由**管理员下载并安全转交**仿真机，仿真端只做本地解压与配置，
        **不向平台请求该端点**。

        Args:
            bundle_zip: 管理员转交的接入包 ZIP 本地路径。
            dest_dir: 解压目标目录；为空则解压到 ZIP 同级的 ``<名>-bundle/``。
            extract: 是否解压（默认解压）。

        Returns:
            解压目录（``extract=True`` 且成功）或校验通过的 zip 路径；失败返回 None。
            任何文件/格式异常仅记日志并返回 None，不抛出（与心跳一样不阻断本机）。
        """
        src = Path(bundle_zip)
        if not src.is_file():
            logger.error(
                f"接入包安装失败：文件不存在 {src}（接入包由管理员下载并转交，仿真机不自行下载）"
            )
            return None
        if not zipfile.is_zipfile(src):
            logger.error(f"接入包安装失败：{src} 不是合法 ZIP")
            return None
        if not extract:
            logger.info(f"sim_node_bundle_validated: {src}（--no-extract，仅校验未解压）")
            return src
        dest = Path(dest_dir) if dest_dir is not None else src.parent
        extract_dir = dest / f"{src.stem}-bundle"
        try:
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(src) as zf:
                zf.extractall(extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            logger.error(f"接入包解压失败：{type(exc).__name__}: {exc}")
            return None
        logger.info(
            f"sim_node_bundle_extracted: {extract_dir}"
            "（后续：把管理员转交的一次性 SCRAM 口令填入 kafka.properties，见接入清单 §1.5 step2）"
        )
        return extract_dir

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _configuration_problem(self) -> str:
        """启动前配置完整性检查（返回空串表示可启动）。"""
        if not self._settings.node_id:
            return "HUNTER_CORE_SIM_NODE_ID 未配置"
        if self._settings.service_token is None or not (
            self._settings.service_token.get_secret_value().strip()
        ):
            return "HUNTER_CORE_SIM_NODE_SERVICE_TOKEN 未配置"
        if not self._base_url:
            return "平台地址为空（HUNTER_CORE_SIM_NODE_BASE_URL / HUNTER_CORE_GATEWAY_URL）"
        parts = urlsplit(self._base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return f"平台地址不合法：{self._base_url}"
        return ""

    def _ready(self) -> bool:
        """运行期就绪判定（配置缺失时静默跳过，启动时已记录原因）。"""
        return bool(
            self._settings.enabled
            and self._settings.node_id
            and self._settings.service_token is not None
            and self._base_url
        )

    def _manager_port(self) -> int:
        """平台回访端口（显式配置优先，否则用控制面监听端口）。"""
        return int(self._settings.manager_port or self._default_manager_port)

    def _register_payload(self) -> dict[str, Any]:
        """构造注册请求体（平台 ``extra="forbid"``，只发送已声明字段）。"""
        payload: dict[str, Any] = {
            "capacity": int(self._settings.capacity),
            "protocol": self._settings.protocol,
            "manager_port": self._manager_port(),
        }
        maps = _parse_maps(self._settings.maps)
        if maps:
            payload["maps"] = maps
        version = (self._settings.carla_version or "").strip() or _detect_carla_version()
        if version:
            payload["carla_version"] = version[:64]
        if self._settings.labels:
            payload["labels"] = {
                str(key)[:128]: str(value)[:512]
                for key, value in self._settings.labels.items()
            }
        hint = (self._settings.runtime_hint or "").strip()
        if hint:
            payload["runtime_hint"] = hint[:256]
        return payload

    def _running_ids(self) -> list[str]:
        """活动实例 ID（平台 heartbeat 只接受字符串数组，上限 32 项）。"""
        try:
            raw = self._running_instance_ids()
        except Exception as exc:  # noqa: BLE001  查询失败不得中断心跳
            logger.warning(f"Sim node running instance query failed: {exc}")
            return []
        items = [str(item) for item in raw if str(item).strip()]
        return items[:MAX_RUNNING_INSTANCES]

    def _call(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None
    ) -> _Outcome:
        """发起一次控制面 HTTP 调用（异常归一为失败结果）。"""
        url = f"{self._base_url}{path}"
        try:
            response = self._client_or_create().request(
                method,
                url,
                json=body,
                headers=self._headers(),
                timeout=float(self._settings.request_timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001  网络/证书/超时统一降级
            message = f"{type(exc).__name__}: {exc}"
            return _Outcome(False, 0, None, message, {})

        payload = _json_object(response)
        code = _as_int(payload.get("code"))
        message = str(payload.get("message") or "")
        data = payload.get("data")
        status_code = int(getattr(response, "status_code", 0) or 0)
        ok = 200 <= status_code < 300 and (code is None or code == 0)
        if not ok and not message:
            message = str(getattr(response, "text", "") or "")[:256]
        return _Outcome(
            ok, status_code, code, message, data if isinstance(data, dict) else {}
        )

    def _client_or_create(self) -> Any:
        """惰性创建 HTTP 客户端。"""
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _headers(self) -> dict[str, str]:
        """节点身份只认服务令牌（Bearer），不携带用户登录态。"""
        token = ""
        if self._settings.service_token is not None:
            token = self._settings.service_token.get_secret_value().strip()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _apply_node_data(self, data: dict[str, Any], *, event: str) -> None:
        """缓存平台回显的租约/地址，并在地址变化时打印排障日志。"""
        endpoint_ip = str(data.get("endpoint_ip") or "")
        endpoint_source = str(data.get("endpoint_source") or "")
        lease_seconds = _as_int(data.get("lease_seconds")) or DEFAULT_LEASE_SECONDS
        running_count = _as_int(data.get("running_count")) or 0
        with self._lock:
            previous = self._endpoint_ip
            self._endpoint_ip = endpoint_ip or self._endpoint_ip
            self._endpoint_source = endpoint_source or self._endpoint_source
            self._lease_seconds = max(1, lease_seconds)
            self._running_count = max(0, running_count)
            self._last_ok_monotonic = time.monotonic()
            self._last_ok_at = time.time()
            self._last_error = ""
            current = self._endpoint_ip
            check_mismatch = bool(current) and current != self._mismatch_checked_endpoint
            if check_mismatch:
                self._mismatch_checked_endpoint = current

        if event == "registered":
            logger.info(
                f"sim_node_registered: node_id={self._settings.node_id}, "
                f"endpoint_ip={current or '<unknown>'}, "
                f"endpoint_source={endpoint_source or '<unknown>'}, "
                f"manager_port={self._manager_port()}, lease_seconds={lease_seconds}"
            )
        if current and previous and current != previous:
            # IP 漂移自愈的关键证据：平台下一次下发会立即打到新地址
            logger.info(
                f"sim_node_endpoint_refreshed: node_id={self._settings.node_id}, "
                f"old={previous}, new={current}, source={endpoint_source or '<unknown>'}"
            )
        if check_mismatch:
            self._warn_endpoint_mismatch(current)

    def _record_failure(self, action: str, outcome: _Outcome) -> None:
        """记录失败（401/403 提示换令牌；404/3001 由调用方转注册）。"""
        hint = ""
        if outcome.status_code in (401, 403):
            hint = "（检查服务令牌是否过期/被撤销，或角色不在 SIM_NODE_ROLES）"
        elif outcome.status_code == 404 or outcome.code == 3001:
            hint = "（节点未注册或租约已过期，下一轮将重新注册）"
        message = (
            f"sim_node_{action}_failed: node_id={self._settings.node_id}, "
            f"status={outcome.status_code}, code={outcome.code}, "
            f"message={outcome.message or '<empty>'}{hint}"
        )
        with self._lock:
            self._last_error = f"{outcome.status_code}/{outcome.code}: {outcome.message}"
        logger.warning(message)

    def _warn_endpoint_mismatch(self, endpoint_ip: str) -> None:
        """平台学到的地址与本机网卡不一致时告警（代理/NAT/直连网关的早期信号）。"""
        if endpoint_ip in {"127.0.0.1", "::1", "localhost"}:
            return
        local = _local_ipv4_addresses()
        if local and endpoint_ip not in local:
            logger.warning(
                f"sim_node_endpoint_mismatch: node_id={self._settings.node_id}, "
                f"platform_learned={endpoint_ip}, local_ipv4={sorted(local)} "
                "（若经代理/NAT 属预期；若平台回访不通请检查接入路径）"
            )


def _default_client() -> Any:
    """构建默认 httpx 客户端（惰性导入，doctor 不拉网络依赖）。"""
    import httpx  # noqa: PLC0415

    return httpx.Client()


def _normalize_base_url(value: str) -> str:
    """规范化平台基址（去尾部斜杠；空串原样返回）。"""
    return (value or "").strip().rstrip("/")


def _parse_maps(raw: str) -> list[str]:
    """逗号分隔地图清单 → 去重列表（平台契约上限 64 项）。"""
    maps: list[str] = []
    seen: set[str] = set()
    for entry in (item.strip() for item in (raw or "").split(",")):
        if not entry or entry in seen:
            continue
        seen.add(entry)
        maps.append(entry)
        if len(maps) >= MAX_MAPS:
            break
    return maps


def _detect_carla_version() -> str:
    """从已安装的 carla SDK 元数据推导版本（失败返回空串）。"""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return str(version("carla"))
    except Exception:  # noqa: BLE001  未安装 SDK / 无包元数据
        return ""


def _json_object(response: Any) -> dict[str, Any]:
    """读取响应 JSON 对象（非 JSON/非对象统一返回空字典）。"""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001  网关错误页可能不是 JSON
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_int(value: Any) -> Optional[int]:
    """宽松整数转换（平台字段缺失/为字符串时不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _local_ipv4_addresses() -> set[str]:
    """本机 IPv4 地址集合（地址自学习排障用；失败返回空集合）。"""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(info[4][0]))
    except OSError:
        return addresses
    return addresses


def _iso(value: float) -> Optional[str]:
    """Unix 时间戳 → ISO8601 UTC（健康视图展示）。"""
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


__all__ = ["DEFAULT_LEASE_SECONDS", "NODE_PATH", "SimNodeClient"]

"""HunterSim 命令行工具入口（V3.1.1）。

当前提供：
- ``huntersim up``：一键启动仿真端（预检 → 控制面 → 探针面 → 打开可视化客户端）；
  ``--demo`` 可在就绪后自动提交一个 SIL 冒烟场景，立即在页面上看到画面/传感器/事件；
- ``huntersim carla-manager``：启动唯一业务入口（控制面 :8090）；
- ``huntersim probes``：启动只读运维探针面（:8080，仅 ``/health`` + ``/metrics``）；
- ``huntersim doctor``：部署前自检（配置/端口/磁盘/CARLA SDK/GPU 环境/
  实例库/录制目录/mcap/Kafka/平台），一次跑完单机部署指南里的手工检查项，
  失败即非零退出，可直接用于 CI 与上线前门禁；
- ``huntersim version``：打印包版本（供脚本核对镜像 tag 与代码版本是否一致）。

设计口径：
- ``doctor`` 只读检查，不修改任何状态，不启动服务；
- 依赖缺失（如无 nvidia-smi、未装 CARLA SDK、未装 mcap）记为 ``warn``/``skip``，
  只有会让服务起不来或跑不稳的问题才记 ``fail``；
- 输出为人读表格 + 摘要行，``--json`` 供脚本消费。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.engine.check_environment import check_environment

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BAD_ARGS = 2

# 磁盘阈值（GB）：低于 warn 阈值可运行但易写满录制文件；低于 fail 阈值直接拒绕
_MIN_DISK_WARN_GB = 20.0
_MIN_DISK_FAIL_GB = 5.0
# 录制目录可用空间 fail 阈值（GB）：低于此值无法支撑一次完整场景录制
_MIN_RECORDINGS_FREE_GB = 5.0

# 一键冒烟场景：默认时长（秒）与创建超时（秒）
_DEMO_DURATION_S = 120.0
_DEMO_CREATE_TIMEOUT_S = 20.0

_STATUS_ORDER = ("ok", "warn", "fail", "skip")


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果。

    Attributes:
        name: 检查项名称。
        status: ``ok`` / ``warn`` / ``fail`` / ``skip``。
        detail: 结论描述。
        hint: 修复建议（仅非 ok 时有意义）。
    """

    name: str
    status: str
    detail: str
    hint: str = ""


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, status="ok", detail=detail)


def _warn(name: str, detail: str, hint: str = "") -> CheckResult:
    return CheckResult(name=name, status="warn", detail=detail, hint=hint)


def _fail(name: str, detail: str, hint: str = "") -> CheckResult:
    return CheckResult(name=name, status="fail", detail=detail, hint=hint)


def _skip(name: str, detail: str, hint: str = "") -> CheckResult:
    return CheckResult(name=name, status="skip", detail=detail, hint=hint)


def _load_settings() -> tuple[Optional[HunterSimSettings], Optional[str]]:
    """加载配置；返回 ``(settings, 错误摘要)``。"""
    try:
        return HunterSimSettings(), None
    except Exception as exc:  # noqa: BLE001  pydantic 校验错误形态多样，只取可读摘要
        return None, f"{type(exc).__name__}: {exc}"


def _check_python() -> CheckResult:
    """Python 运行时版本（服务要求 ≥3.12）。"""
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= (3, 12):
        return _ok("python", f"Python {text}")
    return _fail("python", f"Python {text} < 3.12", "使用 3.12+ 解释器（uv venv --python 3.12）")


def _port_available(host: str, port: int, timeout: float = 1.0) -> bool:
    """端口是否未被占用（可 bind）。host 为空表示通配监听。"""
    family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.bind((host or "0.0.0.0", port))
            return True
        except OSError:
            return False


def _check_ports(settings: HunterSimSettings) -> list[CheckResult]:
    """管理面（:8090）与探针面（:8080）端口占用情况。

    端口已占用但可建立 TCP 连接→warn（疑似已有进程在跑）；无连接→fail。
    """
    results: list[CheckResult] = []
    targets = (
        ("adapter_port", settings.adapter.port, "ADAPTER_PORT"),
        ("probe_port", settings.probe.port, "PROBE_PORT"),
    )
    for name, port, env_key in targets:
        if _port_available("127.0.0.1", port):
            results.append(_ok(name, f"{port} 空闲，可启动服务"))
            continue
        reachable = _tcp_open("127.0.0.1", port)
        results.append(
            CheckResult(
                name=name,
                status="warn" if reachable else "fail",
                detail=(
                    f"127.0.0.1:{port} 已被占用"
                    + ("（疑似 HunterSim 已在运行）" if reachable else "（无法建立连接）")
                ),
                hint=f"停掉旧实例，或改 {env_key} 后重启",
            )
        )
    return results


def _tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """TCP 可连通（用于 CARLA RPC / 已启动服务的探针）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_carla_endpoints(settings: HunterSimSettings) -> list[CheckResult]:
    """CARLA 端点连通性与 SDK 安装情况。"""
    results: list[CheckResult] = []
    if importlib.util.find_spec("carla") is None:
        results.append(
            CheckResult(
                name="carla_sdk",
                status="warn",
                detail="当前解释器未安装 carla Python SDK",
                hint="pip install carla==<与 server 一致的版本>；未安装时场景下发返回 503",
            )
        )
    else:
        results.append(_ok("carla_sdk", "carla 模块可导入"))

    targets: list[tuple[str, int]] = [(settings.carla.host, settings.carla.rpc_port)]
    spec = (settings.execution.engine_endpoints or "").strip()
    if spec:
        targets = []
        for entry in (part.strip() for part in spec.split(",")):
            if not entry:
                continue
            entry_host, _, entry_port = entry.partition(":")
            targets.append((entry_host, int(entry_port.split(":")[0]) if entry_port else 2000))

    for target_host, target_port in targets:
        if _tcp_open(target_host, target_port):
            results.append(_ok("carla_reachable", f"{target_host}:{target_port} RPC 可连通"))
        else:
            managed = bool(settings.carla.supervisor_enabled and settings.carla.supervisor_binary_path)
            results.append(
                CheckResult(
                    name="carla_reachable",
                    status="warn" if managed else "fail",
                    detail=f"{target_host}:{target_port} RPC 不可达",
                    hint=(
                        "已启用进程托管，启动服务时会自动拉起"
                        if managed
                        else "先启动 CarlaUE4.exe，或配置 CARLA_SUPERVISOR_ENABLED=true + CARLA_SUPERVISOR_BINARY_PATH"
                    ),
                )
            )
    return results


def _check_disk(base_path: str = ".") -> CheckResult:
    """磁盘剩余空间（bag/日志写满会直接导致仿真失败）。"""
    try:
        usage = shutil.disk_usage(base_path)
    except OSError as exc:
        return _skip("disk_space", f"无法读取磁盘信息：{exc}")
    free_gb = usage.free / (1024**3)
    if free_gb < _MIN_DISK_FAIL_GB:
        return _fail(
            "disk_space",
            f"剩余 {free_gb:.1f} GB < {_MIN_DISK_FAIL_GB} GB",
            "清理 resources/ 下的历史 bag 与日志后重试",
        )
    if free_gb < _MIN_DISK_WARN_GB:
        return _warn(
            "disk_space",
            f"剩余 {free_gb:.1f} GB（建议 ≥{_MIN_DISK_WARN_GB} GB）",
            "长时间录制 bag 前建议扩容或清理",
        )
    return _ok("disk_space", f"剩余 {free_gb:.1f} GB")


def _check_adapter_db(settings: HunterSimSettings) -> CheckResult:
    """实例注册表（SQLite）可创建/可写（否则重启恢复与幂等失效）。"""
    db_path = Path(settings.adapter.db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        from hunter_sim.platform_adapter.store import InstanceStore  # 局部导入：doctor 不拉重依赖

        store = InstanceStore(str(db_path), settings.adapter.busy_timeout_seconds)
        try:
            store.connect()
        finally:
            store.close()
        return _ok("adapter_db", f"{db_path.as_posix()} 可读写")
    except Exception as exc:  # noqa: BLE001  权限/锁/磁盘问题统一为 fail
        return _fail(
            "adapter_db",
            f"{db_path.as_posix()} 初始化失败：{type(exc).__name__}: {exc}",
            "检查目录写权限与磁盘空间；必要时改用绝对路径 ADAPTER_DB_PATH",
        )


def _check_recordings_dir(settings: HunterSimSettings) -> CheckResult:
    """录制目录可写且剩余空间足够（mcap 写满会导致场景失败）。"""
    target = Path(settings.adapter.recordings_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return _fail(
            "recordings_dir",
            f"{target.as_posix()} 不可用：{type(exc).__name__}: {exc}",
            "检查目录写权限；必要时改用绝对路径 ADAPTER_RECORDINGS_DIR",
        )
    free_gb = usage.free / (1024**3)
    if free_gb < _MIN_RECORDINGS_FREE_GB:
        return _fail(
            "recordings_dir",
            f"{target.as_posix()} 剩余 {free_gb:.1f} GB < {_MIN_RECORDINGS_FREE_GB} GB",
            "清理历史录制文件（ADAPTER_RECORDING_RETENTION_HOURS）后重试",
        )
    return _ok("recordings_dir", f"{target.as_posix()} 可写，剩余 {free_gb:.1f} GB")


def _check_mcap() -> CheckResult:
    """mcap 写入依赖（纯 Python，不依赖 ROS2 运行时）。"""
    missing = [
        name for name in ("mcap", "mcap_ros2") if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return _ok("mcap", "mcap / mcap_ros2 可导入，传感器录制可用")
    return _warn(
        "mcap",
        f"缺少依赖：{', '.join(missing)}（录制降级为 no-op）",
        "pip install 'mcap>=1.1' 'mcap-ros2-support>=0.5'",
    )


def _check_platform(settings: HunterSimSettings) -> CheckResult:
    """平台连接（HunterCore 数据面）：未启用时 skip，启用时探测 api-gateway。

    平台连接不阻断仿真：网关不可达时仅遥测/上传降级，本机场景照常执行，
    因此记 ``warn`` 而非 ``fail``。
    """
    if not settings.cloud.enabled:
        return _skip("platform", "HUNTER_CORE_ENABLED=false（未接入平台，仅本机联调）")
    url = (settings.cloud.gateway_url or "").strip()
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return _warn(
            "platform",
            f"HUNTER_CORE_GATEWAY_URL 不可解析：{url or '<empty>'}",
            "期望形如 http://host:port",
        )
    if _tcp_open(host, port):
        return _ok("platform", f"{host}:{port} 可连通（api-gateway）")
    return _warn(
        "platform",
        f"{host}:{port} 不可达（遥测/上传将降级）",
        "确认网络与 HUNTER_CORE_GATEWAY_URL；本机联调可置 HUNTER_CORE_ENABLED=false",
    )


def _check_kafka(settings: HunterSimSettings) -> CheckResult:
    """Kafka 连通性（VIL 数据面；不可达时 VIL 链路无输入）。"""
    bootstrap = settings.kafka.bootstrap_servers
    if not bootstrap:
        return _skip("kafka", "KAFKA_BOOTSTRAP_SERVERS 未配置")
    first = bootstrap.split(",")[0].strip()
    host, _, port_text = first.rpartition(":")
    if not host or not port_text.isdigit():
        return _warn(
            "kafka",
            f"bootstrap 格式可疑：{bootstrap}",
            "期望形如 host:port[,host:port]",
        )
    if _tcp_open(host, int(port_text)):
        return _ok("kafka", f"{first} 可连通")
    return _warn(
        "kafka",
        f"{first} 不可达（VIL 无输入，场景将停在等待数据）",
        "先启动 Kafka（docker compose up kafka），或确认 KAFKA_BOOTSTRAP_SERVERS 为平台地址",
    )


def _describe_cuda(result: object) -> str:
    """拼装 cuda 检查项描述。

    ``cuda_version`` 是 nvidia-smi 表头里的驱动最高支持版本，``compute_cap`` 是 GPU 计算
    能力（如 8.9），两者不是同一回事，展示时分开标注以免误读。
    """
    cuda_version = getattr(result, "cuda_version", None)
    compute_cap = getattr(result, "gpu_compute_capability", None)
    text = f"CUDA {cuda_version or '未检测到'}"
    if compute_cap:
        text += f"（驱动支持），计算能力 {compute_cap}"
    return text


def _check_gpu() -> list[CheckResult]:
    """GPU 环境（复用 CARLA 环境检查；无 nvidia-smi 时降级为 warn/skip）。"""
    try:
        result = check_environment()
    except Exception as exc:  # noqa: BLE001  环境探测不应让 doctor 崩掉
        return [_skip("gpu_env", f"环境检查不可用：{type(exc).__name__}: {exc}")]

    results = [
        CheckResult(
            name="os",
            status="ok" if result.os_ok else "warn",
            detail=f"操作系统 {sys.platform}"
            + ("" if result.os_ok else "（CARLA 需运行在 Windows 主机）"),
            hint="" if result.os_ok else "在运行 CARLA 的 Windows 主机上执行 doctor，或改用 docker --profile carla",
        ),
        CheckResult(
            name="gpu_driver",
            status="ok" if result.gpu_driver_ok else "warn",
            detail=f"驱动 {result.gpu_driver_version or '未检测到'}，GPU {result.gpu_name or '未知'}",
            hint="" if result.gpu_driver_ok else "升级 NVIDIA 驱动（要求 ≥510）",
        ),
        CheckResult(
            name="cuda",
            status="ok" if result.cuda_ok else "warn",
            detail=_describe_cuda(result),
            hint="" if result.cuda_ok else "安装/升级 NVIDIA 驱动（要求驱动支持 CUDA ≥11.0）",
        ),
        CheckResult(
            name="gpu_memory",
            status="ok" if result.gpu_memory_ok else "warn",
            detail=f"显存 {result.gpu_memory_gb:.1f} GB",
            hint="" if result.gpu_memory_ok else "CARLA 至少需要 4 GB 显存，建议 ≥8 GB",
        ),
    ]
    if result.passed:
        return [_ok("gpu_env", f"GPU 环境检查通过（{result.gpu_name or 'NVIDIA'}）")] + [
            item for item in results if item.status != "ok"
        ]
    return results


def run_doctor(
    settings: HunterSimSettings | None = None,
    *,
    base_path: str = ".",
    strict: bool = False,
) -> list[CheckResult]:
    """执行全部预检项，返回结果列表（不抛异常）。

    Args:
        settings: 已加载配置（None 时自行加载）。
        base_path: 磁盘检查基准路径。
        strict: 将 ``warn`` 也视为失败（CI 上线门禁常用）。

    Returns:
        检查结果列表；配置无法加载时仅返回该条 fail。
    """
    loaded, error = (settings, None) if settings is not None else _load_settings()
    if loaded is None:
        return [
            _fail(
                "config",
                f"配置加载失败：{error}",
                "检查 .env 中的环境变量类型/枚举值；参考 docs/单机部署指南.md §2.3",
            )
        ]

    results: list[CheckResult] = [_ok("config", f"配置加载成功（env={loaded.env}）")]
    results.append(_check_python())
    results.append(_check_disk(base_path))
    results.append(_check_adapter_db(loaded))
    results.append(_check_recordings_dir(loaded))
    results.append(_check_mcap())
    results.append(_check_kafka(loaded))
    results.append(_check_platform(loaded))
    results.extend(_check_gpu())
    results.extend(_check_carla_endpoints(loaded))
    results.extend(_check_ports(loaded))
    if strict:
        results = [
            CheckResult(
                name=item.name,
                status="fail" if item.status == "warn" else item.status,
                detail=item.detail,
                hint=item.hint,
            )
            for item in results
        ]
    return results


def _counts(results: list[CheckResult]) -> dict[str, int]:
    """按状态统计（用于摘要行与退出码）。"""
    counts = {key: 0 for key in _STATUS_ORDER}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def _render_text(results: list[CheckResult]) -> str:
    """人读表格输出。"""
    name_width = max([len("CHECK")] + [len(item.name) for item in results])
    status_width = max([len("STATUS")] + [len(item.status) for item in results])
    lines = [f"{'CHECK'.ljust(name_width)}  {'STATUS'.ljust(status_width)}  DETAIL"]
    for item in results:
        lines.append(
            f"{item.name.ljust(name_width)}  {item.status.ljust(status_width)}  {item.detail}"
        )
        if item.status != "ok" and item.hint:
            lines.append(f"{''.ljust(name_width)}  {''.ljust(status_width)}  → {item.hint}")
    counts = _counts(results)
    lines.append("")
    lines.append(
        f"Summary: {counts['ok']} ok, {counts['warn']} warn, "
        f"{counts['fail']} fail, {counts['skip']} skip"
    )
    return "\n".join(lines)


def _version_text() -> str:
    """包版本（未安装时降级为 unknown）。"""
    try:
        from importlib.metadata import version

        return version("hunter-sim")
    except Exception:  # noqa: BLE001  源码直跑无包元数据
        return "0.0.0+unknown"


def _cmd_version(_args: argparse.Namespace) -> int:
    """``huntersim version`` 子命令。"""
    print(_version_text())
    return EXIT_OK


def _cmd_carla_manager(args: argparse.Namespace) -> int:
    """``huntersim carla-manager`` 子命令：启动平台控制平面（默认 8090）。"""
    from hunter_sim.platform_adapter.app import create_app
    from hunter_sim.platform_adapter.config import AdapterSettings

    overrides: dict[str, Any] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.db_path:
        overrides["db_path"] = args.db_path
    adapter = AdapterSettings(**overrides)

    import uvicorn  # noqa: PLC0415  仅 carla-manager 需要（doctor 不拉 Web 依赖）

    app = create_app(adapter_settings=adapter)
    print(f"CARLA manager listening on http://{adapter.host}:{adapter.port}")
    uvicorn.run(app, host=adapter.host, port=adapter.port, log_level=args.log_level)
    return EXIT_OK


def _cmd_probes(args: argparse.Namespace) -> int:
    """``huntersim probes`` 子命令：启动只读探针面（默认 8080）。"""
    from hunter_sim.common.models import ProbeSettings
    from hunter_sim.probes.app import create_probes_app

    overrides: dict[str, Any] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.adapter_url:
        overrides["adapter_url"] = args.adapter_url
    probe = ProbeSettings(**overrides)

    import uvicorn  # noqa: PLC0415  仅服务类子命令需要（doctor 不拉 Web 依赖）

    settings, error = _load_settings()
    if settings is None:
        print(f"配置加载失败，探针将以默认值启动：{error}", file=sys.stderr)
        settings = HunterSimSettings.model_construct(probe=probe)
    else:
        settings = settings.model_copy(update={"probe": probe})

    app = create_probes_app(settings)
    print(f"HunterSim probes listening on http://{probe.host}:{probe.port}")
    uvicorn.run(app, host=probe.host, port=probe.port, log_level=args.log_level)
    return EXIT_OK


def _print_plan(specs: list[Any], viz: str, settings: HunterSimSettings) -> None:
    """打印启动计划（各模块地址与连接目标）。"""
    carla = settings.carla
    print("")
    print("仿真端启动计划：")
    for spec in specs:
        role = "控制面（唯一业务入口）" if spec.required else "只读探针面"
        print(f"  {spec.name:<14} {spec.url:<28} {role}")
    print(f"  {'可视化客户端':<11} {viz:<28} 画面/传感器/事件/日志")
    print(f"  {'CARLA 引擎':<12} {carla.host}:{carla.rpc_port}" + (
        "（进程托管已启用，未就绪时由控制面自动拉起）"
        if carla.supervisor_enabled and carla.supervisor_binary_path
        else "（需先启动 CarlaUE4.exe）"
    ))
    platform = "未启用（本机联调）" if not settings.cloud.enabled else settings.cloud.gateway_url
    print(f"  {'平台接入':<13} {platform}")
    print("")


def _enable_line_buffering() -> None:
    """标准输出改为行缓冲（现场 `huntersim up > 日志` 时进度仍实时可见）。

    重定向到文件时 ``sys.stdout`` 是块缓冲，子进程启动信息会“压在缓冲区里”
    直到退出才落盘，看起来像卡住。仅尽力而为：非 TTY 替身/已被替换的
    流可能不支持 ``reconfigure``，失败不影响主流程。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(line_buffering=True)
    except (ValueError, OSError):
        pass


def _manager_base_url(settings: HunterSimSettings) -> str:
    """控制面本机基础地址（通配监听回落 ``127.0.0.1``）。"""
    host = (settings.adapter.host or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.adapter.port}"


def _demo_payload(scene_id: str, *, duration_s: float = _DEMO_DURATION_S) -> dict[str, Any]:
    """构造 ``--demo`` 的 SIL 冒烟场景请求体（字段口径与平台下发一致）。"""
    return {
        "scene_id": scene_id,
        "scene_name": "一键冒烟（SIL · Town03）",
        "mode": "sil",
        "scene_config": {
            "map": {
                "map_id": "Town03",
                "spawn_point": {"x": 0.0, "y": 0.0, "z": 0.3, "yaw": 0.0},
            },
            "ego_vehicle": {"model": "hunter_se"},
            # 冒烟不写录制文件，避免占满现场磁盘；需要录制时用平台/curl 下发
            "sensors": [
                {
                    "profile": "hunter_se_full",
                    "enabled": True,
                    "ros_enabled": False,
                    "record": False,
                }
            ],
            "duration": duration_s,
        },
    }


def _create_demo_instance(
    settings: HunterSimSettings,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    scene_id: str = "",
    duration_s: float = _DEMO_DURATION_S,
) -> str:
    """创建一个 SIL 冒烟实例，返回实例 ID。

    Args:
        settings: 全局配置（取 ``adapter.host/port``）。
        opener: HTTP 客户端（默认 ``urllib.request.urlopen``，单测可注入）。
        scene_id: 场景 ID（空则按时间戳生成，ASCII 以免控制台编码丢失）。
        duration_s: 场景时长（秒）。

    Returns:
        新建实例的 ``instance_id``。

    Raises:
        RuntimeError: HTTP 失败、响应非 JSON 或缺字段。
    """
    scene = scene_id or f"demo-smoke-{int(time.time())}"
    url = f"{_manager_base_url(settings)}/api/v1/instances"
    # 显式 UTF-8：Windows 控制台默认 GBK，否则中文场景名会变成“????”
    body = json.dumps(_demo_payload(scene, duration_s=duration_s), ensure_ascii=False)
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with opener(request, timeout=_DEMO_CREATE_TIMEOUT_S) as response:  # noqa: S310  仅本机回环
            raw = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"创建冒烟场景失败（{url}）：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"创建冒烟场景响应不可解析：{exc}") from exc
    instance_id = str(payload.get("instance_id") or payload.get("sim_instance_id") or "")
    if not instance_id:
        raise RuntimeError(f"创建冒烟场景响应缺少 instance_id：{payload}")
    return instance_id


def _run_demo(settings: HunterSimSettings) -> None:
    """``up --demo``：就绪后提交冒烟场景并打印查看方式。"""
    from hunter_sim.visualization.routes import viz_url

    instance_id = _create_demo_instance(settings)
    print(f"[up] 已创建冒烟场景：{instance_id}", flush=True)
    print(f"[up] 在页面左上角下拉选中该实例即可看画面/传感器/事件：{viz_url(settings)}", flush=True)
    print(
        "[up] 若 CARLA 尚未就绪，实例会以 failed 结束并在页面「原因」显示原因；引擎拉起后重跑 --demo",
        flush=True,
    )


def _cmd_up(args: argparse.Namespace) -> int:
    """``huntersim up`` 子命令：一键启动仿真端并打开可视化客户端。"""
    _enable_line_buffering()
    from hunter_sim.visualization.launcher import Launcher, build_service_specs
    from hunter_sim.visualization.routes import viz_url

    settings, error = _load_settings()
    if settings is None:
        print(f"配置加载失败：{error}", file=sys.stderr)
        return EXIT_FAIL

    if not args.skip_doctor:
        results = run_doctor(settings, base_path=args.path, strict=args.strict)
        print(f"HunterSim up (version={_version_text()})")
        print(_render_text(results))
        counts = _counts(results)
        if counts["fail"] > 0 and not args.force:
            print(
                "预检存在 fail 项，已中止启动（确认无影响后可加 --force 继续）",
                file=sys.stderr,
            )
            return EXIT_FAIL
    else:
        print(f"HunterSim up (version={_version_text()})（已跳过预检）")

    specs = build_service_specs(
        settings,
        python=sys.executable,
        log_level=args.log_level,
        include_probes=not args.no_probes,
    )
    url = viz_url(settings)
    _print_plan(specs, url, settings)
    open_browser = (not args.no_browser) and settings.viz.auto_open_browser
    if open_browser:
        print("启动完成后将自动打开浏览器；可用 --no-browser 关闭。")
    on_ready = (lambda: _run_demo(settings)) if args.demo else None
    return Launcher().run(specs, url, open_browser=open_browser, on_ready=on_ready)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``huntersim doctor`` 子命令。"""
    settings, error = _load_settings()
    if settings is None:
        print(f"配置加载失败：{error}", file=sys.stderr)
        results = run_doctor(None, base_path=args.path, strict=args.strict)
    else:
        results = run_doctor(settings, base_path=args.path, strict=args.strict)
    if args.json:
        payload: dict[str, Any] = {
            "version": _version_text(),
            "env": settings.env if settings is not None else None,
            "checks": [asdict(item) for item in results],
            "counts": _counts(results),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"HunterSim doctor (version={_version_text()})")
        print(_render_text(results))
    counts = _counts(results)
    if counts["fail"] > 0:
        return EXIT_FAIL
    if args.strict and counts["warn"] > 0:
        return EXIT_FAIL
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """构造命令解析器。"""
    parser = argparse.ArgumentParser(
        prog="huntersim",
        description="HunterSim 运维命令行工具（部署预检与版本查询）",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    up = subparsers.add_parser(
        "up",
        help="一键启动仿真端（预检 + 控制面 + 探针面 + 可视化客户端）",
        description=(
            "一键启动：先跑 doctor 预检，失败即中止；然后拉起控制面（:8090）与"
            "只读探针面（:8080），最后打开可视化客户端 http://<host>:8090/viz。"
            "Ctrl+C 统一回收子进程。"
        ),
    )
    up.add_argument("--skip-doctor", action="store_true", help="跳过启动前预检")
    up.add_argument("--strict", action="store_true", help="预检把 warn 也视为失败")
    up.add_argument("--force", action="store_true", help="预检有 fail 项时仍继续启动")
    up.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    up.add_argument("--no-probes", action="store_true", help="不拉起只读探针面（:8080）")
    up.add_argument(
        "--demo",
        action="store_true",
        help="就绪后自动创建一个 SIL 冒烟场景（Town03 / hunter_se_full / 120s），页面立即有数据",
    )
    up.add_argument("--path", default=".", help="预检磁盘与状态库的基准路径")
    up.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="子服务 uvicorn 日志级别",
    )
    up.set_defaults(handler=_cmd_up)

    doctor = subparsers.add_parser(
        "doctor",
        help="部署前自检：配置/端口/磁盘/GPU/CARLA/实例库/录制目录/mcap/Kafka",
        description=(
            "一次跑完单机部署的手工检查项。默认只读、不修改任何状态；"
            "存在 fail 项时退出码为 1（--strict 时 warn 也计失败）。"
        ),
    )
    doctor.add_argument("--json", action="store_true", help="以 JSON 输出结果（供脚本消费）")
    doctor.add_argument(
        "--strict", action="store_true", help="把 warn 视为失败（上线门禁）"
    )
    doctor.add_argument(
        "--path", default=".", help="磁盘与状态库检查的基准路径（默认当前目录）"
    )
    doctor.set_defaults(handler=_cmd_doctor)

    manager = subparsers.add_parser(
        "carla-manager",
        help="启动 CARLA 管理面控制平面（HunterCore -> HunterSim，默认 8090）",
        description=(
            "启动平台控制平面：创建/下发/查询/控制仿真实例。单世界串行执行；"
            "地图加载在后台完成，create/submit 立即返回。仅监听内网，不加鉴权头。"
        ),
    )
    manager.add_argument("--host", default="", help="监听地址（默认取 ADAPTER_HOST）")
    manager.add_argument("--port", type=int, default=0, help="监听端口（默认取 ADAPTER_PORT=8090）")
    manager.add_argument("--db-path", default="", help="实例注册表路径（默认取 ADAPTER_DB_PATH）")
    manager.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别",
    )
    manager.set_defaults(handler=_cmd_carla_manager)

    probes = subparsers.add_parser(
        "probes",
        help="启动只读探针面（/health + /metrics，默认 8080）",
        description=(
            "启动只读探针面：只暴露 /health（含管理面聚合状态）与 /metrics；"
            "不承载业务、不写数据库、不依赖鉴权。"
        ),
    )
    probes.add_argument("--host", default="", help="监听地址（默认取 PROBE_HOST）")
    probes.add_argument("--port", type=int, default=0, help="监听端口（默认取 PROBE_PORT=8080）")
    probes.add_argument(
        "--adapter-url", default="", help="管理面基础地址（默认取 PROBE_ADAPTER_URL）"
    )
    probes.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别",
    )
    probes.set_defaults(handler=_cmd_probes)

    version_parser = subparsers.add_parser("version", help="打印包版本")
    version_parser.set_defaults(handler=_cmd_version)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表（None 取 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_BAD_ARGS
    code: int = int(handler(args))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

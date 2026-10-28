"""HunterSim 命令行工具入口（V2.8.0 P2-16）。

当前提供：
- ``huntersim doctor``：部署前自检（配置/密钥强度/端口/磁盘/CARLA SDK/GPU 环境/状态库），
  一次跑完单机部署指南里的手工检查项，失败即非零退出，可直接用于 CI 与上线前门禁；
- ``huntersim version``：打印包版本（供脚本核对镜像 tag 与代码版本是否一致）。

设计口径：
- 只读检查，不修改任何状态，不启动服务；
- 依赖缺失（如无 nvidia-smi、未装 CARLA SDK）记为 ``warn``/``skip``，
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.engine.check_environment import check_environment

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BAD_ARGS = 2

# 磁盘阈值（GB）：低于 warn 阈值可运行但易写满 bag；低于 fail 阈值直接拒绝
_MIN_DISK_WARN_GB = 20.0
_MIN_DISK_FAIL_GB = 5.0
# 生产密钥最小长度（与 api/main.py 启动闸门一致）
_MIN_PROD_SECRET_LEN = 32
_INSECURE_SECRETS = frozenset(
    {"CHANGE_ME_IN_PRODUCTION", "dev-only-change-me-at-least-32-characters"}
)

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


def _check_secrets(settings: HunterSimSettings) -> list[CheckResult]:
    """密钥与口令强度（prod 与启动闸门同口径，dev 只提示）。"""
    results: list[CheckResult] = []
    secret = settings.api.jwt_secret.get_secret_value()
    prod = settings.env == "prod"
    if secret in _INSECURE_SECRETS:
        results.append(
            CheckResult(
                name="jwt_secret",
                status="fail" if prod else "warn",
                detail="API_JWT_SECRET 使用内置默认值",
                hint="生成随机密钥：python -c \"import secrets;print(secrets.token_urlsafe(48))\"",
            )
        )
    elif len(secret) < _MIN_PROD_SECRET_LEN:
        results.append(
            CheckResult(
                name="jwt_secret",
                status="fail" if prod else "warn",
                detail=f"API_JWT_SECRET 长度 {len(secret)} < {_MIN_PROD_SECRET_LEN}",
                hint="prod 启动会被闸门拒绝，请替换为 ≥32 位随机密钥",
            )
        )
    else:
        results.append(_ok("jwt_secret", f"长度 {len(secret)}（已自定义）"))

    admin_password = settings.api.admin_password.get_secret_value()
    if admin_password == "admin":
        results.append(
            CheckResult(
                name="admin_password",
                status="fail" if prod else "warn",
                detail="API_ADMIN_PASSWORD 仍为默认 'admin'",
                hint="V2.8.0 建议改用用户表账号（POST /auth/users）并覆盖内置口令",
            )
        )
    elif len(admin_password) < settings.platform.min_password_length:
        results.append(
            _warn(
                "admin_password",
                f"内置管理员口令长度 {len(admin_password)} < 平台策略 "
                f"{settings.platform.min_password_length}",
                "用户表口令会按 PLATFORM_MIN_PASSWORD_LENGTH 校验，建议同步加强",
            )
        )
    else:
        results.append(_ok("admin_password", "已自定义"))
    return results


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
    """API 端口与 CARLA 端口占用情况（API 端口被占=已有实例或端口冲突）。"""
    results: list[CheckResult] = []
    api_port = settings.api.port
    if _port_available("127.0.0.1", api_port):
        results.append(_ok("api_port", f"{api_port} 空闲，可启动服务"))
    else:
        reachable = _tcp_open("127.0.0.1", api_port)
        results.append(
            CheckResult(
                name="api_port",
                status="warn" if reachable else "fail",
                detail=(
                    f"127.0.0.1:{api_port} 已被占用"
                    + ("（疑似 HunterSim 已在运行）" if reachable else "（无法建立连接）")
                ),
                hint="停掉旧实例，或改 API_PORT 后重启",
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


def _check_state_store(settings: HunterSimSettings) -> CheckResult:
    """状态库可创建/可写（否则重启恢复与 API Key 会整体降级）。"""
    if not settings.state.enabled:
        return _skip(
            "state_store",
            "STATE_ENABLED=false（实例/场景绑定不落库，重启不恢复）",
            "生产建议置 true 以获得重启恢复能力",
        )
    db_path = Path(settings.state.db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        from hunter_sim.persistence.state_store import StateStore  # 局部导入：doctor 不拉重依赖

        store = StateStore(str(db_path), settings.state.busy_timeout_seconds)
        try:
            users = store.count_users()
        finally:
            store.close()
        return _ok(
            "state_store",
            f"{db_path.as_posix()} 可读写（users={users}）",
        )
    except Exception as exc:  # noqa: BLE001  权限/锁/磁盘问题统一为 fail
        return _fail(
            "state_store",
            f"{db_path.as_posix()} 初始化失败：{type(exc).__name__}: {exc}",
            "检查目录写权限与磁盘空间；必要时改用绝对路径 STATE_DB_PATH",
        )


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
            detail=f"CUDA {result.cuda_version or '未检测到'}",
            hint="" if result.cuda_ok else "安装/升级 CUDA（要求 ≥11.0）",
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


def _check_users(settings: HunterSimSettings) -> CheckResult:
    """多用户体系可用性（V2.8.0 P2-13）。"""
    if not settings.platform.users_enabled:
        return _skip(
            "users",
            "PLATFORM_USERS_ENABLED=false（仅内置管理员可登录）",
            "多用户场景请置 true 并启用 STATE_ENABLED",
        )
    if not settings.state.enabled:
        return _warn(
            "users",
            "用户表依赖状态库，但 STATE_ENABLED=false",
            "启用 STATE_ENABLED=true 后 /auth/users 才可用（否则返回 503）",
        )
    return _ok("users", "用户表登录已启用（/auth/users 由 admin 管理）")


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
    results.extend(_check_secrets(loaded))
    results.append(_check_users(loaded))
    results.append(_check_disk(base_path))
    results.append(_check_state_store(loaded))
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
        from importlib.metadata import PackageNotFoundError, version

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

    doctor = subparsers.add_parser(
        "doctor",
        help="部署前自检：配置/密钥/端口/磁盘/GPU/CARLA/状态库",
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
        help="启动 CARLA 管理面控制平面（HunterCore ↔ HunterSim，默认 8090）",
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

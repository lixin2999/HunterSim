"""Kafka 客户端安全参数构建（SASL / TLS / mTLS）。

HunterCore 的 9093 接入面默认 ``KAFKA_CFG_SSL_CLIENT_AUTH=required``：
只配 SCRAM 账号、不提供客户端证书时 TCP 能连上但 TLS 握手直接失败
（表现为 producer 初始化即降级为 no-op，日志里只有一条难定位的 SSL 错误）。

本模块把 ``security_protocol`` / SASL 凭据 / CA / 客户端证书路径翻译成
kafka-python 的关键字参数，由发布侧（``SimDataProducer`` /
``TelemetryProducer``，``HUNTER_CORE_*``）与消费侧（``VILDataConsumer``，
``KAFKA_*``，V3.1.2 起）共用，避免多处各写一份导致配置漂移。

注意 kafka-python 对未识别的配置键**直接抛** ``KafkaConfigurationError``，
因此凭据键名必须是 ``sasl_plain_username`` / ``sasl_plain_password``
（历史写法 ``sasl_username`` 属于非法键，会被消费者初始化直接拒绝）。
"""

from __future__ import annotations

import os
import re
import ssl
from pathlib import Path
from typing import Any, Optional, Protocol

from hunter_sim.common.models import KafkaSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 合法的 security_protocol 取值（大小写不敏感）
VALID_PROTOCOLS = frozenset({"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"})
# 依赖账号口令的 SASL 机制（GSSAPI/OAUTHBEARER 走各自的凭据来源）
PASSWORD_MECHANISMS = frozenset({"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"})

# 证书自动发现：证书目录候选（相对 cwd / 仓库根）与文件名候选
_CERT_DIR_ENV = "HUNTER_SIM_CERT_DIRS"
_CERT_SUBDIRS = ("data/certs", "data/kafka-certs", "certs")
_CA_FILENAMES = ("ca-cert.pem", "ca.pem", "ca.crt")
_CLIENT_CERT_FILENAMES = ("client-cert.pem", "client.pem", "client.crt")
_CLIENT_KEY_FILENAMES = ("client-key.pem", "client.key")


class KafkaTLSSettings(Protocol):
    """SSL/mTLS 配置的最小接口（``CloudSettings`` / ``KafkaSettings`` 均满足）。"""

    security_protocol: str
    ca_cert_path: Optional[str]
    client_cert_path: Optional[str]
    client_key_path: Optional[str]
    ssl_verify: bool
    auto_discover_certs: bool


def normalize_protocol(raw: str) -> str:
    """归一化 ``security_protocol``（去空白 + 大写）。

    Args:
        raw: 原始配置值，空串按 ``PLAINTEXT`` 处理。

    Returns:
        四个合法取值之一。

    Raises:
        ValueError: 取值不在 ``PLAINTEXT/SSL/SASL_PLAINTEXT/SASL_SSL`` 内。
    """
    protocol = (raw or "").strip().upper() or "PLAINTEXT"
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(
            f"不支持的 Kafka 安全协议：{raw!r}"
            "（可选 PLAINTEXT / SSL / SASL_PLAINTEXT / SASL_SSL）"
        )
    return protocol


def build_ssl_kwargs(
    settings: KafkaTLSSettings, *, env_prefix: str = "HUNTER_CORE"
) -> dict[str, Any]:
    """按接入配置生成 kafka-python 的 SSL 参数。

    仅当 ``security_protocol`` 以 ``SSL`` 结尾时才返回参数（``SASL_SSL`` /
    ``SSL``）；客户端证书与私钥必须成对配置，缺一即视为配置错误。

    证书路径未显式配置且 ``auto_discover_certs`` 为真时，自动搜索
    ``data/certs/``、``data/kafka-certs/``、``certs/``（可用
    ``HUNTER_SIM_CERT_DIRS`` 指定额外证书目录），解决“CA 已随接入包下发但
    没写进 .env，导致 self-signed certificate in certificate chain”的现场问题。

    ``ssl_verify=False`` 时改为构造跳过服务端校验的 ``ssl.SSLContext``
    （``ssl_context`` 会覆盖 kafka-python 的其他 ``ssl_*`` 参数），仅限内网
    自签环境临时使用。

    Args:
        settings: 接入配置（发布侧 ``CloudSettings`` / 消费侧 ``KafkaSettings``）。
        env_prefix: 环境变量前缀，仅用于错误信息定位（默认 HunterCore）。

    Returns:
        ``ssl_cafile`` / ``ssl_certfile`` / ``ssl_keyfile``（或 ``ssl_context``）
        组成的字典，非 SSL 协议或未配置证书时为空字典。

    Raises:
        ValueError: 客户端证书与私钥只配置了其中一个。
        FileNotFoundError: 配置的证书文件不存在（提前失败，避免握手期才报错）。
    """
    if not settings.security_protocol.upper().strip().endswith("SSL"):
        return {}

    ca_path = settings.ca_cert_path
    cert_path = settings.client_cert_path
    key_path = settings.client_key_path
    if bool(getattr(settings, "auto_discover_certs", True)) and not (
        ca_path or cert_path or key_path
    ):
        discovered = discover_cert_paths()
        ca_path = ca_path or discovered.get("ca")
        cert_path = cert_path or discovered.get("cert")
        key_path = key_path or discovered.get("key")
        if discovered:
            logger.info(
                "Kafka TLS 证书自动发现: ca=%s cert=%s key=%s",
                ca_path or "-",
                cert_path or "-",
                key_path or "-",
            )

    if not cert_path and not key_path:
        cert_pair: Optional[tuple[str, str]] = None
    elif not cert_path or not key_path:
        raise ValueError(
            "mTLS 客户端证书配置不完整："
            f"{env_prefix}_CLIENT_CERT_PATH 与 {env_prefix}_CLIENT_KEY_PATH 必须同时设置"
        )
    else:
        cert_pair = (cert_path, key_path)

    if not bool(getattr(settings, "ssl_verify", True)):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        if cert_pair is not None:
            context.load_cert_chain(
                _require_file(cert_pair[0], "mTLS 客户端证书"),
                _require_file(cert_pair[1], "mTLS 客户端私钥"),
            )
        logger.warning(
            "Kafka TLS 服务端证书校验已关闭（%s_SSL_VERIFY=false），仅限内网自签环境",
            env_prefix,
        )
        return {"ssl_context": context}

    kwargs: dict[str, Any] = {}
    if ca_path:
        kwargs["ssl_cafile"] = _require_file(ca_path, "CA 证书")
    if cert_pair is not None:
        kwargs["ssl_certfile"] = _require_file(cert_pair[0], "mTLS 客户端证书")
        kwargs["ssl_keyfile"] = _require_file(cert_pair[1], "mTLS 客户端私钥")
    return kwargs


def discover_cert_paths() -> dict[str, str]:
    """自动搜索 CA / 客户端证书 / 私钥。

    搜索根目录：``HUNTER_SIM_CERT_DIRS``（``;``/``,`` 分隔的证书目录）→
    当前工作目录 → 仓库根目录（后两者下依次尝试 ``data/certs`` /
    ``data/kafka-certs`` / ``certs``）。

    Returns:
        ``{"ca": ..., "cert": ..., "key": ...}``；未找到的键不出现。
    """
    found: dict[str, str] = {}
    targets = (
        ("ca", _CA_FILENAMES),
        ("cert", _CLIENT_CERT_FILENAMES),
        ("key", _CLIENT_KEY_FILENAMES),
    )
    for base in _cert_directories():
        for key, names in targets:
            if key in found:
                continue
            path = _first_existing(base, names)
            if path:
                found[key] = path
    return found


def _cert_directories() -> list[Path]:
    """展开证书搜索目录（环境变量给定目录优先，其次 cwd/仓库下默认子目录）。"""
    directives: list[Path] = []
    raw = os.environ.get(_CERT_DIR_ENV, "")
    directives.extend(
        Path(item.strip()).expanduser() for item in re.split(r"[;,]", raw) if item.strip()
    )
    for root in (Path.cwd(), Path(__file__).resolve().parents[3]):
        directives.extend(root / subdir for subdir in _CERT_SUBDIRS)
    directories: list[Path] = []
    seen: set[Path] = set()
    for directive in directives:
        candidate = directive.resolve()
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        directories.append(candidate)
    return directories


def _first_existing(base: Path, names: tuple[str, ...]) -> str:
    """返回目录下第一个存在的文件名（不存在返回空串）。"""
    for name in names:
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    return ""


def ssl_failure_hint(exc: BaseException) -> str:
    """TLS/证书类错误的可操作排查提示（其他异常返回空串，避免噪声）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    if not any(key in text for key in ("ssl", "certificate", "tls", "handshake")):
        return ""
    return (
        " 排查：将平台 CA 放到 data/certs/ca-cert.pem 或 data/kafka-certs/"
        "（默认自动发现），或显式设置 HUNTER_CORE_CA_CERT_PATH / KAFKA_CA_CERT_PATH；"
        "自签证书链无法校验时可临时设置 HUNTER_CORE_SSL_VERIFY=false / "
        "KAFKA_SSL_VERIFY=false（仅限内网）"
    )


def build_consumer_security_kwargs(settings: KafkaSettings) -> dict[str, Any]:
    """生成 VIL 消费端的完整接入参数（协议 + SASL 凭据 + TLS 证书）。

    参数在消费者构造期解析：配置错误立即抛出，让实例以明确原因失败，
    而不是起一个永远收不到数据的空转线程。

    Args:
        settings: Kafka 连接配置（``KAFKA_*``）。

    Returns:
        ``security_protocol`` 必含；协议为 ``SASL_*`` 时追加 ``sasl_mechanism``
        与 ``sasl_plain_username`` / ``sasl_plain_password``；协议以 ``SSL`` 结尾时
        追加 ``ssl_cafile`` / ``ssl_certfile`` / ``ssl_keyfile``。

    Raises:
        ValueError: 协议取值非法、SASL 口令机制缺账号、客户端证书与私钥未成对。
        FileNotFoundError: 证书文件不存在。
    """
    protocol = normalize_protocol(settings.security_protocol)
    # 空串等同未配置（.env 模板里 KAFKA_USERNAME= 是常态，不应触发告警）
    username = settings.username.get_secret_value().strip() if settings.username else ""
    password = settings.password.get_secret_value() if settings.password else ""
    kwargs: dict[str, Any] = {"security_protocol": protocol}

    if protocol.startswith("SASL"):
        mechanism = (settings.sasl_mechanism or "SCRAM-SHA-512").strip().upper()
        kwargs["sasl_mechanism"] = mechanism
        if mechanism in PASSWORD_MECHANISMS:
            if not username:
                raise ValueError(
                    f"KAFKA_SECURITY_PROTOCOL={protocol} 且 "
                    f"KAFKA_SASL_MECHANISM={mechanism} 时必须配置 "
                    "KAFKA_USERNAME/KAFKA_PASSWORD"
                )
            kwargs["sasl_plain_username"] = username
            kwargs["sasl_plain_password"] = password
    elif username:
        logger.warning(
            "KAFKA_SECURITY_PROTOCOL=%s 时 KAFKA_USERNAME/KAFKA_PASSWORD 不生效"
            "（SASL 未启用，broker 只会按匿名明文连接）",
            protocol,
        )

    kwargs.update(build_ssl_kwargs(settings, env_prefix="KAFKA"))
    return kwargs


def _require_file(path: str, label: str) -> str:
    """校验证书文件存在，返回原路径。"""
    if not Path(path).is_file():
        raise FileNotFoundError(f"{label}文件不存在：{path}")
    return path

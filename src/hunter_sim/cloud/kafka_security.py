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

from pathlib import Path
from typing import Any, Optional, Protocol

from hunter_sim.common.models import KafkaSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 合法的 security_protocol 取值（大小写不敏感）
VALID_PROTOCOLS = frozenset({"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"})
# 依赖账号口令的 SASL 机制（GSSAPI/OAUTHBEARER 走各自的凭据来源）
PASSWORD_MECHANISMS = frozenset({"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"})


class KafkaTLSSettings(Protocol):
    """SSL/mTLS 配置的最小接口（``CloudSettings`` / ``KafkaSettings`` 均满足）。"""

    security_protocol: str
    ca_cert_path: Optional[str]
    client_cert_path: Optional[str]
    client_key_path: Optional[str]


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

    Args:
        settings: 接入配置（发布侧 ``CloudSettings`` / 消费侧 ``KafkaSettings``）。
        env_prefix: 环境变量前缀，仅用于错误信息定位（默认 HunterCore）。

    Returns:
        ``ssl_cafile`` / ``ssl_certfile`` / ``ssl_keyfile`` 组成的字典，
        非 SSL 协议或未配置证书时为空字典。

    Raises:
        ValueError: 客户端证书与私钥只配置了其中一个。
        FileNotFoundError: 配置的证书文件不存在（提前失败，避免握手期才报错）。
    """
    if not settings.security_protocol.upper().strip().endswith("SSL"):
        return {}
    kwargs: dict[str, Any] = {}
    if settings.ca_cert_path:
        kwargs["ssl_cafile"] = _require_file(settings.ca_cert_path, "CA 证书")

    cert_path = settings.client_cert_path
    key_path = settings.client_key_path
    if not cert_path and not key_path:
        return kwargs
    if not cert_path or not key_path:
        raise ValueError(
            "mTLS 客户端证书配置不完整："
            f"{env_prefix}_CLIENT_CERT_PATH 与 {env_prefix}_CLIENT_KEY_PATH 必须同时设置"
        )
    kwargs["ssl_certfile"] = _require_file(cert_path, "mTLS 客户端证书")
    kwargs["ssl_keyfile"] = _require_file(key_path, "mTLS 客户端私钥")
    return kwargs


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

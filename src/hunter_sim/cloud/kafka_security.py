"""Kafka TLS 客户端参数构建（SASL_SSL / mTLS）。

HunterCore 的 9093 接入面默认 ``KAFKA_CFG_SSL_CLIENT_AUTH=required``：
只配 SCRAM 账号、不提供客户端证书时 TCP 能连上但 TLS 握手直接失败
（表现为 producer 初始化即降级为 no-op，日志里只有一条难定位的 SSL 错误）。

本模块把 ``CloudSettings`` 里的 CA / 客户端证书 / 私钥路径翻译成 kafka-python
的 ``ssl_*`` 关键字参数，由 ``SimDataProducer`` 与（已弃用的）``TelemetryProducer``
共用，避免两处各写一份导致配置漂移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hunter_sim.common.models import CloudSettings


def build_ssl_kwargs(settings: CloudSettings) -> dict[str, Any]:
    """按接入配置生成 kafka-python 的 SSL 参数。

    仅当 ``security_protocol`` 以 ``SSL`` 结尾时才返回参数（``SASL_SSL`` /
    ``SSL``）；客户端证书与私钥必须成对配置，缺一即视为配置错误。

    Args:
        settings: HunterCore 接入配置。

    Returns:
        ``ssl_cafile`` / ``ssl_certfile`` / ``ssl_keyfile`` 组成的字典，
        非 SSL 协议或未配置证书时为空字典。

    Raises:
        ValueError: 客户端证书与私钥只配置了其中一个。
        FileNotFoundError: 配置的证书文件不存在（提前失败，避免握手期才报错）。
    """
    if not settings.security_protocol.upper().endswith("SSL"):
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
            "mTLS 客户端证书配置不完整：HUNTER_CORE_CLIENT_CERT_PATH 与 "
            "HUNTER_CORE_CLIENT_KEY_PATH 必须同时设置"
        )
    kwargs["ssl_certfile"] = _require_file(cert_path, "mTLS 客户端证书")
    kwargs["ssl_keyfile"] = _require_file(key_path, "mTLS 客户端私钥")
    return kwargs


def _require_file(path: str, label: str) -> str:
    """校验证书文件存在，返回原路径。"""
    if not Path(path).is_file():
        raise FileNotFoundError(f"{label}文件不存在：{path}")
    return path

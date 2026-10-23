"""请求日志脱敏单测（V2.10.0）。

``RequestLogMiddleware`` 会原样记录查询串；WebSocket/回调等场景可能把
Token 放在 query 中，因此敏感键的值必须替换为 ``***`` 后再入日志。
"""

from __future__ import annotations

from urllib.parse import parse_qs

from hunter_sim.api.middleware.logging import _sanitize_query


def test_empty_query_returns_none() -> None:
    assert _sanitize_query("") is None


def test_non_sensitive_query_preserved() -> None:
    sanitized = _sanitize_query("page=2&size=10")
    assert sanitized is not None
    assert parse_qs(sanitized) == {"page": ["2"], "size": ["10"]}


def test_sensitive_values_masked() -> None:
    sanitized = _sanitize_query("token=secret-token&api_key=abc123&page=2")
    assert sanitized is not None
    parsed = parse_qs(sanitized)
    assert parsed["token"] == ["***"]
    assert parsed["api_key"] == ["***"]
    assert parsed["page"] == ["2"]
    assert "secret-token" not in sanitized
    assert "abc123" not in sanitized


def test_sensitive_key_match_is_case_insensitive() -> None:
    sanitized = _sanitize_query("Access_Token=secret")
    assert sanitized is not None
    assert parse_qs(sanitized) == {"Access_Token": ["***"]}

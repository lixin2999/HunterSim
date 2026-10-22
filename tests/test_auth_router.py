"""认证路由与生产启动闸门测试（V2.3.0）。

覆盖：
- POST /auth/token 换取 JWT，并用该 Token 访问受保护接口（打通接入死循环）；
- 错误口令/非 ASCII 用户名返回 401 而不是 500；
- HUNTER_SIM_ENV=prod 时拒绝默认密钥、短密钥、通配 CORS、默认管理员口令。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from hunter_sim.api.deps import get_settings
from hunter_sim.api.main import _enforce_production_guards
from hunter_sim.common.models import APISettings, HunterSimSettings

_LONG_SECRET = "prod-secret-" + "x" * 40


def _prod_settings(**api_overrides: Any) -> HunterSimSettings:
    api_kwargs: dict[str, Any] = {
        "jwt_secret": SecretStr(_LONG_SECRET),
        "cors_allow_origins": ["https://sim.example.com"],
        "admin_password": SecretStr("S3cure-Admin-Pass"),
    }
    api_kwargs.update(api_overrides)
    return HunterSimSettings(env="prod", api=APISettings(**api_kwargs))


class TestAuthToken:
    def test_issue_token_and_access_protected_api(self, client: TestClient) -> None:
        api_settings = get_settings().api
        response = client.post(
            "/api/v1/sim/auth/token",
            json={
                "username": api_settings.admin_username,
                "password": api_settings.admin_password.get_secret_value(),
            },
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == api_settings.jwt_expire_minutes * 60
        assert data["access_token"]

        # 该 Token 能直接访问受保护接口（此前没有任何途径获取 Token）
        protected = client.get(
            "/api/v1/sim/instances",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        )
        assert protected.status_code == 200

    def test_wrong_password_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/sim/auth/token",
            json={"username": get_settings().api.admin_username, "password": "definitely-wrong"},
        )
        assert response.status_code == 401

    def test_non_ascii_username_returns_401_not_500(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/sim/auth/token",
            json={"username": "管理员", "password": "admin"},
        )
        assert response.status_code == 401


class TestProductionGuards:
    def test_dev_allows_defaults(self) -> None:
        _enforce_production_guards(HunterSimSettings(env="dev"))  # 不应抛异常

    def test_prod_rejects_default_secret(self) -> None:
        settings = _prod_settings(jwt_secret=SecretStr("CHANGE_ME_IN_PRODUCTION"))
        with pytest.raises(RuntimeError, match="API_JWT_SECRET"):
            _enforce_production_guards(settings)

    def test_prod_rejects_dev_default_secret(self) -> None:
        settings = _prod_settings(
            jwt_secret=SecretStr("dev-only-change-me-at-least-32-characters")
        )
        with pytest.raises(RuntimeError, match="API_JWT_SECRET"):
            _enforce_production_guards(settings)

    def test_prod_rejects_short_secret(self) -> None:
        settings = _prod_settings(jwt_secret=SecretStr("too-short"))
        with pytest.raises(RuntimeError, match="API_JWT_SECRET"):
            _enforce_production_guards(settings)

    def test_prod_rejects_wildcard_cors(self) -> None:
        settings = _prod_settings(cors_allow_origins=["*"])
        with pytest.raises(RuntimeError, match="CORS"):
            _enforce_production_guards(settings)

    def test_prod_rejects_default_admin_password(self) -> None:
        settings = _prod_settings(admin_password=SecretStr("admin"))
        with pytest.raises(RuntimeError, match="API_ADMIN_PASSWORD"):
            _enforce_production_guards(settings)

    def test_prod_accepts_hardened_config(self) -> None:
        _enforce_production_guards(_prod_settings())  # 不应抛异常

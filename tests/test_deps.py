"""FastAPI 依赖注入（JWT/配置）单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from starlette.testclient import TestClient

from hunter_sim.api import deps
from hunter_sim.api.deps import (
    create_access_token,
    decode_token,
    get_api_settings,
    get_current_user,
    get_optional_user,
    get_settings,
    set_app_settings,
)
from hunter_sim.common.models import HunterSimSettings


@pytest.fixture()
def settings() -> HunterSimSettings:
    s = HunterSimSettings(env="dev")
    set_app_settings(s)
    return s


class TestSettings:
    def test_get_settings(self, settings: HunterSimSettings) -> None:
        assert get_settings() is settings
        assert get_api_settings() is settings.api

    def test_get_settings_uninitialized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "_settings", None)
        with pytest.raises(HTTPException) as ei:
            get_settings()
        assert ei.value.status_code == 500


class TestTokens:
    def test_roundtrip(self, settings: HunterSimSettings) -> None:
        token = create_access_token("alice", settings.api)
        payload = decode_token(token, settings.api)
        assert payload["sub"] == "alice"
        assert "exp" in payload and "iat" in payload

    def test_extra_claims(self, settings: HunterSimSettings) -> None:
        token = create_access_token("bob", settings.api, extra_claims={"role": "admin"})
        assert decode_token(token, settings.api)["role"] == "admin"

    def test_invalid_token_raises_401(self, settings: HunterSimSettings) -> None:
        with pytest.raises(HTTPException) as ei:
            decode_token("not.a.jwt", settings.api)
        assert ei.value.status_code == 401


class TestAuthDependencies:
    @pytest.fixture()
    def client(self, settings: HunterSimSettings) -> TestClient:
        app = FastAPI()

        @app.get("/me")
        def _me(user: str = Depends(get_current_user)) -> dict[str, str]:
            return {"user": user}

        @app.get("/opt")
        def _opt(user: str | None = Depends(get_optional_user)) -> dict[str, object]:
            return {"user": user}

        return TestClient(app)

    def test_missing_header_401(self, client: TestClient) -> None:
        assert client.get("/me").status_code == 401

    def test_valid_token_returns_user(
        self, client: TestClient, settings: HunterSimSettings
    ) -> None:
        token = create_access_token("carol", settings.api)
        resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.json()["user"] == "carol"

    def test_optional_no_credentials(self, client: TestClient) -> None:
        assert client.get("/opt").json()["user"] is None

    def test_optional_invalid_token(
        self, client: TestClient, settings: HunterSimSettings
    ) -> None:
        # 有效但缺 sub 的 token：get_optional_user 返回 None（sub 缺失）
        token = create_access_token("dave", settings.api)
        assert client.get("/opt", headers={"Authorization": f"Bearer {token}"}).json()["user"] == "dave"
        assert client.get("/opt", headers={"Authorization": "Bearer garbage"}).json()["user"] is None

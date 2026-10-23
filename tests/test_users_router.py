"""多用户体系路由单测（V2.8.0 P2-13）。

覆盖：
- 用户 CRUD 六端点的权限、状态码与对外视图（不回显口令摘要）；
- 口令强度策略（长度/字符类别/不得含登录名）；
- 最后一名可用 admin 的锁死保护、内置管理员不可经此接口改动；
- 自助改口令（错误当前口令 401、内置账号 409、成功后新口令可登录）；
- 登录链路：用户表优先（role/tenant 写入 JWT），未命中回退内置管理员；
- 依赖缺失：无状态库 503、PLATFORM_USERS_ENABLED=false 403。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from hunter_sim.api.deps import create_access_token, decode_token
from hunter_sim.api.main import create_app
from hunter_sim.api.routers import users as users_router
from hunter_sim.common.models import (
    APISettings,
    HunterSimSettings,
    PlatformSettings,
    StateSettings,
)

_PREFIX = "/api/v1/sim"
_PASSWORD = "Operator-2026"


def _users_settings(**platform_overrides: Any) -> HunterSimSettings:
    platform_kwargs: dict[str, Any] = {
        # 迭代次数取校验下限，避免单测在 PBKDF2 上耗时
        "password_hash_iterations": 1_000,
        "min_password_length": 10,
        "users_enabled": True,
    }
    platform_kwargs.update(platform_overrides)
    return HunterSimSettings(
        env="dev",
        api=APISettings(rate_limit_enabled=False, login_max_failed_attempts=1_000_000),
        # 用户表必须落库，因此本组测试显式启用状态持久化（内存库）
        state=StateSettings(enabled=True, db_path=":memory:", recover_instances=False),
        platform=PlatformSettings(**platform_kwargs),
    )


@pytest.fixture(scope="module")
def users_app():
    return create_app(_users_settings())


@pytest.fixture(scope="module")
def users_client(users_app):
    with TestClient(users_app) as client:
        yield client


@pytest.fixture(scope="module")
def admin_headers(users_client: TestClient) -> dict[str, str]:
    """内置视角的管理员 Token（经 users_client 保证 lifespan 已装配 state.settings）。"""
    api_settings = users_client.app.state.settings.api
    token = create_access_token("root", api_settings, extra_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def operator_headers(users_client: TestClient) -> dict[str, str]:
    api_settings = users_client.app.state.settings.api
    token = create_access_token(
        "op", api_settings, extra_claims={"role": "operator", "tenant": "default"}
    )
    return {"Authorization": f"Bearer {token}"}


def _create(
    client: TestClient,
    headers: dict[str, str],
    username: str,
    *,
    password: str = _PASSWORD,
    role: str = "operator",
    tenant: str = "",
) -> Any:
    return client.post(
        f"{_PREFIX}/auth/users",
        json={"username": username, "password": password, "role": role, "tenant": tenant},
        headers=headers,
    )


class TestUserCrud:
    def test_create_returns_public_view_without_hash(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        response = _create(users_client, admin_headers, "alice", role="operator", tenant="team-a")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data == {
            **data,
            "username": "alice",
            "role": "operator",
            "tenant": "team-a",
            "disabled": False,
        }
        # 摘要/salt/迭代次数一律不外泄
        for secret_field in ("pass_hash", "salt", "iterations"):
            assert secret_field not in data

    def test_duplicate_username_conflicts(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        assert _create(users_client, admin_headers, "bob").status_code == 200
        again = _create(users_client, admin_headers, "bob")
        assert again.status_code == 409
        assert "already exists" in again.json()["message"]

    def test_created_by_records_caller(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        assert _create(users_client, admin_headers, "carol").status_code == 200
        detail = users_client.get(f"{_PREFIX}/auth/users/carol", headers=admin_headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["created_by"] == "root"

    def test_get_unknown_user_returns_404(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        response = users_client.get(f"{_PREFIX}/auth/users/ghost", headers=admin_headers)
        assert response.status_code == 404

    def test_list_filters_by_tenant_and_disabled(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "dave", tenant="team-b").status_code == 200
        users_client.patch(
            f"{_PREFIX}/auth/users/dave", json={"disabled": True}, headers=admin_headers
        )

        team_b = users_client.get(
            f"{_PREFIX}/auth/users", params={"tenant": "team-b"}, headers=admin_headers
        ).json()["data"]
        assert [item["username"] for item in team_b["items"]] == ["dave"]
        assert team_b["total"] == 1

        enabled_only = users_client.get(
            f"{_PREFIX}/auth/users",
            params={"tenant": "team-b", "include_disabled": False},
            headers=admin_headers,
        ).json()["data"]
        assert enabled_only["items"] == []

        # tenant 留空返回全量（含跨租户）
        all_users = users_client.get(f"{_PREFIX}/auth/users", headers=admin_headers).json()["data"]
        assert {"alice", "bob", "carol", "dave"} <= {item["username"] for item in all_users["items"]}

    def test_patch_updates_role_and_tenant(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        response = users_client.patch(
            f"{_PREFIX}/auth/users/bob",
            json={"role": "viewer", "tenant": "team-c"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["role"] == "viewer"
        assert response.json()["data"]["tenant"] == "team-c"

    def test_patch_unknown_user_returns_404(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        response = users_client.patch(
            f"{_PREFIX}/auth/users/nobody", json={"role": "viewer"}, headers=admin_headers
        )
        assert response.status_code == 404

    def test_delete_removes_user(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        assert _create(users_client, admin_headers, "to-delete").status_code == 200
        response = users_client.delete(f"{_PREFIX}/auth/users/to-delete", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True
        assert users_client.get(f"{_PREFIX}/auth/users/to-delete", headers=admin_headers).status_code == 404

    def test_delete_unknown_user_returns_404(self, users_client: TestClient, admin_headers: dict[str, str]) -> None:
        response = users_client.delete(f"{_PREFIX}/auth/users/ghost", headers=admin_headers)
        assert response.status_code == 404


class TestUserAuthorization:
    def test_missing_token_rejected(self, users_client: TestClient) -> None:
        assert users_client.get(f"{_PREFIX}/auth/users").status_code in (401, 403)

    def test_non_admin_cannot_manage_users(
        self, users_client: TestClient, operator_headers: dict[str, str]
    ) -> None:
        assert users_client.get(f"{_PREFIX}/auth/users", headers=operator_headers).status_code == 403
        created = _create(users_client, operator_headers, "eve")
        assert created.status_code == 403


class TestPasswordPolicy:
    @pytest.mark.parametrize(
        ("password", "expect"),
        [
            ("short1", "at least 10 characters"),
            ("allletters", "one letter and one digit"),
            ("1234567890", "one letter and one digit"),
        ],
    )
    def test_weak_passwords_rejected(
        self, users_client: TestClient, admin_headers: dict[str, str], password: str, expect: str
    ) -> None:
        response = _create(users_client, admin_headers, "frank", password=password)
        assert response.status_code == 422
        assert expect in response.json()["message"]

    def test_password_containing_username_rejected(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """口令包含登录名（不分大小写）一律拒绝，防弱口令与账号名同源。"""
        response = _create(users_client, admin_headers, "grace", password="grace-2026-pass")
        assert response.status_code == 422
        assert "username" in response.json()["message"]
        ci_response = _create(users_client, admin_headers, "henry", password="HENRY-is-secret1")
        assert ci_response.status_code == 422

    def test_invalid_role_rejected_by_schema(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = _create(users_client, admin_headers, "heidi", role="superuser")
        assert response.status_code == 422

    def test_reset_password_applies_policy(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = users_client.patch(
            f"{_PREFIX}/auth/users/bob", json={"password": "abc"}, headers=admin_headers
        )
        assert response.status_code == 422


class TestLockoutProtection:
    def test_builtin_admin_cannot_be_managed(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        builtin = users_client.app.state.settings.api.admin_username
        response = users_client.delete(f"{_PREFIX}/auth/users/{builtin}", headers=admin_headers)
        assert response.status_code == 409
        assert "built-in admin" in response.json()["message"]

    def test_last_enabled_admin_cannot_be_downgraded(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "sole-admin", role="admin").status_code == 200
        response = users_client.patch(
            f"{_PREFIX}/auth/users/sole-admin", json={"role": "operator"}, headers=admin_headers
        )
        assert response.status_code == 409
        assert "last enabled admin" in response.json()["message"]

    def test_last_enabled_admin_cannot_be_disabled_or_deleted(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        disabled = users_client.patch(
            f"{_PREFIX}/auth/users/sole-admin", json={"disabled": True}, headers=admin_headers
        )
        assert disabled.status_code == 409
        assert users_client.delete(f"{_PREFIX}/auth/users/sole-admin", headers=admin_headers).status_code == 409

    def test_second_admin_allows_downgrade(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "second-admin", role="admin").status_code == 200
        response = users_client.patch(
            f"{_PREFIX}/auth/users/sole-admin", json={"role": "operator"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["role"] == "operator"


class TestSelfPasswordChange:
    def test_change_requires_bearer_token(self, users_client: TestClient) -> None:
        response = users_client.post(
            f"{_PREFIX}/auth/password",
            json={"old_password": _PASSWORD, "new_password": "Rotated-2026"},
        )
        assert response.status_code in (401, 403)

    def test_builtin_admin_not_in_user_table(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = users_client.post(
            f"{_PREFIX}/auth/password",
            json={"old_password": "admin", "new_password": "Rotated-2026"},
            headers=admin_headers,
        )
        assert response.status_code == 409
        assert "user table" in response.json()["message"]

    def test_wrong_current_password_rejected(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "ivan").status_code == 200
        login = users_client.post(
            f"{_PREFIX}/auth/token", json={"username": "ivan", "password": _PASSWORD}
        )
        assert login.status_code == 200
        own = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        response = users_client.post(
            f"{_PREFIX}/auth/password",
            json={"old_password": "wrong-current-1", "new_password": "Rotated-2026"},
            headers=own,
        )
        assert response.status_code == 401

    def test_rotation_then_relogin_with_new_password(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "judy", tenant="team-d").status_code == 200
        login = users_client.post(
            f"{_PREFIX}/auth/token", json={"username": "judy", "password": _PASSWORD}
        )
        own = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        changed = users_client.post(
            f"{_PREFIX}/auth/password",
            json={"old_password": _PASSWORD, "new_password": "Rotated-2026"},
            headers=own,
        )
        assert changed.status_code == 200
        assert changed.json()["data"]["changed"] is True

        stale = users_client.post(
            f"{_PREFIX}/auth/token", json={"username": "judy", "password": _PASSWORD}
        )
        assert stale.status_code == 401
        fresh = users_client.post(
            f"{_PREFIX}/auth/token", json={"username": "judy", "password": "Rotated-2026"}
        )
        assert fresh.status_code == 200


class TestLoginIntegration:
    def test_user_table_login_embeds_role_and_tenant(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """用户表命中时不再走内置管理员比对，role/tenant 由用户记录写入 JWT 声明。"""
        assert _create(
            users_client, admin_headers, "karl", password="Ops-Team-2026", role="operator", tenant="team-e"
        ).status_code == 200
        api_settings = users_client.app.state.settings.api
        login = users_client.post(
            f"{_PREFIX}/auth/token", json={"username": "karl", "password": "Ops-Team-2026"}
        )
        assert login.status_code == 200
        payload = decode_token(login.json()["data"]["access_token"], api_settings)
        assert payload["sub"] == "karl"
        assert payload["role"] == "operator"
        assert payload["tenant"] == "team-e"

    def test_disabled_user_cannot_login(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "liam").status_code == 200
        users_client.patch(f"{_PREFIX}/auth/users/liam", json={"disabled": True}, headers=admin_headers)
        response = users_client.post(
            f"{_PREFIX}/auth/token", json={"username": "liam", "password": _PASSWORD}
        )
        assert response.status_code == 401

    def test_builtin_admin_fallback_still_works(self, users_client: TestClient) -> None:
        api_settings = users_client.app.state.settings.api
        login = users_client.post(
            f"{_PREFIX}/auth/token",
            json={
                "username": api_settings.admin_username,
                "password": api_settings.admin_password.get_secret_value(),
            },
        )
        assert login.status_code == 200
        payload = decode_token(login.json()["data"]["access_token"], api_settings)
        assert payload["role"] == "admin"

    def test_last_login_at_recorded(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "maya").status_code == 200
        assert (
            users_client.get(f"{_PREFIX}/auth/users/maya", headers=admin_headers).json()["data"][
                "last_login_at"
            ]
            == 0.0
        )
        users_client.post(f"{_PREFIX}/auth/token", json={"username": "maya", "password": _PASSWORD})
        after = users_client.get(f"{_PREFIX}/auth/users/maya", headers=admin_headers).json()["data"]
        assert after["last_login_at"] > 0.0


class TestDegradedModes:
    def test_users_disabled_returns_403(
        self, users_client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        disabled_settings = _users_settings(users_enabled=False)
        monkeypatch.setattr(users_router, "get_settings", lambda: disabled_settings)
        response = _create(users_client, admin_headers, "nope")
        assert response.status_code == 403
        assert "PLATFORM_USERS_ENABLED" in response.json()["message"]

    def test_missing_state_store_returns_503(self, client: TestClient) -> None:
        """未启用状态持久化时用户管理接口不可用（用户表必须落盘）。"""
        token = create_access_token(
            "root", client.app.state.settings.api, extra_claims={"role": "admin"}
        )
        response = client.post(
            f"{_PREFIX}/auth/users",
            json={"username": "ozzie", "password": _PASSWORD, "role": "operator"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 503
        assert "STATE_ENABLED" in response.json()["message"]


# ─── V2.9.0：续期回查用户表（禁用/删除/降级即时生效） ──────────────────────


def _login(client: TestClient, username: str, password: str = _PASSWORD) -> dict[str, str]:
    response = client.post(
        f"{_PREFIX}/auth/token", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


class TestRefreshRevalidatesUser:
    """旧实现直接沿用 Token claim，账号禁用/删除后仍能无限续期。"""

    def test_refresh_rejects_disabled_account(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "refdis").status_code == 200
        headers = _login(users_client, "refdis")
        patched = users_client.patch(
            f"{_PREFIX}/auth/users/refdis", json={"disabled": True}, headers=admin_headers
        )
        assert patched.status_code == 200

        response = users_client.post(f"{_PREFIX}/auth/refresh", headers=headers)

        assert response.status_code == 401
        assert "disabled" in response.json()["message"]

    def test_refresh_applies_role_downgrade(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # operator → viewer 降级：续期后以用户表为准，不再沿用 Token 里的旧角色
        assert _create(users_client, admin_headers, "refview").status_code == 200
        headers = _login(users_client, "refview")
        patched = users_client.patch(
            f"{_PREFIX}/auth/users/refview", json={"role": "viewer"}, headers=admin_headers
        )
        assert patched.status_code == 200

        response = users_client.post(f"{_PREFIX}/auth/refresh", headers=headers)

        assert response.status_code == 200, response.text
        payload = decode_token(
            response.json()["data"]["access_token"], users_client.app.state.settings.api
        )
        assert payload["role"] == "viewer"

    def test_refresh_rejects_removed_account(
        self, users_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert _create(users_client, admin_headers, "refgone").status_code == 200
        headers = _login(users_client, "refgone")
        removed = users_client.delete(
            f"{_PREFIX}/auth/users/refgone", headers=admin_headers
        )
        assert removed.status_code == 200

        response = users_client.post(f"{_PREFIX}/auth/refresh", headers=headers)

        assert response.status_code == 401
        assert "not stored" in response.json()["message"]

    def test_builtin_admin_can_refresh_without_user_record(
        self, users_client: TestClient
    ) -> None:
        api_settings = users_client.app.state.settings.api
        token = create_access_token(
            api_settings.admin_username, api_settings, extra_claims={"role": "admin"}
        )

        response = users_client.post(
            f"{_PREFIX}/auth/refresh", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200, response.text
        payload = decode_token(response.json()["data"]["access_token"], api_settings)
        assert payload["sub"] == api_settings.admin_username
        assert payload["role"] == "admin"

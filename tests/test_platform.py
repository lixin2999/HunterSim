"""平台化（V2.5.0）单元与接口测试。

覆盖：
- PlatformSettings / Principal 角色层级 / JWT tenant claim；
- StateStore 平台化四张表（api_keys / scene_library / batch_tasks / audit_logs）；
- 租户并发配额；
- 服务账号 API Key 认证、RBAC 与吊销；
- 场景库版本化、批量任务持久化、审计查询与平台概览。
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from hunter_sim.api.deps import Principal, create_access_token, decode_token, principal_from_payload
from hunter_sim.common.exceptions import ResourceError
from hunter_sim.common.models import (
    HunterSimSettings,
    PlatformSettings,
    QualityLevel,
    ResourceSettings,
    SimMode,
    StateSettings,
)
from hunter_sim.persistence.state_store import StateStore
from hunter_sim.resource_manager.instance_manager import SimInstanceManager


def _scene_config(scene_id: str = "scene-001") -> dict[str, Any]:
    """最小合法 SceneConfig。"""
    return {
        "scene_id": scene_id,
        "map_id": "Town03",
        "ego_vehicle": {"spawn_point": {"x": 0.0, "y": 0.0}},
    }


@pytest.fixture()
def store() -> Iterator[StateStore]:
    """独立内存状态库（每个用例隔离）。"""
    instance = StateStore(":memory:")
    yield instance
    instance.close()


# ─── 配置与主体 ───────────────────────────────────────────────────────────────


class TestPlatformSettings:
    def test_defaults(self) -> None:
        settings = PlatformSettings()
        assert settings.default_tenant == "default"
        assert settings.max_instances_per_tenant == 20
        assert settings.api_keys_enabled is True
        assert settings.audit_enabled is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLATFORM_DEFAULT_TENANT", "acme")
        monkeypatch.setenv("PLATFORM_MAX_INSTANCES_PER_TENANT", "3")
        settings = PlatformSettings()
        assert settings.default_tenant == "acme"
        assert settings.max_instances_per_tenant == 3


class TestPrincipal:
    def test_role_hierarchy(self) -> None:
        viewer = Principal(user_id="v", role="viewer")
        operator = Principal(user_id="o", role="operator")
        admin = Principal(user_id="a", role="admin")
        guest = Principal(user_id="g")
        assert viewer.has_role("viewer")
        assert not viewer.has_role("operator")
        assert operator.has_role("viewer") and operator.has_role("operator")
        assert not operator.has_role("admin")
        assert admin.has_role("admin") and admin.is_admin
        assert not guest.has_role("viewer")

    def test_payload_defaults_to_default_tenant(self) -> None:
        principal = principal_from_payload({"sub": "u1", "role": "operator"})
        assert principal.tenant == "default"
        assert principal.role == "operator"
        assert principal.auth_type == "jwt"

    def test_token_carries_tenant(self) -> None:
        settings = HunterSimSettings(env="dev")
        token = create_access_token(
            "u1", settings.api, extra_claims={"role": "operator"}, tenant="acme"
        )
        payload = decode_token(token, settings.api)
        assert payload["tenant"] == "acme"
        assert principal_from_payload(payload).tenant == "acme"


# ─── StateStore 平台化表 ─────────────────────────────────────────────────────


class TestApiKeyStore:
    def _save(self, store: StateStore, *, role: str = "operator", expires_at: float = 0.0) -> str:
        raw, key_id, salt, key_hash = store.generate_api_key()
        store.save_api_key(
            {
                "key_id": key_id,
                "name": "ci-bot",
                "tenant": "acme",
                "role": role,
                "key_hash": key_hash,
                "salt": salt,
                "prefix": raw[:12],
                "created_by": "admin",
                "created_at": time.time(),
                "expires_at": expires_at,
            }
        )
        return raw

    def test_verify_success(self, store: StateStore) -> None:
        raw = self._save(store)
        info = store.verify_api_key(raw)
        assert info is not None
        assert info["tenant"] == "acme"
        assert info["role"] == "operator"
        assert info["name"] == "ci-bot"

    def test_wrong_secret_rejected(self, store: StateStore) -> None:
        raw = self._save(store)
        assert store.verify_api_key(raw[:-1] + ("x" if raw[-1] != "x" else "y")) is None
        assert store.verify_api_key("not-a-key") is None
        assert store.verify_api_key("hsk_missing_secret") is None

    def test_revoked_rejected(self, store: StateStore) -> None:
        raw = self._save(store)
        info = store.verify_api_key(raw)
        assert info is not None
        assert store.revoke_api_key(str(info["key_id"]), tenant="acme") is True
        assert store.verify_api_key(raw) is None
        # 幂等：重复吊销返回 False
        assert store.revoke_api_key(str(info["key_id"]), tenant="acme") is False

    def test_expired_rejected(self, store: StateStore) -> None:
        raw = self._save(store, expires_at=time.time() - 1)
        assert store.verify_api_key(raw) is None

    def test_list_excludes_secret_material(self, store: StateStore) -> None:
        self._save(store)
        items = store.list_api_keys("acme")
        assert len(items) == 1
        assert "key_hash" not in items[0]
        assert "salt" not in items[0]


class TestSceneLibraryStore:
    def test_version_increment_and_archive(self, store: StateStore) -> None:
        config = _scene_config()
        first = store.save_library_scene(
            "acme", "night-rain", "scene-001", "Town03", "desc", config, "u1"
        )
        second = store.save_library_scene(
            "acme", "night-rain", "scene-001", "Town03", "desc2", config, "u1"
        )
        assert first["version"] == 1
        assert second["version"] == 2
        assert store.get_library_scene(first["library_scene_id"])["archived"] is True
        assert store.get_library_scene(second["library_scene_id"])["config"]["scene_id"] == "scene-001"

        latest = store.list_library_scenes("acme")
        assert len(latest) == 1 and latest[0]["version"] == 2
        assert len(store.list_library_scenes("acme", include_archived=True)) == 2

    def test_tenant_isolation_and_delete(self, store: StateStore) -> None:
        item = store.save_library_scene(
            "acme", "fog", "s2", "Town03", "", _scene_config("s2"), "u1"
        )
        assert store.get_library_scene(item["library_scene_id"], tenant="other") is None
        assert store.delete_library_scene(item["library_scene_id"], tenant="other") is False
        assert store.delete_library_scene(item["library_scene_id"], tenant="acme") is True
        assert store.list_library_scenes("acme") == []


class TestBatchTaskStore:
    def _task(self, task_id: str = "t1", status: str = "running") -> dict[str, Any]:
        return {
            "task_id": task_id,
            "tenant": "acme",
            "owner": "u1",
            "status": status,
            "scenes": [{"scene_id": "s1"}],
            "results": [],
            "created_at": "2026-01-01T00:00:00+00:00",
        }

    def test_save_and_load(self, store: StateStore) -> None:
        task = self._task()
        store.save_batch_task(task)
        loaded = store.load_batch_task("t1")
        assert loaded is not None and loaded["status"] == "running"
        assert store.load_batch_tasks("acme")[0]["owner"] == "u1"
        assert store.load_batch_tasks("other") == []

    def test_normalize_running_to_failed(self, store: StateStore) -> None:
        store.save_batch_task(self._task("t1", "running"))
        store.save_batch_task(self._task("t2", "pending"))
        store.save_batch_task(self._task("t3", "completed"))
        assert store.normalize_batch_tasks() == 2
        assert store.load_batch_task("t1")["status"] == "failed"
        assert store.load_batch_task("t2")["status"] == "failed"
        assert store.load_batch_task("t3")["status"] == "completed"
        assert "error_message" in store.load_batch_task("t1")


class TestAuditStore:
    def test_append_and_paginate(self, store: StateStore) -> None:
        store.append_audit("instance.create", "u1", "inst-1", "d1", tenant="acme", auth_type="jwt")
        store.append_audit("instance.destroy", "u2", "inst-1", "", tenant="acme")
        store.append_audit("instance.create", "u3", "inst-2", "", tenant="other")

        page = store.list_audit(tenant="acme", limit=1, offset=0)
        assert page["total"] == 2
        assert len(page["items"]) == 1
        assert page["items"][0]["action"] == "instance.destroy"  # 新→旧

        filtered = store.list_audit(action="instance.create")
        assert filtered["total"] == 2
        by_user = store.list_audit(tenant="acme", user_id="u1")
        assert by_user["total"] == 1


# ─── 租户配额 ─────────────────────────────────────────────────────────────────


class TestTenantQuota:
    def test_tenant_limit_isolated(self) -> None:
        manager = SimInstanceManager(platform=PlatformSettings(max_instances_per_tenant=1))
        first = manager.create_instance(
            mode=SimMode.SIL, map_id="Town03", quality=QualityLevel.MEDIUM, tenant="acme"
        )
        assert first.tenant == "acme"
        with pytest.raises(ResourceError) as exc_info:
            manager.create_instance(
                mode=SimMode.SIL, map_id="Town03", quality=QualityLevel.MEDIUM, tenant="acme"
            )
        assert "Tenant 'acme'" in str(exc_info.value)
        # 其他租户不受影响
        other = manager.create_instance(
            mode=SimMode.SIL, map_id="Town03", quality=QualityLevel.MEDIUM, tenant="beta"
        )
        assert other.tenant == "beta"

    def test_default_tenant_applied(self) -> None:
        manager = SimInstanceManager(platform=PlatformSettings(default_tenant="acme"))
        instance = manager.create_instance(
            mode=SimMode.SIL, map_id="Town03", quality=QualityLevel.MEDIUM
        )
        assert instance.tenant == "acme"
        # 持久化记录与列表过滤均带租户
        assert instance.to_record()["tenant"] == "acme"
        assert manager.list_instances(tenant="acme") == [instance]
        assert manager.list_instances(tenant="beta") == []


# ─── API 集成 ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def platform_client() -> Iterator[tuple[HunterSimSettings, TestClient]]:
    """带状态库的平台化应用（内存库，独立生命周期）。"""
    from hunter_sim.api.deps import set_app_settings
    from hunter_sim.api.main import create_app

    settings = HunterSimSettings(
        env="dev",
        state=StateSettings(
            enabled=True,
            db_path=":memory:",
            recover_instances=False,
            restore_scenes=False,
        ),
        platform=PlatformSettings(
            default_tenant="default",
            max_instances_per_tenant=5,
            api_keys_enabled=True,
            audit_enabled=True,
        ),
        resource=ResourceSettings(max_instances_per_user=5),
    )
    application = create_app(settings)
    with TestClient(application) as client:
        yield settings, client
    # 恢复全局配置，避免影响后续测试模块
    set_app_settings(HunterSimSettings(env="dev"))


def _headers(
    settings: HunterSimSettings,
    user: str = "admin-user",
    role: str = "admin",
    tenant: str = "default",
) -> dict[str, str]:
    token = create_access_token(user, settings.api, extra_claims={"role": role}, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


class TestApiKeyEndpoints:
    def test_issue_use_and_revoke(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        admin = _headers(settings)

        created = client.post(
            "/api/v1/sim/auth/api-keys",
            json={"name": "ci-bot", "role": "operator"},
            headers=admin,
        )
        assert created.status_code == 200
        data = created.json()["data"]
        raw_key = data["api_key"]
        key_id = data["key_id"]
        assert raw_key.startswith("hsk_")
        assert data["tenant"] == "default"

        # 服务账号可访问 op 级接口
        listed = client.get("/api/v1/sim/instances", headers={"X-API-Key": raw_key})
        assert listed.status_code == 200

        # 列表不含明文
        keys = client.get("/api/v1/sim/auth/api-keys", headers=admin).json()["data"]
        assert keys["total"] == 1
        assert "api_key" not in keys["items"][0]

        # 吊销后立即失效
        revoked = client.delete(f"/api/v1/sim/auth/api-keys/{key_id}", headers=admin)
        assert revoked.status_code == 200
        denied = client.get("/api/v1/sim/instances", headers={"X-API-Key": raw_key})
        assert denied.status_code == 401

    def test_invalid_key_rejected(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        _, client = platform_client
        response = client.get("/api/v1/sim/instances", headers={"X-API-Key": "hsk_bad_key"})
        assert response.status_code == 401

    def test_viewer_cannot_create_instance(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        admin = _headers(settings)
        viewer_key = client.post(
            "/api/v1/sim/auth/api-keys",
            json={"name": "viewer-bot", "role": "viewer"},
            headers=admin,
        ).json()["data"]["api_key"]
        operator_key = client.post(
            "/api/v1/sim/auth/api-keys",
            json={"name": "op-bot", "role": "operator"},
            headers=admin,
        ).json()["data"]["api_key"]

        payload = {"mode": "sil", "map": "Town03"}
        denied = client.post(
            "/api/v1/sim/instances", json=payload, headers={"X-API-Key": viewer_key}
        )
        assert denied.status_code == 403

        allowed = client.post(
            "/api/v1/sim/instances", json=payload, headers={"X-API-Key": operator_key}
        )
        assert allowed.status_code == 201
        assert allowed.json()["data"]["status"] == "created"


class TestSceneLibraryEndpoints:
    def test_save_version_list_get_delete(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        operator = _headers(settings, user="op-user", role="operator")

        first = client.post(
            "/api/v1/sim/scenes/library",
            json={"name": "night-rain", "description": "v1", "config": _scene_config()},
            headers=operator,
        )
        assert first.status_code == 201
        first_data = first.json()["data"]
        assert first_data["version"] == 1
        assert first_data["tenant"] == "default"
        assert first_data["map_id"] == "Town03"

        second = client.post(
            "/api/v1/sim/scenes/library",
            json={"name": "night-rain", "description": "v2", "config": _scene_config()},
            headers=operator,
        )
        assert second.status_code == 201
        assert second.json()["data"]["version"] == 2

        listing = client.get("/api/v1/sim/scenes/library", headers=operator).json()["data"]
        assert listing["total"] == 1
        assert listing["items"][0]["version"] == 2

        detail = client.get(
            f"/api/v1/sim/scenes/library/{second.json()['data']['library_scene_id']}",
            headers=operator,
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["config"]["scene_id"] == "scene-001"

        archived = client.delete(
            f"/api/v1/sim/scenes/library/{second.json()['data']['library_scene_id']}",
            headers=operator,
        )
        assert archived.status_code == 200
        assert client.get("/api/v1/sim/scenes/library", headers=operator).json()["data"]["total"] == 0

    def test_invalid_scene_config_rejected(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        operator = _headers(settings, user="op-user", role="operator")
        response = client.post(
            "/api/v1/sim/scenes/library",
            json={"name": "bad", "config": {"map_id": "Town03"}},
            headers=operator,
        )
        assert response.status_code == 422


class TestBatchTaskPersistenceEndpoints:
    def test_create_list_and_report(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        operator = _headers(settings, user="op-user", role="operator")

        created = client.post(
            "/api/v1/sim/scenarios/batch",
            json={"task_id": "batch_platform_1", "scenes": [{"scene_id": "s1"}]},
            headers=operator,
        )
        assert created.status_code == 200
        assert created.json()["data"]["status"] == "pending"

        listing = client.get("/api/v1/sim/scenarios", headers=operator).json()["data"]
        assert any(item["task_id"] == "batch_platform_1" for item in listing["items"])

        report = client.get(
            "/api/v1/sim/scenarios/batch_platform_1/report", headers=operator
        )
        assert report.status_code == 200
        assert report.json()["data"]["total_scenes"] == 1

        # 其他用户不可见
        other = _headers(settings, user="other-user", role="operator")
        hidden = client.get("/api/v1/sim/scenarios/batch_platform_1/report", headers=other)
        assert hidden.status_code == 404

    def test_duplicate_task_rejected(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        operator = _headers(settings, user="op-user", role="operator")
        body = {"task_id": "batch_duplicate", "scenes": [{"scene_id": "s1"}]}
        assert client.post("/api/v1/sim/scenarios/batch", json=body, headers=operator).status_code == 200
        assert client.post("/api/v1/sim/scenarios/batch", json=body, headers=operator).status_code == 409


class TestPlatformObservability:
    def test_audit_query_and_rbac(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        admin = _headers(settings)
        operator = _headers(settings, user="op-user", role="operator")

        client.post(
            "/api/v1/sim/scenarios/batch",
            json={"task_id": "batch_audit_1", "scenes": [{"scene_id": "s1"}]},
            headers=operator,
        )

        response = client.get("/api/v1/sim/audit", headers=admin)
        assert response.status_code == 200
        actions = [item["action"] for item in response.json()["data"]["items"]]
        assert "scenario.batch.create" in actions

        denied = client.get("/api/v1/sim/audit", headers=operator)
        assert denied.status_code == 403

    def test_platform_overview(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        admin = _headers(settings)
        operator = _headers(settings, user="op-user", role="operator")
        client.post(
            "/api/v1/sim/instances", json={"mode": "sil", "map": "Town03"}, headers=operator
        )

        response = client.get("/api/v1/sim/platform/overview", headers=admin)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["tenant"] == "default"
        assert data["limits"]["max_instances_per_tenant"] == 5
        assert data["instances"]["total"] == 1
        assert data["persistence"]["enabled"] is True
        assert "execution" in data and "engines" in data["execution"]
        assert data["features"]["api_keys"] is True

        assert client.get("/api/v1/sim/platform/overview", headers=operator).status_code == 403

    def test_tenant_isolation_for_instances(
        self, platform_client: tuple[HunterSimSettings, TestClient]
    ) -> None:
        settings, client = platform_client
        operator = _headers(settings, user="op-user", role="operator")
        created = client.post(
            "/api/v1/sim/instances", json={"mode": "sil", "map": "Town03"}, headers=operator
        )
        instance_id = created.json()["data"]["sim_instance_id"]

        # 跨租户 admin 默认只列本租户（平台运维可用 tenant=* 统计）
        other_admin = _headers(settings, user="b-admin", role="admin", tenant="tenant-b")
        listing = client.get("/api/v1/sim/instances", headers=other_admin).json()["data"]
        assert listing["total"] == 0
        # 跨租户 operator 详情 404（不泄露存在性）；跨租户 admin 可运维式访问
        other_operator = _headers(settings, user="b-op", role="operator", tenant="tenant-b")
        detail = client.get(f"/api/v1/sim/instances/{instance_id}", headers=other_operator)
        assert detail.status_code == 404
        assert client.get(f"/api/v1/sim/instances/{instance_id}", headers=other_admin).status_code == 200

        # 同租户 admin 可查看
        same_admin = _headers(settings, user="a-admin", role="admin")
        assert client.get(f"/api/v1/sim/instances/{instance_id}", headers=same_admin).status_code == 200

"""产物写入/预签名上传测试（上传失败不阻断结果）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import SecretStr

from hunter_sim.common.models import CloudSettings
from hunter_sim.platform_adapter.artifacts import ArtifactManager


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[dict[str, Any]] = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """假 data-collector 客户端（presign/put/complete）。"""

    def __init__(
        self,
        *,
        presign: Optional[dict[str, Any]] = None,
        put_status: int = 200,
        post_error: bool = False,
    ) -> None:
        self.presign = presign or {}
        self.put_status = put_status
        self.post_error = post_error
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.puts: list[str] = []

    def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        if self.post_error:
            raise RuntimeError("presign unavailable")
        self.posts.append((url, json))
        if url.endswith("/presign"):
            return _FakeResponse(200, {"code": 0, "data": self.presign})
        return _FakeResponse(200, {"code": 0, "data": {}})

    def put(self, url: str, content: Any = None) -> _FakeResponse:
        self.puts.append(url)
        return _FakeResponse(self.put_status, {})


def _cloud(enabled: bool) -> CloudSettings:
    return CloudSettings(
        enabled=enabled,
        gateway_url="http://gateway:8080",
        jwt_token=SecretStr("token") if enabled else None,
    )


def _manager(tmp_path: Path, cloud: CloudSettings, factory: Any = None) -> ArtifactManager:
    return ArtifactManager(
        cloud,
        artifacts_dir=str(tmp_path / "artifacts"),
        bucket="hunter-scene-assets",
        sim_id="sim-1",
        scene_id="scene-1",
        client_factory=factory,
    )


class TestLocalWrites:
    def test_write_json_keeps_local_copy_when_disabled(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, _cloud(False))
        artifact = manager.write_json("eval.json", {"grade": "A"})

        assert artifact.name == "eval.json"
        assert artifact.uploaded is False
        assert artifact.media_type == "application/json"
        assert artifact.size_bytes > 0
        assert artifact.object_key == "scenarios/simulations/sim-1/scene-1/eval.json"
        assert (manager.directory / "eval.json").exists()

    def test_write_jsonl_and_text(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, _cloud(False))
        jsonl = manager.write_jsonl("events.jsonl", [{"event_type": "collision"}])
        text = manager.write_text("run.log", "hello")

        assert jsonl.media_type == "application/x-ndjson"
        assert jsonl.size_bytes > 0
        assert text.media_type == "text/plain"
        assert (manager.directory / "run.log").read_text(encoding="utf-8") == "hello"

    def test_public_view_excludes_bucket_and_url(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, _cloud(False))
        public = manager.write_json("eval.json", {}).to_public()
        assert set(public) == {"name", "object_key", "size_bytes", "sha256", "media_type"}


class TestPresignedUpload:
    def test_upload_returns_presigned_object_key(self, tmp_path: Path) -> None:
        client = _FakeClient(
            presign={
                "object_key": "scenarios/uploaded/eval.json",
                "upload_url": "http://minio/upload",
                "bucket": "hunter-scene-assets",
            }
        )
        manager = _manager(tmp_path, _cloud(True), factory=lambda: client)

        artifact = manager.write_json("eval.json", {"grade": "A"})

        assert artifact.uploaded is True
        assert artifact.object_key == "scenarios/uploaded/eval.json"
        assert client.puts == ["http://minio/upload"]
        assert [url for url, _ in client.posts] == [
            "http://gateway:8080/api/v1/data/files/presign",
            "http://gateway:8080/api/v1/data/files/complete",
        ]
        assert client.posts[0][1]["bucket"] == "hunter-scene-assets"
        assert client.posts[0][1]["vehicle_id"] == "SIM-sim-1"

    def test_upload_failure_falls_back_to_local_key(self, tmp_path: Path) -> None:
        client = _FakeClient(
            presign={"object_key": "scenarios/uploaded/eval.json", "upload_url": "http://minio"}
        )
        client.put_status = 500
        manager = _manager(tmp_path, _cloud(True), factory=lambda: client)

        artifact = manager.write_json("eval.json", {})

        assert artifact.uploaded is False
        assert artifact.object_key == "scenarios/simulations/sim-1/scene-1/eval.json"

    def test_presign_error_does_not_raise(self, tmp_path: Path) -> None:
        client = _FakeClient(post_error=True)
        manager = _manager(tmp_path, _cloud(True), factory=lambda: client)

        artifact = manager.write_json("eval.json", {})

        assert artifact.uploaded is False
        assert (manager.directory / "eval.json").exists()

    def test_upload_without_token_skips_network(self, tmp_path: Path) -> None:
        client = _FakeClient()
        manager = _manager(
            tmp_path,
            CloudSettings(enabled=True, gateway_url="http://gateway:8080"),
            factory=lambda: client,
        )

        artifact = manager.write_json("eval.json", {})

        assert artifact.uploaded is False
        assert client.posts == []

    @pytest.mark.parametrize("file_name", ["../escape.txt", "a/b.txt", ""])
    def test_unsafe_names_are_sanitized(self, tmp_path: Path, file_name: str) -> None:
        manager = _manager(tmp_path, _cloud(False))
        artifact = manager.write_json(file_name, {})
        # 不允许逃逸出实例目录
        resolved = (manager.directory / artifact.object_key.split("/")[-1]).resolve()
        assert manager.directory.resolve() in resolved.parents

"""产物写入/预签名上传测试（上传失败不阻断结果）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import SecretStr

from hunter_sim.common.models import CloudSettings
from hunter_sim.platform_adapter.artifacts import ArtifactManager, _response_detail


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Optional[dict[str, Any]] = None,
        text: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

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
        post_status: int = 200,
        post_text: Optional[str] = None,
    ) -> None:
        self.presign = presign or {}
        self.put_status = put_status
        self.post_error = post_error
        self.post_status = post_status
        self.post_text = post_text
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.puts: list[str] = []

    def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        if self.post_error:
            raise RuntimeError("presign unavailable")
        self.posts.append((url, json))
        if self.post_status >= 400:
            return _FakeResponse(self.post_status, {}, text=self.post_text)
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
        bucket="hunter-raw-data",
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
                "bucket": "hunter-raw-data",
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
        assert client.posts[0][1]["bucket"] == "hunter-raw-data"
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


class TestV389PresignErrorDetail:
    """V3.8.9：presign/complete 4xx 带上截断响应体，使 422 等校验失败可定位。

    旧版 _post 在 4xx 只抛状态码、丢弃响应体（实机 presign 422 无法看到哪个字段
    不合法）；V3.8.9 把响应体截断后拼进异常/日志。
    """

    def test_presign_422_logs_response_body(self, tmp_path: Path, caplog) -> None:
        client = _FakeClient(
            post_status=422,
            post_text='{"detail":[{"loc":["body","bucket"],"msg":"field required"}]}',
        )
        manager = _manager(tmp_path, _cloud(True), factory=lambda: client)

        with caplog.at_level("WARNING"):
            artifact = manager.write_json("eval.json", {})

        assert artifact.uploaded is False
        # 异常信息既含状态码又含响应体片段（不再黑盒）
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "HTTP 422" in joined
        assert "bucket" in joined and "field required" in joined

    def test_response_detail_decodes_bytes_and_truncates(self) -> None:
        class _Resp:
            content = b"a" * 1200

        detail = _response_detail(_Resp())
        assert len(detail) <= 801  # 800 字 + 省略号
        assert detail.endswith("…")

    def test_response_detail_normalizes_whitespace(self) -> None:
        class _Resp:
            text = "  err1\n  err2   err3 "

        assert _response_detail(_Resp()) == "err1 err2 err3"

    def test_response_detail_empty_when_no_text_attr(self) -> None:
        # 兼容旧 mock（无 text/content 属性）：返回空串而非报错
        class _Bare:
            status_code = 500

        assert _response_detail(_Bare()) == ""


class TestV3811PresignedPutNoAuthHeader:
    """V3.8.11：presigned URL 直传 PUT 不得携带 Authorization 头。

    presigned 地址已把签名放在查询串里，若复用带 Bearer 头的客户端 PUT，
    MinIO/S3 会判定「多种鉴权方式并存」返回 400（multiple authentication types）。
    网关 presign/complete POST 仍需带 Bearer；两者必须用不同客户端。
    """

    def test_presigned_put_uses_no_auth_client(self, monkeypatch, tmp_path: Path) -> None:
        import httpx

        made: list[Any] = []

        class _RecClient:
            def __init__(self, timeout: Any = None, headers: Any = None) -> None:
                self.headers = dict(headers or {})
                self.did_put = False
                self.did_post = False
                made.append(self)

            def post(self, url: str, json: Any = None) -> _FakeResponse:
                self.did_post = True
                return _FakeResponse(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "object_key": "scenarios/uploaded/eval.json",
                            "upload_url": "http://minio/put?sig=1",
                            "bucket": "hunter-raw-data",
                        },
                    },
                )

            def put(self, url: str, content: Any = None) -> _FakeResponse:
                self.did_put = True
                return _FakeResponse(200, {})

            def close(self) -> None:
                return None

        monkeypatch.setattr(httpx, "Client", _RecClient)
        manager = _manager(tmp_path, _cloud(True), factory=None)

        artifact = manager.write_json("eval.json", {"grade": "A"})

        assert artifact.uploaded is True
        put_clients = [c for c in made if c.did_put]
        post_clients = [c for c in made if c.did_post]
        assert len(put_clients) == 1
        # 直传客户端不能带 Authorization（否则 MinIO 400 multiple auth types）
        assert "Authorization" not in put_clients[0].headers
        # 网关 POST 客户端必须带 Bearer
        assert post_clients and all("Authorization" in c.headers for c in post_clients)
        # 两者必须是不同实例
        assert put_clients[0] not in post_clients


class TestV3812PresignDiagnostics:
    """V3.8.12/V3.8.13：presigned 直传 403 SignatureDoesNotMatch 诊断。

    输出非机密签名口径（host/path/SigV4 字段）与查询参数名集合（值脱敏），以便区分
    host 不一致 / signed-headers 缺 content-type / SigV2 形态 / 时间过期；绝不泄露签名值。
    """

    def test_diagnostics_expose_signed_headers_but_not_signature(self) -> None:
        from hunter_sim.platform_adapter.artifacts import _presign_diagnostics

        url = (
            "http://minio:9000/hunter-raw-data/SIM-s1/eval.json"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKID%2F20261112%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20261112T060249Z&X-Amz-Expires=900"
            "&X-Amz-SignedHeaders=host%3Bcontent-type"
            "&X-Amz-Signature=deadbeefsecret"
        )
        diag = _presign_diagnostics(url)
        assert "host=minio:9000" in diag
        assert "X-Amz-SignedHeaders=host;content-type" in diag
        assert "X-Amz-Date=20261112T060249Z" in diag
        assert "X-Amz-Expires=900" in diag
        # V3.8.13：查询参数**名**可见（不机密），并标记含签名
        assert "query_keys=[" in diag
        assert "X-Amz-Signature" in diag  # 参数名可见
        assert "has_signature=True" in diag
        # 绝不泄露签名值
        assert "deadbeefsecret" not in diag
        assert "X-Amz-Signature=" not in diag

    def test_diagnostics_reveals_sigv2_query_shape(self) -> None:
        """实机形态：无 X-Amz-* 但查询带 SigV2 的 Signature/AWSAccessKeyId/Expires。"""
        from hunter_sim.platform_adapter.artifacts import _presign_diagnostics

        url = (
            "http://192.168.31.35:9000/hunter-raw-data/SIM-s1/eval.json"
            "?AWSAccessKeyId=AKID&Expires=1794465568&Signature=abc%2Bdef%3D"
        )
        diag = _presign_diagnostics(url)
        assert "host=192.168.31.35:9000" in diag
        # X-Amz 字段全空（无 sigv4 段），但参数名集揭示 SigV2 签名口径
        assert "X-Amz-SignedHeaders" not in diag
        assert "query_keys=[AWSAccessKeyId,Expires,Signature]" in diag
        assert "has_signature=True" in diag
        # 签名值脱敏（不输出 abc/def）
        assert "abc" not in diag

    def test_direct_put_failure_logs_diagnostics(self, tmp_path: Path, caplog) -> None:
        client = _FakeClient(
            presign={
                "object_key": "scenarios/uploaded/eval.json",
                "upload_url": (
                    "http://minio:9000/hunter-raw-data/SIM-s1/eval.json"
                    "?X-Amz-SignedHeaders=host&X-Amz-Expires=900&X-Amz-Signature=sec"
                ),
                "bucket": "hunter-raw-data",
            }
        )
        client.put_status = 403
        manager = _manager(tmp_path, _cloud(True), factory=lambda: client)

        with caplog.at_level("WARNING"):
            artifact = manager.write_json("eval.json", {})

        assert artifact.uploaded is False
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "HTTP 403" in joined
        assert "host=minio:9000" in joined
        assert "X-Amz-SignedHeaders=host" in joined
        # V3.8.13：presign 响应字段名也输出（发现是否漏用 headers/method）
        assert "presign_keys=['bucket', 'object_key', 'upload_url']" in joined
        # 绝不输出签名值
        assert "sec" not in joined
        assert "X-Amz-Signature=" not in joined

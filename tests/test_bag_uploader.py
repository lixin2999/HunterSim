"""BagUploader 单测（V2.2.0，mock httpx 客户端，无真实网络）。

覆盖 presign → 单次 PUT → complete 全流程，以及分片（multipart）分支、统一响应体
{code,message,data} 错误处理、未启用/缺 JWT 降级。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hunter_sim.cloud.bag_uploader import BagUploader, UploadError
from hunter_sim.common.models import CloudSettings
from pydantic import SecretStr


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text

    def json(self) -> Any:
        return self._body


class FakeClient:
    """按调用序列返回预置响应，并记录 put/post。"""

    def __init__(self, presign: dict[str, Any], *, multipart: bool = False) -> None:
        self._presign = presign
        self._multipart = multipart
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.puts: list[Any] = []
        self.put_headers: list[Any] = []

    def post(self, url: str, json: dict[str, Any] = None) -> FakeResponse:  # noqa: ANN001, D102
        self.posts.append((url, json))
        if "presign" in url:
            return FakeResponse(200, {"code": 0, "message": "ok", "data": self._presign})
        return FakeResponse(200, {"code": 0, "message": "ok", "data": {"status": "completed"}})

    def put(self, url: str, content: Any = None, headers: Any = None) -> FakeResponse:  # noqa: ANN001, D102
        self.puts.append((url, content))
        self.put_headers.append(headers)
        resp_headers = {"ETag": '"etag-1"'} if self._multipart else {}
        return FakeResponse(200, headers=resp_headers)

    def close(self) -> None:  # noqa: D102
        return None


def _settings(**overrides: object) -> CloudSettings:
    base: dict[str, Any] = {"enabled": True, "sim_id": "sim-001", "jwt_token": SecretStr("t0k")}
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


@pytest.fixture()
def bag_file(tmp_path: Path) -> Path:
    p = tmp_path / "run01.mcap"
    p.write_bytes(b"x" * 1024)
    return p


def test_single_put_flow(bag_file: Path) -> None:
    """单次 PUT：presign→upload_url PUT→complete。"""
    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/run01.mcap",
        "method": "put",
        "upload_url": "http://minio/put?sig=1",
    }
    client = FakeClient(presign)
    uploader = BagUploader(_settings(), client_factory=lambda: client)
    assert uploader.connect() is True
    result = uploader.upload(bag_file)
    assert result is not None
    assert result.bucket == "hunter-rosbag"
    assert result.object_key == "SIM_001/run01.mcap"
    assert result.size_bytes == 1024
    # presign + complete 两次 post，一次直传 put
    assert len(client.posts) == 2
    assert len(client.puts) == 1


def test_single_put_echoes_signed_content_type(bag_file: Path) -> None:
    """V3.8.14：单次 PUT 回送 presigned URL 签名时纳入的 content-type。"""
    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/run01.mcap",
        "method": "put",
        "upload_url": (
            "http://minio/put?AWSAccessKeyId=AKID&Expires=1"
            "&content-type=application%2Foctet-stream&Signature=a%2Fb%3D"
        ),
    }
    client = FakeClient(presign)
    uploader = BagUploader(_settings(), client_factory=lambda: client)
    uploader.connect()
    assert uploader.upload(bag_file) is not None
    assert client.put_headers[0] == {"content-type": "application/octet-stream"}


def test_multipart_put_echoes_signed_content_type(tmp_path: Path) -> None:
    """V3.8.14：分片 PUT 每个 part 也回送签名时的 content-type。"""
    big = tmp_path / "big.mcap"
    big.write_bytes(b"y" * 2048)
    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/big.mcap",
        "method": "multipart",
        "upload_id": "uid-9",
        "parts": [
            {"part_number": 1, "upload_url": "http://minio/p1?content-type=text%2Fplain"},
            {"part_number": 2, "upload_url": "http://minio/p2"},
        ],
    }
    client = FakeClient(presign, multipart=True)
    uploader = BagUploader(_settings(), client_factory=lambda: client)
    uploader.connect()
    assert uploader.upload(big) is not None
    assert client.put_headers[0] == {"content-type": "text/plain"}
    assert client.put_headers[1] == {}


def test_multipart_flow(tmp_path: Path) -> None:
    """method=multipart：按 parts 分片 PUT 并上报 etags。"""
    big = tmp_path / "big.mcap"
    big.write_bytes(b"y" * 2048)  # 小文件，但 presign 声明 multipart 走分片路径
    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/big.mcap",
        "method": "multipart",
        "upload_id": "uid-9",
        "parts": [
            {"part_number": 1, "upload_url": "http://minio/p1"},
            {"part_number": 2, "upload_url": "http://minio/p2"},
        ],
    }
    client = FakeClient(presign, multipart=True)
    uploader = BagUploader(_settings(), client_factory=lambda: client)
    uploader.connect()
    result = uploader.upload(big)
    assert result is not None
    assert len(client.puts) == 2
    complete_body = client.posts[-1][1]
    assert complete_body["upload_id"] == "uid-9"
    assert len(complete_body["parts"]) == 2


def test_business_error_code_raises(bag_file: Path) -> None:
    """统一响应体 code!=0 抛 UploadError。"""

    class ErrClient(FakeClient):
        def post(self, url, json=None):  # noqa: ANN001, D102
            return FakeResponse(200, {"code": 40001, "message": "denied", "data": None})

    uploader = BagUploader(_settings(), client_factory=lambda: ErrClient({}))
    uploader.connect()
    with pytest.raises(UploadError):
        uploader.upload(bag_file)


def test_complete_422_includes_body_and_hint(bag_file: Path) -> None:
    """V3.8.16：网关 4xx 抛错携截断响应体与 status_hint（实机 complete 422 定位）。"""

    class Complete422Client(FakeClient):
        def post(self, url, json=None):  # noqa: ANN001, D102
            if "complete" in url:
                return FakeResponse(
                    422, body=None, text='{"error":"SizeBytes field required"}'
                )
            return super().post(url, json)

    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/run01.mcap",
        "method": "put",
        "upload_url": "http://minio/put?sig=1",
    }
    uploader = BagUploader(
        _settings(), client_factory=lambda: Complete422Client(presign)
    )
    uploader.connect()
    with pytest.raises(UploadError) as ei:
        uploader.upload(bag_file)
    assert ei.value.status_hint == 422
    assert "field required" in str(ei.value)


def test_single_put_failure_includes_body_and_hint(bag_file: Path) -> None:
    """V3.8.16：单次直传 PUT 失败拼 MinIO 错误体并携 status_hint。"""

    class Put403Client(FakeClient):
        def put(self, url, content=None, headers=None):  # noqa: ANN001, D102
            return FakeResponse(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")

    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/run01.mcap",
        "method": "put",
        "upload_url": "http://minio/put?sig=1",
    }
    uploader = BagUploader(_settings(), client_factory=lambda: Put403Client(presign))
    uploader.connect()
    with pytest.raises(UploadError) as ei:
        uploader.upload(bag_file)
    assert ei.value.status_hint == 403
    assert "SignatureDoesNotMatch" in str(ei.value)


def test_multipart_part_failure_includes_hint(bag_file: Path) -> None:
    """V3.8.16：分片 PUT 失败携分片号、响应体与 status_hint。"""

    class Part403Client(FakeClient):
        def put(self, url, content=None, headers=None):  # noqa: ANN001, D102
            if "p2" in url:
                return FakeResponse(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")
            return super().put(url, content, headers)

    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/run01.mcap",
        "method": "multipart",
        "upload_id": "uid-9",
        "parts": [
            {"part_number": 1, "upload_url": "http://minio/p1"},
            {"part_number": 2, "upload_url": "http://minio/p2"},
        ],
    }
    uploader = BagUploader(_settings(), client_factory=lambda: Part403Client(presign, multipart=True))
    uploader.connect()
    with pytest.raises(UploadError) as ei:
        uploader.upload(bag_file)
    assert ei.value.status_hint == 403
    assert "分片 2" in str(ei.value)


def test_missing_jwt_downgrades() -> None:
    """缺 JWT 时 connect 返回 False，upload 返回 None（不抛）。"""
    uploader = BagUploader(_settings(jwt_token=None))
    assert uploader.connect() is False
    assert uploader.available is False


def test_disabled_noop() -> None:
    """未启用直接 no-op。"""
    uploader = BagUploader(_settings(enabled=False))
    assert uploader.connect() is False


def test_v3811_presigned_put_uses_no_auth_client(monkeypatch, bag_file: Path) -> None:
    """V3.8.11：presigned PUT 直传用无鉴权头客户端，网关 POST 用带 Bearer 客户端。

    复用带 Bearer 头的客户端向 presigned URL PUT 会被 MinIO 判定
    「多种鉴权方式并存」返回 400，故两者必须分开。
    """
    import httpx

    made: list[Any] = []

    presign = {
        "bucket": "hunter-rosbag",
        "object_key": "SIM_001/run01.mcap",
        "method": "put",
        "upload_url": "http://minio/put?sig=1",
    }

    class RecClient:
        def __init__(self, timeout: Any = None, headers: Any = None) -> None:
            self.headers = dict(headers or {})
            self.did_put = False
            self.did_post = False
            made.append(self)

        def post(self, url: str, json: Any = None) -> FakeResponse:
            self.did_post = True
            if "presign" in url:
                return FakeResponse(200, {"code": 0, "message": "ok", "data": presign})
            return FakeResponse(200, {"code": 0, "message": "ok", "data": {"status": "completed"}})

        def put(self, url: str, content: Any = None, headers: Any = None) -> FakeResponse:
            self.did_put = True
            return FakeResponse(200, headers={})

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", RecClient)
    uploader = BagUploader(_settings())  # 无 client_factory → 走生产双客户端分支
    assert uploader.connect() is True
    result = uploader.upload(bag_file)
    assert result is not None
    put_clients = [c for c in made if c.did_put]
    post_clients = [c for c in made if c.did_post]
    assert len(put_clients) == 1
    assert "Authorization" not in put_clients[0].headers
    assert post_clients and all("Authorization" in c.headers for c in post_clients)
    assert put_clients[0] not in post_clients

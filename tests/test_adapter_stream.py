"""实时画面端点测试（``/screenshot`` 与 ``/stream.mjpg``）。

两层覆盖：
- **端点层**（``platform_adapter/app.py``）：404/409/200 分支、JPEG/PNG 媒体类型、
  MJPEG 分片帧头（``--frame`` + Content-Type/Length）与流结束；
- **编排层**（``platform_adapter/service.py``）：最新帧读取、无新帧时重发上一帧、
  连续无帧超时结束、客户端断开后客户端计数归零、并发客户端上限。
"""

from __future__ import annotations

import types
from typing import Any, AsyncIterator, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.common.metrics import counter_value
from hunter_sim.platform_adapter import service as service_module
from hunter_sim.platform_adapter.app import create_app
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.service import AdapterService, StreamLimitExceeded
from hunter_sim.sensor_sim.frame_hub import STREAM_RGB, Frame, FrameHub

_JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"
_PNG = b"\x89PNG\r\n\x1a\npng-bytes"


def _frame(payload: bytes = _JPEG, content_type: str = "image/jpeg") -> Frame:
    return Frame(
        stream=STREAM_RGB,
        ts=1.0,
        seq=1,
        payload=payload,
        meta={"content_type": content_type, "width": 640, "height": 360},
    )


class _StreamService:
    """端点层替身：只实现画面相关方法。"""

    def __init__(self, frame: Optional[Frame], frames: Optional[list[Frame]] = None) -> None:
        self._frame = frame
        self._frames = frames or []
        self.stream_calls = 0

    def get(self, instance_id: str) -> Optional[InstanceRecord]:
        if instance_id != "inst-1":
            return None
        return InstanceRecord(instance_id="inst-1", sim_id="sim-1", scene_id="scene-1")

    def screenshot(self, instance_id: str) -> Optional[Frame]:
        return self._frame

    async def stream_frames(
        self, instance_id: str, fps: Optional[float] = None
    ) -> AsyncIterator[Frame]:
        self.stream_calls += 1
        for frame in self._frames:
            yield frame


def _client(service: Any) -> TestClient:
    return TestClient(create_app(service=service))


class TestScreenshotEndpoint:
    def test_returns_jpeg_with_no_store(self) -> None:
        with _client(_StreamService(_frame())) as client:
            response = client.get("/api/v1/instances/inst-1/screenshot")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        assert response.content == _JPEG

    def test_media_type_follows_frame_meta(self) -> None:
        with _client(_StreamService(_frame(_PNG, "image/png"))) as client:
            response = client.get("/api/v1/instances/inst-1/screenshot")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == _PNG

    def test_unknown_instance_is_404(self) -> None:
        with _client(_StreamService(_frame())) as client:
            response = client.get("/api/v1/instances/inst-404/screenshot")
        assert response.status_code == 404

    def test_missing_camera_is_409(self) -> None:
        with _client(_StreamService(None)) as client:
            response = client.get("/api/v1/instances/inst-1/screenshot")
        assert response.status_code == 409
        assert response.json()["detail"] == "camera_not_attached"


class TestStreamEndpoint:
    def test_unknown_instance_is_404(self) -> None:
        with _client(_StreamService(_frame())) as client:
            assert client.get("/api/v1/instances/inst-404/stream.mjpg").status_code == 404

    def test_missing_camera_is_409(self) -> None:
        with _client(_StreamService(None)) as client:
            response = client.get("/api/v1/instances/inst-1/stream.mjpg")
        assert response.status_code == 409
        assert response.json()["detail"] == "camera_not_attached"

    def test_frames_are_multipart_chunks(self) -> None:
        service = _StreamService(_frame(), [_frame(), _frame(_PNG, "image/png")])
        with _client(service) as client:
            with client.stream("GET", "/api/v1/instances/inst-1/stream.mjpg") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith(
                    "multipart/x-mixed-replace"
                )
                assert "boundary=frame" in response.headers["content-type"]
                body = b"".join(response.iter_raw())
        assert service.stream_calls == 1
        # 两个分片：边界 + 各自 Content-Type/Length + 原始字节
        assert body.count(b"--frame\r\n") == 2
        assert b"Content-Type: image/jpeg\r\n" in body
        assert b"Content-Type: image/png\r\n" in body
        assert b"Content-Length: " + str(len(_JPEG)).encode("ascii") + b"\r\n" in body
        assert _JPEG in body and _PNG in body

    def test_stream_ends_when_generator_exhausted(self) -> None:
        with _client(_StreamService(_frame(), [_frame()])) as client:
            with client.stream("GET", "/api/v1/instances/inst-1/stream.mjpg") as response:
                body = b"".join(response.iter_raw())
        assert body.count(b"--frame\r\n") == 1

    def test_stream_limit_is_503(self) -> None:
        with _client(_LimitedStreamService(_frame())) as client:
            response = client.get("/api/v1/instances/inst-1/stream.mjpg")
        assert response.status_code == 503
        assert response.json()["detail"] == "stream_limit_reached"


class _LimitedStreamService(_StreamService):
    """端点层替身：推流名额已满。"""

    async def stream_frames(
        self, instance_id: str, fps: Optional[float] = None
    ) -> AsyncIterator[Frame]:
        raise StreamLimitExceeded(1)
        yield _frame()  # pragma: no cover  仅为构成异步生成器


class _StubHub:
    """帧源替身：按序吐出帧，耗尽后返回 None。"""

    def __init__(self, frames: Optional[list[Frame]] = None) -> None:
        self._frames = list(frames or [])
        self.latest_calls = 0

    def latest(self, stream: str) -> Optional[Frame]:
        self.latest_calls += 1
        return self._frames.pop(0) if self._frames else None

    def stats(self) -> dict[str, int]:
        return {"total": 0, "rgb": 0}


class _FakeClock:
    """可控时钟（``time.time`` + ``asyncio.sleep`` 同步推进，避免真实等待）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _patch_clock(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    """把 service 模块的 ``time`` / ``asyncio.sleep`` 换成虚拟时钟。"""
    monkeypatch.setattr(service_module, "time", clock)
    monkeypatch.setattr(service_module, "asyncio", types.SimpleNamespace(sleep=clock.sleep))


def _service(hub: Optional[Any]) -> AdapterService:
    settings = HunterSimSettings()
    adapter = AdapterSettings(db_path="unused.db", artifacts_dir="unused")

    class _Executor:
        pass

    provider = (lambda _instance_id: hub) if hub is not None else None
    return AdapterService(settings, adapter, store=None, executor=_Executor(),  # type: ignore[arg-type]
                          frame_hub_provider=provider)


class TestServiceFrameSource:
    def test_screenshot_reads_latest_frame(self) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 1.0, _JPEG, {"content_type": "image/jpeg"})
        frame = _service(hub).screenshot("inst-1")
        assert frame is not None
        assert frame.payload == _JPEG

    def test_screenshot_without_scene_is_none(self) -> None:
        assert _service(None).screenshot("inst-1") is None

    async def test_stream_resends_last_frame_and_stops_on_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _patch_clock(monkeypatch, clock)
        hub = _StubHub([_frame()])
        baseline = counter_value(service_module.STREAM_CLIENTS)
        frames = [frame async for frame in _service(hub).stream_frames("inst-1", fps=10.0)]
        # 首帧之后无新帧：重发上一帧直到空闲超时（5s / 0.1s = 50 次）
        assert len(frames) > 1
        assert all(item.payload == _JPEG for item in frames)
        assert clock.sleeps and clock.sleeps[0] == pytest.approx(0.1)
        # 客户端计数在生成器结束后归零（断开即执行 finally）
        assert counter_value(service_module.STREAM_CLIENTS) == baseline

    async def test_stream_stops_immediately_without_hub(self) -> None:
        assert [frame async for frame in _service(None).stream_frames("inst-1")] == []

    async def test_stream_client_gauge_decrements_on_early_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _patch_clock(monkeypatch, clock)
        service = _service(_StubHub([_frame()]))
        baseline = counter_value(service_module.STREAM_CLIENTS)
        generator = service.stream_frames("inst-1")
        await anext(generator)
        assert service.active_streams == 1
        assert counter_value(service_module.STREAM_CLIENTS) == baseline + 1
        await generator.aclose()
        assert service.active_streams == 0
        assert counter_value(service_module.STREAM_CLIENTS) == baseline

    async def test_stream_rejects_client_beyond_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _patch_clock(monkeypatch, clock)
        service = _service(_StubHub([_frame(), _frame()]))
        service._adapter.stream_max_clients = 1  # noqa: SLF001
        baseline = counter_value(service_module.STREAM_CLIENTS)
        first = service.stream_frames("inst-1", fps=10.0)
        await anext(first)
        with pytest.raises(StreamLimitExceeded) as excinfo:
            await anext(service.stream_frames("inst-1", fps=10.0))
        assert excinfo.value.limit == 1
        await first.aclose()
        assert counter_value(service_module.STREAM_CLIENTS) == baseline

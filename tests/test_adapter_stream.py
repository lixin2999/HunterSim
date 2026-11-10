"""实时画面端点测试（``/screenshot`` 与 ``/stream.mjpg``）。

两层覆盖：
- **端点层**（``platform_adapter/app.py``）：404/204/200 分支、JPEG/PNG 媒体类型、
  MJPEG 分片帧头（``--frame`` + Content-Type/Length）与流结束；
- **编排层**（``platform_adapter/service.py``）：最新帧读取、无新帧时重发上一帧、
  连续无帧超时结束、客户端断开后客户端计数归零、并发客户端上限。

V3.5.5 追加名额租约（``TestStreamSlotLease``）：客户端异常断开留下的僵尸名额必须
被闲置回收，且名额占用/上限/帧率要能从快照读到 ——「MJPEG 不可用（推流已达上限）」
不得靠猜。
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.common.metrics import counter_value
from hunter_sim.platform_adapter import service as service_module
from hunter_sim.platform_adapter.app import create_app
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.service import AdapterService, StreamLimitExceeded
from hunter_sim.platform_adapter.store import InstanceStore
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
        self.screenshot_streams: list[Optional[str]] = []

    def get(self, instance_id: str) -> Optional[InstanceRecord]:
        if instance_id != "inst-1":
            return None
        return InstanceRecord(instance_id="inst-1", sim_id="sim-1", scene_id="scene-1")

    def screenshot(self, instance_id: str, stream: Optional[str] = None) -> Optional[Frame]:
        self.screenshot_streams.append(stream)
        return self._frame

    async def stream_frames(
        self, instance_id: str, fps: Optional[float] = None, stream: Optional[str] = None
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

    def test_missing_camera_is_204(self) -> None:
        """场景启动早期无首帧：204 让前端展示「等待首帧」，不打 409 错误。"""
        with _client(_StreamService(None)) as client:
            response = client.get("/api/v1/instances/inst-1/screenshot")
        assert response.status_code == 204
        assert response.content == b""

    def test_overview_stream_is_forwarded(self) -> None:
        service = _StreamService(_frame())
        with _client(service) as client:
            response = client.get("/api/v1/instances/inst-1/screenshot?stream=overview")
        assert response.status_code == 200
        assert service.screenshot_streams == ["overview"]


class TestStreamEndpoint:
    def test_unknown_instance_is_404(self) -> None:
        with _client(_StreamService(_frame())) as client:
            assert client.get("/api/v1/instances/inst-404/stream.mjpg").status_code == 404

    def test_missing_camera_is_204(self) -> None:
        with _client(_StreamService(None)) as client:
            response = client.get("/api/v1/instances/inst-1/stream.mjpg")
        assert response.status_code == 204
        assert response.content == b""

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


class _HeldSlotGenerator:
    """伪异步生成器：占用名额后在首帧等待期间失败（只实现路由用到的协议方法）。"""

    def __init__(self, owner: "_AbortFirstFrameService") -> None:
        self._owner = owner
        self.aclose_calls = 0
        self.held = False

    async def __anext__(self) -> Frame:
        if self.held:  # pragma: no cover  首帧即失败，不会走到第二次迭代
            raise StopAsyncIteration
        self.held = True
        self._owner.held += 1
        raise RuntimeError("client aborted while waiting for the first frame")

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self.held:
            self.held = False
            self._owner.held -= 1


class _AbortFirstFrameService(_StreamService):
    """端点层替身：``stream_frames`` 返回可观察 ``aclose()`` 的伪生成器。

    旧版路由在首帧拉取失败时不关闭生成器，只靠事件循环的异步生成器回收兜底；
    该兜底需要循环空闲才执行，反复刷新页面就会把 ``stream_max_clients`` 吃满。
    """

    def __init__(self, frame: Optional[Frame]) -> None:
        super().__init__(frame)
        self.held = 0
        self.generator: Optional[_HeldSlotGenerator] = None

    def stream_frames(
        self,
        instance_id: str,
        fps: Optional[float] = None,
        stream: Optional[str] = None,
    ) -> Any:
        self.stream_calls += 1
        self.generator = _HeldSlotGenerator(self)
        return self.generator


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


@pytest.fixture
def stored_service(tmp_path: Path) -> Iterator[AdapterService]:
    """带实例库与帧源的服务（``health_snapshot`` / ``runtime_status`` 需要 store）。"""
    store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    adapter = AdapterSettings(
        db_path=str(tmp_path / "adapter.db"), artifacts_dir="unused"
    )
    hub = _StubHub([_frame() for _ in range(4)])
    yield AdapterService(
        HunterSimSettings(),
        adapter,
        store,
        types.SimpleNamespace(),  # type: ignore[arg-type]
        frame_hub_provider=lambda _instance_id: hub,
    )
    store.close()


class TestStreamSlotLease:
    """名额租约与可观测字段（V3.5.5：「推流已达上限」必须能自愈、能看见）。"""

    def test_first_frame_failure_closes_generator(self) -> None:
        """首帧拉取异常：路由必须关掉生成器，让已占用的名额立即归还。"""
        service = _AbortFirstFrameService(_frame())
        with TestClient(
            create_app(service=service), raise_server_exceptions=False
        ) as client:
            response = client.get("/api/v1/instances/inst-1/stream.mjpg")

        assert response.status_code == 500
        assert service.held == 0
        assert service.generator is not None
        assert service.generator.aclose_calls == 1

    async def test_stale_lease_is_reclaimed_before_new_client(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """僵尸连接（没正常关 MJPEG 响应）不能把名额永久吃满。"""
        clock = _FakeClock()
        _patch_clock(monkeypatch, clock)
        caplog.set_level("WARNING", logger="hunter_sim.platform_adapter.service")
        service = _service(_StubHub([_frame(), _frame()]))
        service._adapter.stream_max_clients = 1  # noqa: SLF001
        baseline = counter_value(service_module.STREAM_CLIENTS)
        zombie = service.stream_frames("inst-1", fps=10.0)
        await anext(zombie)
        assert service.active_streams == 1
        with pytest.raises(StreamLimitExceeded):
            await anext(service.stream_frames("inst-1", fps=10.0))

        # 浏览器刷新/代理掉线：该连接不再 touch 租约，闲置超 30s 即回收
        clock.now += service_module._STREAM_SLOT_STALE_S + 1.0  # noqa: SLF001
        fresh = service.stream_frames("inst-1", fps=10.0)
        assert await anext(fresh) is not None
        assert service.active_streams == 1
        assert counter_value(service_module.STREAM_CLIENTS) == baseline + 1
        assert "Stream slots reclaimed as stale" in caplog.text
        # 已回收的僵尸租约再走 finally：不得重复减计数（否则名额读数跑偏）
        await zombie.aclose()
        assert counter_value(service_module.STREAM_CLIENTS) == baseline + 1
        await fresh.aclose()
        assert service.active_streams == 0
        assert counter_value(service_module.STREAM_CLIENTS) == baseline

    async def test_live_lease_survives_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """活跃客户端每出帧 touch 一次租约：长时间推流不能被误回收。"""
        clock = _FakeClock()
        _patch_clock(monkeypatch, clock)
        service = _service(_StubHub([_frame() for _ in range(4)]))
        generator = service.stream_frames("inst-1", fps=10.0)
        await anext(generator)
        for _ in range(3):
            clock.now += service_module._STREAM_SLOT_STALE_S + 1.0  # noqa: SLF001
            assert await anext(generator) is not None

        assert service._sweep_stale_stream_slots() == 0  # noqa: SLF001
        assert service.active_streams == 1
        await generator.aclose()
        assert service.active_streams == 0

    async def test_stream_state_is_exposed(
        self, monkeypatch: pytest.MonkeyPatch, stored_service: AdapterService
    ) -> None:
        """名额占用/上限/帧率同时出现在健康快照与运行态（卡顿可定性）。"""
        clock = _FakeClock()
        _patch_clock(monkeypatch, clock)
        idle = {"clients": 0, "limit": 8, "fps": 20.0}
        assert stored_service.health_snapshot()["stream"] == idle

        generator = stored_service.stream_frames("inst-1", fps=10.0)
        await anext(generator)
        busy = {"clients": 1, "limit": 8, "fps": 20.0}
        assert stored_service.health_snapshot()["stream"] == busy
        assert stored_service.runtime_status()["stream"] == busy
        await generator.aclose()
        assert stored_service.health_snapshot()["stream"] == idle

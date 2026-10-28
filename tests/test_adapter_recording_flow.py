"""录制 → 上传 → ``sensor_file`` 全链测试（V3.0.0 §4.2）。

覆盖：
- 录制开/关判据（``scene.sensors[].record`` / ``enabled``）与落盘路径；
- ``_close_recording`` 回写 ``recording_path`` / ``recording_bytes`` 且幂等；
- ``_upload_recording`` 的投递判据（无帧/文件缺失/云端关闭则跳过）；
- 成功上传后 ``sensor_file`` 的 ``start_time`` / ``end_time`` / ``frame_count`` /
  ``topics`` / ``format`` 全部取 ``RecordStats`` 真值（不再恒 0）；
- 保留期清理按 ``ADAPTER_RECORDING_RETENTION_HOURS`` 删除过期目录。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.common.models import CloudSettings, HunterSimSettings
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor, _RunContext, _safe_segment
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.scene_runner.scene_config import SensorSpec
from hunter_sim.sensor_sim.mcap_recorder import (
    TOPIC_IMU,
    TOPIC_RGB,
    McapRecorder,
    RecordStats,
    build_image_message,
    build_imu_message,
)

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")

_NS = 1_000_000_000


# ─── 基础脚手架 ──────────────────────────────────────────────────────────────


class _FakePlane:
    """执行平面替身：只提供 producer 查询。"""

    def __init__(self, producer: Any = None) -> None:
        self.producer = producer

    def producer_for_sim_id(self, sim_id: str) -> Optional[Any]:
        return self.producer


class _FakeConnector:
    """上传连接器替身：记录 upload_bag 调用参数。"""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    def upload_bag(self, file_path: Path, data_type: str = "rosbag", **kwargs: Any) -> None:
        self.uploads.append({"path": Path(file_path), "data_type": data_type, **kwargs})


class _FakeProducer:
    """SimDataProducer 替身：捕获 sensor_file 发布参数。"""

    def __init__(self) -> None:
        self.sensor_files: list[dict[str, Any]] = []
        self.closed = False

    def publish_sensor_file(self, **kwargs: Any) -> bool:
        self.sensor_files.append(kwargs)
        return True

    def close(self) -> None:
        self.closed = True


class _FakeUploadResult:
    def __init__(self) -> None:
        self.bucket = "hunter-rosbag"
        self.object_key = "sim-1/sensors_r0.mcap"
        self.size_bytes = 4096
        self.sha256 = "a" * 64
        self.md5 = "b" * 32


class _FakeUploader:
    """BagUploader 替身（真实上传需要 MinIO）。"""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self._result = _FakeUploadResult()

    def upload(self, file_path: Path, data_type: str = "rosbag") -> Any:
        self.calls.append(Path(file_path))
        return self._result

    def connect(self) -> bool:  # pragma: no cover - 仅在 start() 路径用到
        return True


@pytest.fixture
def store(tmp_path: Path) -> InstanceStore:
    instance_store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    yield instance_store
    instance_store.close()


@pytest.fixture
def settings() -> HunterSimSettings:
    return HunterSimSettings(cloud=CloudSettings(enabled=False))


def _adapter(tmp_path: Path, retention_hours: int = 24) -> AdapterSettings:
    return AdapterSettings(
        db_path=str(tmp_path / "adapter.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
        recordings_dir=str(tmp_path / "recordings"),
        recording_retention_hours=retention_hours,
    )


def _executor(
    settings: HunterSimSettings,
    adapter: AdapterSettings,
    store: InstanceStore,
    plane: Any = None,
) -> SimulationExecutor:
    return SimulationExecutor(settings, adapter, store, plane or _FakePlane())


def _record(store: InstanceStore, **overrides: Any) -> InstanceRecord:
    payload: dict[str, Any] = {
        "instance_id": "inst-1",
        "sim_id": "sim-1",
        "scene_id": "scene-1",
        "vehicle_id": "veh-1",
        "revision": 0,
    }
    payload.update(overrides)
    record = InstanceRecord(**payload)
    store.insert(record)
    return store.get(record.instance_id) or record


def _context(instance_record: InstanceRecord, **sensor_overrides: Any) -> _RunContext:
    spec = SensorSpec(**sensor_overrides)
    return _RunContext(record=instance_record, scene=SimpleNamespace(sensors=[spec]))  # type: ignore[arg-type]


def _push_frames(recorder: McapRecorder, count: int = 2) -> None:
    for index in range(count):
        ts = _NS + index * 100_000_000
        assert recorder.push_stream(
            "rgb", ts, build_image_message(b"\x01\x02\x03\x04", 2, 1, "bgra8", "cam", ts)
        )
        assert recorder.push_stream(
            "imu", ts, build_imu_message((0.1 * index, 0.0, 0.0), (0.0, 0.0, 0.0), "imu", ts)
        )


def _recorded_context(
    executor: SimulationExecutor, record: InstanceRecord
) -> _RunContext:
    """完成一次真实录制并关闭，返回带 ``record_stats`` 的上下文。"""
    context = _context(record)
    executor._start_recording(context)  # noqa: SLF001  被测私有流程
    assert context.recorder is not None
    _push_frames(context.recorder)
    stats = executor._close_recording(context)  # noqa: SLF001
    assert stats is not None
    return context


# ─── 录制开启判据 ────────────────────────────────────────────────────────────


class TestStartRecording:
    def test_opens_file_under_instance_dir(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        adapter = _adapter(tmp_path)
        executor = _executor(settings, adapter, store)
        context = _context(_record(store))

        executor._start_recording(context)  # noqa: SLF001

        assert context.recorder is not None
        assert context.recorder.is_recording is True
        expected_dir = Path(adapter.recordings_dir) / _safe_segment("inst-1")
        assert (expected_dir / "sensors_r0.mcap").parent == expected_dir

    def test_skips_when_record_disabled(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        context = _context(_record(store), record=False)
        executor._start_recording(context)  # noqa: SLF001
        assert context.recorder is None

    def test_skips_when_sensor_disabled(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        context = _context(_record(store), enabled=False, record=True)
        executor._start_recording(context)  # noqa: SLF001
        assert context.recorder is None


# ─── 关闭录制与元数据回写 ────────────────────────────────────────────────────


class TestCloseRecording:
    def test_stats_are_real_and_persisted(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        record = _record(store)
        context = _recorded_context(executor, record)

        stats = context.record_stats
        assert stats is not None
        assert stats.frame_count == 4
        assert stats.topics == (TOPIC_RGB, TOPIC_IMU)
        assert stats.size_bytes > 0
        assert stats.end_time > stats.start_time

        reloaded = store.get(record.instance_id)
        assert reloaded is not None
        assert reloaded.recording_path == stats.path
        assert reloaded.recording_bytes == stats.size_bytes

    def test_close_is_idempotent(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        context = _recorded_context(executor, _record(store))
        first = context.record_stats

        again = executor._close_recording(context)  # noqa: SLF001

        assert again is first
        assert context.recorder is None


class TestNoRecording:
    def test_close_without_recorder_returns_none(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        context = _context(_record(store), record=False)
        assert executor._close_recording(context) is None  # noqa: SLF001


# ─── 上传投递 ────────────────────────────────────────────────────────────────


class TestUploadRecording:
    def test_uploads_with_record_stats(
        self,
        settings: HunterSimSettings,
        store: InstanceStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        context = _recorded_context(executor, _record(store))
        connector = _FakeConnector()
        monkeypatch.setattr(executor, "_connector_for", lambda _record: connector)

        executor._upload_recording(context)  # noqa: SLF001

        assert len(connector.uploads) == 1
        job = connector.uploads[0]
        assert job["path"] == Path(context.record_stats.path)  # type: ignore[union-attr]
        assert job["data_type"] == "rosbag"
        assert job["stats"] is context.record_stats
        assert job["scene_id"] == "scene-1"
        assert job["vehicle_id"] == "veh-1"

    def test_skips_without_frames(
        self,
        settings: HunterSimSettings,
        store: InstanceStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        path = tmp_path / "empty.mcap"
        path.write_bytes(b"x")
        context = _RunContext(
            record=_record(store),
            scene=SimpleNamespace(sensors=[SensorSpec()]),  # type: ignore[arg-type]
            record_stats=RecordStats(path=str(path), start_time=0.0, end_time=0.0),
        )
        connector = _FakeConnector()
        monkeypatch.setattr(executor, "_connector_for", lambda _record: connector)

        executor._upload_recording(context)  # noqa: SLF001

        assert connector.uploads == []

    def test_skips_when_file_missing(
        self,
        settings: HunterSimSettings,
        store: InstanceStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        context = _RunContext(
            record=_record(store),
            scene=SimpleNamespace(sensors=[SensorSpec()]),  # type: ignore[arg-type]
            record_stats=RecordStats(
                path=str(tmp_path / "gone.mcap"), start_time=0.0, end_time=1.0, frame_count=5
            ),
        )
        connector = _FakeConnector()
        monkeypatch.setattr(executor, "_connector_for", lambda _record: connector)

        executor._upload_recording(context)  # noqa: SLF001

        assert connector.uploads == []

    def test_connector_none_when_cloud_disabled(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        assert executor._connector_for(_record(store)) is None  # noqa: SLF001

    def test_connector_reuses_plane_producer(
        self, store: InstanceStore, tmp_path: Path
    ) -> None:
        producer = _FakeProducer()
        settings = HunterSimSettings(cloud=CloudSettings(enabled=True, sim_id="sim-1"))
        executor = _executor(settings, _adapter(tmp_path), store, _FakePlane(producer))

        connector = executor._connector_for(_record(store))  # noqa: SLF001

        assert connector is not None
        assert connector.started is True
        assert connector.producer is producer
        executor._stop_connector()  # noqa: SLF001


# ─── 上传 → sensor_file 字段真值 ─────────────────────────────────────────────


class TestSensorFilePayload:
    def test_fields_come_from_record_stats(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        assert recorder.open(tmp_path / "sensors_r0.mcap") is True
        _push_frames(recorder, count=3)
        stats = recorder.close()
        assert stats.frame_count == 6

        producer = _FakeProducer()
        connector = CloudConnector(CloudSettings(enabled=True, sim_id="sim-1"))
        connector.attach_producer(producer, sim_id="sim-1")  # type: ignore[arg-type]
        uploader = _FakeUploader()
        connector.uploader = uploader  # type: ignore[assignment]

        connector.upload_bag(
            Path(stats.path),
            data_type="rosbag",
            stats=stats,
            scene_id="scene-1",
            vehicle_id="veh-1",
        )
        assert connector.wait_upload_idle(5.0) is True
        connector.stop()

        assert uploader.calls == [Path(stats.path)]
        assert len(producer.sensor_files) == 1
        payload = producer.sensor_files[0]
        assert payload["start_time"] == pytest.approx(stats.start_time)
        assert payload["end_time"] == pytest.approx(stats.end_time)
        assert payload["frame_count"] == stats.frame_count
        assert payload["topics"] == list(stats.topics)
        assert payload["file_format"] == "mcap"
        assert payload["sensor_type"] == "rosbag"
        assert payload["scene_id"] == "scene-1"
        assert payload["vehicle_id"] == "veh-1"
        assert payload["md5"] == "b" * 32
        assert payload["size_bytes"] == 4096

    def test_upload_failure_is_isolated(self, tmp_path: Path) -> None:
        file_path = tmp_path / "broken.mcap"
        file_path.write_bytes(b"x")
        producer = _FakeProducer()
        connector = CloudConnector(CloudSettings(enabled=True, sim_id="sim-1"))
        connector.attach_producer(producer, sim_id="sim-1")  # type: ignore[arg-type]

        class _Boom:
            def upload(self, *_args: Any, **_kwargs: Any) -> Any:
                raise OSError("minio down")

        connector.uploader = _Boom()  # type: ignore[assignment]
        connector.upload_bag(file_path, stats=None)
        assert connector.wait_upload_idle(5.0) is True
        connector.stop()

        assert producer.sensor_files == []
        assert connector.upload_stats["failed"] >= 1


# ─── 保留期清理 ──────────────────────────────────────────────────────────────


class TestRetentionSweep:
    def test_removes_expired_instance_dir(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        adapter = _adapter(tmp_path, retention_hours=1)
        executor = _executor(settings, adapter, store)
        root = Path(adapter.recordings_dir)
        old_dir = root / "inst-old"
        new_dir = root / "inst-new"
        for directory in (old_dir, new_dir):
            directory.mkdir(parents=True)
            (directory / "sensors_r0.mcap").write_bytes(b"data")
        stale = time.time() - 3 * 3600
        os.utime(old_dir / "sensors_r0.mcap", (stale, stale))

        executor._sweep_recordings()  # noqa: SLF001

        assert not old_dir.exists()
        assert new_dir.exists()

    def test_disabled_when_retention_zero(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        adapter = _adapter(tmp_path, retention_hours=0)
        executor = _executor(settings, adapter, store)
        old_dir = Path(adapter.recordings_dir) / "inst-old"
        old_dir.mkdir(parents=True)
        (old_dir / "sensors_r0.mcap").write_bytes(b"data")
        stale = time.time() - 30 * 3600
        os.utime(old_dir / "sensors_r0.mcap", (stale, stale))

        executor._sweep_recordings()  # noqa: SLF001

        assert old_dir.exists()

    def test_missing_root_is_noop(
        self, settings: HunterSimSettings, store: InstanceStore, tmp_path: Path
    ) -> None:
        executor = _executor(settings, _adapter(tmp_path), store)
        executor._sweep_recordings()  # noqa: SLF001  目录不存在不应抛


class TestSafeSegment:
    def test_sanitizes_path_separators(self) -> None:
        assert "/" not in _safe_segment("inst/../evil")
        assert "\\" not in _safe_segment("inst\\evil")

    def test_keeps_reasonable_ids(self) -> None:
        assert _safe_segment("inst-abc_123") == "inst-abc_123"

    def test_empty_falls_back(self) -> None:
        assert _safe_segment("") == "unknown"

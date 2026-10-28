"""McapRecorder 单测（V3.0.0）。

覆盖：真实写入并可被 ``mcap`` reader 读回（主题/schema/消息体）、``RecordStats``
真值统计、未知主题/流的忽略、依赖缺失与磁盘异常降级、消息构造函数。
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from hunter_sim.common.metrics import SENSOR_RECORD_BYTES_TOTAL
from hunter_sim.sensor_sim.mcap_recorder import (
    STREAM_TO_TOPIC,
    TOPIC_DEPTH,
    TOPIC_IMU,
    TOPIC_LIDAR,
    TOPIC_RGB,
    TOPIC_SCHEMAS,
    McapRecorder,
    RecordStats,
    build_image_message,
    build_imu_message,
    build_pointcloud2_message,
)

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")

_NS = 1_000_000_000


def _read_back(path: Path) -> list[tuple[str, str, object]]:
    """读回 mcap，返回 ``(topic, schema_name, decoded_message)`` 列表。"""
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory

    rows: list[tuple[str, str, object]] = []
    with path.open("rb") as handle:
        reader = make_reader(handle, decoder_factories=[DecoderFactory()])
        for schema, channel, _record, decoded in reader.iter_decoded_messages():
            rows.append((channel.topic, schema.name if schema else "", decoded))
    return rows


def _write_image(recorder: McapRecorder, stream: str = "rgb", ts: int = _NS) -> bool:
    return recorder.push_stream(
        stream,
        ts,
        build_image_message(b"\x01\x02\x03\x04", 2, 1, "bgra8", "camera_front", ts),
    )


class TestLifecycle:
    def test_open_push_close_produces_file(self, tmp_path: Path) -> None:
        recorder = McapRecorder()

        assert recorder.open(tmp_path / "run.mcap") is True
        assert recorder.is_recording is True
        assert _write_image(recorder) is True
        assert recorder.frame_count == 1

        stats = recorder.close()

        assert recorder.is_recording is False
        assert isinstance(stats, RecordStats)
        assert stats.path.endswith("run.mcap")
        assert Path(stats.path).is_file()
        assert stats.size_bytes > 0
        assert stats.frame_count == 1
        assert stats.topics == (TOPIC_RGB,)
        assert stats.start_time == pytest.approx(1.0)
        assert stats.end_time == pytest.approx(1.0)
        assert stats.duration_s == pytest.approx(0.0)

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        recorder.open(tmp_path / "run.mcap")
        _write_image(recorder)

        first = recorder.close()
        second = recorder.close()

        assert first.frame_count == 1
        assert second == RecordStats()  # 未打开时返回空统计

    def test_push_before_open_is_noop(self) -> None:
        recorder = McapRecorder()

        assert _write_image(recorder) is False
        assert recorder.frame_count == 0
        assert recorder.close() == RecordStats()

    def test_reopen_while_open_keeps_writer(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        assert recorder.open(tmp_path / "a.mcap") is True
        _write_image(recorder)

        assert recorder.open(tmp_path / "b.mcap") is True
        _write_image(recorder, ts=2 * _NS)
        stats = recorder.close()

        assert stats.path.endswith("a.mcap")
        assert stats.frame_count == 2
        assert not (tmp_path / "b.mcap").exists()

    def test_open_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deep" / "run.mcap"
        recorder = McapRecorder()

        assert recorder.open(target) is True
        recorder.close()

        assert target.is_file()

    def test_open_on_directory_degrades(self, tmp_path: Path) -> None:
        target = tmp_path / "occupied"
        target.mkdir()
        recorder = McapRecorder()

        assert recorder.open(target) is False
        assert recorder.degraded is True
        assert recorder.is_recording is False

    def test_missing_dependency_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".")[0] in {"mcap", "mcap_ros2"}:
                raise ImportError(f"simulated missing dependency: {name}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)
        recorder = McapRecorder()

        assert recorder.open(tmp_path / "run.mcap") is False
        assert recorder.degraded is True
        assert _write_image(recorder) is False
        assert recorder.close() == RecordStats()
        assert not (tmp_path / "run.mcap").exists()


class TestPush:
    def test_unknown_topic_is_ignored(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        recorder.open(tmp_path / "run.mcap")

        assert recorder.push("/carla/unknown", _NS, object()) is False

        stats = recorder.close()
        assert stats.frame_count == 0
        assert stats.topics == ()

    def test_unknown_stream_is_ignored(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        recorder.open(tmp_path / "run.mcap")

        assert recorder.push_stream("thermal", _NS, object()) is False
        recorder.close()

    def test_write_error_is_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = McapRecorder()
        recorder.open(tmp_path / "run.mcap")
        writer = recorder._writer  # noqa: SLF001  注入故障需要拿到底层 writer
        original = writer.write_message
        calls = {"n": 0}

        def flaky(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated disk error")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(writer, "write_message", flaky)

        # 单帧写入失败不计数、不影响后续帧
        assert _write_image(recorder, ts=_NS) is False
        assert recorder.frame_count == 0
        assert _write_image(recorder, ts=2 * _NS) is True

        stats = recorder.close()
        assert stats.frame_count == 1

    def test_record_stats_track_multiple_topics(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        recorder.open(tmp_path / "run.mcap")
        lidar = b"\x00" * 32  # 2 点 × 16 字节

        recorder.push_stream(
            "lidar", _NS, build_pointcloud2_message(lidar, 2, "lidar_top", _NS)
        )
        _write_image(recorder, "rgb", 2 * _NS)
        recorder.push_stream(
            "imu",
            3 * _NS,
            build_imu_message((1.0, 2.0, 3.0), (0.1, 0.2, 0.3), "imu_link", 3 * _NS),
        )

        stats = recorder.close()

        assert stats.frame_count == 3
        # topics 按字典序输出，保证 sensor_file 消息可复现
        assert stats.topics == (TOPIC_RGB, TOPIC_IMU, TOPIC_LIDAR)
        assert stats.start_time == pytest.approx(1.0)
        assert stats.end_time == pytest.approx(3.0)
        assert stats.duration_s == pytest.approx(2.0)

    def test_record_bytes_metric_increments(self, tmp_path: Path) -> None:
        recorder = McapRecorder()
        recorder.open(tmp_path / "run.mcap")
        before = SENSOR_RECORD_BYTES_TOTAL._value.get()

        _write_image(recorder, "rgb")
        _write_image(recorder, "rgb", 2 * _NS)
        recorder.close()

        assert SENSOR_RECORD_BYTES_TOTAL._value.get() - before == 8  # 2 帧 × 4 字节


class TestReadBack:
    def test_image_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "image.mcap"
        recorder = McapRecorder()
        recorder.open(path)
        recorder.push_stream(
            "rgb",
            _NS,
            build_image_message(b"\x01\x02\x03\x04\x05\x06", 2, 1, "rgb8", "camera_front", _NS),
        )
        recorder.close()

        rows = _read_back(path)

        assert len(rows) == 1
        topic, schema_name, message = rows[0]
        assert topic == TOPIC_RGB
        assert schema_name == "sensor_msgs/msg/Image"
        assert message.height == 1  # type: ignore[attr-defined]
        assert message.width == 2  # type: ignore[attr-defined]
        assert message.encoding == "rgb8"  # type: ignore[attr-defined]
        assert message.step == 6  # type: ignore[attr-defined]
        assert bytes(message.data) == b"\x01\x02\x03\x04\x05\x06"  # type: ignore[attr-defined]
        assert message.header.frame_id == "camera_front"  # type: ignore[attr-defined]
        assert message.header.stamp.sec == 1  # type: ignore[attr-defined]

    def test_depth_image_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "depth.mcap"
        recorder = McapRecorder()
        recorder.open(path)
        recorder.push_stream(
            "depth",
            _NS,
            build_image_message(b"\x00" * 8, 2, 1, "32FC1", "camera_depth", _NS),
        )
        recorder.close()

        rows = _read_back(path)

        assert len(rows) == 1
        topic, schema_name, message = rows[0]
        assert topic == TOPIC_DEPTH
        assert schema_name == "sensor_msgs/msg/Image"
        assert message.encoding == "32FC1"  # type: ignore[attr-defined]
        assert message.step == 8  # type: ignore[attr-defined]

    def test_pointcloud2_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "lidar.mcap"
        recorder = McapRecorder()
        recorder.open(path)
        payload = b"\x00" * 48  # 3 点 × 16 字节
        recorder.push_stream(
            "lidar", _NS, build_pointcloud2_message(payload, 3, "lidar_top", _NS)
        )
        recorder.close()

        rows = _read_back(path)

        assert len(rows) == 1
        topic, schema_name, message = rows[0]
        assert topic == TOPIC_LIDAR
        assert schema_name == "sensor_msgs/msg/PointCloud2"
        assert message.width == 3  # type: ignore[attr-defined]
        assert message.height == 1  # type: ignore[attr-defined]
        assert message.point_step == 16  # type: ignore[attr-defined]
        assert message.row_step == 48  # type: ignore[attr-defined]
        assert len(message.fields) == 4  # type: ignore[attr-defined]
        assert [f.name for f in message.fields] == [  # type: ignore[attr-defined]
            "x",
            "y",
            "z",
            "intensity",
        ]

    def test_imu_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "imu.mcap"
        recorder = McapRecorder()
        recorder.open(path)
        recorder.push_stream(
            "imu",
            _NS,
            build_imu_message((1.5, -2.5, 0.25), (0.1, 0.2, 0.3), "imu_link", _NS),
        )
        recorder.close()

        rows = _read_back(path)

        assert len(rows) == 1
        topic, schema_name, message = rows[0]
        assert topic == TOPIC_IMU
        assert schema_name == "sensor_msgs/msg/Imu"
        assert message.linear_acceleration.x == pytest.approx(1.5)  # type: ignore[attr-defined]
        assert message.linear_acceleration.y == pytest.approx(-2.5)  # type: ignore[attr-defined]
        assert message.angular_velocity.z == pytest.approx(0.3)  # type: ignore[attr-defined]
        assert message.orientation.w == pytest.approx(1.0)  # type: ignore[attr-defined]


class TestMessageBuilders:
    def test_image_step_depends_on_encoding(self) -> None:
        bgra = build_image_message(b"\x00" * 8, 2, 1, "bgra8", "cam", _NS)
        rgb = build_image_message(b"\x00" * 6, 2, 1, "rgb8", "cam", _NS)

        assert bgra.step == 8
        assert rgb.step == 6
        assert bgra.is_bigendian == 0

    def test_stamp_split(self) -> None:
        message = build_image_message(b"\x00" * 4, 1, 1, "bgra8", "cam", 2 * _NS + 500)

        assert message.header.stamp.sec == 2
        assert message.header.stamp.nanosec == 500

    def test_pointcloud_fields_layout(self) -> None:
        message = build_pointcloud2_message(b"\x00" * 16, 1, "lidar_top", _NS)

        assert [f.offset for f in message.fields] == [0, 4, 8, 12]
        assert all(f.datatype == 7 for f in message.fields)
        assert message.is_dense is True

    def test_imu_covariances_are_zeroed(self) -> None:
        message = build_imu_message((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "imu_link", _NS)

        assert message.orientation_covariance == [0.0] * 9
        assert message.angular_velocity_covariance == [0.0] * 9
        assert message.linear_acceleration_covariance == [0.0] * 9
        assert message.orientation.w == pytest.approx(1.0)


class TestSchemaTable:
    def test_stream_to_topic_is_complete(self) -> None:
        assert STREAM_TO_TOPIC == {
            "lidar": TOPIC_LIDAR,
            "rgb": TOPIC_RGB,
            "depth": TOPIC_DEPTH,
            "imu": TOPIC_IMU,
        }

    def test_every_topic_has_schema(self) -> None:
        assert set(TOPIC_SCHEMAS) == set(STREAM_TO_TOPIC.values())
        for name, msgdef in TOPIC_SCHEMAS.values():
            assert name.startswith("sensor_msgs/msg/")
            assert msgdef.strip()

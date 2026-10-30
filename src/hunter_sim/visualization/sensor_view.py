"""传感器帧渲染（FrameHub 帧 → 可视化客户端可消费的格式）。

``FrameHub`` 只存原始字节（JPEG/PNG、32FC1 米制深度、xyz float32 点云、
IMU 的 ``repr`` 文本），浏览器无法直接使用，因此在这一层做「按流分派」的
轻量渲染：

- ``rgb``     → 原样返回 JPEG/PNG 字节（客户端 ``<img>`` 直显）；
- ``depth``   → 归一化为 8bit 灰度 PNG（近处亮、远处暗，截断 0~50 m）；
- ``lidar``   → 抽样后的点坐标 JSON（默认上限 4000 点，避免一次推上百 KB）；
- ``imu``     → 加速度/角速度 JSON。

可选依赖（numpy / Pillow）缺失或 payload 长度不符时一律返回 ``None``，
由路由层降级为 503/409，绝不因为可视化把控制面拖崩；深度图在 Pillow
缺失时改为标准库 PNG 编码，功能不因图像库缺失而消失。
"""

from __future__ import annotations

import ast
import io
import struct
import zlib
from typing import Any, Optional

from hunter_sim.common.utils import get_logger
from hunter_sim.sensor_sim.frame_hub import (
    STREAM_DEPTH,
    STREAM_IMU,
    STREAM_LIDAR,
    STREAM_RGB,
    Frame,
)

logger = get_logger(__name__)

# 深度图归一化范围（米）：超出按最近/最远截断
_DEPTH_NEAR_M = 0.0
_DEPTH_FAR_M = 50.0
# 点云单点字节数（x/y/z 三个 float32）
_LIDAR_POINT_BYTES = 12
# IMU 文本快照的最大长度（防异常 payload 撑爆响应）
_IMU_TEXT_MAX = 512


def frame_summary(frame: Frame, now: float) -> dict[str, Any]:
    """单帧摘要（传感器面板表格行）。

    Args:
        frame: 帧快照。
        now: 当前时间戳（用于计算数据陈旧度）。

    Returns:
        含 ``stream`` / ``seq`` / ``ts`` / ``size`` / ``age_s`` / ``meta`` 的字典。
    """
    return {
        "stream": frame.stream,
        "seq": frame.seq,
        "ts": round(frame.ts, 3),
        "size": frame.size,
        "age_s": round(max(0.0, float(now) - frame.ts), 3),
        "meta": dict(frame.meta),
    }


def lidar_points(frame: Frame, max_points: int = 4000) -> dict[str, Any]:
    """点云抽稀为 JSON（保留几何形状，限制响应体积）。

    Args:
        frame: ``lidar`` 流帧（payload 为 xyz float32 拼接）。
        max_points: 返回点数上限（``<=0`` 时按 4000 处理）。

    Returns:
        ``{total, sampled, stride, points}``；payload 不可解析时 ``points`` 为空。
    """
    limit = max_points if max_points > 0 else 4000
    payload = frame.payload
    usable = len(payload) - (len(payload) % _LIDAR_POINT_BYTES)
    total = usable // _LIDAR_POINT_BYTES
    result: dict[str, Any] = {
        "stream": STREAM_LIDAR,
        "ts": round(frame.ts, 3),
        "seq": frame.seq,
        "total": total,
        "sampled": 0,
        "stride": 1,
        "points": [],
    }
    if total <= 0:
        return result
    try:
        import numpy as np  # noqa: PLC0415  可选依赖：缺失时降级为无点云
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Lidar preview disabled (numpy unavailable): {exc}")
        return result
    points = np.frombuffer(payload[:usable], dtype=np.float32).reshape(total, 3)
    stride = max(1, total // limit)
    sampled = points[::stride]
    result["stride"] = stride
    result["sampled"] = int(sampled.shape[0])
    # 保留 3 位小数：可视化只需形状，不必回传全精度
    result["points"] = np.round(sampled, 3).tolist()
    return result


def imu_values(frame: Frame) -> dict[str, Any]:
    """解析 IMU 帧（payload 为 ``repr(((ax,ay,az),(gx,gy,gz)))``）。

    Args:
        frame: ``imu`` 流帧。

    Returns:
        ``{accel, gyro}``；无法解析时返回 ``{raw}`` 文本快照。
    """
    text = frame.payload.decode("utf-8", errors="replace").strip()
    base: dict[str, Any] = {"stream": STREAM_IMU, "ts": round(frame.ts, 3), "seq": frame.seq}
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        base["raw"] = text[:_IMU_TEXT_MAX]
        return base
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], tuple)
        and isinstance(value[1], tuple)
    ):
        base["accel"] = [round(float(item), 4) for item in value[0]]
        base["gyro"] = [round(float(item), 4) for item in value[1]]
        return base
    base["raw"] = text[:_IMU_TEXT_MAX]
    return base


def depth_png(frame: Frame, max_width: int = 480) -> Optional[bytes]:
    """把 32FC1 米制深度渲染为 8bit 灰度 PNG。

    Args:
        frame: ``depth`` 流帧（meta 需带 ``width`` / ``height``）。
        max_width: 输出最大宽度（0 表示不缩放）。

    Returns:
        PNG 字节；numpy 缺失或尺寸不符时返回 None。

    Pillow 仅用于缩放，缺失时回落到标准库编码（``_encode_gray_png``），
    因此深度预览不额外依赖图像库。
    """
    try:
        width = int(frame.meta.get("width") or 0)
        height = int(frame.meta.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    expected = width * height * 4  # float32
    if len(frame.payload) < expected:
        logger.debug(
            f"Depth frame payload too short: got={len(frame.payload)}, expected={expected}"
        )
        return None
    try:
        import numpy as np  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001  可选依赖缺失降级
        logger.debug(f"Depth preview disabled (numpy unavailable): {exc}")
        return None
    meters = np.frombuffer(frame.payload[:expected], dtype=np.float32).reshape(height, width)
    # nan/inf 在 CARLA 深度图里可能出现（天空/无回波），先清洗再归一化
    clean = np.nan_to_num(meters, nan=_DEPTH_FAR_M, posinf=_DEPTH_FAR_M, neginf=_DEPTH_NEAR_M)
    span = max(1e-6, _DEPTH_FAR_M - _DEPTH_NEAR_M)
    ratio = np.clip((clean - _DEPTH_NEAR_M) / span, 0.0, 1.0)
    gray = ((1.0 - ratio) * 255.0).astype(np.uint8)
    if max_width > 0 and width > max_width:
        step = max(1, -(-width // max_width))  # 上取整，避免步长过小
        gray = gray[::step, ::step]
        height, width = int(gray.shape[0]), int(gray.shape[1])
    try:
        from PIL import Image  # noqa: PLC0415  仅缩放分支使用
    except Exception:  # noqa: BLE001  Pillow 缺失 → 标准库编码
        return _encode_gray_png(gray.tobytes(), width, height)
    image = Image.fromarray(gray, mode="L")
    if (image.width, image.height) != (width, height):
        image = image.resize((width, height), Image.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _encode_gray_png(rows: bytes, width: int, height: int) -> Optional[bytes]:
    """标准库实现的最小 8bit 灰度 PNG 编码（Pillow 缺失时的兜底）。"""
    if width <= 0 or height <= 0 or len(rows) < width * height:
        return None

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    stride = width
    raw = b"".join(
        b"\x00" + rows[line * stride:(line + 1) * stride] for line in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


def render_stream(frame: Frame, *, max_points: int = 4000, max_width: int = 480) -> tuple[bytes, str]:
    """把一个帧渲染为 (响应体, 媒体类型)。

    Args:
        frame: 帧快照。
        max_points: 点云抽样上限。
        max_width: 深度图输出宽度上限。

    Returns:
        ``(payload, media_type)``；无法渲染时 payload 为空串（由路由转 503）。
    """
    if frame.stream == STREAM_RGB:
        content_type = str(frame.meta.get("content_type") or "image/jpeg")
        return frame.payload, content_type
    if frame.stream == STREAM_DEPTH:
        png = depth_png(frame, max_width=max_width)
        return (png, "image/png") if png else (b"", "image/png")
    if frame.stream == STREAM_LIDAR:
        import json  # noqa: PLC0415  仅该分支需要

        return (
            json.dumps(lidar_points(frame, max_points=max_points), ensure_ascii=False).encode(
                "utf-8"
            ),
            "application/json",
        )
    if frame.stream == STREAM_IMU:
        import json  # noqa: PLC0415  仅该分支需要

        return (
            json.dumps(imu_values(frame), ensure_ascii=False).encode("utf-8"),
            "application/json",
        )
    return b"", "application/octet-stream"


__all__ = [
    "depth_png",
    "frame_summary",
    "imu_values",
    "lidar_points",
    "render_stream",
]

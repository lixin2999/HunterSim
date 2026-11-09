"""传感器图像编码（V3.0.0）。

CARLA 相机回调给出的是 ``Image.raw_data``（BGRA 打包字节），MJPEG 推流与截图
端点需要可直接放进 HTTP 响应的图像字节。

编码策略：
- 装了 Pillow 时走 JPEG（``screenshot_quality`` 生效，带宽最优）；
- 未装 Pillow 时走纯 Python PNG（zlib + struct，无新增依赖，带宽更大但可用）。

深度图不参与图像编码：按 CARLA 官方公式还原为米制 float32，供 mcap 以
``sensor_msgs/Image(encoding=32FC1)`` 落盘。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Optional

import numpy as np
from numpy.typing import NDArray

from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 图像数组类型别名（避免各签名重复写泛型参数）
RgbImage = NDArray[np.uint8]
DepthImage = NDArray[np.float32]

# CARLA 深度图满量程（米）：BGRA 编码值按 1000m 归一化
_DEPTH_FULL_SCALE_M = 1000.0
# BGRA 三通道可表示的最大整数值
_DEPTH_MAX_CODE = 256 ** 3 - 1

CONTENT_TYPE_JPEG = "image/jpeg"
CONTENT_TYPE_PNG = "image/png"


@dataclass(frozen=True)
class EncodedImage:
    """编码后的图像帧。

    Attributes:
        payload: 图像字节（JPEG 或 PNG）。
        content_type: MIME 类型。
        width: 编码后的宽度（降采样后）。
        height: 编码后的高度。
        format: 编码格式标识（``jpeg`` / ``png``）。
    """

    payload: bytes
    content_type: str
    width: int
    height: int
    format: str

    @property
    def size(self) -> int:
        """payload 字节数。"""
        return len(self.payload)


def bgra_to_rgb(pixels: bytes, width: int, height: int) -> RgbImage:
    """把 CARLA BGRA 字节还原为 ``(H, W, 3)`` RGB 数组。

    Args:
        pixels: BGRA 排列的原始字节（长度 = width*height*4）。
        width: 图像宽度。
        height: 图像高度。

    Returns:
        ``uint8`` RGB 数组。

    Raises:
        ValueError: 字节长度与尺寸不匹配。
    """
    expected = width * height * 4
    if len(pixels) != expected:
        raise ValueError(
            f"bgra buffer size mismatch: expect {expected}, got {len(pixels)}"
        )
    array = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
    return array[:, :, [2, 1, 0]]


def depth_bgra_to_meters(pixels: bytes, width: int, height: int) -> DepthImage:
    """把 CARLA 深度图 BGRA 字节还原为米制 ``float32`` 数组。

    公式（CARLA 官方）：``depth = 1000 * (R + G*256 + B*65536) / (256^3 - 1)``。

    Args:
        pixels: BGRA 排列的原始字节。
        width: 图像宽度。
        height: 图像高度。

    Returns:
        ``float32`` 深度数组（单位米）。
    """
    expected = width * height * 4
    if len(pixels) != expected:
        raise ValueError(
            f"depth buffer size mismatch: expect {expected}, got {len(pixels)}"
        )
    array = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
    codes: NDArray[np.uint32] = (
        array[:, :, 2].astype(np.uint32)
        + array[:, :, 1].astype(np.uint32) * 256
        + array[:, :, 0].astype(np.uint32) * 65536
    )
    return (codes.astype(np.float32) / _DEPTH_MAX_CODE * _DEPTH_FULL_SCALE_M).astype(
        np.float32
    )


def _downscale(rgb: RgbImage, max_width: int) -> RgbImage:
    """按最大宽度做整数倍最近邻降采样（0 表示不限制）。"""
    if max_width <= 0 or rgb.shape[1] <= max_width:
        return rgb
    step = max(1, rgb.shape[1] // max_width)
    return rgb[::step, ::step]


def encode_png(rgb: RgbImage) -> bytes:
    """把 RGB 数组编码为 PNG 字节（纯 Python，无第三方依赖）。

    Args:
        rgb: ``(H, W, 3)`` uint8 数组。

    Returns:
        PNG 字节串。
    """
    height, width = rgb.shape[0], rgb.shape[1]
    # PNG 每行前置 1 字节 filter type（0 = None）
    filtered = np.zeros((height, width * 3 + 1), dtype=np.uint8)
    filtered[:, 1:] = rgb.reshape(height, width * 3)
    compressed = zlib.compress(filtered.tobytes(), 6)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def encode_jpeg(rgb: RgbImage, quality: int = 75) -> Optional[bytes]:
    """用 Pillow 编码 JPEG（未安装 Pillow 时返回 None）。

    Args:
        rgb: ``(H, W, 3)`` uint8 数组。
        quality: JPEG 质量（1-95）。

    Returns:
        JPEG 字节串；Pillow 不可用时为 None。
    """
    try:
        from PIL import Image  # noqa: PLC0415  可选依赖，缺失时降级 PNG
    except ImportError:
        return None
    from io import BytesIO  # noqa: PLC0415

    buffer = BytesIO()
    # 不传 mode：Pillow 13 已移除该参数，(H, W, 3) uint8 会自动推断为 RGB
    Image.fromarray(rgb).save(
        buffer, format="JPEG", quality=max(1, min(95, quality))
    )
    return buffer.getvalue()


def encode_bgra_jpeg(
    pixels: bytes,
    width: int,
    height: int,
    quality: int = 75,
) -> Optional[bytes]:
    """把 CARLA 的 BGRA 字节**直接**编码为 JPEG（不经过 numpy，主视区快路径）。

    不降采样的帧如果走 ``bgra_to_rgb`` → ``Image.fromarray``，会在 Python 侧
    多出两次整帧拷贝（1080p 每帧约 6 MB）；而主视区需要 10 Hz 持续编码，这两次
    拷贝会成为 CPU/内存带宽热点，表现为画面卡顿。这里用 ``Image.frombuffer``
    零拷贝读原缓冲区，``convert("RGB")`` 在 C 层丢 alpha。

    Args:
        pixels: BGRA 排列的原始字节（长度 = width*height*4）。
        width: 图像宽度。
        height: 图像高度。
        quality: JPEG 质量（1-95）。

    Returns:
        JPEG 字节串；Pillow 不可用或尺寸不匹配时返回 None（调用方回退慢路径）。
    """
    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        return None
    try:
        from PIL import Image  # noqa: PLC0415  可选依赖，缺失时降级 PNG
    except ImportError:
        return None
    from io import BytesIO  # noqa: PLC0415

    buffer = BytesIO()
    try:
        image = Image.frombuffer(
            "RGBA", (width, height), pixels, "raw", "BGRA", 0, 1
        ).convert("RGB")
        image.save(buffer, format="JPEG", quality=max(1, min(95, quality)))
    except Exception as exc:  # noqa: BLE001  Pillow 版本差异时回退通用路径
        logger.debug(f"BGRA direct JPEG encode failed: {exc}")
        return None
    return buffer.getvalue()


def encode_rgb_frame(
    pixels: bytes,
    width: int,
    height: int,
    *,
    quality: int = 75,
    max_width: int = 0,
) -> EncodedImage:
    """把 CARLA RGB 相机帧编码为可直传 HTTP 的图像。

    Args:
        pixels: BGRA 原始字节。
        width: 图像宽度。
        height: 图像高度。
        quality: JPEG 质量（仅 Pillow 可用时生效）。
        max_width: 最大输出宽度（0 表示不降采样）。

    Returns:
        编码结果（JPEG 优先，PNG 兜底）。
    """
    if max_width <= 0 or width <= max_width:
        # 不降采样（主视区）：直接从 BGRA 缓冲区编码，省掉 numpy 侧两次整帧拷贝
        direct = encode_bgra_jpeg(pixels, width, height, quality)
        if direct is not None:
            return EncodedImage(direct, CONTENT_TYPE_JPEG, width, height, "jpeg")
    rgb = _downscale(bgra_to_rgb(pixels, width, height), max_width)
    out_h, out_w = rgb.shape[0], rgb.shape[1]
    jpeg = encode_jpeg(rgb, quality)
    if jpeg is not None:
        return EncodedImage(jpeg, CONTENT_TYPE_JPEG, out_w, out_h, "jpeg")
    return EncodedImage(encode_png(rgb), CONTENT_TYPE_PNG, out_w, out_h, "png")


__all__ = [
    "CONTENT_TYPE_JPEG",
    "CONTENT_TYPE_PNG",
    "DepthImage",
    "EncodedImage",
    "RgbImage",
    "bgra_to_rgb",
    "depth_bgra_to_meters",
    "encode_jpeg",
    "encode_bgra_jpeg",
    "encode_png",
    "encode_rgb_frame",
]

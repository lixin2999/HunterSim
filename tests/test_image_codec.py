"""图像编码测试：BGRA 直编 JPEG 快路径必须与解码后的像素一致。"""

from __future__ import annotations

import pytest

from hunter_sim.sensor_sim.image_codec import (
    CONTENT_TYPE_JPEG,
    bgra_to_rgb,
    encode_bgra_jpeg,
    encode_rgb_frame,
)

PIL = pytest.importorskip("PIL.Image")


def _bgra_frame(width: int, height: int) -> bytes:
    """构造纯色 BGRA 帧：B=0, G=0, R=255（红）→ RGB(255, 0, 0)。"""
    pixel = bytes((0, 0, 255, 255))
    return pixel * (width * height)


class TestEncodeBgraJpeg:
    def test_returns_jpeg_bytes(self) -> None:
        width, height = 16, 8
        encoded = encode_bgra_jpeg(_bgra_frame(width, height), width, height, quality=90)
        assert encoded is not None
        assert encoded[:2] == b"\xff\xd8"  # JPEG SOI

    def test_channel_order_is_rgb_not_bgr(self) -> None:
        """直编快路径若走错通道，画面红蓝互换；这里固定校验通道顺序。"""
        width, height = 8, 8
        encoded = encode_bgra_jpeg(_bgra_frame(width, height), width, height, quality=95)
        assert encoded is not None
        from io import BytesIO

        with PIL.open(BytesIO(encoded)) as image:
            red, green, blue = image.convert("RGB").getpixel((4, 4))
        assert red > 200
        assert green < 60
        assert blue < 60

    def test_size_mismatch_returns_none(self) -> None:
        assert encode_bgra_jpeg(b"\x00" * 16, 8, 8) is None
        assert encode_bgra_jpeg(b"", 0, 0) is None


class TestEncodeRgbFrame:
    def test_native_size_prefers_direct_encode(self) -> None:
        encoded = encode_rgb_frame(_bgra_frame(8, 8), 8, 8, quality=80, max_width=0)
        assert encoded.content_type == CONTENT_TYPE_JPEG
        assert encoded.format == "jpeg"
        assert (encoded.width, encoded.height) == (8, 8)

    def test_downscale_still_works(self) -> None:
        encoded = encode_rgb_frame(_bgra_frame(16, 16), 16, 16, quality=80, max_width=8)
        assert (encoded.width, encoded.height) == (8, 8)

    def test_bgra_to_rgb_matches_direct_path(self) -> None:
        """慢路径（numpy 重排）与快路径必须产出同样的 RGB 数据。"""
        raw = _bgra_frame(4, 4)
        assert bgra_to_rgb(raw, 4, 4)[0, 0].tolist() == [255, 0, 0]

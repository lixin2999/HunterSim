"""CARLA 相机帧 → RGB 数组转换（V2.2.0，无 Qt 依赖）。

CARLA `sensor.camera.*` 回调给出 BGRA 原始字节（自底向上行序）。本模块把
`carla.Image` 转成 RGB `numpy.ndarray`（H×W×3，uint8，已翻正为自顶向下），
供 Qt 侧封装为 `QImage` 显示。纯 numpy，可脱离界面与引擎单测。
"""

from __future__ import annotations

from typing import Any

import numpy as np


def image_to_rgb_array(carla_image: Any) -> "np.ndarray[Any, np.dtype[np.uint8]]":
    """把 `carla.Image` 转为 RGB 数组。

    Args:
        carla_image: CARLA 图像传感器回调对象（含 width/height 与原始字节缓冲）。

    Returns:
        shape=(height, width, 3)、dtype=uint8 的 RGB 数组（自顶向下行序）。
    """
    width = int(carla_image.width)
    height = int(carla_image.height)
    # 官方 CARLA PythonAPI 的 carla.Image 暴露 raw_data（部分旧版/桩以 .data 提供），
    # 两者兼容取数，真机不会因属性名不存在而 AttributeError。
    raw = getattr(carla_image, "raw_data", None)
    if raw is None:
        raw = carla_image.data
    buffer = np.frombuffer(raw, dtype=np.uint8)
    bgra = buffer.reshape((height, width, 4))
    # BGRA → RGB：交换 R/B 通道（QImage Format_RGB888 期望 R,G,B 顺序）
    rgb = bgra[:, :, [2, 1, 0]]
    # CARLA 图像行序自底向上，翻正为自顶向下供 QImage 使用
    return np.ascontiguousarray(rgb[::-1, :, :])


def rgb_array_size(arr: "np.ndarray[Any, np.dtype[np.uint8]]") -> tuple[int, int]:
    """返回 (width, height)，供 QImage 构造校验。"""
    height, width = arr.shape[0], arr.shape[1]
    return width, height

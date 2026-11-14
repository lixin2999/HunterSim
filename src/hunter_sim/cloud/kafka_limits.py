"""Kafka 记录尺寸口径（V3.9.2）：producer 侧显式对齐 broker 的 ``message.max.bytes``。

kafka-python 的 ``max_request_size`` 默认 1 MiB，broker ``message.max.bytes`` 默认
也是 1 MiB —— 两个默认值"碰巧"一致。一旦仿真侧自己放宽单帧上限（例如
``EdgeLinkSettings.max_payload_bytes`` 默认 4 MiB，base64 后约 5.6 MB），就会在
``KafkaProducer.send()`` 处**同步**抛 ``MessageSizeTooLargeError``：

- 异常栈直接冒到日志/面板，且不带 stream / 尺寸上下文（排查时无法定位来源）；
- 该异常发生在各 producer 自己的 ``send_errors`` 计数之前，造成「上行错误 0，
  但面板报超大记录」的假象（V3.9.1 现场即如此）。

本模块把口径收敛到一处：

- ``MAX_RECORD_BYTES``：单条记录（**序列化后**字节）上限，对齐 Kafka 默认
  ``message.max.bytes``（1 MiB）；
- ``MAX_REQUEST_OVERHEAD_BYTES``：``max_request_size`` 相对记录上限的协议头/
  批次开销预留；
- ``MAX_REQUEST_SIZE``：各 producer 传给 kafka-python 的 ``max_request_size``。

现场要抬高交付上限必须**同时**改两处：broker ``message.max.bytes``（含 topic
级覆盖）与本侧记录上限；只改一侧仍会在发送端或 broker 侧失败。
"""

from __future__ import annotations

#: 单条记录（序列化后字节）上限：对齐 Kafka 默认 ``message.max.bytes``（1 MiB）。
MAX_RECORD_BYTES = 1_048_576

#: ``max_request_size`` 相对记录上限的预留（Kafka 协议头/批次开销）。
MAX_REQUEST_OVERHEAD_BYTES = 65_536

#: producer ``max_request_size`` 取值（须 ≥ broker ``message.max.bytes``）。
MAX_REQUEST_SIZE = MAX_RECORD_BYTES + MAX_REQUEST_OVERHEAD_BYTES

__all__ = ["MAX_RECORD_BYTES", "MAX_REQUEST_OVERHEAD_BYTES", "MAX_REQUEST_SIZE"]

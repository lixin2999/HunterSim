"""HunterSim 统一异常体系。

层次结构（见 HunterSim AI 编码规则 §4.1）::

    HunterSimError
      ├── ConfigurationError
      ├── ConnectionError
      │     ├── CarlaConnectionError
      │     ├── KafkaConnectionError
      │     └── ROS2ConnectionError
      ├── ValidationError
      ├── SimulationError
      │     ├── CarlaSimulationError
      │     └── SensorSimulationError
      ├── ResourceError
      └── TimeoutError
"""

from __future__ import annotations


class HunterSimError(Exception):
    """所有 HunterSim 自定义异常的基类。"""


class ConfigurationError(HunterSimError):
    """配置加载、解析或校验失败。"""


class ConnectionError(HunterSimError):  # 故意与内建同名，贴合领域语义
    """外部服务连接建立或维持失败。"""


class CarlaConnectionError(ConnectionError):
    """与 CARLA 服务端连接失败。"""


class KafkaConnectionError(ConnectionError):
    """与 Kafka Broker 连接失败。"""


class ROS2ConnectionError(ConnectionError):
    """与 ROS2 运行时连接失败。"""


class ValidationError(HunterSimError):
    """数据对象或接口输入校验失败。"""


class SimulationError(HunterSimError):
    """仿真运行期错误。"""


class CarlaSimulationError(SimulationError):
    """CARLA 引擎调用或步进错误。"""


class SensorSimulationError(SimulationError):
    """传感器仿真、回调或数据错误。"""


class ResourceError(HunterSimError):
    """资源创建、销毁或管理失败。"""


class BufferClosedError(HunterSimError):
    """向已关闭的缓冲读写数据。"""


class DataWriteError(HunterSimError):
    """数据落盘失败（序列化 / IO / 重试耗尽）。"""


class ConversionError(HunterSimError):
    """原始传感器数据向标准契约帧转换失败。"""


class DataReplayError(HunterSimError):
    """数据回放失败（运行目录 / 帧文件缺失或格式不可读）。"""


__all__ = [
    "BufferClosedError",
    "CarlaConnectionError",
    "CarlaSimulationError",
    "ConfigurationError",
    "ConnectionError",
    "ConversionError",
    "DataReplayError",
    "DataWriteError",
    "HunterSimError",
    "KafkaConnectionError",
    "ROS2ConnectionError",
    "ResourceError",
    "SensorSimulationError",
    "SimulationError",
    "ValidationError",
]

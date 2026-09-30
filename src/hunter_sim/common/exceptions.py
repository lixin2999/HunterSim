"""HunterSim 公共异常层次定义。

所有模块的自定义异常必须继承 :class:`HunterSimError`。
禁止在业务代码中使用裸 ``except:`` 或抛出基础 Python 异常。
"""

from __future__ import annotations


class HunterSimError(Exception):
    """HunterSim 系统异常基类。

    Args:
        module: 产生异常的模块名称。
        operation: 触发异常的操作描述。
        message: 异常详细说明。
    """

    def __init__(self, module: str, operation: str, message: str) -> None:
        self.module = module
        self.operation = operation
        self.message = message
        super().__init__(f"[{module}][{operation}] {message}")


class ConfigurationError(HunterSimError):
    """配置参数错误，包括缺失必填项、格式不合法、范围越界等。"""

    def __init__(self, operation: str, message: str, module: str = "config") -> None:
        super().__init__(module, operation, message)


class ConnectionError(HunterSimError):
    """外部服务连接失败基类。"""

    def __init__(self, module: str, operation: str, message: str) -> None:
        super().__init__(module, operation, message)


class CarlaConnectionError(ConnectionError):
    """CARLA RPC 连接失败或超时。

    Args:
        host: CARLA 服务器地址。
        port: RPC 端口号。
        retry_count: 已重试次数。
    """

    def __init__(self, host: str, port: int, message: str, retry_count: int = 0) -> None:
        self.host = host
        self.port = port
        self.retry_count = retry_count
        super().__init__(
            "carla_engine",
            f"connect({host}:{port})",
            f"{message} (retry={retry_count})",
        )


class KafkaConnectionError(ConnectionError):
    """Kafka 集群连接失败。"""

    def __init__(self, bootstrap_servers: str, message: str) -> None:
        super().__init__(
            "vil_mapper",
            f"kafka_connect({bootstrap_servers})",
            message,
        )


class ROS2ConnectionError(ConnectionError):
    """ROS2 节点发现或通信失败。"""

    def __init__(self, node_name: str, message: str) -> None:
        super().__init__(
            "sensor_sim",
            f"ros2_node({node_name})",
            message,
        )


class ValidationError(HunterSimError):
    """数据模型校验失败。"""

    def __init__(self, field: str, value: object, rule: str, module: str = "validator") -> None:
        super().__init__(
            module,
            f"validate({field})",
            f"Value '{value}' violates rule: {rule}",
        )


class SimulationError(HunterSimError):
    """仿真运行期间发生的错误基类。"""

    def __init__(self, module: str, operation: str, message: str) -> None:
        super().__init__(module, operation, message)


class CarlaSimulationError(SimulationError):
    """CARLA 世界操作失败（地图加载、Actor 生成、tick 超时等）。"""

    def __init__(self, operation: str, message: str, world_snapshot: str = "") -> None:
        super().__init__("carla_engine", operation, f"{message} | snapshot={world_snapshot[:100]}")


class SensorSimulationError(SimulationError):
    """传感器仿真异常（数据回调失败、缓冲区溢出等）。"""

    def __init__(self, sensor_type: str, sensor_id: str, message: str) -> None:
        super().__init__(
            "sensor_sim",
            f"sensor({sensor_type}:{sensor_id})",
            message,
        )


class ResourceError(HunterSimError):
    """仿真资源管理错误（GPU 不足、实例配额耗尽、容器启动失败等）。"""

    def __init__(self, resource_type: str, message: str) -> None:
        super().__init__("resource_manager", f"resource({resource_type})", message)


class SimTimeoutError(HunterSimError):
    """操作超时（CARLA tick、Kafka 消费、WebSocket 响应等）。"""

    def __init__(self, operation: str, timeout_seconds: float, module: str = "runtime") -> None:
        super().__init__(
            module,
            operation,
            f"Operation timed out after {timeout_seconds}s",
        )


class InstanceStateError(HunterSimError):
    """仿真实例状态机非法转换。"""

    def __init__(self, instance_id: str, current_state: str, target_state: str) -> None:
        super().__init__(
            "resource_manager",
            f"state_machine({instance_id})",
            f"Cannot transition from '{current_state}' to '{target_state}'",
        )

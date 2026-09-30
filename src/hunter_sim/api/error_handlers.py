"""统一异常处理（PROMPT-API-001）。

将 HunterSim 各层异常映射为统一 JSON 错误响应：
{"code": 错误码, "message": "描述", "data": null}
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from hunter_sim.common.exceptions import (
    CarlaConnectionError,
    CarlaSimulationError,
    ConfigurationError,
    HunterSimError,
    InstanceStateError,
    KafkaConnectionError,
    ResourceError,
    ROS2ConnectionError,
    SensorSimulationError,
    SimTimeoutError,
    SimulationError,
    ValidationError,
)
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


# 错误码定义
_ERR_CODES: dict[str, int] = {
    "configuration": 1001,
    "connection": 1002,
    "carla_connection": 1003,
    "kafka_connection": 1004,
    "ros2_connection": 1005,
    "validation": 1006,
    "simulation": 1007,
    "carla_simulation": 1008,
    "sensor_simulation": 1009,
    "resource": 1010,
    "timeout": 1011,
    "instance_state": 1012,
    "request_validation": 1013,
    "not_found": 1014,
    "forbidden": 1015,
    "unknown": 9999,
}


def _err_response(code: int, message: str, http_status: int) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=http_status,
        content={"code": code, "message": message, "data": None},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册所有异常处理器到 FastAPI 应用。

    Args:
        app: FastAPI 应用实例。
    """

    @app.exception_handler(HunterSimError)
    async def hunter_sim_error_handler(_: Request, exc: HunterSimError) -> ORJSONResponse:
        """处理所有 HunterSim 自定义异常。"""
        exc_name = type(exc).__name__
        logger.error(f"[{exc_name}] {exc}")

        if isinstance(exc, CarlaConnectionError):
            return _err_response(_ERR_CODES["carla_connection"], str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(exc, KafkaConnectionError):
            return _err_response(_ERR_CODES["kafka_connection"], str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(exc, ROS2ConnectionError):
            return _err_response(_ERR_CODES["ros2_connection"], str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(exc, ConfigurationError):
            return _err_response(_ERR_CODES["configuration"], str(exc), status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, ValidationError):
            return _err_response(_ERR_CODES["validation"], str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
        if isinstance(exc, InstanceStateError):
            return _err_response(_ERR_CODES["instance_state"], str(exc), status.HTTP_409_CONFLICT)
        if isinstance(exc, ResourceError):
            return _err_response(_ERR_CODES["resource"], str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(exc, SimTimeoutError):
            return _err_response(_ERR_CODES["timeout"], str(exc), status.HTTP_504_GATEWAY_TIMEOUT)
        if isinstance(exc, SensorSimulationError):
            return _err_response(_ERR_CODES["sensor_simulation"], str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
        if isinstance(exc, CarlaSimulationError):
            return _err_response(_ERR_CODES["carla_simulation"], str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
        if isinstance(exc, SimulationError):
            return _err_response(_ERR_CODES["simulation"], str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)

        return _err_response(_ERR_CODES["unknown"], str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        """处理 FastAPI 请求体验证错误。"""
        errors = exc.errors()
        first_msg = errors[0].get("msg", "Validation error") if errors else "Validation error"
        logger.warning(f"RequestValidationError: {errors}")
        return _err_response(
            _ERR_CODES["request_validation"],
            f"Request validation failed: {first_msg}",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> ORJSONResponse:
        """处理 HTTPException（401/403/404 等）。"""
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return _err_response(_ERR_CODES["not_found"], str(exc.detail), exc.status_code)
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return _err_response(_ERR_CODES["forbidden"], str(exc.detail), exc.status_code)
        return ORJSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": str(exc.detail), "data": None},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> ORJSONResponse:
        """处理所有未捕获异常，防止 500 错误堆栈泄露。"""
        logger.exception(f"Unhandled exception: {exc}")
        return _err_response(
            _ERR_CODES["unknown"],
            "Internal server error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    logger.info("Exception handlers registered")

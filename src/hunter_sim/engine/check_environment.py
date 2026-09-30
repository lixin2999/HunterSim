"""CARLA 运行环境验证工具（PROMPT-ENG-001-A 配套）。

检查 GPU 驱动版本、CUDA 版本、可用显存，验证 CARLA 服务端运行环境是否满足要求。
仅在 Windows 主机（运行 CARLA 的机器）上执行。
"""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 最低驱动版本要求（NVIDIA Windows 驱动）
_MIN_DRIVER_VERSION: tuple[int, int, int, int] = (510, 0, 0, 0)
# 最低 CUDA 版本
_MIN_CUDA_VERSION: tuple[int, int] = (11, 0)
# 最低显存要求（GB）
_MIN_GPU_MEMORY_GB: float = 4.0


@dataclass
class EnvironmentCheckResult:
    """环境检查结果。

    Attributes:
        os_ok: 操作系统是否为 Windows 10/11。
        gpu_driver_version: 检测到的 GPU 驱动版本字符串。
        gpu_driver_ok: 驱动版本是否满足最低要求。
        cuda_version: 检测到的 CUDA 版本字符串。
        cuda_ok: CUDA 版本是否满足最低要求。
        gpu_memory_gb: GPU 显存大小（GB）。
        gpu_memory_ok: 显存是否满足最低要求。
        gpu_name: GPU 型号名称。
        python_version: Python 版本字符串。
        python_ok: Python 版本是否 >= 3.12。
        passed: 总体是否通过所有检查。
        errors: 失败项说明列表。
    """

    os_ok: bool
    gpu_driver_version: Optional[str]
    gpu_driver_ok: bool
    cuda_version: Optional[str]
    cuda_ok: bool
    gpu_memory_gb: float
    gpu_memory_ok: bool
    gpu_name: Optional[str]
    python_version: str
    python_ok: bool
    passed: bool
    errors: list[str]


def _parse_version(ver_str: str) -> tuple[int, ...]:
    """将版本字符串解析为整数元组。"""
    parts = re.findall(r"\d+", ver_str)
    return tuple(int(p) for p in parts)


def _check_nvidia_smi() -> tuple[Optional[str], Optional[str], float, Optional[str]]:
    """通过 nvidia-smi 获取 GPU 驱动版本、CUDA 版本、显存和型号。

    Returns:
        (driver_version, cuda_version, memory_gb, gpu_name) 元组，查询失败时各项为 None/0.0。
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version,memory.total,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning("nvidia-smi returned non-zero exit code")
            return None, None, 0.0, None

        lines = result.stdout.strip().splitlines()
        if not lines:
            return None, None, 0.0, None

        # 取第一块 GPU
        parts = [p.strip() for p in lines[0].split(",")]
        driver_version: Optional[str] = parts[0] if len(parts) >= 1 else None
        memory_mb: float = float(parts[1]) if len(parts) >= 2 else 0.0
        gpu_name: Optional[str] = parts[2] if len(parts) >= 3 else None

        # 获取 CUDA 版本
        cuda_result = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        cuda_version: Optional[str] = cuda_result.stdout.strip().splitlines()[0] if cuda_result.returncode == 0 and cuda_result.stdout.strip() else None

        return driver_version, cuda_version, memory_mb / 1024.0, gpu_name

    except FileNotFoundError:
        logger.warning("nvidia-smi not found in PATH")
        return None, None, 0.0, None
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi query timed out")
        return None, None, 0.0, None
    except (ValueError, IndexError) as exc:
        logger.warning(f"Failed to parse nvidia-smi output: {exc}")
        return None, None, 0.0, None


def check_environment() -> EnvironmentCheckResult:
    """执行完整环境检查。

    检查项目：
    - 操作系统是否为 Windows 10/11（CARLA 必须运行在 Windows 上）
    - NVIDIA GPU 驱动版本 >= 510
    - CUDA 版本 >= 11.0
    - GPU 显存 >= 4GB
    - Python 版本 >= 3.12

    Returns:
        EnvironmentCheckResult 包含所有检查项结果。
    """
    errors: list[str] = []

    # 操作系统检查
    os_name = platform.system()
    os_ok = os_name == "Windows"
    if not os_ok:
        errors.append(f"CARLA must run on Windows, detected: {os_name}")

    # Python 版本检查
    python_version = platform.python_version()
    py_ver_tuple = _parse_version(python_version)
    python_ok = py_ver_tuple >= (3, 12, 0)
    if not python_ok:
        errors.append(f"Python >= 3.12 required, found: {python_version}")

    # GPU 检查
    driver_version, cuda_version, gpu_memory_gb, gpu_name = _check_nvidia_smi()

    gpu_driver_ok = True
    if driver_version:
        drv_tuple = _parse_version(driver_version)
        gpu_driver_ok = drv_tuple >= _MIN_DRIVER_VERSION[:len(drv_tuple)]
        if not gpu_driver_ok:
            errors.append(
                f"NVIDIA driver {driver_version} < required {_MIN_DRIVER_VERSION[0]}. "
                "Please update GPU drivers."
            )
    else:
        errors.append("Could not detect NVIDIA GPU driver version")
        gpu_driver_ok = False

    cuda_ok = True
    if cuda_version:
        cuda_tuple = _parse_version(cuda_version)
        cuda_ok = cuda_tuple >= _MIN_CUDA_VERSION[:len(cuda_tuple)]
        if not cuda_ok:
            errors.append(f"CUDA {cuda_version} < required {_MIN_CUDA_VERSION[0]}.{_MIN_CUDA_VERSION[1]}")
    else:
        errors.append("Could not detect CUDA version")
        cuda_ok = False

    gpu_memory_ok = gpu_memory_gb >= _MIN_GPU_MEMORY_GB
    if not gpu_memory_ok:
        errors.append(f"GPU memory {gpu_memory_gb:.1f}GB < minimum {_MIN_GPU_MEMORY_GB}GB required")

    passed = os_ok and python_ok and gpu_driver_ok and cuda_ok and gpu_memory_ok

    return EnvironmentCheckResult(
        os_ok=os_ok,
        gpu_driver_version=driver_version,
        gpu_driver_ok=gpu_driver_ok,
        cuda_version=cuda_version,
        cuda_ok=cuda_ok,
        gpu_memory_gb=gpu_memory_gb,
        gpu_memory_ok=gpu_memory_ok,
        gpu_name=gpu_name,
        python_version=python_version,
        python_ok=python_ok,
        passed=passed,
        errors=errors,
    )


def raise_if_environment_invalid() -> None:
    """环境检查失败时抛出 ConfigurationError。

    Raises:
        ConfigurationError: 任一检查项未通过时抛出。
    """
    result = check_environment()
    if not result.passed:
        raise ConfigurationError(
            operation="check_environment",
            message="Environment check failed: " + "; ".join(result.errors),
        )

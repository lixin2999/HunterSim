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
# nvidia-smi 表头中的 CUDA 版本字段，形如 "CUDA Version: 13.0"
_CUDA_VERSION_PATTERN = re.compile(r"CUDA Version:\s*(\d+(?:\.\d+)*)")


@dataclass
class EnvironmentCheckResult:
    """环境检查结果。

    Attributes:
        os_ok: 操作系统是否为 Windows 10/11。
        gpu_driver_version: 检测到的 GPU 驱动版本字符串。
        gpu_driver_ok: 驱动版本是否满足最低要求。
        cuda_version: 检测到的 CUDA 版本字符串（驱动最高支持版本）。
        cuda_ok: CUDA 版本是否满足最低要求。
        gpu_memory_gb: GPU 显存大小（GB）。
        gpu_memory_ok: 显存是否满足最低要求。
        gpu_name: GPU 型号名称。
        python_version: Python 版本字符串。
        python_ok: Python 版本是否 >= 3.12。
        passed: 总体是否通过所有检查。
        errors: 失败项说明列表。
        gpu_compute_capability: GPU 计算能力（如 8.9），仅用于展示，不参与版本判定。
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
    gpu_compute_capability: Optional[str] = None


@dataclass(frozen=True)
class _GpuProbe:
    """nvidia-smi 探测结果，查询失败的字段保持 None/0.0。"""

    driver_version: Optional[str] = None
    cuda_version: Optional[str] = None
    compute_capability: Optional[str] = None
    memory_gb: float = 0.0
    gpu_name: Optional[str] = None


def _parse_version(ver_str: str) -> tuple[int, ...]:
    """将版本字符串解析为整数元组。"""
    parts = re.findall(r"\d+", ver_str)
    return tuple(int(p) for p in parts)


def _run_nvidia_smi(args: list[str], timeout: int) -> Optional[str]:
    """执行 nvidia-smi，返回 stdout；非零退出码返回 None。"""
    completed = subprocess.run(
        ["nvidia-smi", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout if completed.returncode == 0 else None


def _parse_cuda_version(smi_output: str) -> Optional[str]:
    """从 nvidia-smi 表头解析驱动支持的 CUDA 版本，形如 ``CUDA Version: 13.0``。"""
    match = _CUDA_VERSION_PATTERN.search(smi_output)
    return match.group(1) if match else None


def _probe_gpu() -> _GpuProbe:
    """通过 nvidia-smi 获取驱动版本、CUDA 版本、计算能力、显存和型号。

    CUDA 版本取自 nvidia-smi 表头（驱动最高支持版本）；``compute_cap`` 是 GPU 计算能力
    （Ada 架构 RTX 40 系为 8.9），与 CUDA 版本无关，不得混用同一字段判定。

    Returns:
        _GpuProbe，查询失败的字段为 None/0.0。
    """
    try:
        # 旧驱动可能不认识 compute_cap 列，整体查询失败时退化为基础字段
        stdout = _run_nvidia_smi(
            [
                "--query-gpu=driver_version,memory.total,name,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            timeout=10,
        )
        if stdout is None:
            stdout = _run_nvidia_smi(
                [
                    "--query-gpu=driver_version,memory.total,name",
                    "--format=csv,noheader,nounits",
                ],
                timeout=10,
            )
        if stdout is None:
            logger.warning("nvidia-smi returned non-zero exit code")
            return _GpuProbe()

        lines = stdout.strip().splitlines()
        if not lines:
            return _GpuProbe()

        # 取第一块 GPU
        parts = [p.strip() for p in lines[0].split(",")]
        driver_version: Optional[str] = parts[0] or None
        memory_mb: float = float(parts[1]) if len(parts) >= 2 and parts[1] else 0.0
        gpu_name: Optional[str] = parts[2] if len(parts) >= 3 and parts[2] else None
        compute_capability: Optional[str] = parts[3] if len(parts) >= 4 and parts[3] else None

        # CUDA 版本只在无参数的 nvidia-smi 表头里出现
        header = _run_nvidia_smi([], timeout=5) or ""

        return _GpuProbe(
            driver_version=driver_version,
            cuda_version=_parse_cuda_version(header),
            compute_capability=compute_capability,
            memory_gb=memory_mb / 1024.0,
            gpu_name=gpu_name,
        )

    except FileNotFoundError:
        logger.warning("nvidia-smi not found in PATH")
        return _GpuProbe()
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi query timed out")
        return _GpuProbe()
    except (ValueError, IndexError) as exc:
        logger.warning(f"Failed to parse nvidia-smi output: {exc}")
        return _GpuProbe()


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
    probe = _probe_gpu()
    driver_version = probe.driver_version
    gpu_memory_gb = probe.memory_gb
    gpu_name = probe.gpu_name

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
    if probe.cuda_version:
        cuda_tuple = _parse_version(probe.cuda_version)
        cuda_ok = cuda_tuple >= _MIN_CUDA_VERSION[:len(cuda_tuple)]
        if not cuda_ok:
            errors.append(f"CUDA {probe.cuda_version} < required {_MIN_CUDA_VERSION[0]}.{_MIN_CUDA_VERSION[1]}")
    elif probe.compute_capability:
        # 仅拿到计算能力（如 8.9）：能枚举出 GPU 即说明驱动自带 CUDA 运行时，
        # 不能把计算能力与 CUDA 版本比较，否则 RTX 40 系会被误判为 CUDA < 11
        logger.debug("CUDA version unavailable, compute capability=%s", probe.compute_capability)
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
        cuda_version=probe.cuda_version,
        cuda_ok=cuda_ok,
        gpu_memory_gb=gpu_memory_gb,
        gpu_memory_ok=gpu_memory_ok,
        gpu_name=gpu_name,
        python_version=python_version,
        python_ok=python_ok,
        passed=passed,
        errors=errors,
        gpu_compute_capability=probe.compute_capability,
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

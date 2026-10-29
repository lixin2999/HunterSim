"""CARLA 运行环境校验测试。

重点覆盖 CUDA 版本口径：``nvidia-smi --query-gpu=compute_cap`` 返回的是 GPU 计算能力
（Ada 架构 RTX 40 系为 8.9），而 CUDA 版本只出现在无参数 ``nvidia-smi`` 的表头里。
两者混用会把 RTX 4070 误判成「CUDA 8.9 < 11.0」并给出错误的安装 CUDA 提示。
全部 nvidia-smi 调用打桩，不依赖真实 GPU。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

# hunter_sim.engine 包内的 check_environment 名称被导出函数占用，直接导入具体符号
from hunter_sim.engine.check_environment import (
    _GpuProbe,
    _parse_cuda_version,
    _probe_gpu,
    check_environment,
)

_QUERY_WITH_CAP = (
    "--query-gpu=driver_version,memory.total,name,compute_cap",
    "--format=csv,noheader,nounits",
)
_QUERY_BASIC = (
    "--query-gpu=driver_version,memory.total,name",
    "--format=csv,noheader,nounits",
)

_HEADER = (
    "Thu Oct 29 17:39:08 2026\n"
    "+-------------------------------------------------------------------------+\n"
    "| NVIDIA-SMI 617.42       Driver Version: 617.42       CUDA Version: 13.0  |\n"
    "+-------------------------------------------------------------------------+\n"
)
_CSV_WITH_CAP = "617.42, 12282, NVIDIA GeForce RTX 4070, 8.9\n"


class _Completed:
    """subprocess.CompletedProcess 的最小替身。"""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _install_smi(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], _Completed | BaseException],
) -> list[tuple[str, ...]]:
    """按命令参数分派打桩输出，并记录调用序列。"""
    calls: list[tuple[str, ...]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        key = tuple(cmd[1:])
        calls.append(key)
        outcome = responses.get(key)
        if outcome is None:
            return _Completed(1, "")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


class TestParseCudaVersion:
    def test_reads_cuda_version_from_header(self) -> None:
        assert _parse_cuda_version(_HEADER) == "13.0"

    def test_returns_none_without_header_field(self) -> None:
        assert _parse_cuda_version("8.9\n") is None


class TestProbeGpu:
    def test_compute_cap_is_not_reported_as_cuda_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_smi(
            monkeypatch,
            {
                _QUERY_WITH_CAP: _Completed(0, _CSV_WITH_CAP),
                (): _Completed(0, _HEADER),
            },
        )

        probe = _probe_gpu()

        assert probe.cuda_version == "13.0"
        assert probe.compute_capability == "8.9"
        assert probe.driver_version == "617.42"
        assert probe.gpu_name == "NVIDIA GeForce RTX 4070"
        assert probe.memory_gb == pytest.approx(11.99, abs=0.01)

    def test_falls_back_when_compute_cap_column_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_smi(
            monkeypatch,
            {
                _QUERY_WITH_CAP: _Completed(1, ""),
                _QUERY_BASIC: _Completed(0, "550.00, 8192, NVIDIA GeForce RTX 3080\n"),
                (): _Completed(0, _HEADER),
            },
        )

        probe = _probe_gpu()

        assert calls[0] == _QUERY_WITH_CAP
        assert calls[1] == _QUERY_BASIC
        assert probe.compute_capability is None
        assert probe.cuda_version == "13.0"
        assert probe.gpu_name == "NVIDIA GeForce RTX 3080"

    def test_missing_nvidia_smi_yields_empty_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_smi(monkeypatch, {_QUERY_WITH_CAP: FileNotFoundError("nvidia-smi")})

        assert _probe_gpu() == _GpuProbe()


class TestCheckEnvironmentCudaVerdict:
    def test_rtx4070_is_not_reported_as_cuda_too_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_smi(
            monkeypatch,
            {
                _QUERY_WITH_CAP: _Completed(0, _CSV_WITH_CAP),
                (): _Completed(0, _HEADER),
            },
        )

        result = check_environment()

        assert result.cuda_version == "13.0"
        assert result.cuda_ok is True
        assert result.gpu_compute_capability == "8.9"
        assert not [err for err in result.errors if err.startswith("CUDA ")]

    def test_compute_cap_only_does_not_fail_cuda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 表头缺失 CUDA Version（部分驱动/受限环境）：能枚举 GPU 即视为驱动自带 CUDA
        _install_smi(
            monkeypatch,
            {
                _QUERY_WITH_CAP: _Completed(0, _CSV_WITH_CAP),
                (): _Completed(0, "Thu Oct 29 17:39:08 2026\n"),
            },
        )

        result = check_environment()

        assert result.cuda_version is None
        assert result.cuda_ok is True
        assert "Could not detect CUDA version" not in result.errors

    def test_no_gpu_marks_cuda_and_driver_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_smi(monkeypatch, {_QUERY_WITH_CAP: _Completed(1, "")})

        result = check_environment()

        assert result.cuda_ok is False
        assert result.gpu_driver_ok is False
        assert "Could not detect CUDA version" in result.errors

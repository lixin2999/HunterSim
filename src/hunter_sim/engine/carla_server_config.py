"""CARLA 0.9.16 服务端配置管理模块（PROMPT-ENG-001-A）。

负责管理 CARLA 服务端的启动参数、仿真模式配置、画质等级设置。
仅适用于 Windows 11 环境（CARLA 原生运行平台）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from hunter_sim.common.models import QualityLevel
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# ─── 画质-性能映射表 ──────────────────────────────────────────────────────────

_QUALITY_PROFILES: dict[QualityLevel, dict[str, object]] = {
    QualityLevel.LOW: {
        "res_width": 1280,
        "res_height": 720,
        "gpu_memory_gb": 4.0,
        "fps_estimate_rtx3090": 60,
        "description": "低画质：批量 SIL 测试，最多 8 实例/RTX3090",
    },
    QualityLevel.MEDIUM: {
        "res_width": 1920,
        "res_height": 1080,
        "gpu_memory_gb": 6.0,
        "fps_estimate_rtx3090": 45,
        "description": "中画质：VIL 可视化、常规测试，最多 4 实例/RTX3090",
    },
    QualityLevel.EPIC: {
        "res_width": 1920,
        "res_height": 1080,
        "gpu_memory_gb": 10.0,
        "fps_estimate_rtx3090": 30,
        "description": "高画质：演示、高质量录像，最多 2 实例/RTX3090",
    },
}

_MAX_CONCURRENT_INSTANCES: dict[QualityLevel, int] = {
    QualityLevel.LOW: 8,
    QualityLevel.MEDIUM: 4,
    QualityLevel.EPIC: 2,
}


# 画质等级 → CarlaUE4 命令行 -quality-level 参数值（设计文档 §3.1.1）
_QUALITY_LEVEL_FLAGS: dict[QualityLevel, str] = {
    QualityLevel.LOW: "Low",
    QualityLevel.MEDIUM: "Medium",
    QualityLevel.EPIC: "Epic",
}


class CarlaServerConfig(BaseModel):
    """CARLA 服务端启动参数配置。

    Attributes:
        carla_root: CARLA 安装根目录（Windows 路径）。
        gpu_id: NVIDIA GPU 设备编号，-1 表示自动选择。
        quality: 画质等级。
        resolution_width: 渲染分辨率宽度（像素）。
        resolution_height: 渲染分辨率高度（像素）。
        rpc_port: RPC 通信端口。
        stream_port: 数据流端口。
        world_map: 默认加载地图名称。
        offscreen: 是否使用离屏渲染（无窗口模式）。
        no_rendering: 是否禁用渲染（纯物理仿真）。
        fps: 目标帧率（设计文档默认 50）。
        benchmark: 基准模式（禁用帧率限制）。⚠️ 与同步模式互斥：启用后
            CARLA 自由跑帧，fixed_delta_seconds 定步推进失效，仅用于纯异步压力测试。
        nosound: 禁用声音。
        fixed_delta_seconds: 同步模式固定步长（秒）。
        substepping: 是否启用子步。
        max_substep_delta_time: 子步最大时间（秒，设计文档 §3.1.2 为 0.01）。
        max_substeps: 最大子步数。
        extra_args: 额外的 CarlaUE4.exe 命令行参数。
    """

    carla_root: Path = Field(
        default=Path(r"C:\CARLA_0.9.16"),
        description="CARLA 安装目录",
    )
    gpu_id: int = Field(-1, ge=-1, le=8, description="GPU 编号，-1 为自动选择")
    quality: QualityLevel = Field(QualityLevel.MEDIUM, description="渲染画质等级")
    resolution_width: int = Field(1920, ge=640, le=3840)
    resolution_height: int = Field(1080, ge=480, le=2160)
    rpc_port: int = Field(2000, ge=1024, le=65535)
    stream_port: int = Field(2001, ge=1024, le=65535)
    world_map: str = Field("Town03", description="默认地图")
    offscreen: bool = Field(True, description="离屏渲染模式")
    no_rendering: bool = Field(False, description="禁用渲染（仅物理）")
    fps: int = Field(50, ge=1, le=100, description="目标帧率（设计文档 §3.1.1）")
    benchmark: bool = Field(
        False,
        description="基准模式（与同步模式互斥，默认关闭；仅异步压测时显式启用）",
    )
    nosound: bool = Field(True, description="禁用声音")
    fixed_delta_seconds: float = Field(0.02, gt=0.0, le=0.1, description="50Hz 步长")
    substepping: bool = True
    max_substep_delta_time: float = Field(0.01, gt=0.0, le=0.1, description="子步最大时间（秒）")
    max_substeps: int = Field(4, ge=1, le=10)
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("carla_root")
    @classmethod
    def validate_carla_root(cls, v: Path) -> Path:
        """验证 CARLA 根目录存在性。"""
        if not v.exists():
            logger.warning(f"CARLA root not found: {v} (may not be installed yet)")
        return v

    def get_quality_profile(self) -> dict[str, object]:
        """返回当前画质等级对应的性能配置文件。"""
        return _QUALITY_PROFILES[self.quality]

    def max_concurrent_instances(self) -> int:
        """返回当前画质下单 GPU 最大并发实例数。"""
        return _MAX_CONCURRENT_INSTANCES[self.quality]

    def build_sync_settings(self) -> dict[str, object]:
        """构建同步模式（VIL 实时）world settings 参数字典（设计文档 §3.1.2）。

        Returns:
            可直接应用于 carla.WorldSettings 属性的键值字典：
            同步模式 + 固定步长 20ms + 物理子步（0.01s × 4）。
        """
        return {
            "synchronous_mode": True,
            "fixed_delta_seconds": self.fixed_delta_seconds,
            "substepping": self.substepping,
            "max_substep_delta_time": self.max_substep_delta_time,
            "max_substeps": self.max_substeps,
        }

    def build_async_settings(self) -> dict[str, object]:
        """构建异步模式（回放/SIL）world settings 参数字典（设计文档 §3.1.2）。

        Returns:
            异步模式键值字典，步长可变（None = 按真实时间推进）。
        """
        return {
            "synchronous_mode": False,
            "fixed_delta_seconds": None,
        }

    def build_carla_exe_args(self) -> list[str]:
        """构建 CarlaUE4.exe 命令行参数列表。

        Returns:
            命令行参数字符串列表。
        """
        # 设计文档 §3.1.1 标准启动参数：RPC/流端口、画质、音频、帧率、基准模式
        args: list[str] = [
            f"-carla-rpc-port={self.rpc_port}",
            f"-carla-streaming-port={self.stream_port}",
            f"-quality-level={_QUALITY_LEVEL_FLAGS[self.quality]}",
            f"-ini:Script/Carla.Config.DefaultGeneralSettings:[/Script/Carla.CarlaSettings]DefaultMap={self.world_map}",
        ]
        if self.nosound:
            args.append("-nosound")
        if self.benchmark:
            # 审查项 M：-benchmark 禁用帧率限制，与同步定步推进互斥；
            # 启用时不再附加 -fps（会被 benchmark 覆盖，避免配置意图歧义）
            args.append("-benchmark")
        else:
            args.append(f"-fps={self.fps}")
        if self.offscreen:
            args.append("-RenderOffScreen")
        if self.no_rendering:
            args.append("-nullrhi")
        if self.gpu_id >= 0:
            # CARLA 0.9.16（UE4 26.x 基线）选择显卡的正确参数是 -graphicsadapter，
            # 旧式 -gpu= 在新版引擎上不生效
            args.append(f"-graphicsadapter={self.gpu_id}")
        args.extend(self.extra_args)
        return args

    def to_cmd_string(self) -> str:
        """生成 Windows CMD 启动命令字符串。

        Returns:
            可直接在 CMD 中执行的命令字符串。

        Raises:
            ConfigurationError: 如果找不到 CarlaUE4.exe。
        """
        exe_path = self.carla_root / "CarlaUE4.exe"
        args_str = " ".join(self.build_carla_exe_args())
        return f'start "" "{exe_path}" {args_str}'

    def to_powershell_string(self) -> str:
        """生成 PowerShell 启动命令字符串。"""
        exe_path = self.carla_root / "CarlaUE4.exe"
        args_str = " ".join(self.build_carla_exe_args())
        return f'Start-Process -FilePath "{exe_path}" -ArgumentList "{args_str}"'


def check_carla_installed(carla_root: Path) -> bool:
    """检查 CARLA 是否已正确安装。

    Args:
        carla_root: CARLA 安装根目录。

    Returns:
        True 表示安装有效。
    """
    exe_path = carla_root / "CarlaUE4.exe"
    return exe_path.exists()

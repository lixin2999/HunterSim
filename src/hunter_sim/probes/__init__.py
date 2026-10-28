"""只读运维探针面（V3.0.0）。

业务入口统一收敛到 ``huntersim carla-manager``（:8090）；本包只暴露
``/health`` 与 ``/metrics``，供容器探针、Prometheus 抓取与人工排障使用，
不承载任何仿真业务、不写数据库、不依赖鉴权。
"""

from hunter_sim.probes.app import create_probes_app

__all__ = ["create_probes_app"]

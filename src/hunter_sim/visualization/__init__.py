"""可视化客户端（V3.1.0）。

- ``logbus``：进程内日志环形缓冲 + 增量订阅（日志面板数据源）；
- ``sensor_view``：FrameHub 原始帧 → 浏览器可消费格式（图片/点云/IMU JSON）；
- ``routes``：``/viz`` 页面与 ``/viz/api/*`` 只读端点（挂控制面 :8090）；
- ``launcher``：``huntersim up`` 的一键启动编排（预检 → 起服务 → 开页面）。

本包只做**只读展示**：不新增实例状态、不写数据库、不持有 CARLA 句柄。
"""

from __future__ import annotations

from hunter_sim.visualization.logbus import LogBus, LogRecord
from hunter_sim.visualization.routes import register_viz_routes, viz_url

__all__ = ["LogBus", "LogRecord", "register_viz_routes", "viz_url"]

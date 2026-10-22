"""HunterSim 运维 CLI 包（V2.8.0 P2-16）。

入口位于 :mod:`hunter_sim.cli.main`（``huntersim`` 控制台脚本）。
本 ``__init__`` 不做任何重导出，保证 ``python -m hunter_sim.cli.main`` 与
控制台脚本共用同一模块实例（避免 runpy 的双导入告警）。
"""

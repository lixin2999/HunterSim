"""无孤儿导出检查（V3.0.0 §6.3）。

规则：包的 ``__init__.py`` 在 ``__all__`` 中声明的符号，必须在 ``src/hunter_sim``
的**非 `__init__` 生产代码**里被引用（定义处不算引用，仅测试引用也不算）。
本检查用于拦截「导出并实现、单测也覆盖，但生产链路从未装配」的死代码
（V3.0.0 修复的 ``StateVisualizer`` 即此类型：导出 + 有单测，但 VIL 链路未接）。

``_LEGACY_EXPORTS`` 是 V3.0.0 之前既已存在的历史导出（多为基础工具/异常/
可选扩展点），收敛不在本期范围，登记为已知欠账；新增导出不得再落入此名单。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hunter_sim"

# 历史遗留导出（本期不处理；新增导出禁止加入本集合）
_LEGACY_EXPORTS: frozenset[str] = frozenset(
    {
        "CarlaConnectionError",
        "ResourceError",
        "InstanceStatus",
        "get_struct_logger",
        "deg_to_rad",
        "rad_to_deg",
        "rmse",
        "CarlaServerConfig",
        "check_carla_installed",
        "MapHeightGetterProtocol",
        "get_sensor_mount",
        "spawn_hunter_se",
        "VehicleControllerFactory",
        "ScenarioRunnerAdapter",
        "SceneConfigValidator",
        "ScriptedActorController",
        "create_behavior_from_config",
        "TrafficFlowManager",
        "create_trigger_from_config",
        "WalkerControllerWrapper",
        "CalibrationManager",
    }
)


def _exported_names(init_path: Path) -> list[str]:
    """读取 ``__init__.py`` 的 ``__all__`` 字面量（非字面量声明返回空）。"""
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__all__" not in targets:
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            return [
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
    return []


def _loaded_names(path: Path) -> set[str]:
    """收集一个模块中「被使用」的标识符（排除定义点与字符串常量）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[-1] for alias in node.names)
    return found


def _module_references() -> set[str]:
    """全部生产代码（非 ``__init__.py``）中被使用的标识符集合。"""
    references: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        references |= _loaded_names(path)
    return references


@pytest.fixture(scope="module")
def references() -> set[str]:
    return _module_references()


def test_package_inits_are_discoverable() -> None:
    """至少扫描到若干包，避免路径算错导致空跑通过。"""
    inits = list(_SRC_ROOT.rglob("__init__.py"))

    assert {path.parent.name for path in inits} >= {"common", "vil_mapper", "sensor_sim"}


def test_officially_wired_symbols_are_referenced(references: set[str]) -> None:
    """V3.0.0 新接入的导出必须有生产代码引用（StateVisualizer 回归守卫）。"""
    for symbol in ("StateVisualizer", "FrameHub", "McapRecorder", "FrameAligner", "CarlaRosBridge"):
        assert symbol in references, f"{symbol} 已导出但生产链路未引用"


def test_no_new_orphan_exports(references: set[str]) -> None:
    """``__all__`` 导出的符号不得是「无生产引用」的孤儿（历史欠账除外）。"""
    orphans: dict[str, list[str]] = {}
    for init_path in sorted(_SRC_ROOT.rglob("__init__.py")):
        missing = [
            name
            for name in _exported_names(init_path)
            if name not in references and name not in _LEGACY_EXPORTS
        ]
        if missing:
            package = init_path.parent.relative_to(_SRC_ROOT).as_posix() or "<root>"
            orphans[package] = missing

    assert orphans == {}, f"存在孤儿导出（导出但无生产引用）：{orphans}"


def test_legacy_allowlist_stays_honest(references: set[str]) -> None:
    """历史欠账名单只减不增：已被引用或已删除的条目必须从名单移除。"""
    stale = sorted(name for name in _LEGACY_EXPORTS if name in references)

    assert stale == [], f"以下名单条目已被生产代码引用，请从 _LEGACY_EXPORTS 移除：{stale}"

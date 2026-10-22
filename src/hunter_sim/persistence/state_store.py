"""SQLite 状态持久化（V2.4.0）。

控制面的实例表与场景绑定必须跨进程重启保留，否则：
- 服务重启后实例丢失，客户端无法继续查询/销毁；
- 已下发场景需要重新 POST，且无法解释「重启前跑到哪了」。

本模块只做键值/JSON 读写，不依赖 CARLA 与 FastAPI，便于单测与运维直接检查。
数据库默认位于 ``data/state/hunter_sim.db``（compose 挂载持久卷）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    sim_instance_id TEXT PRIMARY KEY,
    payload         TEXT NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_bindings (
    instance_id     TEXT PRIMARY KEY,
    scene_id        TEXT NOT NULL,
    map_id          TEXT NOT NULL,
    config          TEXT NOT NULL,
    status          TEXT NOT NULL,
    progress        REAL NOT NULL DEFAULT 0,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    error_message   TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL
);
"""

# 服务重启后执行面已丢失：运行中的场景统一回到「已就绪（可重新启动）」
_RESTART_SCENE_STATUS = {"loading": "ready", "running": "ready", "paused": "ready"}


class StateStore:
    """实例与场景绑定的 SQLite 存储（线程安全）。

    Args:
        db_path: 数据库路径，父目录不存在时自动创建；``:memory:`` 用于单测。
        busy_timeout_seconds: SQLite 忙等待超时。
    """

    def __init__(self, db_path: str = "data/state/hunter_sim.db", busy_timeout_seconds: float = 5.0) -> None:
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            timeout=busy_timeout_seconds,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info(f"State store initialized: {db_path}")

    @property
    def path(self) -> str:
        """数据库路径（供 /health 与排障展示）。"""
        return self._path

    # ─── 实例 ─────────────────────────────────────────────────────────────────

    def save_instance(self, record: dict[str, Any]) -> None:
        """写入或更新一条实例记录（record 需含 sim_instance_id）。"""
        instance_id = str(record["sim_instance_id"])
        payload = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO instances (sim_instance_id, payload, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(sim_instance_id) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at",
                    (instance_id, payload, time.time()),
                )

    def load_instances(self) -> list[dict[str, Any]]:
        """读取全部实例记录（按创建时间升序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM instances ORDER BY updated_at ASC"
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                records.append(json.loads(row["payload"]))
            except json.JSONDecodeError as exc:  # 损坏记录不阻断启动
                logger.warning(f"Skip corrupted instance record: {exc}")
        return records

    def delete_instance(self, instance_id: str) -> None:
        """删除实例及其场景绑定（销毁实例时调用）。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM instances WHERE sim_instance_id = ?", (instance_id,)
                )
                self._conn.execute(
                    "DELETE FROM scene_bindings WHERE instance_id = ?", (instance_id,)
                )

    # ─── 场景绑定 ─────────────────────────────────────────────────────────────

    def save_scene(
        self,
        instance_id: str,
        scene_id: str,
        map_id: str,
        config: dict[str, Any],
        status: str = "ready",
    ) -> None:
        """写入或更新场景绑定（场景下发成功后调用）。"""
        payload = json.dumps(config, ensure_ascii=False, default=str)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO scene_bindings "
                    "(instance_id, scene_id, map_id, config, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(instance_id) DO UPDATE SET "
                    "scene_id = excluded.scene_id, map_id = excluded.map_id, "
                    "config = excluded.config, status = excluded.status, "
                    "progress = 0, elapsed_seconds = 0, error_message = '', "
                    "updated_at = excluded.updated_at",
                    (instance_id, scene_id, map_id, payload, status, time.time()),
                )

    def update_scene_snapshot(
        self,
        instance_id: str,
        status: str,
        progress: float = 0.0,
        elapsed_seconds: float = 0.0,
        error_message: str = "",
    ) -> None:
        """更新场景运行快照（状态机回调调用；无绑定记录时忽略）。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE scene_bindings SET status = ?, progress = ?, "
                    "elapsed_seconds = ?, error_message = ?, updated_at = ? "
                    "WHERE instance_id = ?",
                    (
                        status,
                        float(progress),
                        float(elapsed_seconds),
                        error_message or "",
                        time.time(),
                        instance_id,
                    ),
                )

    def load_scene(self, instance_id: str) -> Optional[dict[str, Any]]:
        """读取场景绑定（含原始配置 JSON 与最近快照）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scene_bindings WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            config = json.loads(row["config"])
        except json.JSONDecodeError as exc:
            logger.warning(f"Corrupted scene config for instance {instance_id}: {exc}")
            return None
        return {
            "instance_id": row["instance_id"],
            "scene_id": row["scene_id"],
            "map_id": row["map_id"],
            "config": config,
            "status": row["status"],
            "progress": row["progress"],
            "elapsed_seconds": row["elapsed_seconds"],
            "error_message": row["error_message"],
            "updated_at": row["updated_at"],
        }

    def list_scenes(self) -> list[dict[str, Any]]:
        """列出全部场景绑定（不含配置体，供排障）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT instance_id, scene_id, map_id, status, progress, "
                "elapsed_seconds, error_message, updated_at "
                "FROM scene_bindings ORDER BY updated_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_scene(self, instance_id: str) -> None:
        """删除场景绑定。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM scene_bindings WHERE instance_id = ?", (instance_id,)
                )

    # ─── 重启恢复 ─────────────────────────────────────────────────────────────

    def normalize_scene_statuses(self) -> int:
        """重启后将运行中的场景状态归一为 ready，返回受影响行数。

        执行面（tick 线程与 CARLA world）不跨进程存活，直接展示 running
        会误导调用方；保留场景定义供重新装配，状态回到「已就绪」。
        """
        updated = 0
        with self._lock:
            with self._conn:
                for old_status, new_status in _RESTART_SCENE_STATUS.items():
                    cursor = self._conn.execute(
                        "UPDATE scene_bindings SET status = ?, updated_at = ? WHERE status = ?",
                        (new_status, time.time(), old_status),
                    )
                    updated += cursor.rowcount
        return updated

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as exc:  # 关闭失败仅记录
                logger.warning(f"State store close failed: {exc}")


__all__ = ["StateStore"]

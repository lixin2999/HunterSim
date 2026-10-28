"""实例注册表（SQLite，重启恢复 + scene_id/sim_id 幂等）。

适配器只用一个进程、一个 SQLite 文件：单世界串行执行，无需分布式锁。
连接使用 ``check_same_thread=False`` + 进程内 ``RLock``，所有写操作串行。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.utils import get_logger
from hunter_sim.platform_adapter.models import (
    InstanceRecord,
    PLATFORM_FAILED,
    PLATFORM_PENDING,
    PLATFORM_RUNNING,
    TERMINAL_STATUSES,
)

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    sim_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    scene_name TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'sil',
    vehicle_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    carla_status TEXT NOT NULL DEFAULT 'CREATED',
    message TEXT,
    progress_percent REAL NOT NULL DEFAULT 0,
    current_time_s REAL NOT NULL DEFAULT 0,
    total_time_s REAL NOT NULL DEFAULT 0,
    actors_count INTEGER NOT NULL DEFAULT 0,
    collisions INTEGER NOT NULL DEFAULT 0,
    lane_invasions INTEGER NOT NULL DEFAULT 0,
    ego_state TEXT NOT NULL DEFAULT '{}',
    scene_config TEXT NOT NULL DEFAULT '{}',
    vil_config TEXT,
    result TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    watchdog_deadline REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_instances_scene ON instances(scene_id);
CREATE INDEX IF NOT EXISTS idx_instances_sim ON instances(sim_id);
CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status);
"""

_UPDATABLE = (
    "scene_name",
    "mode",
    "vehicle_id",
    "status",
    "carla_status",
    "message",
    "progress_percent",
    "current_time_s",
    "total_time_s",
    "actors_count",
    "collisions",
    "lane_invasions",
    "ego_state",
    "scene_config",
    "vil_config",
    "result",
    "revision",
    "updated_at",
    "started_at",
    "finished_at",
    "watchdog_deadline",
)


class InstanceStore:
    """实例注册表。

    Args:
        db_path: SQLite 文件路径（父目录自动创建）。
        busy_timeout_seconds: 忙等待超时。
    """

    def __init__(self, db_path: str, busy_timeout_seconds: float = 5.0) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = int(max(1.0, busy_timeout_seconds) * 1000)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def connect(self) -> "InstanceStore":
        """打开连接并建表（幂等）。"""
        with self._lock:
            if self._conn is not None:
                return self
            path = Path(self._db_path)
            if path.parent and str(path.parent) not in ("", "."):
                path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(path),
                check_same_thread=False,
                timeout=max(1.0, self._busy_timeout_ms / 1000.0),
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            logger.info(f"Adapter instance store ready: {path}")
        return self

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # ── 写入 ────────────────────────────────────────────────────────────────

    def insert(self, record: InstanceRecord) -> None:
        """插入新实例记录。"""
        with self._lock:
            self._connection().execute(
                """
                INSERT INTO instances (
                    instance_id, sim_id, scene_id, scene_name, mode, vehicle_id,
                    status, carla_status, message, progress_percent, current_time_s,
                    total_time_s, actors_count, collisions, lane_invasions, ego_state,
                    scene_config, vil_config, result, revision, created_at, updated_at,
                    started_at, finished_at, watchdog_deadline
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.instance_id,
                    record.sim_id,
                    record.scene_id,
                    record.scene_name,
                    record.mode,
                    record.vehicle_id,
                    record.status,
                    record.carla_status,
                    record.message,
                    record.progress_percent,
                    record.current_time_s,
                    record.total_time_s,
                    record.actors_count,
                    record.collisions,
                    record.lane_invasions,
                    _dump(record.ego_state),
                    _dump(record.scene_config),
                    _dump(record.vil_config) if record.vil_config is not None else None,
                    _dump(record.result) if record.result is not None else None,
                    record.revision,
                    record.created_at,
                    record.updated_at,
                    record.started_at,
                    record.finished_at,
                    record.watchdog_deadline,
                ),
            )
            self._connection().commit()

    def update(self, record: InstanceRecord) -> None:
        """按主键更新可变字段。"""
        record.updated_at = time.time()
        values = [getattr(record, name) for name in _UPDATABLE]
        encoded: list[Any] = []
        for name, value in zip(_UPDATABLE, values):
            if name in ("ego_state", "scene_config"):
                encoded.append(_dump(value if isinstance(value, dict) else {}))
            elif name in ("vil_config", "result"):
                encoded.append(_dump(value) if value is not None else None)
            else:
                encoded.append(value)
        assignments = ", ".join(f"{name} = ?" for name in _UPDATABLE)
        with self._lock:
            self._connection().execute(
                f"UPDATE instances SET {assignments} WHERE instance_id = ?",
                (*encoded, record.instance_id),
            )
            self._connection().commit()

    def patch(self, instance_id: str, **fields: Any) -> None:
        """局部更新（自动刷新 updated_at，忽略未知字段）。"""
        updates = {key: value for key, value in fields.items() if key in _UPDATABLE}
        updates["updated_at"] = time.time()
        encoded: dict[str, Any] = {}
        for key, value in updates.items():
            if key in ("ego_state", "scene_config"):
                encoded[key] = _dump(value if isinstance(value, dict) else {})
            elif key in ("vil_config", "result"):
                encoded[key] = _dump(value) if value is not None else None
            else:
                encoded[key] = value
        assignments = ", ".join(f"{key} = ?" for key in encoded)
        with self._lock:
            self._connection().execute(
                f"UPDATE instances SET {assignments} WHERE instance_id = ?",
                (*encoded.values(), instance_id),
            )
            self._connection().commit()

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get(self, instance_id: str) -> Optional[InstanceRecord]:
        """按实例 ID 查询。"""
        with self._lock:
            row = self._connection().execute(
                "SELECT * FROM instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_by_scene(self, scene_id: str) -> list[InstanceRecord]:
        """按场景 ID 查询（按创建时间倒序）。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT * FROM instances WHERE scene_id = ? ORDER BY created_at DESC",
                (scene_id,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_all(self) -> list[InstanceRecord]:
        """查询全部实例（按创建时间倒序）。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT * FROM instances ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def find_by_sim_id(self, sim_id: str) -> Optional[InstanceRecord]:
        """按 sim_id 查询最近一条（平台 sim_id 是会话主键）。"""
        if not sim_id:
            return None
        with self._lock:
            row = self._connection().execute(
                "SELECT * FROM instances WHERE sim_id = ? ORDER BY created_at DESC LIMIT 1",
                (sim_id,),
            ).fetchone()
        if row is None:
            return None
        record = _row_to_record(row)
        return record if record.status not in TERMINAL_STATUSES else None

    def find_active_by_scene(self, scene_id: str) -> Optional[InstanceRecord]:
        """查询该场景最近的进行中实例。"""
        with self._lock:
            row = self._connection().execute(
                """
                SELECT * FROM instances
                WHERE scene_id = ? AND status IN (?, ?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (scene_id, PLATFORM_PENDING, PLATFORM_RUNNING),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def non_terminal(self) -> list[InstanceRecord]:
        """查询所有非终态实例（启动恢复用）。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT * FROM instances WHERE status IN (?, ?)",
                (PLATFORM_PENDING, PLATFORM_RUNNING),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def mark_restarted_instances_failed(self, message: str = "adapter restarted") -> int:
        """把重启前未终结的实例标记为 failed，返回受影响条数。"""
        now = time.time()
        with self._lock:
            cursor = self._connection().execute(
                """
                UPDATE instances
                SET status = ?, carla_status = 'FAILED', message = ?, finished_at = ?,
                    updated_at = ?, watchdog_deadline = 0
                WHERE status IN (?, ?)
                """,
                (
                    PLATFORM_FAILED,
                    message,
                    now,
                    now,
                    PLATFORM_PENDING,
                    PLATFORM_RUNNING,
                ),
            )
            self._connection().commit()
            count = int(cursor.rowcount or 0)
        if count:
            logger.warning(f"Marked {count} non-terminal instance(s) as failed: {message}")
        return count


def _dump(value: Any) -> str:
    """JSON 序列化（``default=str`` 兼容非原生类型）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _load_dict(raw: Optional[str]) -> dict[str, Any]:
    """读取 JSON 对象（空/非法回退空字典）。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _row_to_record(row: sqlite3.Row) -> InstanceRecord:
    """SQLite 行 → InstanceRecord。"""
    vil_raw = row["vil_config"]
    result_raw = row["result"]
    return InstanceRecord(
        instance_id=str(row["instance_id"]),
        sim_id=str(row["sim_id"]),
        scene_id=str(row["scene_id"]),
        scene_name=str(row["scene_name"] or ""),
        mode=str(row["mode"] or "sil"),
        vehicle_id=str(row["vehicle_id"] or ""),
        status=str(row["status"]),
        carla_status=str(row["carla_status"]),
        message=row["message"],
        progress_percent=float(row["progress_percent"]),
        current_time_s=float(row["current_time_s"]),
        total_time_s=float(row["total_time_s"]),
        actors_count=int(row["actors_count"]),
        collisions=int(row["collisions"]),
        lane_invasions=int(row["lane_invasions"]),
        ego_state=_load_dict(row["ego_state"]),
        scene_config=_load_dict(row["scene_config"]),
        vil_config=_load_dict(vil_raw) if vil_raw else None,
        result=(json.loads(result_raw) if result_raw else None),
        revision=int(row["revision"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
        watchdog_deadline=float(row["watchdog_deadline"] or 0.0),
    )


__all__ = ["InstanceStore"]

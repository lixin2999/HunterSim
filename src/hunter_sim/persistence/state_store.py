"""SQLite 状态持久化（V2.4.0 实例/场景，V2.5.0 平台化）。

控制面的实例表与场景绑定必须跨进程重启保留，否则：
- 服务重启后实例丢失，客户端无法继续查询/销毁；
- 已下发场景需要重新 POST，且无法解释「重启前跑到哪了」。

V2.5.0 新增平台化四张表：服务账号（api_keys）、场景库（scene_library）、
批量任务（batch_tasks）与审计日志（audit_logs），同样以 SQLite 单库承载。

本模块只做键值/JSON 读写，不依赖 CARLA 与 FastAPI，便于单测与运维直接检查。
数据库默认位于 ``data/state/hunter_sim.db``（compose 挂载持久卷）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
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

CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tenant       TEXT NOT NULL,
    role         TEXT NOT NULL,
    key_hash     TEXT NOT NULL,
    salt         TEXT NOT NULL,
    prefix       TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    revoked_at   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scene_library (
    library_scene_id TEXT PRIMARY KEY,
    tenant           TEXT NOT NULL,
    name             TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    scene_id         TEXT NOT NULL DEFAULT '',
    map_id           TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    config           TEXT NOT NULL,
    created_by       TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    archived         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scene_library_tenant ON scene_library(tenant, name);

CREATE TABLE IF NOT EXISTS batch_tasks (
    task_id    TEXT PRIMARY KEY,
    tenant     TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    auth_type  TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_time ON audit_logs(tenant, created_at);
"""

# 服务重启后执行面已丢失：运行中的场景统一回到「已就绪（可重新启动）」
_RESTART_SCENE_STATUS = {"loading": "ready", "running": "ready", "paused": "ready"}

# 服务账号密钥前缀与长度（明文仅在签发时返回一次）
_API_KEY_PREFIX = "hsk"
_API_KEY_SECRET_BYTES = 24
# last_used_at 写库节流：每次请求都写会放大 SQLite 写竞争
_API_KEY_TOUCH_INTERVAL_S = 60.0


def _to_epoch(value: Any) -> float:
    """将时间戳/ISO8601 字符串归一为 epoch 秒（无法解析时用当前时间）。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return time.time()
    return time.time()


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

    # ─── 服务账号（API Key，V2.5.0） ─────────────────────────────────────────

    @staticmethod
    def _hash_api_key(salt: str, secret: str) -> str:
        """计算密钥摘要（salt 参与哈希，库中不保存明文）。"""
        return hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()

    def generate_api_key(self) -> tuple[str, str, str, str]:
        """生成一组服务账号密钥。

        Returns:
            ``(明文密钥, key_id, salt, key_hash)``；明文形如
            ``hsk_<key_id>_<secret>``，仅在签发时返回一次。
        """
        key_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(_API_KEY_SECRET_BYTES)
        salt = secrets.token_hex(16)
        raw = f"{_API_KEY_PREFIX}_{key_id}_{secret}"
        return raw, key_id, salt, self._hash_api_key(salt, secret)

    def save_api_key(self, record: dict[str, Any]) -> None:
        """写入服务账号记录（含 key_hash/salt，不含明文）。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO api_keys (key_id, name, tenant, role, key_hash, salt, "
                    "prefix, created_by, created_at, expires_at, last_used_at, revoked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(record["key_id"]),
                        str(record.get("name", "")),
                        str(record.get("tenant", "default")),
                        str(record.get("role", "operator")),
                        str(record["key_hash"]),
                        str(record["salt"]),
                        str(record.get("prefix", "")),
                        str(record.get("created_by", "")),
                        float(record.get("created_at", time.time())),
                        float(record.get("expires_at", 0.0)),
                        0.0,
                        0.0,
                    ),
                )

    def get_api_key(self, key_id: str) -> Optional[dict[str, Any]]:
        """按 key_id 读取服务账号记录（含 key_hash/salt，仅供内部校验）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_api_keys(self, tenant: str = "") -> list[dict[str, Any]]:
        """列出服务账号（不含 hash/salt，按创建时间倒序）。"""
        query = (
            "SELECT key_id, name, tenant, role, prefix, created_by, created_at, "
            "expires_at, last_used_at, revoked_at FROM api_keys"
        )
        params: tuple[Any, ...] = ()
        if tenant:
            query += " WHERE tenant = ?"
            params = (tenant,)
        query += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def revoke_api_key(self, key_id: str, tenant: str = "") -> bool:
        """吊销服务账号（幂等）；返回是否命中未吊销记录。"""
        query = "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at = 0"
        params: tuple[Any, ...] = (time.time(), key_id)
        if tenant:
            query += " AND tenant = ?"
            params = (time.time(), key_id, tenant)
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(query, params)
        return bool(cursor.rowcount > 0)

    def verify_api_key(self, raw_key: str) -> Optional[dict[str, Any]]:
        """校验明文 API Key，成功时返回主体信息（含 tenant/role），否则 None。

        密钥格式 ``hsk_<key_id>_<secret>``；已吊销/已过期/摘要不匹配均失败。
        ``last_used_at`` 按 60 秒节流回写，避免每个请求都写库。
        """
        parts = raw_key.split("_", 2)
        if len(parts) != 3 or parts[0] != _API_KEY_PREFIX:
            return None
        _, key_id, secret = parts
        if not key_id or not secret:
            return None
        record = self.get_api_key(key_id)
        if record is None or float(record.get("revoked_at", 0.0)) > 0:
            return None
        expires_at = float(record.get("expires_at", 0.0))
        now = time.time()
        if expires_at > 0 and now >= expires_at:
            return None
        expected = str(record["key_hash"])
        actual = self._hash_api_key(str(record["salt"]), secret)
        if not hmac.compare_digest(expected, actual):
            return None
        if now - float(record.get("last_used_at", 0.0)) >= _API_KEY_TOUCH_INTERVAL_S:
            try:
                self.touch_api_key(key_id, now)
            except sqlite3.Error as exc:  # 仅统计信息，失败不影响鉴权
                logger.debug(f"API key last_used update failed: {exc}")
        return {
            "key_id": key_id,
            "name": str(record.get("name", "")),
            "tenant": str(record.get("tenant", "default")),
            "role": str(record.get("role", "operator")),
            "created_by": str(record.get("created_by", "")),
            "expires_at": expires_at,
        }

    def touch_api_key(self, key_id: str, timestamp: Optional[float] = None) -> None:
        """更新服务账号最近使用时间。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                    (float(timestamp or time.time()), key_id),
                )

    # ─── 场景库（V2.5.0） ────────────────────────────────────────────────────

    def save_library_scene(
        self,
        tenant: str,
        name: str,
        scene_id: str,
        map_id: str,
        description: str,
        config: dict[str, Any],
        created_by: str = "",
    ) -> dict[str, Any]:
        """保存场景库条目；同名（同租户）自动递增版本并归档旧版本。"""
        now = time.time()
        payload = json.dumps(config, ensure_ascii=False, default=str)
        library_scene_id = uuid.uuid4().hex
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT MAX(version) FROM scene_library WHERE tenant = ? AND name = ?",
                    (tenant, name),
                ).fetchone()
                version = int(row[0] or 0) + 1
                if version > 1:
                    self._conn.execute(
                        "UPDATE scene_library SET archived = 1, updated_at = ? "
                        "WHERE tenant = ? AND name = ? AND archived = 0",
                        (now, tenant, name),
                    )
                self._conn.execute(
                    "INSERT INTO scene_library (library_scene_id, tenant, name, version, "
                    "scene_id, map_id, description, config, created_by, created_at, "
                    "updated_at, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        library_scene_id,
                        tenant,
                        name,
                        version,
                        scene_id,
                        map_id,
                        description,
                        payload,
                        created_by,
                        now,
                        now,
                    ),
                )
        item = self.get_library_scene(library_scene_id)
        if item is None:  # 写入后立即读不应发生，保留异常语义便于排障
            raise RuntimeError(f"Scene library insert failed: {library_scene_id}")
        return item

    def get_library_scene(
        self, library_scene_id: str, tenant: str = ""
    ) -> Optional[dict[str, Any]]:
        """读取场景库条目（含 config）；指定 tenant 时做租户隔离。"""
        query = "SELECT * FROM scene_library WHERE library_scene_id = ?"
        params: tuple[Any, ...] = (library_scene_id,)
        if tenant:
            query += " AND tenant = ?"
            params = (library_scene_id, tenant)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["config"] = json.loads(item["config"])
        except json.JSONDecodeError as exc:
            logger.warning(f"Corrupted library scene config {library_scene_id}: {exc}")
            return None
        item["archived"] = bool(item.get("archived", 0))
        return item

    def list_library_scenes(
        self, tenant: str = "", include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """列出场景库条目（不含 config，按更新时间倒序）。"""
        query = (
            "SELECT library_scene_id, tenant, name, version, scene_id, map_id, "
            "description, created_by, created_at, updated_at, archived FROM scene_library"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if tenant:
            clauses.append("tenant = ?")
            params.append(tenant)
        if not include_archived:
            clauses.append("archived = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["archived"] = bool(item.get("archived", 0))
        return items

    def delete_library_scene(self, library_scene_id: str, tenant: str = "") -> bool:
        """归档场景库条目（软删除）；返回是否命中。"""
        query = "UPDATE scene_library SET archived = 1, updated_at = ? WHERE library_scene_id = ?"
        params: tuple[Any, ...] = (time.time(), library_scene_id)
        if tenant:
            query += " AND tenant = ?"
            params = (time.time(), library_scene_id, tenant)
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(query, params)
        return bool(cursor.rowcount > 0)

    # ─── 批量任务（V2.5.0） ──────────────────────────────────────────────────

    def save_batch_task(self, task: dict[str, Any]) -> None:
        """写入或更新批量任务快照（payload 存完整任务字典）。"""
        payload = json.dumps(task, ensure_ascii=False, default=str)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO batch_tasks (task_id, tenant, owner, status, payload, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET status = excluded.status, "
                    "payload = excluded.payload, updated_at = excluded.updated_at",
                    (
                        str(task["task_id"]),
                        str(task.get("tenant", "default")),
                        str(task.get("owner", "")),
                        str(task.get("status", "pending")),
                        payload,
                        _to_epoch(task.get("created_at")),
                        time.time(),
                    ),
                )

    def load_batch_tasks(self, tenant: str = "") -> list[dict[str, Any]]:
        """读取批量任务（按创建时间升序）。"""
        query = "SELECT payload FROM batch_tasks"
        params: tuple[Any, ...] = ()
        if tenant:
            query += " WHERE tenant = ?"
            params = (tenant,)
        query += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            try:
                tasks.append(json.loads(row["payload"]))
            except json.JSONDecodeError as exc:  # 损坏记录不阻断启动
                logger.warning(f"Skip corrupted batch task record: {exc}")
        return tasks

    def load_batch_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """按 task_id 读取批量任务快照。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM batch_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return dict(json.loads(row["payload"]))
        except json.JSONDecodeError as exc:
            logger.warning(f"Corrupted batch task {task_id}: {exc}")
            return None

    def prune_batch_tasks(self, keep: int = 1000) -> int:
        """仅保留最新 ``keep`` 条批量任务，返回清理数量（防止库无限增长）。"""
        if keep <= 0:
            return 0
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM batch_tasks WHERE task_id NOT IN ("
                    "SELECT task_id FROM batch_tasks ORDER BY created_at DESC LIMIT ?)",
                    (keep,),
                )
        return int(cursor.rowcount)

    def normalize_batch_tasks(self) -> int:
        """重启后将未完成的批量任务统一标记为 failed（执行器不跨进程存活）。"""
        updated = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, payload FROM batch_tasks WHERE status IN ('running', 'pending')"
            ).fetchall()
            with self._conn:
                for row in rows:
                    try:
                        task = dict(json.loads(row["payload"]))
                    except json.JSONDecodeError:
                        continue
                    task["status"] = "failed"
                    task["error_message"] = "interrupted by service restart"
                    self._conn.execute(
                        "UPDATE batch_tasks SET status = ?, payload = ?, updated_at = ? "
                        "WHERE task_id = ?",
                        (
                            "failed",
                            json.dumps(task, ensure_ascii=False, default=str),
                            time.time(),
                            str(row["task_id"]),
                        ),
                    )
                    updated += 1
        return updated

    # ─── 审计日志（V2.5.0） ─────────────────────────────────────────────────

    def append_audit(
        self,
        action: str,
        user_id: str,
        target: str = "",
        detail: str = "",
        tenant: str = "default",
        auth_type: str = "",
    ) -> None:
        """追加一条审计事件（失败只告警，不影响主流程）。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO audit_logs (tenant, user_id, action, target, detail, "
                    "auth_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tenant, user_id, action, target, detail, auth_type, time.time()),
                )

    def list_audit(
        self,
        tenant: str = "",
        action: str = "",
        user_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询审计事件（新→旧），返回 ``{items, total, limit, offset}``。"""
        clauses: list[str] = []
        params: list[Any] = []
        if tenant:
            clauses.append("tenant = ?")
            params.append(tenant)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM audit_logs{where}", tuple(params)  # noqa: S608  参数已绑定
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                f"SELECT * FROM audit_logs{where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
                tuple(params) + (int(limit), int(offset)),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": int(limit),
            "offset": int(offset),
        }

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

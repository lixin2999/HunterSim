"""rosbag 经 data-collector 预签名直传 MinIO（V2.2.0）。

对齐 HunterCore 5.5 节上传流程与 `contracts/openapi/data-collector.yaml`：

1. `POST /api/v1/data/files/presign`（Bearer JWT）→ 服务端生成 `object_key` 与预签名地址；
2. 直传（`method=put` 单次 PUT / `method=multipart` 分片 PUT，收集各 part ETag）；
3. `POST /api/v1/data/files/complete` 上报 size/md5/sha256 做完整性校验并投递 sensor_file。

`httpx` 客户端可注入（单测用桩替换），未启用或缺 JWT 时降级为 no-op。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hunter_sim.common.models import CloudSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_PRESIGN_PATH = "/api/v1/data/files/presign"
_COMPLETE_PATH = "/api/v1/data/files/complete"
_CHUNK = 1024 * 1024  # 分片大小 1 MiB
# 超过该阈值走分片（大文件 ROS Bag）
_MULTIPART_THRESHOLD = 32 * 1024 * 1024

ClientFactory = Callable[[], Any]


class UploadError(RuntimeError):
    """上传流程失败（预签名拒绝 / 直传失败 / 完整性校验不通过）。"""


@dataclass(frozen=True)
class BagUploadResult:
    """一次成功上传的结果摘要。"""

    bucket: str
    object_key: str
    size_bytes: int
    md5: str
    sha256: str


class BagUploader:
    """data-collector 预签名上传封装。

    Args:
        settings: HunterCore 接入配置。
        client_factory: 可选 httpx.Client 工厂（单测注入桩）；默认惰性构建 httpx 客户端。
    """

    def __init__(self, settings: CloudSettings, client_factory: Optional[ClientFactory] = None) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._available = False

    def connect(self) -> bool:
        """检查启用与凭据齐备性；缺失时降级为 no-op。"""
        if not self._settings.enabled:
            logger.info("HunterCore 上传未启用（HUNTER_CORE_ENABLED=false），BagUploader 降级为 no-op")
            self._available = False
            return False
        if self._settings.jwt_token is None or not self._settings.jwt_token.get_secret_value():
            logger.warning("未配置 HUNTER_CORE_JWT_TOKEN，BagUploader 降级为 no-op")
            self._available = False
            return False
        self._available = True
        return True

    @property
    def available(self) -> bool:
        """上传器是否可用。"""
        return self._available

    def upload(self, file_path: Path, data_type: str = "rosbag") -> Optional[BagUploadResult]:
        """上传单个 bag 文件；不可用时返回 None（不抛出，不阻断仿真）。

        Raises:
            UploadError: 已启用但流程某步失败。
        """
        if not self._available:
            return None
        if not file_path.exists():
            raise UploadError(f"待上传文件不存在：{file_path}")

        size, md5, sha256 = _digest_file(file_path)
        client = self._client_factory() if self._client_factory is not None else self._default_client()
        try:
            presign = self._presign(client, file_path.name, size)
            method = presign.get("method", "put")
            if method == "multipart":
                etags = self._upload_multipart(client, presign, file_path)
            else:
                self._upload_single(client, presign, file_path)
            self._complete(client, presign, size, md5, sha256, data_type, etags if method == "multipart" else None)
        finally:
            if self._client_factory is None:
                client.close()

        result = BagUploadResult(
            bucket=presign["bucket"],
            object_key=presign["object_key"],
            size_bytes=size,
            md5=md5,
            sha256=sha256,
        )
        logger.info(f"rosbag 上传完成：{result.object_key}（{size} bytes）")
        return result

    # ── 三步流程 ──────────────────────────────────────────────────────────
    def _presign(self, client: Any, file_name: str, size: int) -> dict[str, Any]:
        """第 1-2 步：申请预签名地址。"""
        part_count = _part_count(size) if size > _MULTIPART_THRESHOLD else 1
        body = {
            "vehicle_id": self._settings.vehicle_id,
            "bucket": self._settings.rosbag_bucket,
            "data_type": "rosbag",
            "file_name": file_name,
            "file_size": size,
        }
        if part_count > 1:
            body["part_count"] = part_count
        data = self._post(client, _PRESIGN_PATH, body)
        if not isinstance(data, dict) or "object_key" not in data:
            raise UploadError(f"presign 响应缺少 object_key：{data!r}")
        return data

    def _upload_single(self, client: Any, presign: dict[str, Any], file_path: Path) -> None:
        """单次 PUT 直传。"""
        url = presign.get("upload_url")
        if not url:
            raise UploadError("presign 未返回 upload_url")
        with file_path.open("rb") as fh:
            resp = client.put(url, content=fh)
        if resp.status_code >= 300:
            raise UploadError(f"直传失败：HTTP {resp.status_code}")

    def _upload_multipart(self, client: Any, presign: dict[str, Any], file_path: Path) -> list[dict[str, Any]]:
        """分片 PUT，返回 complete 所需的 parts（part_number + etag）。"""
        parts = presign.get("parts") or []
        if not parts:
            raise UploadError("method=multipart 但 presign 未返回 parts")
        etags: list[dict[str, Any]] = []
        with file_path.open("rb") as fh:
            for part in parts:
                chunk = fh.read(_CHUNK)
                resp = client.put(part["upload_url"], content=chunk)
                if resp.status_code >= 300:
                    raise UploadError(f"分片 {part['part_number']} 上传失败：HTTP {resp.status_code}")
                etags.append({"part_number": part["part_number"], "etag": resp.headers.get("ETag", "")})
        return etags

    def _complete(
        self,
        client: Any,
        presign: dict[str, Any],
        size: int,
        md5: str,
        sha256: str,
        data_type: str,
        etags: Optional[list[dict[str, Any]]],
    ) -> None:
        """第 4-6 步：上报完成，服务端做完整性校验。"""
        body: dict[str, Any] = {
            "vehicle_id": self._settings.vehicle_id,
            "bucket": presign["bucket"],
            "object_key": presign["object_key"],
            "size_bytes": size,
            "md5": md5,
            "sha256": sha256,
            "data_type": data_type,
            "timestamp": _now(),
        }
        if etags is not None:
            body["upload_id"] = presign.get("upload_id")
            body["parts"] = etags
        self._post(client, _COMPLETE_PATH, body)

    # ── HTTP 辅助 ──────────────────────────────────────────────────────────
    def _post(self, client: Any, path: str, body: dict[str, Any]) -> Any:
        """向网关 POST 并解统一响应体 `{code,message,data}`，非 0 抛 UploadError。"""
        url = self._settings.gateway_url.rstrip("/") + path
        resp = client.post(url, json=body)
        if resp.status_code >= 400:
            raise UploadError(f"{path} HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"{path} 响应非 JSON：{exc}") from exc
        code = payload.get("code")
        if code not in (0, None):
            raise UploadError(f"{path} 业务错误 code={code}: {payload.get('message')}")
        return payload.get("data")

    def _default_client(self) -> Any:
        """构建带 Bearer JWT 的 httpx 客户端。"""
        import httpx  # noqa: PLC0415

        token = self._settings.jwt_token.get_secret_value() if self._settings.jwt_token else ""
        return httpx.Client(
            timeout=self._settings.upload_timeout_s,
            headers={"Authorization": f"Bearer {token}"},
        )


def _digest_file(path: Path) -> tuple[int, str, str]:
    """流式计算 (size, md5, sha256)。"""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            size += len(block)
            md5.update(block)
            sha256.update(block)
    return size, md5.hexdigest(), sha256.hexdigest()


def _part_count(size: int) -> int:
    """按分片大小估算 part 数。"""
    return max(1, (size + _CHUNK - 1) // _CHUNK)


def _now() -> float:
    """当前 Unix epoch 秒。"""
    import time  # noqa: PLC0415

    return time.time()

"""场景产物写入与预签名上传（``hunter-scene-assets``）。

平台 ``get_result`` 只需要 ``name`` + ``object_key`` 即可登记产物并换发下载地址；
本模块先把产物写到本地暂存目录，再尽力通过 data-collector 预签名上传到
``hunter-scene-assets``。上传失败**不阻断**结果发布（平台侧 presign 失败可降级）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hunter_sim.common.models import CloudSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_PRESIGN_PATH = "/api/v1/data/files/presign"
_COMPLETE_PATH = "/api/v1/data/files/complete"

ClientFactory = Callable[[], Any]


@dataclass(frozen=True)
class ArtifactResult:
    """单个产物元数据（结果视图只暴露 name/object_key/size 等）。"""

    name: str
    object_key: str
    size_bytes: int
    sha256: str
    media_type: str
    uploaded: bool = False

    def to_public(self) -> dict[str, Any]:
        """结果/消息中的产物条目（不含 bucket/URL）。"""
        return {
            "name": self.name,
            "object_key": self.object_key,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }


class ArtifactManager:
    """产物落盘 + 预签名直传。

    Args:
        cloud_settings: HunterCore 接入配置（enabled/JWT/gateway）。
        artifacts_dir: 本地暂存根目录。
        bucket: 目标 Bucket（默认 ``hunter-scene-assets``）。
        sim_id: 仿真实例 ID。
        scene_id: 场景 ID。
        client_factory: 可注入的 httpx 客户端工厂（单测用）。
    """

    def __init__(
        self,
        cloud_settings: CloudSettings,
        artifacts_dir: str,
        bucket: str,
        sim_id: str,
        scene_id: str,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        self._settings = cloud_settings
        self._root = Path(artifacts_dir) / _safe(sim_id) / _safe(scene_id)
        self._bucket = bucket
        self._sim_id = sim_id
        self._scene_id = scene_id
        self._client_factory = client_factory

    @property
    def directory(self) -> Path:
        """本实例产物目录。"""
        return self._root

    def write_json(self, name: str, payload: Any) -> ArtifactResult:
        """写入 JSON 产物并尽力上传。"""
        content = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        return self.write_bytes(name, content, "application/json")

    def write_jsonl(self, name: str, rows: list[dict[str, Any]]) -> ArtifactResult:
        """写入 JSONL 产物并尽力上传。"""
        content = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
        if content:
            content += "\n"
        return self.write_bytes(name, content.encode("utf-8"), "application/x-ndjson")

    def write_text(self, name: str, text: str) -> ArtifactResult:
        """写入文本产物并尽力上传。"""
        return self.write_bytes(name, text.encode("utf-8"), "text/plain")

    def write_bytes(self, name: str, content: bytes, media_type: str) -> ArtifactResult:
        """写入本地文件并尝试上传（异常不外抛）。"""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / _safe(name)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        object_key = f"scenarios/simulations/{_safe(self._sim_id)}/{_safe(self._scene_id)}/{_safe(name)}"
        uploaded_key = self._try_upload(path, name)
        return ArtifactResult(
            name=name,
            object_key=uploaded_key or object_key,
            size_bytes=len(content),
            sha256=digest,
            media_type=media_type,
            uploaded=uploaded_key is not None,
        )

    # ── 上传（best effort）────────────────────────────────────────────────

    def _try_upload(self, path: Path, file_name: str) -> Optional[str]:
        """预签名直传；未启用/失败返回 None（只保留本地产物）。"""
        if not self._settings.enabled:
            return None
        token = (
            self._settings.jwt_token.get_secret_value()
            if self._settings.jwt_token is not None
            else ""
        )
        if not token:
            logger.debug("HUNTER_CORE_JWT_TOKEN 未配置，产物仅落本地")
            return None
        client = None
        try:
            client = self._client_factory() if self._client_factory else self._default_client(token)
            size = path.stat().st_size
            presign = self._presign(client, file_name, size)
            object_key = presign.get("object_key")
            if not object_key:
                return None
            upload_url = presign.get("upload_url")
            if upload_url:
                with path.open("rb") as handle:
                    response = client.put(upload_url, content=handle)
                if getattr(response, "status_code", 500) >= 300:
                    logger.warning(
                        f"产物直传失败 {file_name}: HTTP {response.status_code}"
                    )
                    return None
            self._complete(client, presign, path)
            logger.info(f"产物已上传：{object_key}")
            return str(object_key)
        except Exception as exc:  # noqa: BLE001  上传失败不阻断结果
            logger.warning(f"产物上传失败（保留本地副本）：{type(exc).__name__}: {exc}")
            return None
        finally:
            if client is not None and self._client_factory is None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

    def _presign(self, client: Any, file_name: str, size: int) -> dict[str, Any]:
        """申请预签名地址。"""
        body = {
            "vehicle_id": f"SIM-{self._sim_id}" if self._sim_id else "SIM-unknown",
            "bucket": self._bucket,
            "data_type": "other",
            "file_name": file_name,
            "file_size": size,
        }
        data = self._post(client, _PRESIGN_PATH, body)
        return data if isinstance(data, dict) else {}

    def _complete(self, client: Any, presign: dict[str, Any], path: Path) -> None:
        """上报完成（服务端做完整性校验）；失败仅告警。"""
        content = path.read_bytes()
        body = {
            "vehicle_id": f"SIM-{self._sim_id}" if self._sim_id else "SIM-unknown",
            "bucket": presign.get("bucket", self._bucket),
            "object_key": presign.get("object_key"),
            "size_bytes": len(content),
            "md5": hashlib.md5(content).hexdigest(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "data_type": "other",
            "timestamp": time.time(),
        }
        try:
            self._post(client, _COMPLETE_PATH, body)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"产物 complete 上报失败（对象已直传）：{exc}")

    def _post(self, client: Any, path: str, body: dict[str, Any]) -> Any:
        """POST 到 data-collector 并解统一响应体。"""
        url = self._settings.gateway_url.rstrip("/") + path
        response = client.post(url, json=body)
        if getattr(response, "status_code", 500) >= 400:
            raise RuntimeError(f"{path} HTTP {response.status_code}")
        payload = response.json()
        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in (0, None):
            raise RuntimeError(f"{path} code={code}: {payload.get('message')}")
        return payload.get("data") if isinstance(payload, dict) else None

    def _default_client(self, token: str) -> Any:
        """构建带 Bearer JWT 的 httpx 客户端。"""
        import httpx  # noqa: PLC0415

        return httpx.Client(
            timeout=self._settings.upload_timeout_s,
            headers={"Authorization": f"Bearer {token}"},
        )


def _safe(value: str) -> str:
    """路径/对象键安全化（只保留字母数字与 ``._-``）。"""
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in value)
    return cleaned or "unknown"


__all__ = ["ArtifactManager", "ArtifactResult"]

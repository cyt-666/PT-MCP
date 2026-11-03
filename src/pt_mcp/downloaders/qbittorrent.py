"""qBittorrent Web API 适配器与单次提交边界。"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any, Callable

import qbittorrentapi
import requests
from qbittorrentapi.definitions import APINames
from qbittorrentapi.exceptions import (
    Conflict409Error,
    HTTP4XXError,
    HTTP5XXError,
    HTTP403Error,
    HTTP401Error,
)
from urllib3.util import Retry

from pt_mcp.credentials import CredentialProvider, SiteCredentials
from pt_mcp.errors import (
    CredentialMissing,
    DownloaderError,
    SubmissionTransportUnknown,
)
from pt_mcp.identity import download_hashes
from pt_mcp.models import DownloadAck, DownloadView, DownloaderConfig

logger = logging.getLogger(__name__)
_HEX_HASH_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_NO_RETRY = Retry(
    total=0,
    connect=0,
    read=0,
    redirect=0,
    status=0,
    other=0,
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
)


class _SingleAddRequestClient(qbittorrentapi.Client):
    """为 torrent add 关闭库级 request-manager 重试。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化官方客户端，并记录 torrent add 是否已发出。"""
        self.torrent_add_attempted = False
        super().__init__(*args, **kwargs)

    def _auth_request(
        self,
        http_method: str,
        api_namespace: APINames | str,
        api_method: str,
        **kwargs: Any,
    ) -> Any:
        """对 torrents/add 直接执行一次底层请求，其他调用保留库行为。

        qbittorrent-api 2026.6.0 的 ``_request_manager`` 至少执行两轮请求，
        包括 POST。这里仅绕过 torrents/add 的重试和自动重登分支；客户端其余
        GET 仍使用官方 request manager。
        """
        is_add = (
            http_method.lower() == "post"
            and api_namespace in {APINames.Torrents, APINames.Torrents.value}
            and api_method.lower() == "add"
        )
        if not is_add:
            return super()._auth_request(
                http_method=http_method,
                api_namespace=api_namespace,
                api_method=api_method,
                **kwargs,
            )

        # 这些参数由 _request_manager 消费，_request 不接受它们。
        kwargs.pop("_retry_backoff_factor", None)
        kwargs.pop("version_introduced", None)
        kwargs.pop("version_removed", None)
        self.torrent_add_attempted = True
        return self._request(
            http_method=http_method,
            api_namespace=api_namespace,
            api_method=api_method,
            **kwargs,
        )


class QBittorrentDownloader:
    """将稳定 hash 查询和 torrent add 映射到 qBittorrent Web API。"""

    def __init__(
        self,
        config: DownloaderConfig,
        credentials: CredentialProvider,
        *,
        client: _SingleAddRequestClient | None = None,
        client_factory: Callable[..., _SingleAddRequestClient] = _SingleAddRequestClient,
    ) -> None:
        """绑定服务端配置、懒加载凭据和可替换的官方 qB 客户端。"""
        self._config = config
        self._credential_provider = credentials
        self._client = client
        self._client_factory = client_factory
        self._client_lock = asyncio.Lock()
        self._request_lock = threading.Lock()

    async def list_downloads(
        self,
        *,
        hashes: Iterable[str] | None = None,
    ) -> list[DownloadView]:
        """读取下载任务，可按 v1、v2 或截断 v2 hash 过滤。"""
        query_hashes = self._query_hashes(hashes) if hashes is not None else None
        if query_hashes == set():
            return []
        client = await self._get_client()
        return await asyncio.to_thread(self._list_downloads_sync, client, query_hashes)

    async def get_by_hashes(self, hashes: set[str]) -> list[DownloadView]:
        """按 hash 查询并在本地复核返回的身份集合。"""
        requested = self._normalize_hashes(hashes)
        if not requested:
            return []
        candidates = await self.list_downloads(hashes=requested)
        return [item for item in candidates if requested.intersection(item.hashes)]

    async def add(
        self,
        *,
        content: bytes | str,
        save_path: str | None,
        paused: bool = False,
    ) -> DownloadAck:
        """提交 torrent 文件或 magnet；该 POST 最多发送一次。"""
        hashes = download_hashes(content)
        if not hashes:
            raise DownloaderError(
                "资源缺少可核验的 torrent hash",
                code="resource_identity_unknown",
            )
        if isinstance(content, str) and not content.startswith("magnet:"):
            raise DownloaderError(
                "下载器只接受已解析的 magnet 或 torrent 文件",
                code="resource_locator_invalid",
            )

        client = await self._get_client()
        try:
            return await asyncio.to_thread(
                self._add_sync,
                client,
                content,
                save_path,
                paused,
            )
        except (SubmissionTransportUnknown, DownloaderError):
            raise
        except Exception as error:  # noqa: BLE001
            attempted = bool(getattr(client, "torrent_add_attempted", False))
            if attempted:
                raise SubmissionTransportUnknown() from None
            logger.error(
                "qBittorrent add 前请求失败：error_type=%s",
                type(error).__name__,
            )
            raise DownloaderError(
                "无法连接 qBittorrent；尚未提交下载",
                code="downloader_unavailable",
            ) from None

    async def aclose(self) -> None:
        """关闭本地 HTTP Session，不向 qBittorrent 发送请求。"""
        client = self._client
        self._client = None
        if client is not None:
            await asyncio.to_thread(client._trigger_session_initialization)

    async def _get_client(self) -> _SingleAddRequestClient:
        """按需解析下载器凭据并创建客户端，不在启动阶段连接 qB。"""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            credentials = await self._credential_provider.get(self._config.credential_ref)
            if credentials is None:
                raise CredentialMissing("未配置 qBittorrent 凭据")
            self._client = self._make_client(credentials)
            return self._client

    def _make_client(self, credentials: SiteCredentials) -> _SingleAddRequestClient:
        """从服务端 SecretStr 创建官方 qBittorrent 客户端。"""
        if not self._config.host:
            raise DownloaderError(
                "qBittorrent 地址尚未配置",
                code="downloader_not_configured",
            )
        api_key = self._secret_value(getattr(credentials, "api_key", None))
        username = self._secret_value(getattr(credentials, "username", None))
        password = self._secret_value(getattr(credentials, "password", None))
        if not api_key and not (username and password):
            raise CredentialMissing("qBittorrent 凭据引用缺少 API Key 或用户名和密码")
        client = self._client_factory(
            host=self._config.host or "",
            port=self._config.port,
            username=username,
            password=password,
            api_key=api_key,
            VERIFY_WEBUI_CERTIFICATE=self._config.verify_tls,
            REQUESTS_ARGS={
                "timeout": self._config.timeout_s,
                "allow_redirects": False,
            },
            HTTPADAPTER_ARGS={"max_retries": _NO_RETRY},
        )
        # qB host 来自服务端配置，不跟随进程环境代理或站点重定向。
        client._session.trust_env = False
        return client

    @staticmethod
    def _secret_value(value: Any) -> str | None:
        """只在当前客户端构造时解出 SecretStr。"""
        if value is None:
            return None
        getter = getattr(value, "get_secret_value", None)
        return getter() if callable(getter) else str(value)

    @staticmethod
    def _normalize_hashes(values: Iterable[str]) -> set[str]:
        """只保留规范的 v1、完整 v2 和 qB 截断 v2 十六进制 hash。"""
        normalized: set[str] = set()
        for value in values:
            candidate = str(value).strip().lower()
            if _HEX_HASH_RE.fullmatch(candidate):
                normalized.add(candidate)
                if len(candidate) == 64:
                    normalized.add(candidate[:40])
        return normalized

    @classmethod
    def _query_hashes(cls, values: Iterable[str]) -> set[str]:
        """为 qB 查询同时包含 v2 完整值与 40 位兼容别名。"""
        return cls._normalize_hashes(values)

    def _list_downloads_sync(
        self,
        client: _SingleAddRequestClient,
        hashes: set[str] | None,
    ) -> list[DownloadView]:
        """执行 qB 查询并投影为不依赖标题的公共任务模型。"""
        try:
            with self._request_lock:
                torrents = client.torrents_info(
                    torrent_hashes=sorted(hashes) if hashes is not None else None,
                )
            return [view for torrent in torrents if (view := self._project_torrent(torrent)) is not None]
        except Exception as error:  # noqa: BLE001
            logger.error(
                "qBittorrent 查询失败：error_type=%s",
                type(error).__name__,
            )
            raise DownloaderError(
                "qBittorrent 查询失败",
                code="downloader_unavailable",
            ) from None

    @staticmethod
    def _read_field(torrent: Any, name: str) -> Any:
        """读取 qBittorrent API 返回的 mapping 或对象字段。"""
        if isinstance(torrent, Mapping):
            return torrent.get(name)
        return getattr(torrent, name, None)

    @classmethod
    def _project_torrent(cls, torrent: Any) -> DownloadView | None:
        """使用 qBittorrent 明确返回的 hash 字段生成任务视图。"""
        hashes: set[str] = set()
        for field_name in ("hash", "infohash_v1", "infohash_v2"):
            raw_hash = cls._read_field(torrent, field_name)
            if isinstance(raw_hash, str):
                hashes.update(cls._normalize_hashes([raw_hash]))
        if not hashes:
            return None

        raw_progress = cls._read_field(torrent, "progress")
        try:
            progress = float(raw_progress) if raw_progress is not None else None
        except (TypeError, ValueError):
            progress = None
        if progress is not None and 0 <= progress <= 1:
            progress *= 100
        if progress is not None and not 0 <= progress <= 100:
            progress = None

        raw_size = cls._read_field(torrent, "size")
        try:
            size_bytes = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        if size_bytes is not None and size_bytes < 0:
            size_bytes = None

        raw_state = cls._read_field(torrent, "state")
        raw_name = cls._read_field(torrent, "name")
        raw_dir = cls._read_field(torrent, "save_path")
        return DownloadView(
            hashes=sorted(hashes),
            name=raw_name if isinstance(raw_name, str) and raw_name else None,
            state=str(raw_state) if raw_state is not None else None,
            progress=progress,
            size_bytes=size_bytes,
            download_dir=raw_dir if isinstance(raw_dir, str) and raw_dir else None,
        )

    def _add_sync(
        self,
        client: _SingleAddRequestClient,
        content: bytes | str,
        save_path: str | None,
        paused: bool,
    ) -> DownloadAck:
        """提交一次 torrent add 并读取 qB 的明确回执。"""
        client.torrent_add_attempted = False
        try:
            with self._request_lock:
                if getattr(client, "_api_key", "") == "" and getattr(client, "username", ""):
                    client.auth_log_in()
                if isinstance(content, bytes):
                    response = client.torrents_add(
                        torrent_files=content,
                        save_path=save_path,
                        is_paused=paused,
                    )
                else:
                    response = client.torrents_add(
                        urls=content,
                        save_path=save_path,
                        is_paused=paused,
                    )
        except Conflict409Error:
            # qB 409 可能表示资源已经存在；service 随后按 hash 只读核验。
            return DownloadAck(accepted=False)
        except (HTTP401Error, HTTP403Error):
            raise DownloaderError(
                "qBittorrent 拒绝认证",
                code="downloader_auth_failed",
            ) from None
        except HTTP5XXError:
            if client.torrent_add_attempted:
                raise SubmissionTransportUnknown() from None
            raise DownloaderError(
                "qBittorrent 暂不可用；尚未提交下载",
                code="downloader_unavailable",
            ) from None
        except HTTP4XXError:
            raise DownloaderError(
                "qBittorrent 拒绝该下载请求",
                code="downloader_rejected",
            ) from None
        except requests.exceptions.RequestException:
            if client.torrent_add_attempted:
                raise SubmissionTransportUnknown() from None
            raise DownloaderError(
                "无法连接 qBittorrent；尚未提交下载",
                code="downloader_unavailable",
            ) from None

        if isinstance(response, str):
            normalized = response.strip().lower()
            if normalized == "ok.":
                return DownloadAck(accepted=True)
            if normalized == "fails.":
                return DownloadAck(accepted=False)
            raise SubmissionTransportUnknown()

        if isinstance(response, Mapping):
            try:
                success_count = int(response.get("success_count", 0) or 0)
                pending_count = int(response.get("pending_count", 0) or 0)
                failure_count = int(response.get("failure_count", 0) or 0)
            except (TypeError, ValueError):
                raise SubmissionTransportUnknown() from None
            if success_count > 0 or pending_count > 0:
                return DownloadAck(accepted=True)
            if failure_count > 0:
                return DownloadAck(accepted=False)
        raise SubmissionTransportUnknown()


__all__ = ["QBittorrentDownloader"]

"""离线验证 qBittorrent hash 查询及 torrent add 单次 POST 边界。"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
import requests
from qbittorrentapi.request import QbittorrentSession
from pydantic import SecretStr
from pt_mcp.credentials import SiteCredentials
from pt_mcp.downloaders.qbittorrent import (
    QBittorrentDownloader,
    _SingleAddRequestClient,
)
from pt_mcp.errors import DownloaderError, SubmissionTransportUnknown
from pt_mcp.models import DownloadAck, DownloaderConfig


class _Credentials:
    """返回仅供测试使用的 qBittorrent API Key。"""

    async def get(self, _reference: str | None) -> SiteCredentials:
        return SiteCredentials(api_key=SecretStr("test-api-key"))


class _UserPasswordCredentials:
    """返回仅供测试使用的 qBittorrent 用户凭据。"""

    async def get(self, _reference: str | None) -> SiteCredentials:
        return SiteCredentials(
            username=SecretStr("test-user"),
            password=SecretStr("test-password"),
        )


def _config(**kwargs: Any) -> DownloaderConfig:
    """构造不包含真实凭据的 qBittorrent 配置。"""
    return DownloaderConfig(
        downloader_id="default",
        host="qbittorrent.example.invalid",
        port=8080,
        credential_ref="test-qb",
        **kwargs,
    )


def test_download_projection_preserves_v2_full_and_truncated_hashes() -> None:
    """qB v2 任务应同时保留完整身份和 40 位兼容别名。"""
    v2 = hashlib.sha256(b"sample v2 info").hexdigest()

    view = QBittorrentDownloader._project_torrent(
        {
            "hash": v2[:40],
            "infohash_v1": "",
            "infohash_v2": v2,
            "name": "Same title",
            "state": "downloading",
            "progress": 0.5,
            "size": 128,
            "save_path": "/downloads/books",
        }
    )

    assert view is not None
    assert set(view.hashes) == {v2, v2[:40]}
    assert view.progress == 50
    assert view.size_bytes == 128
    assert view.download_dir == "/downloads/books"


def test_get_by_hashes_filters_same_title_by_identity() -> None:
    """同标题任务不能替代 hash 不匹配的资源。"""
    v1 = hashlib.sha1(b"right torrent").hexdigest()
    other = hashlib.sha1(b"different torrent").hexdigest()

    class FakeClient:
        """返回两个同名任务，测试由 hash 而不是标题筛选。"""

        def torrents_info(self, *, torrent_hashes):
            assert v1 in torrent_hashes
            return [
                {"hash": v1, "name": "Same title", "state": "downloading"},
                {"hash": other, "name": "Same title", "state": "completed"},
            ]

    downloader = QBittorrentDownloader(
        _config(),
        _Credentials(),
        client=FakeClient(),  # type: ignore[arg-type]
    )
    results = asyncio.run(downloader.get_by_hashes({v1}))

    assert len(results) == 1
    assert results[0].hashes == [v1]


def test_add_timeout_sends_torrent_post_once_with_real_library_session(monkeypatch) -> None:
    """真实 qbittorrent-api Session.send 断线后不重放 torrents/add。"""
    client = _SingleAddRequestClient(
        host="http://qbittorrent.example.invalid:8080",
        api_key="test-api-key",
        HTTPADAPTER_ARGS={"max_retries": 0},
    )
    client.app_web_api_version = lambda: "2.14.0"  # type: ignore[method-assign]
    calls: list[requests.PreparedRequest] = []
    request_paths: list[tuple[str, str]] = []
    request_error_types: list[str] = []

    def fake_send(
        _session: QbittorrentSession,
        request: requests.PreparedRequest,
        **_kwargs: Any,
    ) -> requests.Response:
        from urllib.parse import urlsplit

        request_paths.append((request.method, urlsplit(request.url).path))
        if request.url.endswith("/api/v2/torrents/add"):
            calls.append(request)
            raise requests.ConnectionError("connection closed after request")
        response = requests.Response()
        response.status_code = 200
        response._content = b"ok"
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(QbittorrentSession, "send", fake_send)
    original_request = client._request

    def track_request(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_request(*args, **kwargs)
        except Exception as error:  # noqa: BLE001
            request_error_types.append(type(error).__name__)
            raise

    monkeypatch.setattr(client, "_request", track_request)
    downloader = QBittorrentDownloader(
        _config(),
        _Credentials(),
        client=client,
    )
    magnet = "magnet:?xt=urn:btih:" + "ab" * 20

    with pytest.raises(SubmissionTransportUnknown):
        asyncio.run(downloader.add(content=magnet, save_path="/downloads/books"))

    assert len(calls) == 1, (request_paths, request_error_types)
    assert request_paths[-1] == ("POST", "/api/v2/torrents/add")
    assert calls[0].method == "POST"
    assert client._session.get_adapter("http://").max_retries.total == 0


def test_add_accepts_only_known_qbittorrent_acknowledgements() -> None:
    """qBittorrent 的明确成功和失败文本分别投影为 ack 状态。"""

    class FakeClient:
        """避免连接真实 qBittorrent。"""

        torrent_add_attempted = False
        _api_key = "test"
        username = ""

        def torrents_add(self, **_kwargs: Any) -> str:
            self.torrent_add_attempted = True
            return "Ok."

    downloader = QBittorrentDownloader(
        _config(),
        _Credentials(),
        client=FakeClient(),  # type: ignore[arg-type]
    )
    ack = asyncio.run(
        downloader.add(
            content="magnet:?xt=urn:btih:" + "cd" * 20,
            save_path="/downloads/books",
        )
    )

    assert ack == DownloadAck(accepted=True)


def test_add_failure_text_is_not_treated_as_success() -> None:
    """qBittorrent 的显式 Fails. 回执不能被当作已接受。"""

    class FakeClient:
        """返回 qBittorrent 旧 Web API 的明确拒绝文本。"""

        torrent_add_attempted = False
        _api_key = "test"
        username = ""

        def torrents_add(self, **_kwargs: Any) -> str:
            self.torrent_add_attempted = True
            return "Fails."

    downloader = QBittorrentDownloader(
        _config(),
        _Credentials(),
        client=FakeClient(),  # type: ignore[arg-type]
    )

    ack = asyncio.run(
        downloader.add(
            content="magnet:?xt=urn:btih:" + "ef" * 20,
            save_path="/downloads/books",
        )
    )

    assert ack == DownloadAck(accepted=False)


def test_add_preflight_failure_is_not_reported_as_post_unknown() -> None:
    """qBittorrent version preflight 失败且未发 add POST 时可明确报告连接错误。"""

    class FakeClient:
        """在执行 add 之前模拟网络错误。"""

        torrent_add_attempted = False
        _api_key = "test"
        username = ""

        def torrents_add(self, **_kwargs: Any) -> str:
            raise requests.ConnectionError("offline fake")

    downloader = QBittorrentDownloader(
        _config(),
        _Credentials(),
        client=FakeClient(),  # type: ignore[arg-type]
    )

    with pytest.raises(DownloaderError) as error:
        asyncio.run(
            downloader.add(
                content="magnet:?xt=urn:btih:" + "12" * 20,
                save_path="/downloads/books",
            )
        )

    assert error.value.code == "downloader_unavailable"


def test_configured_client_disables_environment_proxy_redirects_and_adapter_retries() -> None:
    """新建客户端使用服务端凭据，且禁止隐式代理、重定向和 HTTPAdapter 重试。"""
    downloader = QBittorrentDownloader(_config(), _UserPasswordCredentials())
    client = asyncio.run(downloader._get_client())

    assert client._session.trust_env is False
    assert client._REQUESTS_ARGS["allow_redirects"] is False
    assert client._session.get_adapter("http://").max_retries.total == 0

"""本地 ASGI MCP 测试，不连接真实站点或下载器。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from pt_mcp.config import AppConfig
from pt_mcp.errors import SubmissionTransportUnknown
from pt_mcp.models import (
    DownloadView,
    DownloaderSummary,
    NativeCategory,
    ResourceView,
    RssReadPage,
    SearchPage,
    SiteSummary,
    SubmissionReceipt,
)
from pt_mcp.server import BearerAuthASGI, create_app

TOKEN = "test-token-not-for-production"
BASE_URL = "http://127.0.0.1:8000"


class FakeService:
    """提供纯内存数据，确保 MCP 测试不访问站点和 qBittorrent。"""

    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def aclose(self) -> None:
        self.closed = True

    async def list_sites(self) -> list[SiteSummary]:
        self.calls.append(("list_sites", {}))
        return []

    async def list_categories(self, site_id: str) -> list[NativeCategory]:
        self.calls.append(("list_categories", {"site_id": site_id}))
        return [NativeCategory(id="ebooks", label="电子书")]

    async def search_torrents(
        self,
        site_id: str,
        keyword: str,
        category_ids: list[str] | None = None,
        page: int = 1,
    ) -> SearchPage[ResourceView]:
        self.calls.append(
            (
                "search_torrents",
                {
                    "site_id": site_id,
                    "keyword": keyword,
                    "category_ids": category_ids,
                    "page": page,
                },
            )
        )
        return SearchPage(items=[], page=page, has_more=False)

    async def read_rss(
        self, site_id: str, cursor: str | None = None, limit: int = 100
    ) -> RssReadPage:
        self.calls.append(
            ("read_rss", {"site_id": site_id, "cursor": cursor, "limit": limit})
        )
        return RssReadPage(items=[], cursor="opaque-next", has_more=False)

    async def list_downloaders(self) -> list[DownloaderSummary]:
        self.calls.append(("list_downloaders", {}))
        return []

    async def add_download(
        self,
        resource_id: str,
        request_id: str,
        downloader_id: str = "default",
        save_profile_id: str | None = None,
        paused: bool = False,
    ) -> SubmissionReceipt:
        self.calls.append(
            (
                "add_download",
                {
                    "resource_id": resource_id,
                    "request_id": request_id,
                    "downloader_id": downloader_id,
                    "save_profile_id": save_profile_id,
                    "paused": paused,
                },
            )
        )
        now = datetime.now(UTC)
        return SubmissionReceipt(
            request_id=request_id,
            payload_digest="a" * 64,
            state="accepted",
            downloader_id=downloader_id,
            hashes=["b" * 40],
            task=DownloadView(hashes=["b" * 40], state="downloading"),
            created_at=now,
            updated_at=now,
        )

    async def get_submission(self, request_id: str) -> SubmissionReceipt | None:
        self.calls.append(("get_submission", {"request_id": request_id}))
        raise SubmissionTransportUnknown()

    async def list_downloads(
        self, downloader_id: str = "default", hashes: list[str] | None = None
    ) -> list[DownloadView]:
        self.calls.append(
            ("list_downloads", {"downloader_id": downloader_id, "hashes": hashes})
        )
        return []


def _config(*, token: str | None = TOKEN) -> AppConfig:
    """构造只允许本地测试 Host 和 Origin 的配置。"""
    return AppConfig(
        bearer_token=SecretStr(token) if token is not None else None,
        data_dir=Path("/tmp/pt-mcp-test"),
        sites=[],
        downloaders=[],
        allowed_hosts=["127.0.0.1:8000"],
        allowed_origins=[BASE_URL],
    )


@asynccontextmanager
async def _http_client(app: BearerAuthASGI) -> AsyncIterator[httpx.AsyncClient]:
    """手动进入 MCP lifespan，并用纯 ASGI transport 发 HTTP 请求。"""
    inner_app = app.application
    async with inner_app.router.lifespan_context(inner_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
        ) as client:
            yield client


def test_missing_bearer_token_does_not_authorize_any_http_method() -> None:
    """缺少凭据时所有 HTTP 方法都在 MCP 协议处理前被拒绝。"""
    service = FakeService()
    app = create_app(_config(), service)

    async def run() -> None:
        async with _http_client(app) as client:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
                response = await client.request(method, "/mcp")
                assert response.status_code == 401
                assert "www-authenticate" in response.headers

    asyncio.run(run())
    assert service.calls == []


def test_unconfigured_server_token_rejects_even_a_supplied_bearer() -> None:
    """未配置服务端令牌时，即使请求自带令牌也无法进入 MCP。"""
    service = FakeService()
    app = create_app(_config(token=None), service)

    async def run() -> None:
        async with _http_client(app) as client:
            response = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 401

    asyncio.run(run())
    assert service.calls == []


def test_sdk_host_origin_guard_rejects_unlisted_origin() -> None:
    """合法 Bearer 不能绕过 MCP SDK 的 Host 和 Origin allowlist。"""
    service = FakeService()
    app = create_app(_config(), service)

    async def run() -> None:
        async with _http_client(app) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Host": "evil.example.invalid:8000",
                    "Origin": "http://evil.example.invalid:8000",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 421

    asyncio.run(run())
    assert service.calls == []


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic test", "Bearer wrong", f"Bearer {TOKEN} ", f"Bearer {TOKEN},bad"],
)
def test_invalid_authorization_is_rejected(authorization: str | None) -> None:
    """无效、空白或歧义 Bearer 头不会进入 MCP 服务。"""
    service = FakeService()
    app = create_app(_config(), service)

    async def run() -> None:
        async with _http_client(app) as client:
            headers = {"Authorization": authorization} if authorization else {}
            response = await client.post("/mcp", headers=headers, json={})
            assert response.status_code == 401

    asyncio.run(run())
    assert service.calls == []


def test_empty_configuration_discovers_fixed_typed_tool_directory() -> None:
    """空站点和下载器配置仍可发现 8 个固定工具并调用空列表。"""
    service = FakeService()
    app = create_app(_config(), service)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def run() -> None:
        async with _http_client(app) as client:
            initialize = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "local-test", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200

            initialized = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert initialized.status_code in {202, 204}

            listed = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert listed.status_code == 200
            tools = listed.json()["result"]["tools"]
            assert {tool["name"] for tool in tools} == {
                "list_sites",
                "list_categories",
                "search_torrents",
                "read_rss",
                "list_downloaders",
                "add_download",
                "get_submission",
                "list_downloads",
            }
            assert len(tools) == 8
            schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
            search_schema = schemas["search_torrents"]
            assert search_schema["properties"]["page"]["default"] == 1
            assert "category_ids" in search_schema["properties"]
            assert "mtype" not in search_schema["properties"]
            add_schema = schemas["add_download"]
            assert set(add_schema["required"]) == {"resource_id", "request_id"}
            assert add_schema["properties"]["paused"]["default"] is False
            all_property_names = {
                name
                for schema in schemas.values()
                for name in schema.get("properties", {})
            }
            assert not all_property_names.intersection(
                {"url", "torrent", "cookie", "api_key", "password", "save_path"}
            )

            called = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_sites", "arguments": {}},
                },
            )
            assert called.status_code == 200
            result = called.json()["result"]
            assert result.get("isError", False) is False
            assert "structuredContent" in result
            assert service.calls == [("list_sites", {})]

    asyncio.run(run())
    assert service.closed is True


def test_tool_errors_are_safe_and_preserve_unknown_submission_code() -> None:
    """预期业务错误返回安全代码，不泄漏异常文本或 URL。"""
    service = FakeService()
    app = create_app(_config(), service)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def run() -> None:
        async with _http_client(app) as client:
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "get_submission",
                        "arguments": {"request_id": "request-1"},
                    },
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            text = "".join(item["text"] for item in result["content"])
            assert result["isError"] is True
            assert "submission_unknown" in text
            assert "Cookie" not in text
            assert "https://" not in text

    asyncio.run(run())


def test_site_categories_and_download_arguments_remain_native() -> None:
    """工具包装保留站点分类和稳定请求 ID，不添加影视类型参数。"""
    service = FakeService()
    app = create_app(_config(), service)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def call_tool(client: httpx.AsyncClient, request_id: int, name: str, arguments: dict) -> dict:
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200
        return response.json()["result"]

    async def run() -> None:
        async with _http_client(app) as client:
            await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "local-test", "version": "1"},
                    },
                },
            )
            category_result = await call_tool(
                client, 2, "list_categories", {"site_id": "site-a"}
            )
            assert category_result["isError"] is False
            assert service.calls[-1] == ("list_categories", {"site_id": "site-a"})

            search_result = await call_tool(
                client,
                3,
                "search_torrents",
                {
                    "site_id": "site-a",
                    "keyword": "sample book",
                    "category_ids": ["ebooks"],
                    "page": 2,
                },
            )
            assert search_result["isError"] is False
            assert service.calls[-1] == (
                "search_torrents",
                {
                    "site_id": "site-a",
                    "keyword": "sample book",
                    "category_ids": ["ebooks"],
                    "page": 2,
                },
            )

            add_result = await call_tool(
                client,
                4,
                "add_download",
                {
                    "resource_id": "res_opaque",
                    "request_id": "stable-request",
                    "downloader_id": "default",
                    "save_profile_id": "ebooks",
                    "paused": False,
                },
            )
            assert add_result["isError"] is False
            assert service.calls[-1] == (
                "add_download",
                {
                    "resource_id": "res_opaque",
                    "request_id": "stable-request",
                    "downloader_id": "default",
                    "save_profile_id": "ebooks",
                    "paused": False,
                },
            )

    asyncio.run(run())

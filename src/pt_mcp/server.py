"""Streamable HTTP MCP 服务及固定工具目录。"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from pt_mcp import __version__
from pt_mcp.config import AppConfig
from pt_mcp.errors import AppError
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
from pt_mcp.service import PtMcpService

logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT")
_SAFE_INTERNAL_ERROR = "internal_error: 操作未完成。请先查询状态，再决定是否重试。"


class BearerAuthASGI:
    """在 MCP ASGI 应用前验证固定 Bearer Token。"""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        """保存下游 ASGI 应用和服务端令牌。"""
        self.application = app
        self._token = token.strip() if token and token.strip() else None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """认证所有 HTTP 方法，并原样转发 lifespan 与已认证请求。"""
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        supplied = self._bearer_value(scope)
        if self._token is None or supplied is None or not hmac.compare_digest(
            supplied.encode("utf-8"), self._token.encode("utf-8")
        ):
            await self._reject(send)
            return

        await self.application(scope, receive, send)

    @staticmethod
    def _bearer_value(scope: dict[str, Any]) -> str | None:
        """只接受格式完整的 Authorization Bearer 头。"""
        values = [
            header_value.decode("latin-1")
            for header_name, header_value in scope.get("headers", [])
            if header_name.lower() == b"authorization"
        ]
        if len(values) != 1:
            return None
        value = values[0]
        scheme, separator, token = value.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
            return None
        return token

    @staticmethod
    async def _reject(send: Any) -> None:
        """返回不含请求值和凭据信息的标准 401 响应。"""
        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"www-authenticate", b'Bearer realm="pt-mcp"'),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _service_lifespan(service: PtMcpService) -> Callable[[MCPServer], Any]:
    """在 MCP lifespan 结束时释放服务的 HTTP 和存储资源。"""

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.aclose()

    return lifespan


def _tool_call(service: PtMcpService, tool_name: str, call: Callable[[], Any]) -> Any:
    """构造一个将业务错误脱敏后交给模型的异步工具调用。"""

    async def invoke() -> Any:
        try:
            return await call()
        except AppError as error:
            raise ToolError(f"{error.code}: {error.public_message}") from None
        except Exception as error:  # noqa: BLE001
            logger.error(
                "MCP 工具执行失败：tool=%s error_type=%s",
                tool_name,
                type(error).__name__,
            )
            raise ToolError(_SAFE_INTERNAL_ERROR) from None

    return invoke


def create_mcp_server(service: PtMcpService) -> MCPServer:
    """构造包含固定 8 个工具的 MCP Server。"""
    server = MCPServer(
        "pt-mcp",
        version=__version__,
        lifespan=_service_lifespan(service),
    )

    @server.tool()
    async def list_sites() -> list[SiteSummary]:
        """列出服务端配置的站点及其公开能力。"""
        return await _tool_call(service, "list_sites", service.list_sites)()

    @server.tool()
    async def list_categories(site_id: str) -> list[NativeCategory]:
        """列出站点原生分类；分类标识保持站点自己的命名空间。"""
        return await _tool_call(
            service,
            "list_categories",
            lambda: service.list_categories(site_id),
        )()

    @server.tool()
    async def search_torrents(
        site_id: str,
        keyword: str,
        category_ids: list[str] | None = None,
        page: int = 1,
    ) -> SearchPage[ResourceView]:
        """在一个站点内按关键词和原生分类搜索资源。页码从 1 开始。"""
        return await _tool_call(
            service,
            "search_torrents",
            lambda: service.search_torrents(
                site_id=site_id,
                keyword=keyword,
                category_ids=category_ids,
                page=page,
            ),
        )()

    @server.tool()
    async def read_rss(
        site_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RssReadPage:
        """读取当前调用方在指定站点的 RSS 快照并返回不透明后续游标。

        游标只用于同一站点、同一调用方的后续读取；服务不会推进共享站点游标。
        """
        return await _tool_call(
            service,
            "read_rss",
            lambda: service.read_rss(site_id=site_id, cursor=cursor, limit=limit),
        )()

    @server.tool()
    async def list_downloaders() -> list[DownloaderSummary]:
        """列出服务端配置的下载器，不返回连接凭据。"""
        return await _tool_call(
            service,
            "list_downloaders",
            service.list_downloaders,
        )()

    @server.tool()
    async def add_download(
        resource_id: str,
        request_id: str,
        downloader_id: str = "default",
        save_profile_id: str | None = None,
        paused: bool = False,
    ) -> SubmissionReceipt:
        """添加搜索结果中的资源；新任务默认立即开始。

        同一用户意图的重试必须沿用 request_id。若回执未知，先查询状态，不要换
        request_id 再次提交。paused 只控制新建任务，不会解除已有任务的暂停。
        """
        return await _tool_call(
            service,
            "add_download",
            lambda: service.add_download(
                resource_id=resource_id,
                request_id=request_id,
                downloader_id=downloader_id,
                save_profile_id=save_profile_id,
                paused=paused,
            ),
        )()

    @server.tool()
    async def get_submission(request_id: str) -> SubmissionReceipt | None:
        """查询持久提交回执；unknown 状态须先按 hash 核验，不要直接重发。"""
        return await _tool_call(
            service,
            "get_submission",
            lambda: service.get_submission(request_id),
        )()

    @server.tool()
    async def list_downloads(
        downloader_id: str = "default",
        hashes: list[str] | None = None,
    ) -> list[DownloadView]:
        """查询下载器任务，可按已知种子 hash 限定结果。"""
        return await _tool_call(
            service,
            "list_downloads",
            lambda: service.list_downloads(downloader_id=downloader_id, hashes=hashes),
        )()

    return server


def create_app(config: AppConfig, service: PtMcpService | None = None) -> ASGIApp:
    """构造带 Host/Origin 校验和全方法 Bearer 认证的 ASGI 应用。"""
    active_service = service or PtMcpService(config)
    server = create_mcp_server(active_service)
    sdk_app = server.streamable_http_app(
        host=config.host,
        streamable_http_path=config.path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.resolved_allowed_hosts,
            allowed_origins=config.resolved_allowed_origins,
        ),
    )
    token = config.bearer_token.get_secret_value() if config.bearer_token else None
    return BearerAuthASGI(sdk_app, token)


__all__ = ["BearerAuthASGI", "create_app", "create_mcp_server"]

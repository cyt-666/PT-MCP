"""Focused local acceptance checks for HTTP guards and empty configuration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from pt_mcp.config import AppConfig
from pt_mcp.server import BearerAuthASGI, create_app

TOKEN = "local-acceptance-token"
BASE_URL = "http://127.0.0.1:8000"


class EmptyService:
    """Empty in-memory service; these tests cannot reach sites or downloaders."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def aclose(self) -> None:
        return None

    async def list_sites(self) -> list[object]:
        self.calls.append("list_sites")
        return []

    async def list_downloaders(self) -> list[object]:
        self.calls.append("list_downloaders")
        return []


def _app(service: EmptyService) -> BearerAuthASGI:
    """Create an empty, loopback-only service using resolved default allowlists."""
    config = AppConfig(
        bearer_token=SecretStr(TOKEN),
        data_dir=Path("/tmp/pt-mcp-acceptance"),
        sites=[],
        downloaders=[],
    )
    return create_app(config, service)  # type: ignore[arg-type]


@asynccontextmanager
async def _client(app: BearerAuthASGI) -> AsyncIterator[httpx.AsyncClient]:
    """Use an in-process ASGI transport and close the MCP lifespan cleanly."""
    inner_app = app.application
    async with inner_app.router.lifespan_context(inner_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
        ) as client:
            yield client


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_missing_and_wrong_bearer_reject_get_post_delete(
    method: str,
    token: str | None,
) -> None:
    """The outer bearer guard rejects read, protocol, and delete methods."""
    service = EmptyService()
    app = _app(service)

    async def run() -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with _client(app) as client:
            response = await client.request(method, "/mcp", headers=headers, json={})
            assert response.status_code == 401

    asyncio.run(run())
    assert service.calls == []


@pytest.mark.parametrize(
    ("host", "origin", "expected_status"),
    [
        ("evil.example.invalid:8000", BASE_URL, 421),
        ("127.0.0.1:8000", "http://evil.example.invalid:8000", 403),
    ],
)
def test_default_host_and_origin_guards_reject_each_unlisted_value(
    host: str,
    origin: str,
    expected_status: int,
) -> None:
    """Default loopback allowlists reject a bad Host and a bad Origin separately."""
    service = EmptyService()
    app = _app(service)

    async def run() -> None:
        async with _client(app) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Host": host,
                    "Origin": origin,
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == expected_status

    asyncio.run(run())
    assert service.calls == []


def test_empty_configuration_calls_both_inventory_tools() -> None:
    """The protocol surface can call both inventory tools with no configured entries."""
    service = EmptyService()
    app = _app(service)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def run() -> None:
        async with _client(app) as client:
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
                        "clientInfo": {"name": "local-acceptance", "version": "1"},
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

            for request_id, tool_name in enumerate(("list_sites", "list_downloaders"), 2):
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": {}},
                    },
                )
                assert response.status_code == 200
                assert response.json()["result"].get("isError", False) is False

    asyncio.run(run())
    assert service.calls == ["list_sites", "list_downloaders"]

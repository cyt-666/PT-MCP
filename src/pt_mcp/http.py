"""No-retry asynchronous HTTP transport with bounded site concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from pt_mcp.errors import HttpError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Small response projection used by site adapters."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes
    text: str
    url: str


class AsyncHttpClient:
    """Shared HTTPX clients with per-site pacing and no automatic retries."""

    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        site_concurrency: int = 2,
        min_site_interval_s: float = 1.0,
        max_response_bytes: int = 52_428_800,
        verify_tls: bool = True,
    ) -> None:
        if timeout_s <= 0 or site_concurrency < 1 or min_site_interval_s < 0 or max_response_bytes < 1:
            raise ValueError("invalid HTTP client limits")
        self._timeout_s = timeout_s
        self._site_concurrency = site_concurrency
        self._min_site_interval_s = min_site_interval_s
        self._max_response_bytes = max_response_bytes
        self._verify_tls = verify_tls
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._rate_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._state_lock = asyncio.Lock()
        self._closed = False

    async def _client(self, proxy: str | None) -> httpx.AsyncClient:
        async with self._state_lock:
            if self._closed:
                raise HttpError("http_client_closed", "HTTP 客户端已关闭")
            client = self._clients.get(proxy)
            if client is None:
                client = httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=self._timeout_s,
                    proxy=proxy,
                    trust_env=False,
                    verify=self._verify_tls,
                )
                self._clients[proxy] = client
            return client

    async def _pace(self, site_id: str | None) -> None:
        if site_id is None or self._min_site_interval_s == 0:
            return
        async with self._state_lock:
            lock = self._rate_locks.setdefault(site_id, asyncio.Lock())
            semaphore = self._semaphores.setdefault(site_id, asyncio.Semaphore(self._site_concurrency))
        async with lock:
            now = monotonic()
            last = self._last_request.get(site_id)
            if last is not None:
                delay = self._min_site_interval_s - (now - last)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request[site_id] = monotonic()

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json: object | None = None,
        content: bytes | None = None,
        cookies: Mapping[str, str] | str | None = None,
        timeout_s: float | None = None,
        proxy: str | None = None,
        follow_redirects: bool = False,
        site_id: str | None = None,
    ) -> HttpResponse:
        """Send exactly one HTTP request and return a bounded response body.

        Redirect following is intentionally disabled; adapters must validate every
        Location before issuing the next request. The transport never retries.
        """
        if follow_redirects:
            raise HttpError("redirect_policy", "HTTP 跳转必须由站点适配器逐跳验证")
        if not method or not url:
            raise HttpError("invalid_request", "HTTP 请求参数无效")
        client = await self._client(proxy)
        semaphore: asyncio.Semaphore | None = None
        if site_id is not None:
            async with self._state_lock:
                semaphore = self._semaphores.setdefault(site_id, asyncio.Semaphore(self._site_concurrency))

        request_headers = dict(headers or {})
        request_cookies: Mapping[str, str] | None
        if isinstance(cookies, str):
            if cookies:
                request_headers["Cookie"] = cookies
            request_cookies = None
        else:
            request_cookies = cookies

        async def send_once() -> HttpResponse:
            await self._pace(site_id)
            try:
                async with client.stream(
                    method=method.upper(),
                    url=url,
                    headers=request_headers or None,
                    params=params,
                    json=json,
                    content=content,
                    cookies=request_cookies,
                    timeout=timeout_s or self._timeout_s,
                    follow_redirects=False,
                ) as response:
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_response_bytes:
                            raise HttpError("response_too_large", "HTTP 响应超过大小限制")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    text = body.decode(encoding, errors="replace")
                    return HttpResponse(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=body,
                        text=text,
                        url=str(response.url),
                    )
            except HttpError:
                raise
            except httpx.TimeoutException:
                raise HttpError("http_timeout", "远端请求超时") from None
            except httpx.RequestError:
                raise HttpError("http_network_error", "无法连接远端服务") from None

        if semaphore is None:
            return await send_once()
        async with semaphore:
            return await send_once()

    async def aclose(self) -> None:
        """Close the HTTPX pools after the server stops accepting requests."""
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            clients = tuple(self._clients.values())
            self._clients.clear()
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)

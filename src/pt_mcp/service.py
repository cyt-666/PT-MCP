"""Use-case orchestration for search, caller-owned RSS snapshots, and downloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlsplit

from pydantic import SecretStr

from pt_mcp.adapters import MTeamAdapter, NexusPHPAdapter, parse_rss
from pt_mcp.adapters.base import (
    SiteAdapter,
    allowed_hosts,
    response_location,
    response_ok,
    validate_allowed_url,
)
from pt_mcp.adapters.mteam import load_profile_for_site as load_mteam_profile
from pt_mcp.adapters.nexusphp import load_profile_for_site as load_nexusphp_profile
from pt_mcp.config import AppConfig
from pt_mcp.credentials import CredentialProvider, EnvironmentCredentialProvider, SiteCredentials
from pt_mcp.downloaders.qbittorrent import QBittorrentDownloader
from pt_mcp.errors import (
    AppError,
    CredentialMissing,
    IdempotencyConflict,
    NotFound,
    SubmissionTransportUnknown,
)
from pt_mcp.http import AsyncHttpClient
from pt_mcp.identity import download_hashes
from pt_mcp.models import (
    DownloadView,
    DownloaderConfig,
    DownloaderSummary,
    NativeCategory,
    ResourceRecord,
    ResourceView,
    RssReadPage,
    SearchPage,
    SiteConfig,
    SiteSummary,
    SubmissionReceipt,
    make_resource_id,
)
from pt_mcp.storage import SQLiteStorage

logger = logging.getLogger(__name__)


class PtMcpService:
    """Coordinate explicit site adapters, local snapshots, and qBittorrent."""

    def __init__(
        self,
        config: AppConfig,
        *,
        storage: SQLiteStorage | None = None,
        http: AsyncHttpClient | None = None,
        credential_provider: CredentialProvider | None = None,
        adapters: dict[str, SiteAdapter] | None = None,
        downloaders: dict[str, Any] | None = None,
    ) -> None:
        """Construct local state and lazy clients without connecting to any service."""
        self.config = config
        self.storage = storage or SQLiteStorage(config.data_dir)
        self.http = http or AsyncHttpClient(
            timeout_s=config.request_timeout_s,
            site_concurrency=config.site_concurrency,
            min_site_interval_s=config.min_site_interval_s,
        )
        self.credentials = credential_provider or EnvironmentCredentialProvider(config.credentials)
        self._sites = {site.site_id: site for site in config.sites}
        self._downloaders_config = {item.downloader_id: item for item in config.downloaders}
        self._profile_fingerprints: dict[str, str] = {}
        self._adapters = adapters if adapters is not None else self._build_adapters()
        self._downloaders = downloaders if downloaders is not None else self._build_downloaders()
        self._closed = False
        self._request_locks: dict[str, asyncio.Lock] = {}
        self._lock_registry = asyncio.Lock()

    def _build_adapters(self) -> dict[str, SiteAdapter]:
        """Build adapters from profiles without resolving site credentials."""
        result: dict[str, SiteAdapter] = {}
        for site in self.config.sites:
            if site.kind == "nexusphp":
                profile = load_nexusphp_profile(site.profile, profile_dir=self.config.profile_dir)
                self._profile_fingerprints[site.site_id] = _model_fingerprint(profile.model_dump(mode="json"))
                result[site.site_id] = NexusPHPAdapter(self.http, self.credentials, profile=profile)
            elif site.kind == "mteam":
                profile = load_mteam_profile(site.profile, profile_dir=self.config.profile_dir)
                self._profile_fingerprints[site.site_id] = _model_fingerprint(profile.model_dump(mode="json"))
                result[site.site_id] = MTeamAdapter(self.http, self.credentials, profile=profile)
        return result

    def _build_downloaders(self) -> dict[str, Any]:
        """Construct lazy qBittorrent wrappers without opening network connections."""
        return {
            item.downloader_id: QBittorrentDownloader(item, self.credentials)
            for item in self.config.downloaders
        }

    async def aclose(self) -> None:
        """Close owned HTTP, SQLite, and downloader resources."""
        if self._closed:
            return
        self._closed = True
        await self.http.aclose()
        await self.storage.aclose()
        for downloader in self._downloaders.values():
            close = getattr(downloader, "aclose", None)
            if callable(close):
                await close()

    async def list_sites(self) -> list[SiteSummary]:
        """List configured sites without touching the network or credentials."""
        return [
            SiteSummary(
                site_id=site.site_id,
                name=site.name,
                kind=site.kind,
                base_url=site.base_url,
                categories=list(site.native_categories),
            )
            for site in self.config.sites
        ]

    async def list_categories(self, site_id: str) -> list[NativeCategory]:
        """Return configured native categories; do not invent site category APIs."""
        return list(self._site(site_id).native_categories)

    async def search_torrents(
        self,
        site_id: str,
        keyword: str,
        category_ids: list[str] | None = None,
        page: int = 1,
    ) -> SearchPage[ResourceView]:
        """Search one site page and cache complete private download records."""
        if page < 1:
            raise AppError("invalid_argument", "页码必须大于等于 1")
        if not keyword.strip() or len(keyword) > 256:
            raise AppError("invalid_argument", "关键词长度必须为 1 到 256 个字符")
        site = self._site(site_id)
        adapter = self._adapter(site_id)
        result = await adapter.search_torrents(
            site,
            keyword=keyword,
            category_ids=category_ids,
            page=page,
        )
        records = [self._normalize_record(site, record) for record in result.items]
        await self.storage.put_resources(records)
        return SearchPage[ResourceView](
            items=[ResourceView.from_record(record) for record in records],
            page=result.page,
            has_more=result.has_more,
        )

    async def read_rss(
        self,
        site_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RssReadPage:
        """Read an on-demand feed using a caller-owned immutable seen/pending cursor."""
        if not 1 <= limit <= 500:
            raise AppError("invalid_argument", "limit 必须在 1 到 500 之间")
        site = self._site(site_id)
        feed_url, credentials = await self._rss_url(site)
        version = _feed_version(site, feed_url, self._site_version(site))

        if cursor:
            page_items, next_cursor, has_more = await self.storage.read_rss_snapshot(
                cursor,
                limit,
                expected_site_id=site.site_id,
                expected_feed_version=version,
            )
            if page_items or has_more:
                return self._rss_page(site, page_items, next_cursor or cursor, has_more)
            # A drained cursor is a continuation point. Fetch only when the caller
            # asks again, then let storage extend this caller's seen/pending state.
            previous_cursor = cursor
        else:
            previous_cursor = None

        records = await self._fetch_rss(site, feed_url, credentials)
        normalized = [self._normalize_record(site, record) for record in records]
        await self.storage.put_resources(normalized)
        first_cursor = await self.storage.create_rss_snapshot(
            site_id=site.site_id,
            feed_version=version,
            items=normalized,
            previous_cursor=previous_cursor,
        )
        page_items, next_cursor, has_more = await self.storage.read_rss_snapshot(
            first_cursor,
            limit,
            expected_site_id=site.site_id,
            expected_feed_version=version,
        )
        return self._rss_page(site, page_items, next_cursor or first_cursor, has_more)

    async def list_downloaders(self) -> list[DownloaderSummary]:
        """List configured downloader entries without connecting to qBittorrent."""
        return [
            DownloaderSummary(
                downloader_id=item.downloader_id,
                kind=item.kind,
                configured=bool(item.host),
            )
            for item in self.config.downloaders
        ]

    async def add_download(
        self,
        resource_id: str,
        request_id: str,
        downloader_id: str = "default",
        save_profile_id: str | None = None,
        paused: bool = False,
    ) -> SubmissionReceipt:
        """Resolve one resource, claim its identities, then issue at most one add POST."""
        if not request_id or len(request_id) > 128:
            raise AppError("invalid_argument", "request_id 必须为 1 到 128 个字符")
        if not resource_id:
            raise AppError("invalid_argument", "resource_id 不能为空")
        digest = _submission_digest(resource_id, downloader_id, save_profile_id, paused)
        async with self._request_lock(request_id):
            # Idempotency is decided from the durable caller-visible payload
            # before consulting a short-lived resource reference.
            existing = await self.storage.get_request(request_id)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise IdempotencyConflict()
                return await self._reconcile_receipt(existing)
            return await self._add_download_locked(
                resource_id=resource_id,
                request_id=request_id,
                downloader_id=downloader_id,
                save_profile_id=save_profile_id,
                paused=paused,
                payload_digest=digest,
            )

    async def _add_download_locked(
        self,
        *,
        resource_id: str,
        request_id: str,
        downloader_id: str,
        save_profile_id: str | None,
        paused: bool,
        payload_digest: str,
    ) -> SubmissionReceipt:
        """Run a request while serializing calls that reuse its request ID."""
        resource = await self.storage.get_resource(resource_id)
        if resource is None:
            raise NotFound("资源不存在或已过期，请重新搜索")
        site = self._site(resource.site_id)
        if resource.config_version != self._site_version(site):
            raise NotFound("资源配置已更新，请重新搜索")

        downloader_config = self._downloader_config(downloader_id)
        downloader = self._downloader(downloader_id)
        save_path = self._save_path(downloader_config, save_profile_id)
        claim = await self.storage.claim_request(
            request_id=request_id,
            payload_digest=payload_digest,
            downloader_id=downloader_id,
            hashes=[],
            resource_identity=resource_id,
        )
        if not claim.created:
            return await self._reconcile_receipt(claim.receipt)

        try:
            content = await self._adapter(site.site_id).resolve_download(site, resource)
            hashes = download_hashes(content)
            if not hashes:
                raise AppError("resource_identity_unknown", "资源内容没有可核验的 torrent hash")

            existing = await downloader.get_by_hashes(set(hashes))
            if existing:
                return await self.storage.finish_request(
                    request_id,
                    state="already_exists",
                    task=_best_hash_match(existing, set(hashes)),
                )

            bound = await self.storage.bind_hashes(request_id, downloader_id, list(hashes))
            if not bound.hash_claimed:
                # The storage transaction has persisted this request's own
                # terminal receipt and released its resource claim.
                return bound.receipt

            # Recheck after acquiring the durable hash claim to cover external adds.
            existing = await downloader.get_by_hashes(set(hashes))
            if existing:
                return await self.storage.finish_request(
                    request_id,
                    state="already_exists",
                    task=_best_hash_match(existing, set(hashes)),
                )
        except AppError as error:
            await self.storage.finish_request(
                request_id,
                state="failed",
                error_code=error.code,
            )
            raise
        except asyncio.CancelledError:
            await self.storage.finish_request(
                request_id,
                state="failed",
                error_code="cancelled_before_submit",
            )
            raise

        try:
            acknowledgement = await downloader.add(
                content=content,
                save_path=save_path,
                paused=paused,
            )
        except SubmissionTransportUnknown:
            return await self.storage.finish_request(
                request_id,
                state="unknown",
                error_code="submission_unknown",
            )
        except asyncio.CancelledError:
            await self.storage.finish_request(
                request_id,
                state="unknown",
                error_code="submission_unknown",
            )
            raise
        except AppError as error:
            await self.storage.finish_request(
                request_id,
                state="failed",
                error_code=error.code,
            )
            raise
        except Exception as error:  # noqa: BLE001
            logger.error("qBittorrent add failed: error_type=%s", type(error).__name__)
            return await self.storage.finish_request(
                request_id,
                state="unknown",
                error_code="submission_unknown",
            )

        if not acknowledgement.accepted:
            try:
                duplicate = await downloader.get_by_hashes(set(hashes))
            except AppError:
                # The qBittorrent response was negative, but without a hash
                # lookup the service cannot safely distinguish a rejected add
                # from a duplicate or a partial response.
                return await self.storage.finish_request(
                    request_id,
                    state="unknown",
                    error_code="submission_reconciliation_failed",
                )
            if duplicate:
                return await self.storage.finish_request(
                    request_id,
                    state="already_exists",
                    task=_best_hash_match(duplicate, set(hashes)),
                )
            return await self.storage.finish_request(
                request_id,
                state="failed",
                error_code="downloader_rejected",
            )
        try:
            tasks = await downloader.get_by_hashes(set(hashes))
        except AppError:
            tasks = []
        task = _best_hash_match(tasks, set(hashes)) if tasks else None
        return await self.storage.finish_request(
            request_id,
            state="accepted",
            task=task,
        )

    async def get_submission(self, request_id: str) -> SubmissionReceipt | None:
        """Reconcile a durable receipt by hashes only; never issue an add POST."""
        if not request_id:
            raise AppError("invalid_argument", "request_id 不能为空")
        async with self._request_lock(request_id):
            receipt = await self.storage.get_request(request_id)
            if receipt is None:
                return None
            return await self._reconcile_receipt(receipt)

    async def list_downloads(
        self,
        downloader_id: str = "default",
        hashes: list[str] | None = None,
    ) -> list[DownloadView]:
        """Query downloader tasks, optionally filtering by torrent hashes."""
        return await self._downloader(downloader_id).list_downloads(
            hashes=set(hashes) if hashes is not None else None,
        )

    async def _reconcile_receipt(self, receipt: SubmissionReceipt) -> SubmissionReceipt:
        """Read-only reconciliation for claimed or unknown outcomes."""
        if receipt.state in {"accepted", "already_exists", "failed"}:
            return receipt
        if not receipt.hashes:
            if receipt.state == "claimed":
                return await self.storage.finish_request(
                    receipt.request_id,
                    state="unknown",
                    error_code="submission_unknown",
                )
            return receipt
        try:
            downloader = self._downloader(receipt.downloader_id)
        except AppError:
            return receipt
        try:
            tasks = await downloader.get_by_hashes(set(receipt.hashes))
        except AppError:
            return receipt
        if tasks:
            return await self.storage.finish_request(
                receipt.request_id,
                state="accepted",
                task=_best_hash_match(tasks, set(receipt.hashes)),
            )
        if receipt.state == "claimed":
            return await self.storage.finish_request(
                receipt.request_id,
                state="unknown",
                error_code="submission_unknown",
            )
        return receipt

    async def _fetch_rss(
        self,
        site: SiteConfig,
        feed_url: str,
        credentials: SiteCredentials | None,
    ) -> list[ResourceRecord]:
        """Fetch one feed on demand and validate every redirect host."""
        allowed = allowed_hosts(site)
        url = validate_allowed_url(feed_url, allowed)
        site_host = (urlsplit(site.base_url).hostname or "").lower()
        cookie = _secret(credentials.cookie) if credentials else None
        for _ in range(6):
            current_host = (urlsplit(url).hostname or "").lower()
            response = await self.http.request(
                method="GET",
                url=url,
                headers={"User-Agent": "pt-mcp/0.1"},
                cookies=cookie if current_host == site_host else None,
                timeout_s=site.timeout_s,
                proxy=site.proxy,
                follow_redirects=False,
                site_id=site.site_id,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response_location(response)
                if not location:
                    raise AppError("rss_redirect_invalid", "RSS 响应缺少跳转地址")
                url = validate_allowed_url(urljoin(url, location), allowed)
                continue
            if response.status_code in {401, 403}:
                raise AppError("site_auth_required", "RSS 站点需要重新认证")
            response_ok(response, "RSS 读取")
            return [self._normalize_record(site, record) for record in parse_rss(
                response.body,
                feed_url=feed_url,
                site=site,
            )]
        raise AppError("rss_redirect_limit", "RSS 跳转次数超过上限")

    async def _rss_url(self, site: SiteConfig) -> tuple[str, SiteCredentials | None]:
        """Resolve the configured feed URL lazily from a referenced environment value."""
        credentials = (
            await self.credentials.get(site.credential_ref)
            if site.credential_ref
            else None
        )
        feed_url = _secret(credentials.rss_url) if credentials else None
        if not feed_url and site.rss_url_env:
            feed_url = os.environ.get(site.rss_url_env)
        if not feed_url:
            raise CredentialMissing("该站点未配置 RSS URL")
        return feed_url, credentials

    def _rss_page(
        self,
        site: SiteConfig,
        records: list[ResourceRecord],
        cursor: str,
        has_more: bool,
    ) -> RssReadPage:
        return RssReadPage(
            items=[ResourceView.from_record(self._normalize_record(site, item)) for item in records],
            cursor=cursor,
            has_more=has_more,
        )

    def _normalize_record(self, site: SiteConfig, record: ResourceRecord) -> ResourceRecord:
        if record.site_id != site.site_id:
            raise AppError("adapter_contract_error", "适配器返回了其他站点的资源")
        if not record.source_id:
            raise AppError("adapter_contract_error", "资源缺少稳定来源身份")
        return record.model_copy(
            update={
                "resource_id": make_resource_id(site.site_id, record.source_id),
                "config_version": self._site_version(site),
            }
        )

    def _site(self, site_id: str) -> SiteConfig:
        site = self._sites.get(site_id)
        if site is None:
            raise NotFound("站点不存在或未配置")
        return site

    def _adapter(self, site_id: str) -> SiteAdapter:
        adapter = self._adapters.get(site_id)
        if adapter is None:
            raise NotFound("站点适配器不可用")
        return adapter

    def _downloader_config(self, downloader_id: str) -> DownloaderConfig:
        config = self._downloaders_config.get(downloader_id)
        if config is None:
            raise NotFound("下载器不存在或未配置")
        return config

    def _downloader(self, downloader_id: str) -> Any:
        downloader = self._downloaders.get(downloader_id)
        if downloader is None:
            config = self._downloaders_config.get(downloader_id)
            if config is None:
                raise NotFound("下载器不存在或未配置")
            # Recreate a lazy adapter after a restart or a cache miss. The
            # constructor only binds configuration; credentials/network are
            # resolved by the first actual qBittorrent operation.
            downloader = QBittorrentDownloader(config, self.credentials)
            self._downloaders[downloader_id] = downloader
        return downloader

    @staticmethod
    def _save_path(config: DownloaderConfig, profile_id: str | None) -> str | None:
        selected = profile_id or config.default_save_profile_id
        if selected is None:
            return None
        path = config.save_profiles.get(selected)
        if path is None:
            raise NotFound("保存配置不存在")
        return path

    def _site_version(self, site: SiteConfig) -> str:
        """Fingerprint non-secret site and category settings for cached resources."""
        payload = {
            "site_id": site.site_id,
            "kind": site.kind,
            "base_url": site.base_url,
            "api_base_url": site.api_base_url,
            "profile": site.profile,
            "profile_fingerprint": self._profile_fingerprints.get(site.site_id),
            "credential_ref": site.credential_ref,
            "rss_url_env": site.rss_url_env,
            "native_categories": [item.model_dump(mode="json") for item in site.native_categories],
            "download_hosts": site.download_hosts,
            "config_version": site.config_version,
            "timeout_s": site.timeout_s,
            "proxy": site.proxy,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @asynccontextmanager
    async def _request_lock(self, request_id: str) -> AsyncIterator[None]:
        """Serialize concurrent retries sharing an idempotency key in this process."""
        async with self._lock_registry:
            lock = self._request_locks.setdefault(request_id, asyncio.Lock())
        async with lock:
            yield


def _best_hash_match(tasks: list[DownloadView], hashes: set[str]) -> DownloadView:
    for task in tasks:
        if hashes.intersection(task.hashes):
            return task
    return tasks[0]


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _feed_version(site: SiteConfig, feed_url: str, site_version: str) -> str:
    """Hash secret feed URL and non-secret config without persisting the URL."""
    payload = f"{site.site_id}\0{site_version}\0{feed_url}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _submission_digest(
    resource_id: str,
    downloader_id: str,
    save_profile_id: str | None,
    paused: bool,
) -> str:
    """Hash the caller-visible fields that define one idempotent add intent."""
    payload = {
        "downloader_id": downloader_id,
        "paused": paused,
        "resource_id": resource_id,
        "save_profile_id": save_profile_id,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _model_fingerprint(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = ["PtMcpService"]
